#!/usr/bin/env python3
"""Push-to-talk loop with fake keyboard/mic/STT, and the coach's voice-question path with a fake API."""
import json, os, sys, threading, time, http.server
from unittest.mock import patch
import gc
import weakref
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
import ptt
from ptt_audio import Recording

class ObservedRecording(Recording):
    """Weak-referenceable recording at the controlled microphone boundary."""
    pass

assert ptt.parse_key("F8") == [0x77] and ptt.parse_key("ctrl+q") == [0x11, 0x51] and ptt.parse_key("MOUSE5") == [6]
try:
    ptt.parse_key("F99"); raise SystemExit("bad key accepted")
except ValueError:
    pass

class FakeUser32:
    def __init__(self): self.down = False
    def GetAsyncKeyState(self, k): return 0x8000 if self.down else 0
class FakeCapture:
    """Controlled microphone boundary; each capture owns its own terminal result."""
    def __init__(self, microphone):
        self.microphone = microphone
        self.ready, self.done = threading.Event(), threading.Event()
        self.recording, self.error = None, None
    def start(self):
        self.microphone.calls.append("start")
        if self.microphone.start_error:
            self.error = self.microphone.start_error
            self.done.set()
        elif not self.microphone.delay_start:
            self.ready.set()
    def finish(self):
        self.microphone.calls.append("stop")
        if not self.microphone.delay_finish:
            self.complete()
    def complete(self):
        self.recording = ObservedRecording(b"\x00\x80\xff\x7f\x01\x00")
        self.done.set()
    def cancel(self):
        self.microphone.calls.append("stop")
        self.done.set()

class FakeMicrophone:
    def __init__(self):
        self.calls, self.sessions = [], []
        self.start_error = None
        self.delay_start = self.delay_finish = False
    def __call__(self):
        capture = FakeCapture(self)
        self.sessions.append(capture)
        return capture

class FakeSTT:
    name = "fake"
    def __init__(self): self.recordings = []
    def transcribe(self, recording, prompt=""):
        self.prompt = prompt
        self.recordings.append(recording.pcm)
        return "should I skip the wolf quest"

def prepare_loop(instance):
    instance._stop = threading.Event()
    instance._attempt_lock = threading.RLock()
    instance._attempt = None
    instance._capture = None
    instance.capture_factory = FakeMicrophone()
    instance.fallback = None

got, status = [], []
p = ptt.PushToTalk.__new__(ptt.PushToTalk)
p.cfg = {"key": "F8", "beep": False, "min_seconds": 0.2}
p.keys = ptt.parse_key("F8"); p.user32 = FakeUser32(); p.stt = FakeSTT()
prepare_loop(p)
p.on_question = got.append; p.on_status = status.append; p.prompt_fn = lambda: "Names: Wolf Den"; p.focus_check = None
states = []; p.on_state = states.append
def until(what, cond, timeout=5):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out waiting for " + what
        time.sleep(0.005)
loop = threading.Thread(target=p._loop, daemon=True)
loop.start()
# tap: ignored (let go as soon as the loop has seen the key, well under min_seconds)
p.user32.down = True; until("listening", lambda: states == ["listening"]); p.user32.down = False
until("the tap's end", lambda: len(states) >= 2)
assert states == ["listening", "idle"] and not got, (states, got)
# hold: asks (held 0.25 s after the loop saw the key, so over min_seconds 0.2 however the loop is scheduled)
p.user32.down = True; until("listening again", lambda: len(states) >= 3); time.sleep(0.25); p.user32.down = False
until("the question", lambda: got and len(states) >= 5)
assert got == ["should I skip the wolf quest"], got
assert p.stt.prompt == "Names: Wolf Den" and status[0]["kind"] == "question", status
assert p.stt.recordings == [b"\x00\x80\xff\x7f\x01\x00"], p.stt.recordings
# the indicator, as it happens: a tap listens and goes idle; a hold listens, transcribes, then goes idle
assert states == ["listening", "idle", "listening", "transcribing", "idle"], states

# Relinquish the fake device's observation too: neither idle polling nor a
# completed transcription may retain audio until another recording completes.
completed_capture = p.capture_factory.sessions[-1]
recording_ref = weakref.ref(completed_capture.recording)
completed_capture.recording = None
gc.collect()
until("finished recording released without another press", lambda: recording_ref() is None)
print("ptt loop OK", p.capture_factory.calls)
# a press that arrives while the last recording is still being turned into text is ignored until the key is
# let go again: no second recording, no second listening state; a fresh press after idle works as before
class SlowSTT:
    name = "slow"
    def __init__(self): self.gate = threading.Event(); self.entered = threading.Event()
    def transcribe(self, recording, prompt=""):
        self.entered.set()
        self.gate.wait(5)
        return "and the next question"
slow = SlowSTT(); p.stt = slow
p.user32.down = True; until("listening again", lambda: len(states) >= 6)
time.sleep(0.25); p.user32.down = False
until("slow transcription starts", lambda: len(states) >= 7 and states[-1] == "transcribing")
p.user32.down = True; time.sleep(0.3)                      # pressed during transcription: ignored
assert len(states) == 7 and p.capture_factory.calls == ["start", "stop", "start", "stop", "start", "stop"], (states, p.capture_factory.calls)
slow.gate.set()                                             # transcription ends while the key is still held
until("idle with the key still held", lambda: len(states) >= 8)
time.sleep(0.25)                                            # holding through idle must not start recording
assert len(states) == 8 and len(p.capture_factory.calls) == 6, (states, p.capture_factory.calls)
p.user32.down = False; time.sleep(0.15)                     # releasing the ignored press changes nothing
assert len(states) == 8, states
p.user32.down = True; until("a fresh press listens", lambda: len(states) >= 9 and states[-1] == "listening")
time.sleep(0.25); p.user32.down = False
until("the next question", lambda: len(got) >= 3 and len(states) >= 11)
assert got[1] == got[2] == "and the next question" and states[-3:] == ["listening", "transcribing", "idle"], (got, states)
print("ptt press during transcription ignored OK")

# the transcription's "idle" is published before the gate opens: a press that arrives while that idle is
# still being delivered is ignored, so a stale "idle" can never land after the next press's "listening"
class HeldStates:
    def __init__(self): self.states = []; self.release = threading.Event()
    def __call__(self, st):
        if st == "idle" and not self.release.is_set():
            self.release.wait(5)
        self.states.append(st)
p2 = ptt.PushToTalk.__new__(ptt.PushToTalk)
p2.cfg = {"key": "F8", "beep": False, "min_seconds": 0.2}
p2.keys = ptt.parse_key("F8"); p2.user32 = FakeUser32()
prepare_loop(p2)
slow2 = SlowSTT(); p2.stt = slow2; p2.fallback = None
p2.on_question = lambda text: None; p2.on_status = lambda m: None; p2.prompt_fn = None; p2.focus_check = None
held = HeldStates(); p2.on_state = held
threading.Thread(target=p2._loop, daemon=True).start()
p2.user32.down = True; until("p2 listening", lambda: held.states == ["listening"])
time.sleep(0.25); p2.user32.down = False
until("p2 transcribing", lambda: len(held.states) >= 2 and held.states[-1] == "transcribing")
slow2.gate.set(); time.sleep(0.3)             # the transcription ends; its idle sits undelivered
p2.user32.down = True; time.sleep(0.3)        # a press while that idle is still in flight
assert held.states == ["listening", "transcribing"], held.states
assert p2.capture_factory.calls == ["start", "stop"], p2.capture_factory.calls
held.release.set()
until("the held idle lands", lambda: held.states == ["listening", "transcribing", "idle"])
p2.user32.down = False; time.sleep(0.15)      # the skipped press needs a fresh one after the gate opens
p2.user32.down = True
until("a fresh press listens after the gate", lambda: len(held.states) == 4 and held.states[-1] == "listening")
p2.user32.down = False
print("ptt idle published before the gate opens OK")

# The capture can fail while the physical key remains down. No partial question, no reopen loop.
p.stop()
loop.join(2)
p3 = ptt.PushToTalk.__new__(ptt.PushToTalk)
p3.cfg = {"key": "F8", "beep": False, "min_seconds": 0}
p3.keys = ptt.parse_key("F8"); p3.user32 = FakeUser32(); p3.stt = FakeSTT()
prepare_loop(p3)
questions3, errors3, states3 = [], [], []
p3.on_question = questions3.append; p3.on_status = errors3.append
p3.prompt_fn = None; p3.focus_check = None; p3.on_state = states3.append
loop3 = threading.Thread(target=p3._loop, daemon=True)
with patch("builtins.open", side_effect=AssertionError("PTT must not write audio files")):
    loop3.start()
    p3.user32.down = True
    until("capture starts", lambda: states3 == ["listening"])
    first = p3.capture_factory.sessions[-1]
    first.error = "recording reached the 120-second limit"
    first.done.set()
    until("capture failure is visible", lambda: errors3 and states3[-1] == "idle")
    time.sleep(.08)
    assert len(p3.capture_factory.sessions) == 1 and not questions3
    assert not p3.key_down and not p3.transcribing
    p3.user32.down = False; time.sleep(.05)
    p3.user32.down = True
    until("fresh capture after failure", lambda: len(states3) == 3)
    p3.user32.down = False
    until("question after recovery", lambda: questions3)
    assert questions3 == ["should I skip the wolf quest"]

    # A failed microphone open also requires a fresh press.
    until("recognition ends", lambda: not p3.transcribing)
    p3.capture_factory.start_error = "microphone access denied"
    p3.user32.down = True
    until("open error", lambda: len(errors3) >= 3)
    count = len(p3.capture_factory.sessions)
    time.sleep(.08)
    assert len(p3.capture_factory.sessions) == count
    p3.user32.down = False; time.sleep(.05)
    p3.capture_factory.start_error = None

    # Key-up before Start acknowledgement cancels, rather than recognizing a post-release recording.
    p3.capture_factory.delay_start = True
    p3.user32.down = True
    until("pending start", lambda: len(p3.capture_factory.sessions) > count)
    pending_start = p3.capture_factory.sessions[-1]
    p3.user32.down = False
    until("pending start cancelled", lambda: pending_start.done.is_set())
    pending_start.ready.set()
    time.sleep(.06)
    assert questions3 == ["should I skip the wolf quest"]
    p3.capture_factory.delay_start = False

    # A new press during finalization is ignored, including after the old capture finishes.
    p3.capture_factory.delay_finish = True
    p3.user32.down = True
    until("delayed finish capture", lambda: states3[-1] == "listening")
    pending_finish = p3.capture_factory.sessions[-1]
    p3.user32.down = False
    until("finish requested", lambda: p3.capture_factory.calls[-1] == "stop")
    p3.user32.down = True; time.sleep(.05)
    pending_finish.complete()
    until("delayed question", lambda: len(questions3) == 2)
    count = len(p3.capture_factory.sessions)
    time.sleep(.08)
    assert len(p3.capture_factory.sessions) == count
    p3.user32.down = False; time.sleep(.05)
    p3.capture_factory.delay_finish = False

    # Shutdown retires pending recognition. Late output cannot submit a question or change state.
    slow3 = SlowSTT()
    p3.stt = slow3
    p3.user32.down = True
    until("last recording", lambda: states3[-1] == "listening")
    p3.user32.down = False
    until("pending recognition", lambda: slow3.entered.is_set() and p3.transcribing)
    p3.stop()
    stopped_states = list(states3)
    slow3.gate.set()
    time.sleep(.08)
    assert len(questions3) == 2 and states3 == stopped_states
loop3.join(2)
assert not loop3.is_alive()
held.release.set()
p2.stop()
print("ptt fileless capture, cancellation, late output and fresh-press recovery OK")

# coach: voice question -> fast model, short answer, urgent (interrupts speech)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        H.last = b
        out = json.dumps({"content": [{"type": "text", "text": "Skip it; the den is off-route."}], "usage": {}}).encode()
        self.send_response(200); self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ANTHROPIC_API_KEY"] = "test"
from coach import Coach
from glidedb import GlideDB
from state import GameState
msgs = []
c = Coach({"api_url": "http://127.0.0.1:%d/v1/messages" % port, "fast_model": "FAST", "deep_model": "DEEP"},
          GlideDB("/nonexistent.db"), msgs.append)
c.state = GameState()
c.start()
c.ask("should I skip the wolf quest", voice=True)
until("the answer", lambda: msgs, 5)
assert H.last["model"] == "FAST" and H.last["max_tokens"] == 600, (H.last["model"], H.last["max_tokens"])
_c = H.last["messages"][0]["content"]
assert "push-to-talk" in (_c if isinstance(_c, str) else "".join(b["text"] for b in _c))
assert msgs[0]["urgent"] and msgs[0]["speak"] and msgs[0]["kind"] == "answer", msgs
c.ask("typed question")
until("the second answer", lambda: len(msgs) > 1, 5)
assert H.last["model"] == "DEEP" and not msgs[1]["urgent"], (H.last["model"], msgs[1])
print("coach voice path OK")
print("OK")
