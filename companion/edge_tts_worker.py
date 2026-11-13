"""Persistent Edge speech worker. stdin/stdout are bounded JSONL commands/events.

Audio is a candidate, not a success: only the caller's complete-file decoder can
commit a fragment. The original request deadline covers that feedback as well.
"""
import asyncio
import base64
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
import json
import math
import os
import re
import sys
import threading
import time
from xml.sax.saxutils import escape

from edge_tts.communicate import mkssml, remove_incompatible_characters
from edge_tts.data_classes import TTSConfig

from edge_tts_pool import SynthesisJob, Transport, TransportFailure, WorkerConfig

MAX_LINE_BYTES = 65536
MAX_REQUESTS = 16
ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
VOICE_PATTERN = re.compile(r"[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9-]{1,100}Neural\Z")


def fragments(text):
    """Lossless Python ranges and their JavaScript UTF-16 source offsets."""
    sentences = []
    start = 0
    for match in re.finditer(r'''[.!?。！？]+["'”’）)\]]*\s*''', text):
        end = match.end()
        punctuation = match.start()
        if (text[punctuation] == "." and punctuation and punctuation + 1 < len(text)
                and text[punctuation - 1].isdigit() and text[punctuation + 1].isdigit()):
            continue
        if end < len(text) and not text[end - 1].isspace() and text[punctuation] in ".!?":
            continue
        sentences.append((start, end))
        start = end
    if start < len(text):
        sentences.append((start, len(text)))
    ranges = []
    for start, end in sentences:
        while end - start > 120:
            punctuation = [m.end() for m in re.finditer(r"[,;，；、]\s*", text[start:end])
                           if 60 <= m.end() <= 120]
            spaces = [m.end() for m in re.finditer(r"\s+", text[start:end]) if 60 <= m.end() <= 120]
            if punctuation:
                length = min(punctuation, key=lambda n: abs(n - 100))
            elif spaces:
                length = min(spaces, key=lambda n: abs(n - 100))
            else:
                # A long word is indivisible, even when it exceeds the target.
                boundary = re.search(r"\s+", text[start + 100:end])
                if boundary is None:
                    break
                length = 100 + boundary.end()
            ranges.append((start, start + length))
            start += length
        if start < end:
            if ranges and end - start < 20 and end - ranges[-1][0] <= 140:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
    offset = 0
    result = []
    for index, (start, end) in enumerate(ranges):
        length = len(text[start:end].encode("utf-16-le")) // 2
        result.append({"index": index, "start": offset, "end": offset + length,
                       "source_start": start, "source_end": end})
        offset += length
    return result


@dataclass(eq=False)
class Copy:
    request: object
    fragment: object
    pool: str
    attempt: str
    verdict: asyncio.Future[bool]
    task: object = None
    draining: bool = False
    audio_bytes: int = 0


@dataclass
class Fragment:
    span: dict
    accepted: str | None = None


@dataclass(eq=False)
class Request:
    id: str
    text: str
    voice: str
    deadline: float
    fragments: list
    order: int
    task: object = None
    copies: set = field(default_factory=set)
    ended: bool = False
    completed: bool = False


class EdgeWorker:
    """Public command/event boundary; constructor config is also the local-test seam."""
    def __init__(self, emit, config=None):
        self.emit = emit
        self.config = config or WorkerConfig()
        self.requests = {}
        self.attempts = {}
        self.jobs = set()
        self.transport = Transport(self.config, self.diagnostic)
        self.closed = False
        self.sequence = 0

    def diagnostic(self, event, **fields):
        self.emit({"type": "diagnostic", "event": event, **fields})

    async def open(self):
        # Socket ownership is entirely private to the transport.
        # Never await socket startup: cancellation and shutdown must already work.
        self.emit({"type": "ready"})
        await self.transport.open()

    def track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)
        return task

    def finish(self, request, reason, error=None):
        if request.ended:
            return
        request.ended = True
        request.completed = reason == "complete"
        if not request.completed:
            # Mark intentional stops before any network task can report failure.
            for copy in request.copies:
                copy.task.cancel()
        if self.requests.get(request.id) is request:
            del self.requests[request.id]
        for copy in request.copies:
            self.attempts.pop(copy.attempt, None)
        event = {"type": "end", "id": request.id, "reason": reason}
        if error is not None:
            event["error"] = error
        self.emit(event)

    def handle(self, command, received=None):
        if self.closed or not isinstance(command, dict):
            return
        operation = command.get("op")
        request_id = command.get("id")
        if operation == "shutdown":
            return
        if not isinstance(request_id, str) or not ID_PATTERN.fullmatch(request_id):
            self.diagnostic("invalid_command")
            return
        if operation == "cancel":
            request = self.requests.get(request_id)
            if request is not None:
                self.finish(request, "cancelled")
                request.task.cancel()
            return
        if operation == "decoded":
            attempt = command.get("attempt")
            if not isinstance(attempt, str) or len(attempt) > 80 or type(command.get("ok")) is not bool:
                return
            copy = self.attempts.get(attempt)
            # Execution is registered before its decoder-visible candidate exists.
            if (copy is None or copy.request.id != request_id or copy.verdict.done()
                    or copy.audio_bytes == 0):
                return
            request, fragment = copy.request, copy.fragment
            if request.ended or fragment.accepted is not None:
                return
            if time.monotonic() >= request.deadline:
                self.finish(request, "deadline")
                request.task.cancel()
                return
            if command["ok"]:
                fragment.accepted = attempt
                # Commit before any cleanup await or a competing verdict. This is
                # the exact earliest decoded-valid winner, not first network EOF.
                self.emit({"type": "accepted", "id": request_id,
                           "index": fragment.span["index"], "attempt": attempt})
                self.dispose_losers(
                    loser for loser in request.copies if loser.fragment is fragment and loser is not copy)
            else:
                self.diagnostic(
                    "turn_failed",
                    id=request_id,
                    index=fragment.span["index"],
                    attempt=attempt,
                    pool=copy.pool,
                    code="decode_rejected",
                    audio_bytes=copy.audio_bytes,
                    draining=False,
                )
            copy.verdict.set_result(command["ok"])
            return
        if operation != "start":
            self.diagnostic("invalid_command")
            return
        if request_id in self.requests:
            self.diagnostic("duplicate_id")
            return
        text, voice, budget = command.get("text"), command.get("voice"), command.get("budget_ms")
        if (not isinstance(text, str) or not isinstance(voice, str)
                or not isinstance(budget, (int, float)) or isinstance(budget, bool)):
            self.emit({"type": "end", "id": request_id, "reason": "failed", "error": "invalid_request"})
            return
        valid = (bool(text.strip()) and VOICE_PATTERN.fullmatch(voice)
                 and 0 < budget <= 15000 and math.isfinite(budget))
        if valid:
            try:
                valid = len(text.encode("utf-16-le")) <= 8000
            except UnicodeError:
                valid = False
        if not valid or len(self.requests) >= MAX_REQUESTS:
            self.emit({"type": "end", "id": request_id, "reason": "failed",
                       "error": "invalid_request" if not valid else "overloaded"})
            return
        deadline = (time.monotonic() if received is None else received) + budget / 1000
        self.sequence += 1
        request = Request(request_id, text, voice, deadline,
                          [Fragment(span) for span in fragments(text)], self.sequence)
        self.requests[request_id] = request
        self.emit({"type": "plan", "id": request_id, "fragments": [
            {key: fragment.span[key] for key in ("index", "start", "end")} for fragment in request.fragments]})
        request.task = self.track(self.run_request(request))

    async def run_copy(self, job, ssml):
        try:
            async with asyncio.timeout_at(job.deadline):
                audio = await self.transport.synthesize(job, ssml)
                if audio is None:
                    return False
                # Do not retain a request across network I/O. Completed drains
                # have only immutable transport identity and cannot publish.
                copy = self.attempts.get(job.attempt_id)
                if copy is None:
                    return False
                request, fragment = copy.request, copy.fragment
                if request.ended or fragment.accepted is not None or time.monotonic() >= job.deadline:
                    return False
                copy.audio_bytes = len(audio)
                self.emit({"type": "candidate", "id": job.request_id, "index": job.fragment_index,
                           "attempt": job.attempt_id, "start": fragment.span["start"], "end": fragment.span["end"],
                           "audio": base64.b64encode(audio).decode("ascii")})
                del audio
                return await copy.verdict
        except (TransportFailure, TimeoutError):
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            self.diagnostic("attempt_failed", id=job.request_id)
            return False
        finally:
            self.attempts.pop(job.attempt_id, None)

    def dispose_losers(self, copies):
        for copy in copies:
            self.attempts.pop(copy.attempt, None)
            if copy.task.done():
                continue
            if self.transport.allow_drain(copy.attempt):
                copy.draining = True
            else:
                copy.task.cancel()

    async def run_pool_attempts(self, request, fragment, pool, ssml):
        for attempt_index in range(2):
            if fragment.accepted is not None or request.ended or time.monotonic() >= request.deadline:
                return
            if (attempt_index == 1 and self.config.retry_delay > 0
                    and request.deadline - time.monotonic() >= 4 * self.config.retry_delay):
                await asyncio.sleep(self.config.retry_delay)
                if fragment.accepted is not None or request.ended or time.monotonic() >= request.deadline:
                    return
            self.sequence += 1
            copy = Copy(request, fragment, pool, "a%d" % self.sequence,
                        asyncio.get_running_loop().create_future())
            job = SynthesisJob(request.id, request.order, fragment.span["index"],
                               copy.attempt, pool, request.deadline)
            self.attempts[copy.attempt] = copy
            copy.task = self.track(self.run_copy(job, ssml))
            request.copies.add(copy)
            try:
                if await asyncio.shield(copy.task):
                    return
            except asyncio.CancelledError:
                if not copy.draining and not copy.task.done():
                    copy.task.cancel()
                raise

    async def run_fragment(self, request, fragment):
        text = request.text[fragment.span["source_start"]:fragment.span["source_end"]]
        config = TTSConfig(voice=request.voice, rate="+0%", volume="+0%", pitch="+0Hz",
                           boundary="SentenceBoundary")
        ssml = mkssml(config, escape(remove_incompatible_characters(text)))
        chains = [asyncio.create_task(self.run_pool_attempts(request, fragment, pool, ssml))
                  for pool in ("A", "B")]
        pending = set(chains)
        try:
            while pending:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if fragment.accepted is not None:
                    return
        finally:
            for chain in chains:
                if not chain.done():
                    chain.cancel()
            await asyncio.gather(*chains, return_exceptions=True)
        if time.monotonic() >= request.deadline:
            raise TimeoutError
        raise TransportFailure("both_pairs_failed")

    async def run_request(self, request):
        tasks = []
        try:
            async with asyncio.timeout_at(request.deadline):
                tasks = [asyncio.create_task(self.run_fragment(request, fragment))
                         for fragment in request.fragments]
                await asyncio.gather(*tasks)
                if time.monotonic() >= request.deadline:
                    raise TimeoutError
                self.finish(request, "complete")
        except TimeoutError:
            self.finish(request, "deadline")
        except asyncio.CancelledError:
            self.finish(request, "cancelled")
        except Exception:
            self.diagnostic("request_failed", id=request.id)
            self.finish(request, "failed", "synthesis_failed")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            remaining = []
            for copy in request.copies:
                self.attempts.pop(copy.attempt, None)
                if copy.draining and request.completed:
                    continue
                copy.task.cancel()
                remaining.append(copy.task)
            await asyncio.gather(*remaining, return_exceptions=True)
            request.copies.clear()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for request in tuple(self.requests.values()):
            self.finish(request, "cancelled")
        for task in tuple(self.jobs):
            task.cancel()
        while self.jobs:
            jobs = tuple(self.jobs)
            await asyncio.gather(*jobs, return_exceptions=True)
            # Gathering completed tasks need not yield to their discard callbacks.
            self.jobs.difference_update(jobs)
        await self.transport.close()
        self.attempts.clear()
        self.requests.clear()


def stdin_reader(loop, queue, stop):
    """Joinable pipe reader; never leave a buffered read alive at Python exit.

    Windows anonymous stdin pipes are synchronous, not Proactor read handles.
    Peek before each unbuffered read so shutdown can stop this thread even when
    the parent keeps stdin open (including after a broken stdout pipe).
    """
    def publish(item):
        if stop.is_set():
            return False
        coroutine = queue.put((time.monotonic(), item))
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            return False
        while not stop.is_set():
            try:
                future.result(timeout=0.02)
                return True
            except TimeoutError:
                continue
            except (RuntimeError, CancelledError):
                return False
        future.cancel()
        return False

    def dispatch(raw, oversized=False):
        try:
            item = {} if oversized else json.loads(raw)
            if not isinstance(item, dict):
                item = {}
        except (ValueError, UnicodeError, RecursionError):
            item = {}
        return publish(item) and item.get("op") != "shutdown"

    try:
        descriptor = sys.stdin.fileno()
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            msvcrt.setmode(descriptor, os.O_BINARY)
            handle = msvcrt.get_osfhandle(descriptor)
            peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            peek.restype = wintypes.BOOL

            def readable():
                available = wintypes.DWORD()
                if not peek(handle, None, 0, None, ctypes.byref(available), None):
                    error = ctypes.get_last_error()
                    if error in (109, 233):  # Broken/disconnected parent pipe.
                        return 0
                    raise ctypes.WinError(error)
                return min(available.value, MAX_LINE_BYTES) if available.value else None
        else:
            import select

            def readable():
                ready, _, _ = select.select([descriptor], [], [], 0)
                return MAX_LINE_BYTES if ready else None

        pending = bytearray()
        oversized = False
        while not stop.is_set():
            available = readable()
            if available is None:
                stop.wait(0.02)
                continue
            chunk = os.read(descriptor, available) if available else b""
            if not chunk:
                if pending or oversized:
                    if not dispatch(pending, oversized):
                        return
                publish(None)
                return
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > MAX_LINE_BYTES:
                        pending.clear()
                        oversized = True
                    break
                raw = pending[:newline + 1]
                del pending[:newline + 1]
                if not dispatch(raw, oversized or len(raw) > MAX_LINE_BYTES):
                    return
                oversized = False
    except (OSError, RuntimeError):
        publish(None)


async def run_stdio(config=None):
    queue = asyncio.Queue(maxsize=128)
    output_failed = asyncio.Event()

    def emit(event):
        try:
            sys.stdout.write(json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            output_failed.set()

    worker = EdgeWorker(emit, config)
    await worker.open()
    loop = asyncio.get_running_loop()
    stop_reader = threading.Event()
    reader = threading.Thread(target=stdin_reader, args=(loop, queue, stop_reader), name="edge-stdin")
    reader.start()
    failure = asyncio.create_task(output_failed.wait())
    read = None
    try:
        while not output_failed.is_set():
            read = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait((read, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure in done:
                break
            received, command = read.result()
            if command is None or (isinstance(command, dict) and command.get("op") == "shutdown"):
                break
            worker.handle(command, received)
    finally:
        stop_reader.set()
        failure.cancel()
        if read is not None:
            read.cancel()
        await asyncio.gather(*(task for task in (failure, read) if task is not None), return_exceptions=True)
        try:
            await worker.close()
        finally:
            await asyncio.to_thread(reader.join)


if __name__ == "__main__":
    try:
        asyncio.run(run_stdio())
    except (KeyboardInterrupt, BrokenPipeError):
        pass
