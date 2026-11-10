#!/usr/bin/env python3
"""Standalone speech: only completed turns update the UI, using the actual voice and matching text.

The external SAPI5 process is a controlled JSON-line peer over real pipes. Exercise the public Voice/Hub
boundaries, including cancellation, process replacement and reconnecting after the startup greeting.
"""
import json
import os
import queue
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import dashboard  # noqa: E402
import voice  # noqa: E402


class Commands:
    def __init__(self):
        self.sent = queue.Queue()

    def write(self, text):
        self.sent.put(json.loads(text))

    def flush(self):
        pass


class Worker:
    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8")
        self.output = os.fdopen(write_fd, "w", encoding="utf-8", buffering=1)
        self.stdin = Commands()
        self.returncode = None

    def poll(self):
        return self.returncode

    def command(self):
        return self.stdin.sent.get(timeout=3)

    def emit(self, event, ident, **fields):
        self.output.write(json.dumps({"event": event, "id": ident, **fields}) + "\n")
        self.output.flush()

    def close(self):
        self.returncode = 0
        self.output.close()


def received(q):
    event = q.get(timeout=3)
    assert event.startswith("event: voice_spoken\n"), event  # never a replayable speak command
    return json.loads(event.split("\ndata: ", 1)[1])


workers = []


def spawn(*args, **kwargs):
    worker = Worker()
    workers.append(worker)
    return worker


h = dashboard.Hub()
q, _ = h.connect(lite=True)
before = set(threading.enumerate())
with tempfile.TemporaryDirectory() as tmp:
    try:
        with patch.object(voice.sys, "platform", "win32"), patch.object(voice.subprocess, "Popen", side_effect=spawn):
            v = voice.Voice({"enabled": True, "voice": "Requested voice", "announce": False}, log_dir=tmp,
                            on_spoken=lambda data: h.publish("voice_spoken", data))
            w = workers[-1]
            v.say("Русская\nфраза.")
            first = w.command()
            w.emit("started", first["id"], voice="Actual installed voice")
            w.emit("done", first["id"], voice="Actual installed voice")
            done = received(q)
            assert done["text"] == "Русская фраза." and done["voice"] == "Actual installed voice", done
            assert done["at"] > 0
            # An already completed id, an unknown id, failure and cancellation cannot overwrite success.
            w.emit("done", first["id"], voice="Duplicate")
            w.emit("done", -1, voice="Unknown")
            v.say("Failed turn")
            failed = w.command()
            w.emit("error", failed["id"], error="Engine failed")
            v.say("Interrupted turn")
            interrupted = w.command()
            v.hush()
            cancel = w.command()
            assert cancel["op"] == "cancel"
            w.emit("cancelled", interrupted["id"])
            w.emit("ok", cancel["id"])
            v.say("Muted turn")
            muted = w.command()
            v.set_muted(True)
            cancel = w.command()
            w.emit("cancelled", muted["id"])
            w.emit("ok", cancel["id"])
            v.say("Must stay silent")
            assert w.stdin.sent.empty()
            v.set_muted(False)
            v.say("Surviving turn")
            survivor = w.command()
            w.emit("done", survivor["id"], voice="Surviving voice")
            latest = received(q)
            assert latest["text"] == "Surviving turn" and latest["voice"] == "Surviving voice", latest
            # Reconnect retains the completion, not the old command, with its original completion time.
            # A delayed callback from an earlier turn cannot roll back the reconnect snapshot.
            h.publish("voice_spoken", {**done, "at": latest["at"] - 1})
            for lite in (False, True):
                replay, _ = h.connect(lite=lite)
                assert received(replay) == latest
                assert replay.empty()
                h.disconnect(replay)
            v.say("Old worker's queued turn")
            stale = w.command()
            w.returncode = 0
            v.say("New worker's turn")
            new = workers[-1]
            assert new is not w
            fresh = new.command()
            w.emit("done", stale["id"], voice="Stale voice")
            new.emit("done", fresh["id"], voice="New voice")
            latest = received(q)
            assert latest["text"] == "New worker's turn" and latest["voice"] == "New voice", latest
    finally:
        for worker in workers:
            worker.close()
        for thread in set(threading.enumerate()) - before:
            thread.join(3)
            assert not thread.is_alive(), "speech reader did not exit after EOF"
    assert q.empty(), "an unsuccessful or stale turn was published"
    with open(os.path.join(tmp, "voice.log"), encoding="utf-8") as log:
        assert '"event": "error"' in log.read(), "native diagnostics must still reach voice.log"
print("OK")
