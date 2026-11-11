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
        self.active = 0
        self.peak = 0
        self.behavior = self.reply
        self.block_connect = None
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
        bootstrap = ("import sys,asyncio;sys.path.insert(0,%r);"
                     "from edge_tts_worker import run_stdio,WorkerConfig;"
                     "asyncio.run(run_stdio(WorkerConfig(endpoint=%r)))") % (root, self.endpoint)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "faulthandler", "-u", "-c", bootstrap,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert process.stdin is not None and process.stdout is not None

        async def send(command):
            process.stdin.write(json.dumps(command).encode() + b"\n")
            await process.stdin.drain()

        async def read(kind):
            async with asyncio.timeout(3):
                while True:
                    event = json.loads(await process.stdout.readline())
                    if event["type"] == kind:
                        return event

        try:
            await read("ready")
            await send({"op": "start", "id": "startup", "text": "Startup speech.",
                        "voice": "en-US-AriaNeural", "budget_ms": 1000})
            await read("plan")
            await send({"op": "cancel", "id": "startup"})
            self.assertEqual((await read("end"))["reason"], "cancelled")
            await send({"op": "shutdown"})
            await asyncio.wait_for(process.wait(), 3)
            assert process.stderr is not None
            stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, "Worker stderr:\n" + stderr)
        finally:
            self.block_connect.set()
            if process.returncode is None:
                process.kill()
                await process.wait()

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

    async def test_idle_config_renewal_is_real_config_and_reuses_connections(self):
        async with asyncio.timeout(2):
            while len(self.configs) < 12:
                await asyncio.sleep(0.01)
        self.assertGreaterEqual(len(self.configs), 12)
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
        self.worker = EdgeWorker(self.events.put_nowait, WorkerConfig(
            endpoint=self.endpoint, backoff_initial=0.04, backoff_max=0.16))
        await self.worker.open()
        await asyncio.sleep(0.23)
        attempts = self.connection_attempts - before
        self.assertGreaterEqual(attempts, 6)
        self.assertLessEqual(attempts, 18, "Failed sockets must not reconnect in a spin")
        self.reject_connect = False
        self.start()
        await self.accept()
        self.assertEqual((await self.event("end"))["reason"], "complete")

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


if __name__ == "__main__":
    unittest.main()
