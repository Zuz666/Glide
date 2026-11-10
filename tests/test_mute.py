#!/usr/bin/env python3
"""Mute the narrator (footer button, tray, Ctrl+Alt+M -> POST /mute -> App.set_muted): neither voice speaks while
muted, the line being spoken is cut off, and unmuting brings the voice back."""
import json
import os
import sys
import threading
import types

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import app as glide_app  # noqa: E402
from voice import HubVoice, Voice  # noqa: E402

published = []
hub = types.SimpleNamespace(publish=lambda kind, data: published.append((kind, data)))
v = HubVoice({"enabled": True, "announce": False}, hub)
fake = types.SimpleNamespace(voice=v)

v.say("Next: Thendal Grove")
assert glide_app.App.set_muted(fake, True) == {"muted": True}
v.say("Level 3. Check new spells")
assert [data["text"] for _, data in published] == ["Next: Thendal Grove"], "a muted voice spoke"
glide_app.App.set_muted(fake, False)
v.say("Back again")
assert [data["text"] for _, data in published] == ["Next: Thendal Grove", "Back again"], "unmuting didn't bring the voice back"


class Pipe:
    def __init__(self):
        self.active = "Previous line"
        self.spoken = []

    def write(self, s):
        command = json.loads(s)
        if command["op"] == "cancel":
            self.active = None
        elif command["op"] == "speak":
            self.active = command["text"]
            self.spoken.append(self.active)

    def flush(self):
        pass


# the coach's own speech process: muting interrupts the current line (under the voice's lock), nothing is written
# while muted, and unmuting speaks again
proc_voice = Voice({"enabled": False})
proc_voice.enabled = True                           # as on Windows, with a stand-in for the speech process
proc_voice.proc = types.SimpleNamespace(stdin=Pipe(), poll=lambda: None)
assert glide_app.App.set_muted(types.SimpleNamespace(voice=proc_voice), True) == {"muted": True}
assert proc_voice.muted and proc_voice.proc.stdin.active is None, "muting didn't interrupt the active line"
proc_voice.say("Level 4")
assert proc_voice.proc.stdin.active is None and proc_voice.proc.stdin.spoken == [], "a muted voice spoke"
proc_voice.lock.acquire()                          # mute waits for a line being written
t = threading.Thread(target=proc_voice.set_muted, args=(False,))
t.start()
t.join(0.2)
assert t.is_alive(), "set_muted didn't take the voice's lock"
proc_voice.lock.release()
t.join()
proc_voice.say("Level 5")
assert proc_voice.proc.stdin.active == "Level 5" and proc_voice.proc.stdin.spoken == ["Level 5"] and not proc_voice.muted

print("mute: ok")
