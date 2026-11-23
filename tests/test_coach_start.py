#!/usr/bin/env python3
"""The coach thread started inside Coach.__init__, before the app had set its ten hooks (reminders, memory, early
speech, the catalog...), so for a moment after startup the thread could run with them all None. The app now hands
them over at construction (CoachDeps) and starts the thread itself once the coach is built."""
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(k, None)
import reminders  # noqa: E402
from coach import Coach, CoachDeps  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from state import GameState  # noqa: E402


def coach_threads():
    return [t for t in threading.enumerate() if t.name == "coach"]


# built: no thread yet, and what the app hands over is there from the start
msgs, said = [], []
rem = reminders.Reminders(None)
c = Coach({"fast_model": "F", "deep_model": "D"}, GlideDB("/x.db"), msgs.append,
          deps=CoachDeps(reminders=rem, speak_fn=lambda t, kind, urgent: said.append(t)))
assert not coach_threads(), "the coach thread waits for start()"
assert c.deps.reminders is rem and c.deps.speak_fn is not None
assert [t.__self__ for t in c._jobs if hasattr(t, "__self__")] == [c.briefings, c.reminder_ticker, c.reflector, c, c.route,
                                                                    c.banter]

# started: questions are answered on the coach thread (offline here: no API key)
c.state = GameState()
c.start()
c.start()                                     # a second start() is a no-op
assert len(coach_threads()) == 1, threading.enumerate()
c.ask("what now?")
for _ in range(50):
    if any(m.get("kind") == "answer" for m in msgs):
        break
    time.sleep(0.1)
ans = [m for m in msgs if m.get("kind") == "answer"]
assert ans and ans[0]["model"].startswith("offline"), msgs

# a coach for evals and tools never runs a thread
e = Coach({"fast_model": "F"}, GlideDB("/x.db"), lambda m: None, offline_ok=True)
e.start()
assert e._thread is None and len(coach_threads()) == 1

# the app builds every hook before the coach exists
import app  # noqa: E402

tmp = tempfile.mkdtemp()
app.live_path = lambda name: os.path.join(tmp, name)
fake = types.SimpleNamespace(args=types.SimpleNamespace(sim=True), cfg={"voice": {}}, write_log=lambda rec: None,
                             on_settings=lambda changes: None, set_watch=lambda name, on: None,
                             speak_early=lambda text, kind, urgent=False: None,
                             rules=types.SimpleNamespace(addon_status={}, watched=set()))
deps = app.App._coach_deps(fake, {"fast_model": "F"})
missing = [k for k, v in vars(deps).items() if v is None]
assert not missing, missing
assert fake.catalog is deps.catalog and fake.memory is deps.memory
print("coach start: ok")
