#!/usr/bin/env python3
"""The shipped worker's JSON commands/events against a controlled local WebSocket.

No provider traffic, audio device, private-method assertions, or diagnostic prototype.
The provider intentionally supplies opaque MP3 bytes: Chromium's verdict is a separate
public command and is exercised independently of transport completion.
"""
import asyncio
import base64
import json
from pathlib import Path
import sys
import time
import unittest

try:
    from aiohttp import web
    import edge_tts  # noqa: F401
except ImportError:
    print("SKIP: needs bundled edge-tts/aiohttp")
    sys.exit(77)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "companion"))
from edge_tts_worker import EdgeWorker, WorkerConfig  # noqa: E402


class WorkerBehavior(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = asyncio.Queue()
        self.connections = []
        self.transports = {}
        self.turns = []
        self.configs = []
        self.config_events = asyncio.Queue()
        self.active = 0
        self.peak = 0
        self.behavior = self.reply
        self.block_connect = None
        self.blocked_connects = asyncio.Queue()
        self.reject_connect = False
        self.connection_attempts = 0
        app = web.Application()
        app.router.add_get("/speech", self.socket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        self.endpoint = "http://127.0.0.1:%d/speech" % port
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(
            endpoint=self.endpoint,
            renewal_seconds=0.08, backoff_initial=0.04, backoff_max=0.16))
        await self.worker.open()
        self.history = []

    async def asyncTearDown(self):
        await self.worker.close()
        await self.runner.cleanup()

    async def socket(self, request):
        self.connection_attempts += 1
        if self.reject_connect:
            raise web.HTTPServiceUnavailable()
        if self.block_connect is not None:
            self.blocked_connects.put_nowait(None)
            await self.block_connect.wait()
        ws = web.WebSocketResponse()
        try:
            await ws.prepare(request)
        except ConnectionResetError:
            # Cancelling a connection while the fixture deliberately holds its
            # handshake closes the transport before it can become a WebSocket.
            if request.transport is None or request.transport.is_closing():
                return ws
            raise
        self.transports[ws] = request.transport
        self.connections.append(ws)
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                headers, _, body = message.data.partition("\r\n\r\n")
                fields = dict(line.split(":", 1) for line in headers.split("\r\n"))
                if fields["Path"] == "speech.config":
                    self.configs.append((ws, json.loads(body)))
                    self.config_events.put_nowait(ws)
                elif fields["Path"] == "ssml":
                    self.turns.append((ws, fields["X-RequestId"], body))
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    try:
                        await self.behavior(ws, fields["X-RequestId"], body)
                    finally:
                        self.active -= 1
        except (ConnectionError, RuntimeError):
            pass
        return ws

    async def reply(self, ws, request_id, body):
        await self.audio(ws, request_id)

    async def audio(self, ws, request_id, *, end=True, wrong_id=False):
        await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
        headers = ("X-RequestId:%s\r\nContent-Type:audio/mpeg\r\nPath:audio\r\n" % request_id).encode()
        await ws.send_bytes(len(headers).to_bytes(2, "big") + headers + b"complete-mp3")
        if end:
            await ws.send_str("X-RequestId:%s\r\nPath:turn.end\r\n\r\n" % ("wrong" if wrong_id else request_id))

    def command(self, **fields):
        self.worker.handle(fields)

    def start(self, text="A complete sentence.", id="r", budget_ms=2000):
        self.command(op="start", id=id, text=text, voice="en-US-AriaNeural", budget_ms=budget_ms)

    async def event(self, kind, id="r", timeout=2):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                self.history.append(event)
                if event["type"] == kind and (id is None or event.get("id") == id):
                    return event

    async def accept(self, id="r"):
        candidate = await self.event("candidate", id)
        self.assertEqual(base64.b64decode(candidate["audio"]), b"complete-mp3")
        self.command(op="decoded", id=id, attempt=candidate["attempt"], ok=True)
        return candidate, await self.event("accepted", id)

    async def status(self, *, ready_slots=None, renewals=None, connect_failures=None, timeout=2):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.event("diagnostic", None, timeout)
                if event.get("event") != "transport_status":
                    continue
                if ready_slots is not None and event["ready_slots"] != ready_slots:
                    continue
                if renewals is not None and event["renewals"] < renewals:
                    continue
                if connect_failures is not None and event["connect_failures"] < connect_failures:
                    continue
                return event

    async def test_command_readiness_precedes_initial_disconnected_transport_snapshot(self):
        await self.worker.close()
        self.block_connect = asyncio.Event()
        while not self.events.empty():
            self.events.get_nowait()
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(endpoint=self.endpoint))
        await self.worker.open()
        self.assertEqual(await self.event("ready", None), {"type": "ready"})
        initial = await self.status(ready_slots=0)
        self.assertEqual(initial, {
            "type": "diagnostic", "event": "transport_status",
            "ready_slots": 0, "total_slots": 6, "connections": 0,
            "renewals": 0, "reconnects": 0, "connect_failures": 0, "retired": 0,
        })
        self.start(id="queued")
        self.command(op="cancel", id="queued")
        self.assertEqual((await self.event("end", "queued"))["reason"], "cancelled")
        self.block_connect.set()
        connected = await self.status(ready_slots=6)
        self.assertEqual(connected["connections"], 6)
        self.assertEqual(connected["reconnects"], 0)

    async def test_busy_and_completed_draining_turns_remain_connected_capacity(self):
        connected = await self.status(ready_slots=6)
        both_started = asyncio.Event()
        send_winner = asyncio.Event()
        drop_loser = asyncio.Event()
        count = 0

        async def held_hedge(ws, request_id, body):
            nonlocal count
            count += 1
            if count == 1:
                await both_started.wait()
                await send_winner.wait()
                await self.audio(ws, request_id)
            else:
                both_started.set()
                await drop_loser.wait()
                await ws.close()

        self.behavior = held_hedge
        self.start()
        try:
            await asyncio.wait_for(both_started.wait(), 1)
            busy = await self.status(renewals=connected["renewals"] + 1)
            self.assertEqual(busy["ready_slots"], 6, "Busy turns still count as connected")
            send_winner.set()
            await self.accept()
            self.assertEqual((await self.event("end"))["reason"], "complete")
            draining = await self.status(renewals=busy["renewals"] + 1)
            self.assertEqual(draining["ready_slots"], 6, "A background drain remains connected")
            self.command(op="cancel", id="r")  # Completed requests cannot cancel their drains.
            self.block_connect = asyncio.Event()
            drop_loser.set()
            retired = await self.status(ready_slots=5)
            self.assertEqual(retired["retired"], draining["retired"] + 1)
            failure = await self.event("diagnostic", "r")
            self.assertEqual(failure["event"], "turn_failed")
            self.assertEqual(failure["code"], "zero_audio_reset")
            self.assertIs(failure["draining"], True)
            self.block_connect.set()
            recovered = await self.status(ready_slots=6)
            self.assertEqual(recovered["connections"], connected["connections"] + 1)
            self.assertEqual(recovered["reconnects"], 1)
        finally:
            send_winner.set()
            drop_loser.set()
            if self.block_connect is not None:
                self.block_connect.set()

    async def test_completed_network_turn_releases_capacity_before_decoder_verdict(self):
        await self.status(ready_slots=6)
        candidates = []
        for index in range(4):
            request_id = "decode%d" % index
            self.start(id=request_id)
            candidates.append((request_id, await self.event("candidate", request_id)))
        # Eight turns exceed the six sockets, yet all four requests can reach
        # Chromium while earlier candidates are still awaiting their verdicts.
        self.assertGreaterEqual(len(self.turns), 7)
        self.assertEqual(len(self.connections), 6)
        for request_id, candidate in candidates:
            self.command(op="decoded", id=request_id, attempt=candidate["attempt"], ok=True)
            self.assertEqual((await self.event("end", request_id))["reason"], "complete")

    async def test_decoder_verdict_before_candidate_cannot_commit_fragment(self):
        started = asyncio.Queue()
        release = asyncio.Event()

        async def held(ws, request_id, body):
            started.put_nowait(None)
            await release.wait()
            await self.audio(ws, request_id)

        self.behavior = held
        self.start()
        try:
            for _ in range(2):
                await asyncio.wait_for(started.get(), 1)
            # Probe valid wire identifiers before any candidate has been offered.
            for attempt in ("a1", "a2", "a3", "a4"):
                self.command(op="decoded", id="r", attempt=attempt, ok=True)
            while not self.events.empty():
                self.history.append(self.events.get_nowait())
            self.assertFalse(any(event["type"] == "accepted" for event in self.history))
            release.set()
            candidate, accepted = await self.accept()
            self.assertEqual(accepted["attempt"], candidate["attempt"])
            self.assertEqual((await self.event("end"))["reason"], "complete")
        finally:
            release.set()

    async def test_decode_is_commit_and_ready_does_not_wait_for_connections(self):
        self.assertEqual((await self.event("ready", None))["type"], "ready")
        self.start()
        plan = await self.event("plan")
        self.assertEqual(plan["fragments"], [{"index": 0, "start": 0, "end": 20}])
        candidate, accepted = await self.accept()
        self.assertEqual(accepted["attempt"], candidate["attempt"])
        self.assertEqual((await self.event("end"))["reason"], "complete")
        self.command(op="decoded", id="r", attempt=candidate["attempt"], ok=True)
        self.command(op="cancel", id="r")
        self.assertLessEqual(self.peak, 6)

    async def test_rejected_initial_pair_gets_exactly_one_extra_pair(self):
        self.start()
        candidates = []
        for _ in range(4):
            candidate = await self.event("candidate")
            candidates.append(candidate["attempt"])
            self.command(op="decoded", id="r", attempt=candidate["attempt"], ok=False)
        self.assertEqual(len(set(candidates)), 4)
        self.assertEqual((await self.event("end"))["reason"], "failed")
        self.assertEqual(len(self.turns), 4)

    async def test_decoder_silence_expires_and_late_success_is_ignored(self):
        # This case isolates decoder silence, not cold network establishment.
        # Observe both pools' initial configuration at the external provider
        # before starting the unchanged 100 ms end-to-end request deadline.
        async with asyncio.timeout(2):
            while len({ws for ws, _ in self.configs}) < 6:
                await asyncio.sleep(0.005)
        self.start(budget_ms=100)
        candidate = await self.event("candidate")
        self.assertEqual((await self.event("end"))["reason"], "deadline")
        self.command(op="decoded", id="r", attempt=candidate["attempt"], ok=True)
        self.start(id="next")
        await self.accept("next")
        self.assertEqual((await self.event("end", "next"))["reason"], "complete")
        self.assertFalse(any(e["type"] == "accepted" and e.get("id") == "r" for e in self.history))

    async def test_utf16_plan_covers_unchanged_text_without_splitting_decimal_or_word(self):
        text = "😀 First sentence. " + "A number 3.14 and ordinary words, " * 6 + "Tiny!"
        self.start(text)
        plan = await self.event("plan")
        ranges = plan["fragments"]
        self.assertEqual(ranges[0], {"index": 0, "start": 0, "end": 19})
        encoded = text.encode("utf-16-le")
        pieces = [encoded[f["start"] * 2:f["end"] * 2].decode("utf-16-le") for f in ranges]
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(not piece.endswith("3.") for piece in pieces))
        self.assertTrue(all(len(piece) >= 20 for piece in pieces[1:]))
        accepted = []
        async with asyncio.timeout(2):
            while True:
                event = await self.events.get()
                self.history.append(event)
                if event["type"] == "candidate":
                    self.command(op="decoded", id="r", attempt=event["attempt"], ok=True)
                elif event["type"] == "accepted":
                    accepted.append(event["index"])
                elif event["type"] == "end":
                    self.assertEqual(event["reason"], "complete")
                    break
        self.assertEqual(sorted(accepted), list(range(len(ranges))))

    async def test_first_fragment_finishes_before_loser_and_queued_pressure_retires_loser(self):
        both_started = asyncio.Event()
        release = asyncio.Event()
        held_transport = []
        count = 0

        async def hedged(ws, request_id, body):
            nonlocal count
            count += 1
            if count == 1:
                await both_started.wait()
                await self.audio(ws, request_id)
            elif count == 2:
                held_transport.append(self.transports[ws])
                both_started.set()
                await release.wait()
            else:
                await self.audio(ws, request_id)

        self.behavior = hedged
        self.start()
        try:
            await self.accept()
            self.assertEqual((await self.event("end"))["reason"], "complete")
            self.assertFalse(held_transport[0].is_closing(), "Unpressured loser should still drain")
            for index in range(3):
                self.start(id="queued%d" % index)
            async with asyncio.timeout(1):
                while not held_transport[0].is_closing():
                    await asyncio.sleep(0.005)
            self.assertLessEqual(self.peak, 6)
        finally:
            release.set()
            for index in range(3):
                self.command(op="cancel", id="queued%d" % index)

    async def test_startup_stdin_is_responsive_and_shutdown_closes_blocked_connects(self):
        await self.worker.close()
        self.block_connect = asyncio.Event()
        root = str(Path(__file__).resolve().parents[1] / "companion")
        # Deliberately exceed the old 3s cold-start assumption: dependency/TLS
        # initialization is not command handling, and can be slow under load.
        bootstrap = ("import sys,asyncio,time;time.sleep(3.1);sys.path.insert(0,%r);"
                     "from edge_tts_worker import run_stdio,WorkerConfig;"
                     "asyncio.run(run_stdio(WorkerConfig(endpoint=%r)))") % (root, self.endpoint)
        launched = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "faulthandler", "-u", "-c", bootstrap,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        stderr_chunks = []

        async def collect_stderr():
            while chunk := await process.stderr.read(4096):
                stderr_chunks.append(chunk)

        stderr_reader = asyncio.create_task(collect_stderr())
        readiness_seconds = None

        async def send(command):
            process.stdin.write(json.dumps(command).encode() + b"\n")
            await process.stdin.drain()

        def failure(kind):
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            return AssertionError(
                "Worker did not emit %s after %.3fs; ready=%r; exit=%r; stderr before kill:\n%s"
                % (kind, time.monotonic() - launched, readiness_seconds, process.returncode, stderr)
            )

        async def read(kind, timeout=3):
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        line = await process.stdout.readline()
                        if not line:
                            raise AssertionError("Worker stdout closed before " + kind)
                        event = json.loads(line)
                        if event["type"] == kind:
                            return event
            except (TimeoutError, AssertionError) as error:
                raise failure(kind) from error

        try:
            # Match the real cold-start watchdog, not an arbitrary import-speed
            # requirement (desktop/src/main/edge-tts.ts: ensureWorker/DEADLINE_MS).
            await read("ready", timeout=15)
            readiness_seconds = time.monotonic() - launched
            self.assertGreater(readiness_seconds, 3, "The slow-bootstrap regression must exceed 3s")
            try:
                # All six provider handshakes stay blocked for this entire
                # shared command/cancellation/shutdown responsiveness budget.
                async with asyncio.timeout(3):
                    for _ in range(6):
                        await self.blocked_connects.get()
                    await send({"op": "start", "id": "startup", "text": "Startup speech.",
                                "voice": "en-US-AriaNeural", "budget_ms": 1000})
                    await read("plan")
                    await send({"op": "cancel", "id": "startup"})
                    self.assertEqual((await read("end"))["reason"], "cancelled")
                    await send({"op": "shutdown"})
                    await process.wait()
                    await stderr_reader
            except TimeoutError as error:
                raise failure("plan/cancel/shutdown with blocked connections") from error
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, "Worker stderr:\n" + stderr)
        finally:
            self.block_connect.set()
            if process.returncode is None:
                process.kill()
                await process.wait()
            await stderr_reader

    async def test_silent_partial_audio_never_becomes_playable(self):
        release = asyncio.Event()

        async def partial(ws, request_id, body):
            await self.audio(ws, request_id, end=False)
            await release.wait()

        self.behavior = partial
        self.start(budget_ms=100)
        try:
            self.assertEqual((await self.event("end"))["reason"], "deadline")
            self.assertFalse(any(e["type"] == "candidate" for e in self.history))
        finally:
            release.set()

    async def test_invalid_or_oversized_text_is_rejected_not_truncated(self):
        self.start(text="word " * 1000)
        self.assertEqual((await self.event("end"))["error"], "invalid_request")
        self.assertEqual(self.turns, [])
        self.command(op="decoded", id="r", attempt="missing", ok=True)
        self.start(text="A valid replacement.")
        await self.accept()
        self.assertEqual((await self.event("end"))["reason"], "complete")

    async def test_partial_or_wrong_turn_never_becomes_candidate(self):
        async def wrong(ws, request_id, body):
            await self.audio(ws, request_id, wrong_id=True)
        self.behavior = wrong
        self.start()
        self.assertEqual((await self.event("end"))["reason"], "failed")
        self.assertFalse(any(e["type"] == "candidate" for e in self.history))
        self.assertEqual(len(self.turns), 4)

    async def test_cancel_network_busy_keeps_other_idle_sockets_and_cleans_state(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def held(ws, request_id, body):
            entered.set()
            await release.wait()
            if not ws.closed:
                await self.audio(ws, request_id)
        self.behavior = held
        async with asyncio.timeout(2):
            while len(self.connections) < 6:
                await asyncio.sleep(0.005)
        self.start()
        await asyncio.wait_for(entered.wait(), 1)
        self.command(op="cancel", id="r")
        self.assertEqual((await self.event("end"))["reason"], "cancelled")
        idle_before = {ws for ws in self.connections if ws not in [turn[0] for turn in self.turns]}
        self.behavior = self.reply
        release.set()
        self.start(id="next")
        await self.accept("next")
        self.assertEqual((await self.event("end", "next"))["reason"], "complete")
        self.assertTrue(any(not ws.closed for ws in idle_before))
        self.assertEqual(sum(e["type"] == "end" and e.get("id") == "r" for e in self.history), 1)

    async def test_active_cancel_wins_over_provider_reset_without_failure_diagnostic(self):
        await self.status(ready_slots=6)
        both_started = asyncio.Event()
        reset = asyncio.Event()
        reset_finished = asyncio.Queue()
        count = 0

        async def reset_after_cancel(ws, request_id, body):
            nonlocal count
            count += 1
            if count == 2:
                both_started.set()
            await reset.wait()
            await ws.close()
            reset_finished.put_nowait(None)

        self.behavior = reset_after_cancel
        self.start()
        try:
            await asyncio.wait_for(both_started.wait(), 1)
            self.command(op="cancel", id="r")
            reset.set()
            self.assertEqual((await self.event("end"))["reason"], "cancelled")
            for _ in range(2):
                await asyncio.wait_for(reset_finished.get(), 1)
            async with asyncio.timeout(2):
                while (await self.status(ready_slots=6))["retired"] < 2:
                    pass
            self.behavior = self.reply
            self.start(id="after-cancel")
            await self.accept("after-cancel")
            self.assertEqual((await self.event("end", "after-cancel"))["reason"], "complete")
            while not self.events.empty():
                self.history.append(self.events.get_nowait())
            self.assertFalse(any(
                event.get("event") == "turn_failed" and event.get("id") == "r"
                for event in self.history
            ))
            self.assertFalse(any(
                event["type"] == "candidate" and event.get("id") == "r" for event in self.history
            ))
        finally:
            reset.set()

    async def test_shutdown_cancels_completed_request_drains_without_failure(self):
        both_started = asyncio.Event()
        release = asyncio.Event()
        loser_transport = []
        count = 0

        async def complete_with_drain(ws, request_id, body):
            nonlocal count
            count += 1
            if count == 1:
                await both_started.wait()
                await self.audio(ws, request_id)
            else:
                loser_transport.append(self.transports[ws])
                both_started.set()
                await release.wait()

        self.behavior = complete_with_drain
        self.start()
        try:
            await self.accept()
            self.assertEqual((await self.event("end"))["reason"], "complete")
            self.assertFalse(loser_transport[0].is_closing())
            await self.worker.close()
            self.assertTrue(loser_transport[0].is_closing())
            while not self.events.empty():
                self.history.append(self.events.get_nowait())
            self.assertEqual(sum(
                event["type"] == "end" and event.get("id") == "r" for event in self.history
            ), 1)
            self.assertFalse(any(
                event.get("event") == "turn_failed" and event.get("id") == "r"
                for event in self.history
            ))
        finally:
            release.set()

    async def test_completed_drain_keeps_original_deadline_without_another_terminal_event(self):
        await self.status(ready_slots=6)
        both_started = asyncio.Event()
        release = asyncio.Event()
        count = 0

        async def silent_loser(ws, request_id, body):
            nonlocal count
            count += 1
            if count == 1:
                await both_started.wait()
                await self.audio(ws, request_id)
            else:
                both_started.set()
                await release.wait()

        self.behavior = silent_loser
        self.start(budget_ms=300)
        try:
            await self.accept()
            self.assertEqual((await self.event("end"))["reason"], "complete")
            self.command(op="cancel", id="r")
            # Original-budget expiry retires the silent drain, not a fresh
            # deadline measured from winner acceptance.
            await self.status(ready_slots=5)
            await self.worker.close()
            while not self.events.empty():
                self.history.append(self.events.get_nowait())
            self.assertFalse(any(
                event.get("event") == "turn_failed" and event.get("id") == "r"
                for event in self.history
            ))
            self.assertEqual(sum(
                event["type"] == "end" and event.get("id") == "r" for event in self.history
            ), 1)
        finally:
            release.set()

    async def test_request_failure_cancels_drains_of_already_accepted_fragments(self):
        await self.status(ready_slots=6)
        both_started = asyncio.Event()
        release = asyncio.Event()
        first_turns = 0

        async def first_wins_second_rejected(ws, request_id, body):
            nonlocal first_turns
            if "First fragment" not in body:
                await self.audio(ws, request_id)
                return
            first_turns += 1
            if first_turns == 1:
                await both_started.wait()
                await self.audio(ws, request_id)
            else:
                both_started.set()
                await release.wait()

        self.behavior = first_wins_second_rejected
        self.start("First fragment has enough words. Second fragment has enough words.")
        try:
            async with asyncio.timeout(2):
                while True:
                    event = await self.events.get()
                    self.history.append(event)
                    if event["type"] == "candidate":
                        self.command(op="decoded", id="r", attempt=event["attempt"], ok=event["index"] == 0)
                    elif event["type"] == "end":
                        self.assertEqual(event["reason"], "failed")
                        break
            await self.status(ready_slots=5)
            failures = [event for event in self.history if event.get("event") == "turn_failed"]
            self.assertEqual(len(failures), 4)
            self.assertTrue(all(event["code"] == "decode_rejected" for event in failures))
            self.assertEqual(sum(event["type"] == "accepted" for event in self.history), 1)
        finally:
            release.set()

    async def test_idle_config_renewal_is_real_config_and_reuses_connections(self):
        async with asyncio.timeout(2):
            for _ in range(12):
                await self.config_events.get()
        renewed = await self.status(ready_slots=6, renewals=6)
        self.assertEqual(renewed["connections"], 6)
        self.assertEqual(renewed["reconnects"], 0)
        self.assertEqual(len(self.turns), 0)
        self.assertEqual(len(self.connections), 6)
        for _, config in self.configs:
            self.assertEqual(config["context"]["synthesis"]["audio"]["outputFormat"],
                             "audio-24khz-48kbitrate-mono-mp3")
        self.start()
        await self.accept()
        await self.event("end")
        self.assertEqual(len(self.connections), 6)

    async def test_failed_connections_back_off_and_recover_without_restart(self):
        await self.worker.close()
        self.reject_connect = True
        before = self.connection_attempts
        while not self.events.empty():
            self.events.get_nowait()
        self.history.clear()
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(
            endpoint=self.endpoint, backoff_initial=0.04, backoff_max=0.16))
        await self.worker.open()
        first_failure = await self.status(ready_slots=0, connect_failures=6)
        started = time.monotonic()
        next_failure = await self.status(ready_slots=0, connect_failures=12)
        self.assertGreaterEqual(time.monotonic() - started, 0.025)
        self.assertEqual(first_failure["connections"], 0)
        self.assertEqual(next_failure["connections"], 0)
        self.assertEqual(self.connection_attempts, before + 12)
        self.reject_connect = False
        recovered = await self.status(ready_slots=6)
        self.assertEqual(recovered["connections"], 6)
        self.assertEqual(recovered["reconnects"], 0)
        self.start()
        await self.accept()
        self.assertEqual((await self.event("end"))["reason"], "complete")
        snapshots = [event for event in self.history if event.get("event") == "transport_status"]
        for previous, current in zip(snapshots, snapshots[1:]):
            self.assertEqual(set(current), {
                "type", "event", "ready_slots", "total_slots", "connections",
                "renewals", "reconnects", "connect_failures", "retired",
            })
            for counter in ("connections", "renewals", "reconnects", "connect_failures", "retired"):
                self.assertGreaterEqual(current[counter], previous[counter])

    async def test_failed_head_retries_once_without_regenerating_accepted_fragments(self):
        text = ("First fragment has enough words. Second fragment has enough words. "
                "Third fragment has enough words. Fourth fragment has enough words. "
                "Fifth fragment has enough words.")
        self.start(text)
        plan = await self.event("plan")
        self.assertEqual(len(plan["fragments"]), 5)
        head_candidates = 0
        accepted = []
        async with asyncio.timeout(2):
            while True:
                event = await self.events.get()
                self.history.append(event)
                if event["type"] == "candidate":
                    if event["index"] == 0:
                        head_candidates += 1
                    self.command(op="decoded", id="r", attempt=event["attempt"], ok=event["index"] != 0)
                elif event["type"] == "accepted":
                    accepted.append(event["index"])
                elif event["type"] == "end":
                    self.assertEqual(event["reason"], "failed")
                    break
        self.assertEqual(head_candidates, 4)
        self.assertTrue(accepted, "Already valid later fragments must not be regenerated for a head retry")
        self.assertEqual(len(accepted), len(set(accepted)))
        for index in accepted:
            fragment = plan["fragments"][index]
            part = text[fragment["start"]:fragment["end"]]
            self.assertLessEqual(sum(part in body for _, _, body in self.turns), 2)

    async def test_complete_audio_survives_provider_close_after_matching_end(self):
        async def end_and_close(ws, request_id, body):
            await self.audio(ws, request_id)
            await ws.close()

        self.behavior = end_and_close
        self.start()
        await self.accept()
        self.assertEqual((await self.event("end"))["reason"], "complete")

    async def test_cancelled_queued_leases_do_not_strand_pool_capacity(self):
        for cycle in range(8):
            for index in range(3):
                self.start(id="race%d-%d" % (cycle, index))
            await asyncio.sleep(0)
            for index in range(3):
                self.command(op="cancel", id="race%d-%d" % (cycle, index))
            await asyncio.sleep(0)
        self.start(id="survivor")
        await self.accept("survivor")
        self.assertEqual((await self.event("end", "survivor"))["reason"], "complete")

    async def test_reset_pool_retries_after_delay_without_waiting_for_silent_sibling_pool(self):
        await self.worker.close()
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(
            endpoint=self.endpoint, renewal_seconds=0.5,
            backoff_initial=0.04, backoff_max=0.16, retry_delay=0.08))
        await self.worker.open()
        async with asyncio.timeout(2):
            while len({ws for ws, _ in self.configs}) < 6:
                await asyncio.sleep(0.005)
        release = asyncio.Event()
        reset_at = None
        retry_at = None
        count = 0

        async def one_resets_one_hangs(ws, request_id, body):
            nonlocal count, reset_at, retry_at
            count += 1
            if count == 1:
                await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
                reset_at = time.monotonic()
                await ws.close()
            elif count == 2:
                await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
                await release.wait()
            else:
                retry_at = time.monotonic()
                await self.audio(ws, request_id)

        self.behavior = one_resets_one_hangs
        self.start(budget_ms=1500)
        try:
            await self.accept()
            self.assertEqual((await self.event("end"))["reason"], "complete")
            assert reset_at is not None and retry_at is not None
            self.assertGreaterEqual(retry_at - reset_at, 0.06)
            self.assertLess(retry_at - reset_at, 0.5)
            self.assertEqual(count, 3)
        finally:
            release.set()


    async def test_silent_turn_hits_first_audio_timeout_and_all_silent_retries_end_at_deadline(self):
        await self.worker.close()
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(
            endpoint=self.endpoint, renewal_seconds=0.5, backoff_initial=0.04, backoff_max=0.16,
            first_audio_timeout=0.15, stall_timeout=0.08, retry_delay=0.02))
        await self.worker.open()
        async with asyncio.timeout(2):
            while len({ws for ws, _ in self.configs}) < 6:
                await asyncio.sleep(0.005)
        release = asyncio.Event()

        async def silent_after_metadata(ws, request_id, body):
            await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
            await ws.send_str(
                "X-RequestId:%s\r\nContent-Type:application/json; charset=utf-8\r\n"
                "Path:response\r\n\r\n{\"audio\":{\"type\":\"inline\"}}" % request_id)
            await release.wait()

        self.behavior = silent_after_metadata
        started = time.monotonic()
        self.start(id="silent", budget_ms=300)
        try:
            end = await self.event("end", "silent")
            elapsed = time.monotonic() - started
            self.assertEqual(end["reason"], "deadline")
            self.assertGreaterEqual(elapsed, 0.27)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(len(self.turns), 4)
            failures = [
                e for e in self.history
                if e["type"] == "diagnostic" and e.get("event") == "turn_failed" and e.get("id") == "silent"
            ]
            self.assertEqual(len(failures), 2)
            for item in failures:
                self.assertEqual(item["code"], "first_audio_timeout")
                self.assertEqual(item["audio_bytes"], 0)
                self.assertIs(item["draining"], False)

            # Startup/queueing consumed most of the 300 ms budget. Its remaining
            # 60 ms must expire before first-audio timeout can start another retry.
            short_start = time.monotonic()
            turns_before_tight = len(self.turns)
            self.worker.handle(
                {"op": "start", "id": "tight", "text": "Tight budget.",
                 "voice": "en-US-AriaNeural", "budget_ms": 300},
                received=short_start - 0.24,
            )
            tight_end = await self.event("end", "tight")
            self.assertEqual(tight_end["reason"], "deadline")
            self.assertLessEqual(len(self.turns) - turns_before_tight, 2)
            self.assertFalse(any(
                event.get("event") == "turn_failed" and event.get("id") == "tight"
                for event in self.history
            ))
        finally:
            release.set()

    async def test_slow_first_audio_succeeds_while_midstream_silence_fails_as_stream_stall(self):
        await self.worker.close()
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(
            endpoint=self.endpoint, renewal_seconds=0.5, backoff_initial=0.04, backoff_max=0.16,
            first_audio_timeout=0.14, stall_timeout=0.07, retry_delay=0.03))
        await self.worker.open()
        async with asyncio.timeout(2):
            while len({ws for ws, _ in self.configs}) < 6:
                await asyncio.sleep(0.005)

        async def slow_valid(ws, request_id, body):
            await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
            await asyncio.sleep(0.09)  # Longer than stall_timeout, shorter than first_audio_timeout.
            headers = ("X-RequestId:%s\r\nContent-Type:audio/mpeg\r\nPath:audio\r\n" % request_id).encode()
            await ws.send_bytes(len(headers).to_bytes(2, "big") + headers + b"complete-")
            await asyncio.sleep(0.03)  # Within stall_timeout.
            await ws.send_bytes(len(headers).to_bytes(2, "big") + headers + b"mp3")
            await ws.send_str("X-RequestId:%s\r\nPath:turn.end\r\n\r\n" % request_id)

        self.behavior = slow_valid
        self.start(id="slow", budget_ms=1500)
        await self.accept("slow")
        self.assertEqual((await self.event("end", "slow"))["reason"], "complete")
        self.assertFalse(any(
            e["type"] == "diagnostic" and e.get("event") == "turn_failed" and e.get("id") == "slow"
            for e in self.history
        ))

        release = asyncio.Event()

        async def stalled_midstream(ws, request_id, body):
            await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
            headers = ("X-RequestId:%s\r\nContent-Type:audio/mpeg\r\nPath:audio\r\n" % request_id).encode()
            await ws.send_bytes(len(headers).to_bytes(2, "big") + headers + b"part1")
            await release.wait()

        self.behavior = stalled_midstream
        stalled_start = time.monotonic()
        self.start(id="stalled", budget_ms=2000)
        try:
            end = await self.event("end", "stalled")
            self.assertEqual(end["reason"], "failed")
            self.assertLess(time.monotonic() - stalled_start, 0.8)
            stalls = [
                e for e in self.history
                if e["type"] == "diagnostic" and e.get("event") == "turn_failed" and e.get("id") == "stalled"
            ]
            self.assertEqual(len(stalls), 4)
            for item in stalls:
                self.assertEqual(item["code"], "stream_stall")
                self.assertEqual(item["audio_bytes"], 5)
                self.assertIs(item["draining"], False)
        finally:
            release.set()

    async def test_draining_loser_reset_emits_turn_failed_while_preemption_and_cancel_do_not(self):
        both_started = asyncio.Event()
        drop_loser = asyncio.Event()
        turn_count = 0

        async def winner_then_loser_resets(ws, request_id, body):
            nonlocal turn_count
            turn_count += 1
            if turn_count == 1:
                await both_started.wait()
                await self.audio(ws, request_id)
            elif turn_count == 2:
                await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
                both_started.set()
                await drop_loser.wait()
                await ws.close()
            else:
                await self.audio(ws, request_id)

        self.behavior = winner_then_loser_resets
        self.start(id="hedged")
        await self.accept("hedged")
        self.assertEqual((await self.event("end", "hedged"))["reason"], "complete")
        drop_loser.set()
        async with asyncio.timeout(1):
            while True:
                event = await self.events.get()
                self.history.append(event)
                if event["type"] == "diagnostic" and event.get("event") == "turn_failed" and event.get("id") == "hedged":
                    self.assertEqual(event["code"], "zero_audio_reset")
                    self.assertEqual(event["audio_bytes"], 0)
                    self.assertIs(event["draining"], True)
                    self.assertIn(event["pool"], ("A", "B"))
                    break

        # Now verify that evicting a draining loser via queued pool pressure does NOT emit turn_failed,
        # and cancelling an active request does NOT emit turn_failed.
        preempt_started = asyncio.Event()
        preempt_hold = asyncio.Event()
        held_transport = []
        second_count = 0

        async def winner_then_preempted_loser(ws, request_id, body):
            nonlocal second_count
            second_count += 1
            if second_count == 1:
                await preempt_started.wait()
                await self.audio(ws, request_id)
            elif second_count == 2:
                held_transport.append(self.transports[ws])
                preempt_started.set()
                await preempt_hold.wait()
            else:
                await preempt_hold.wait()

        self.behavior = winner_then_preempted_loser
        self.start(id="preempted")
        try:
            await self.accept("preempted")
            self.assertEqual((await self.event("end", "preempted"))["reason"], "complete")
            for idx in range(3):
                self.start(id="q%d" % idx)
            async with asyncio.timeout(1):
                while not held_transport[0].is_closing():
                    await asyncio.sleep(0.005)
            for idx in range(3):
                self.command(op="cancel", id="q%d" % idx)
                self.assertEqual((await self.event("end", "q%d" % idx))["reason"], "cancelled")
            await asyncio.sleep(0.05)
            while not self.events.empty():
                self.history.append(self.events.get_nowait())
            unexpected = [
                e for e in self.history
                if e["type"] == "diagnostic" and e.get("event") == "turn_failed"
                and e.get("id") in {"preempted", "q0", "q1", "q2"}
            ]
            self.assertEqual(unexpected, [])
        finally:
            preempt_hold.set()

    async def test_turn_failed_codes_classify_resets_empty_protocol_and_decode_rejections(self):
        modes = iter([
            "partial_reset",
            "empty_audio",
            "protocol_error",
            "decode_rejected",
        ])

        async def varied_failures(ws, request_id, body):
            mode = next(modes)
            await ws.send_str("X-RequestId:%s\r\nPath:turn.start\r\n\r\n" % request_id)
            headers = ("X-RequestId:%s\r\nContent-Type:audio/mpeg\r\nPath:audio\r\n" % request_id).encode()
            if mode == "partial_reset":
                await ws.send_bytes(len(headers).to_bytes(2, "big") + headers + b"abc")
                await ws.close()
            elif mode == "empty_audio":
                await ws.send_str("X-RequestId:%s\r\nPath:turn.end\r\n\r\n" % request_id)
            elif mode == "protocol_error":
                await ws.send_str("X-RequestId:wrong\r\nPath:turn.end\r\n\r\n")
            else:
                await ws.send_bytes(len(headers).to_bytes(2, "big") + headers + b"bad-mp3")
                await ws.send_str("X-RequestId:%s\r\nPath:turn.end\r\n\r\n" % request_id)

        self.behavior = varied_failures
        self.start(id="codes")
        async with asyncio.timeout(2):
            while True:
                event = await self.events.get()
                self.history.append(event)
                if event["type"] == "candidate" and event["id"] == "codes":
                    self.command(op="decoded", id="codes", attempt=event["attempt"], ok=False)
                elif event["type"] == "end" and event["id"] == "codes":
                    self.assertEqual(event["reason"], "failed")
                    break
        while not self.events.empty():
            self.history.append(self.events.get_nowait())
        codes = {
            e["code"]: e["audio_bytes"]
            for e in self.history
            if e["type"] == "diagnostic" and e.get("event") == "turn_failed" and e.get("id") == "codes"
        }
        self.assertEqual(codes, {
            "partial_audio_reset": 3,
            "empty_audio": 0,
            "protocol_error": 0,
            "decode_rejected": 7,
        })

if __name__ == "__main__":
    unittest.main()
