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
    def __init__(self, worker=None):
        self.sent = queue.Queue()
        self.worker = worker

    def write(self, text):
        command = json.loads(text)
        if command["op"] == "voices" and self.worker is not None and self.worker.auto_catalogue:
            self.worker.emit("voices", command["id"], voices=self.worker.catalogue)
            return
        self.sent.put(json.loads(text))

    def flush(self):
        pass


class Worker:
    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8")
        self.output = os.fdopen(write_fd, "w", encoding="utf-8", buffering=1)
        self.stdin = Commands(self)
        self.auto_catalogue = True
        self.catalogue = [{"id": "Requested voice", "name": "Requested voice",
                           "identity": "token:A", "isDefault": True}]
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


# A preparation token predates discovery/style work; mute/unmute or PTT cannot revive it.
speech_events = []
event_sink = type("Events", (), {"publish": lambda self, key, data: speech_events.append((key, data))})()
hub_voice = voice.HubVoice({"enabled": True, "announce": False}, event_sink, announce=False)
token = hub_voice.cancellation_token()
hub_voice.say("Accepted.", cancellation_token=token)
hub_voice.hush()
hub_voice.say("Cancelled before preparation.", cancellation_token=token)
token = hub_voice.cancellation_token()
hub_voice.set_muted(True)
hub_voice.set_muted(False)
hub_voice.say("Cancelled by mute.", cancellation_token=token)
hub_voice.style_fn = lambda: hub_voice.hush() or ""
hub_voice.say("Cancelled during preparation.")
assert [data["text"] for _, data in speech_events] == ["Accepted."], speech_events
hub_voice.style_fn = None
hub_voice.say("Fresh.")
assert [data["text"] for _, data in speech_events] == ["Accepted.", "Fresh."], speech_events


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
            # Same display name, different actual defaults: permissions follow tokens, not selectors.
            v.cfg["voice"] = ""
            v.cfg["markup_voices"] = {"token:A": ["rate", "pitch"], "token:B": ["pauses"]}
            markup = {"text": "A 😀 & <B>.", "spans": [{"start": 2, "end": 3, "rate": "slow", "pitch": "high"}]}
            v.say(markup["text"], markup=markup, features=["rate"])
            frozen = new.command()
            assert frozen["voice"] == "token:A" and frozen["features"] == ["rate"], frozen
            assert frozen["markup"]["text"] == "A 😀 & <B>."
            assert frozen["markup"]["spans"] == [{"start": 2, "end": 3, "rate": "slow"}], frozen
            new.catalogue = [{"id": "Requested voice", "name": "Requested voice",
                              "identity": "token:B", "isDefault": True}]
            v.say(markup["text"], markup=markup, features=["rate"])
            changed = new.command()
            assert changed["voice"] == "token:B" and changed["features"] == [], changed
            assert "markup" not in changed and changed["text"] == markup["text"], changed
            assert frozen["markup"]["spans"][0]["rate"] == "slow", "later settings changed accepted controls"
            new.emit("cancelled", frozen["id"])
            new.emit("cancelled", changed["id"])

            # Discovery cannot hold off PTT or revive a line whose enqueue was waiting for the catalogue.
            new.auto_catalogue = False
            waiting = threading.Thread(target=lambda: v.say("Cancelled during discovery"), daemon=True)
            waiting.start()
            discovery = new.command()
            assert discovery["op"] == "voices", discovery
            v.hush()
            stop = new.command()
            assert stop["op"] == "cancel", stop
            new.emit("voices", discovery["id"], voices=new.catalogue)
            waiting.join(3)
            assert not waiting.is_alive() and new.stdin.sent.empty(), "PTT-cancelled discovery resurrected speech"
            stale_token = v.cancellation_token()
            v.hush()
            assert new.command()["op"] == "cancel"
            v.say("Cancelled before native preparation.", cancellation_token=stale_token)
            assert new.stdin.sent.empty(), "a stale supplied token began catalogue discovery"
            stale_token = v.cancellation_token()
            v.set_muted(True)
            assert new.command()["op"] == "cancel"
            v.set_muted(False)
            v.say("Cancelled before mute was released.", cancellation_token=stale_token)
            assert new.stdin.sent.empty(), "mute/unmute revived an old preparation token"
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
