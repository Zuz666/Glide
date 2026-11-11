"""Bounded, single-turn Edge WebSocket pools used by the stdio worker."""
import asyncio
import heapq
from dataclasses import dataclass, field
import json
import time

import aiohttp
from edge_tts.communicate import _SSL_CTX, connect_id, date_to_string, ssml_headers_plus_data
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.drm import DRM

MAX_AUDIO_BYTES = 1024 * 1024
MAX_FRAME_BYTES = MAX_AUDIO_BYTES + 16384

FAILURE_CODES = frozenset({
    "zero_audio_reset",
    "partial_audio_reset",
    "first_audio_timeout",
    "stream_stall",
    "empty_audio",
    "protocol_error",
    "decode_rejected",
})

class TransportFailure(Exception):
    """Only fixed, safe categories may cross the worker protocol boundary."""


@dataclass
class WorkerConfig:
    # Constructor-only external transport seam. There is deliberately no CLI,
    # environment or app setting capable of changing the production endpoint.
    endpoint: str | None = None
    renewal_seconds: float = 20.0
    backoff_initial: float = 0.25
    backoff_max: float = 8.0
    connect_timeout: float = 8.0
    first_audio_timeout: float = 7.5
    stall_timeout: float = 4.0
    retry_delay: float = 0.25


@dataclass(eq=False)
class Connection:
    slot: int
    ws: aiohttp.ClientWebSocketResponse
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    busy: bool = False
    turn: "Turn | None" = None
    receiver: asyncio.Task[None] | None = None
    successful_turn: bool = False
    renewer: asyncio.Task[None] | None = None
    drain: asyncio.Task[bool] | None = None


@dataclass
class Turn:
    request_id: str
    done: asyncio.Future[bool]
    activity: asyncio.Event = field(default_factory=asyncio.Event)
    audio: bytearray = field(default_factory=bytearray)
    ended: bool = False
    started: bool = False
    has_audio: bool = False
    preempted: bool = False
    failure_code: str | None = None


def classify_turn_error(error: Exception, has_audio: bool) -> str:
    if isinstance(error, TransportFailure) and error.args:
        reason = error.args[0]
        if reason in FAILURE_CODES:
            return reason
        if reason == "socket_closed":
            return "partial_audio_reset" if has_audio else "zero_audio_reset"
        if isinstance(reason, str) and (reason.startswith("protocol_") or reason == "audio_limit"):
            return "protocol_error"
    if isinstance(error, ValueError):
        return "protocol_error"
    return "partial_audio_reset" if has_audio else "zero_audio_reset"


def headers(raw):
    if not raw or len(raw) > 16384:
        raise TransportFailure("protocol_headers")
    result = {}
    for line in raw.split(b"\r\n"):
        if not line:
            continue
        key, sep, value = line.partition(b":")
        if not sep:
            raise TransportFailure("protocol_headers")
        try:
            key = key.decode("ascii").lower()
            value = value.strip().decode("ascii")
        except UnicodeError:
            raise TransportFailure("protocol_headers") from None
        if not key or key in result:
            raise TransportFailure("protocol_headers")
        result[key] = value
    if "path" not in result:
        raise TransportFailure("protocol_headers")
    return result


async def speech_config(ws):
    await ws.send_str(
        "X-Timestamp:%s\r\nContent-Type:application/json; charset=utf-8\r\n"
        "Path:speech.config\r\n\r\n" % date_to_string()
        + json.dumps({"context": {"synthesis": {"audio": {
            "metadataoptions": {"sentenceBoundaryEnabled": "true", "wordBoundaryEnabled": "false"},
            "outputFormat": "audio-24khz-48kbitrate-mono-mp3"}}}}, separators=(",", ":")) + "\r\n")


class Pool:
    def __init__(self, name, session, config, diagnostic):
        self.name = name
        self.session = session
        self.config = config
        self.diagnostic = diagnostic
        self.slots: list[Connection | None] = [None] * 3
        self.waiters = []
        self.queue_sequence = 0
        self.tasks = set()
        self.closed = False
        self.generations = [0] * 3
        self.counts = dict(connections=0, renewals=0, connect_failures=0, retired=0)

    def track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def open(self):
        for slot in range(3):
            self.track(self.maintain(slot))

    async def maintain(self, slot):
        delay = self.config.backoff_initial
        while not self.closed:
            ws = None
            connection = None
            try:
                async with asyncio.timeout(self.config.connect_timeout):
                    if self.config.endpoint is None:
                        endpoint = ("%s&ConnectionId=%s&Sec-MS-GEC=%s&Sec-MS-GEC-Version=%s" %
                                    (WSS_URL, connect_id(), DRM.generate_sec_ms_gec(), SEC_MS_GEC_VERSION))
                        auth_headers = DRM.headers_with_muid(WSS_HEADERS)
                    else:
                        endpoint, auth_headers = self.config.endpoint, {}
                    # aiohttp's attrs fields use ib(type=...) rather than field
                    # annotations. Pyright misses that generated constructor;
                    # these exact keywords are declared in client_ws.py.
                    timeout = aiohttp.ClientWSTimeout(ws_receive=None, ws_close=0.5)  # pyright: ignore[reportCallIssue]
                    ws = await self.session.ws_connect(
                        endpoint, headers=auth_headers, ssl=_SSL_CTX, compress=15,
                        max_msg_size=MAX_FRAME_BYTES,
                        timeout=timeout)
                    await speech_config(ws)
                connection = Connection(slot, ws)
                self.slots[slot] = connection
                self.counts["connections"] += 1
                self.diagnostic("connection_ready", pool=self.name, slot=slot,
                                reconnect=self.generations[slot] > 0, **self.counts)
                self.generations[slot] += 1
                connection.receiver = self.track(self.receive(connection))
                connection.renewer = self.track(self.renew(connection))
                connected_at = time.monotonic()
                self.dispatch()
                await connection.stopped.wait()
                if connection.successful_turn or time.monotonic() - connected_at >= self.config.renewal_seconds:
                    delay = self.config.backoff_initial
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.counts["connect_failures"] += 1
                self.diagnostic("connect_failed", pool=self.name, slot=slot,
                                error_type=type(error).__name__, **self.counts)
            finally:
                if connection is not None:
                    self.retire(connection)
                if ws is not None:
                    await ws.close()
            if not self.closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.backoff_max)

    def retire(self, connection):
        if connection.stopped.is_set():
            return
        if self.slots[connection.slot] is connection:
            self.slots[connection.slot] = None
        connection.stopped.set()
        self.counts["retired"] += 1
        self.diagnostic("connection_retired", pool=self.name, slot=connection.slot, **self.counts)
        if connection.turn is not None and not connection.turn.done.done():
            connection.turn.done.set_result(False)
            connection.turn.activity.set()
        current = asyncio.current_task()
        for task in (connection.receiver, connection.renewer):
            if task is not None and task is not current:
                task.cancel()
        self.track(connection.ws.close())

    def dispatch(self):
        while self.waiters and self.waiters[0][3].done():
            heapq.heappop(self.waiters)
        for connection in self.slots:
            if not self.waiters:
                break
            if connection is None or connection.busy or connection.stopped.is_set():
                continue
            if connection.ws.closed:
                self.retire(connection)
                continue
            connection.busy = True
            heapq.heappop(self.waiters)[3].set_result(connection)
            while self.waiters and self.waiters[0][3].done():
                heapq.heappop(self.waiters)
        if self.waiters:
            # A loser may drain only while it costs nobody a queued slot.
            for connection in tuple(self.slots):
                if connection is not None and connection.drain is not None:
                    if connection.turn is not None:
                        connection.turn.preempted = True
                    connection.drain.cancel()
                    self.retire(connection)

    async def acquire(self, copy):
        if self.closed:
            raise TransportFailure("shutdown")
        future = asyncio.get_running_loop().create_future()
        self.queue_sequence += 1
        entry = (copy.request.order, copy.fragment.span["index"], self.queue_sequence, future)
        heapq.heappush(self.waiters, entry)
        self.dispatch()
        try:
            # shield preserves a lease already delivered in the cancellation race.
            return await asyncio.shield(future)
        except BaseException:
            if future.done() and not future.cancelled():
                self.release(future.result())
            else:
                future.cancel()
            try:
                self.waiters.remove(entry)
                heapq.heapify(self.waiters)
            except ValueError:
                pass
            self.dispatch()
            raise

    def release(self, connection):
        connection.turn = None
        connection.drain = None
        connection.busy = False
        self.dispatch()

    def allow_drain(self, connection, task):
        if connection is None or self.waiters or connection.stopped.is_set():
            return False
        connection.drain = task
        return True

    async def renew(self, connection):
        try:
            while True:
                await asyncio.sleep(self.config.renewal_seconds)
                if connection.busy:
                    continue
                connection.busy = True
                try:
                    async with asyncio.timeout(self.config.connect_timeout):
                        await speech_config(connection.ws)
                    self.counts["renewals"] += 1
                    self.diagnostic("config_renewed", pool=self.name, slot=connection.slot, **self.counts)
                finally:
                    connection.busy = False
                self.dispatch()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.diagnostic("renewal_failed", pool=self.name, slot=connection.slot,
                            error_type=type(error).__name__)
            self.retire(connection)

    async def receive(self, connection):
        try:
            while True:
                message = await connection.ws.receive()
                if message.type == aiohttp.WSMsgType.TEXT:
                    raw = message.data.encode("utf-8")
                    head, sep, body = raw.partition(b"\r\n\r\n")
                    if not sep:
                        raise TransportFailure("protocol_headers")
                    fields = headers(head)
                    binary = False
                elif message.type == aiohttp.WSMsgType.BINARY:
                    raw = message.data
                    if len(raw) < 2:
                        raise TransportFailure("protocol_binary")
                    length = int.from_bytes(raw[:2], "big")
                    if not 0 < length <= len(raw) - 2:
                        raise TransportFailure("protocol_binary")
                    fields = headers(raw[2:2 + length])
                    body = raw[2 + length:]
                    binary = True
                else:
                    raise TransportFailure("socket_closed")
                turn = connection.turn
                if turn is None or turn.ended:
                    raise TransportFailure("protocol_unsolicited")
                path = fields["path"]
                request_id = fields.get("x-requestid")
                if ((request_id is not None and request_id.lower() != turn.request_id.lower())
                        or (path == "turn.end" and request_id is None)):
                    raise TransportFailure("protocol_request_id")
                if binary:
                    if path != "audio":
                        raise TransportFailure("protocol_audio")
                    if fields.get("content-type") is None and not body:
                        continue
                    if fields.get("content-type") != "audio/mpeg" or not body:
                        raise TransportFailure("protocol_audio")
                    if len(turn.audio) + len(body) > MAX_AUDIO_BYTES:
                        raise TransportFailure("audio_limit")
                    turn.audio.extend(body)
                    turn.has_audio = True
                    turn.activity.set()
                elif path == "turn.start":
                    if turn.started:
                        raise TransportFailure("protocol_duplicate_start")
                    turn.started = True
                elif path == "turn.end":
                    if not turn.audio:
                        raise TransportFailure("empty_audio")
                    turn.ended = True
                    connection.successful_turn = True
                    turn.done.set_result(True)
                    turn.activity.set()
                elif path in ("response", "audio.metadata"):
                    if body.strip() and not isinstance(json.loads(body), dict):
                        raise TransportFailure("protocol_metadata")
                    if turn.has_audio:
                        turn.activity.set()
                else:
                    raise TransportFailure("protocol_path")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            turn = connection.turn
            if turn is not None and not turn.ended and turn.failure_code is None:
                turn.failure_code = classify_turn_error(error, bool(turn.audio))
            self.diagnostic("socket_retired", pool=self.name, slot=connection.slot,
                            error_type=type(error).__name__)
            self.retire(connection)

    async def synthesize(self, ssml, copy):
        connection = await self.acquire(copy)
        copy.connection = connection
        turn = Turn(connect_id(), asyncio.get_running_loop().create_future())
        connection.turn = turn
        try:
            try:
                await connection.ws.send_str(ssml_headers_plus_data(turn.request_id, date_to_string(), ssml))
            except Exception as error:
                turn.failure_code = classify_turn_error(error, False)
                raise TransportFailure(turn.failure_code) from None
            sent_at = time.monotonic()
            while not turn.done.done():
                turn.activity.clear()
                if turn.done.done():
                    break
                if not turn.has_audio:
                    remaining = (sent_at + self.config.first_audio_timeout) - time.monotonic()
                    if remaining <= 0:
                        turn.failure_code = "first_audio_timeout"
                        raise TransportFailure("first_audio_timeout")
                    try:
                        await asyncio.wait_for(turn.activity.wait(), remaining)
                    except TimeoutError:
                        if not turn.has_audio and not turn.done.done():
                            turn.failure_code = "first_audio_timeout"
                            raise TransportFailure("first_audio_timeout") from None
                else:
                    try:
                        await asyncio.wait_for(turn.activity.wait(), self.config.stall_timeout)
                    except TimeoutError:
                        if not turn.done.done():
                            turn.failure_code = "stream_stall"
                            raise TransportFailure("stream_stall") from None
            if not turn.done.result() or not turn.ended:
                if turn.preempted or self.closed:
                    raise asyncio.CancelledError
                code = turn.failure_code or ("partial_audio_reset" if turn.audio else "zero_audio_reset")
                turn.failure_code = code
                raise TransportFailure(code)
            return bytes(turn.audio)
        except TransportFailure as error:
            if (not turn.preempted and not self.closed and not copy.request.cancelled
                    and error.args and error.args[0] in FAILURE_CODES):
                self.diagnostic(
                    "turn_failed",
                    id=copy.request.id,
                    index=copy.fragment.span["index"],
                    attempt=copy.attempt,
                    pool=self.name,
                    code=error.args[0],
                    audio_bytes=len(turn.audio),
                    draining=bool(copy.draining),
                )
            if not turn.ended:
                self.retire(connection)
            raise
        except BaseException:
            if not turn.ended:
                self.retire(connection)
            raise
        finally:
            # Exactly the acquiring coroutine releases the lease, including when
            # acquire's future resolved on the same turn as task cancellation.
            copy.connection = None
            self.release(connection)

    async def close(self):
        self.closed = True
        for entry in self.waiters:
            if not entry[3].done():
                entry[3].cancel()
        self.waiters.clear()
        for connection in tuple(self.slots):
            if connection is not None:
                self.retire(connection)
        for task in tuple(self.tasks):
            task.cancel()
        while self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
