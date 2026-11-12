#!/usr/bin/env python3
"""Push-to-talk loop with fake keyboard/mic/STT, and the coach's voice-question path with a fake API."""
import json, os, sys, threading, time, http.server
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
import ptt

assert ptt.parse_key("F8") == [0x77] and ptt.parse_key("ctrl+q") == [0x11, 0x51] and ptt.parse_key("MOUSE5") == [6]
try:
    ptt.parse_key("F99"); raise SystemExit("bad key accepted")
except ValueError:
    pass

class FakeUser32:
    def __init__(self): self.down = False
    def GetAsyncKeyState(self, k): return 0x8000 if self.down else 0
class FakeRec:
    def __init__(self): self.calls = []
    def start(self): self.calls.append("start")
    def stop(self, path): self.calls.append("stop"); return path
class FakeSTT:
    name = "fake"
    def transcribe(self, path, prompt=""): self.prompt = prompt; return "should I skip the wolf quest"

got, status = [], []
p = ptt.PushToTalk.__new__(ptt.PushToTalk)
p.cfg = {"key": "F8", "beep": False, "min_seconds": 0.2}
p.keys = ptt.parse_key("F8"); p.user32 = FakeUser32(); p.rec = FakeRec(); p.stt = FakeSTT()
p.on_question = got.append; p.on_status = status.append; p.prompt_fn = lambda: "Names: Wolf Den"; p.focus_check = None
states = []; p.on_state = states.append
def until(what, cond, timeout=5):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out waiting for " + what
        time.sleep(0.005)
threading.Thread(target=p._loop, daemon=True).start()
# tap: ignored (let go as soon as the loop has seen the key, well under min_seconds)
p.user32.down = True; until("listening", lambda: states == ["listening"]); p.user32.down = False
until("the tap's end", lambda: len(states) >= 2)
assert states == ["listening", "idle"] and not got, (states, got)
# hold: asks (held 0.25 s after the loop saw the key, so over min_seconds 0.2 however the loop is scheduled)
p.user32.down = True; until("listening again", lambda: len(states) >= 3); time.sleep(0.25); p.user32.down = False
until("the question", lambda: got and len(states) >= 5)
assert got == ["should I skip the wolf quest"], got
assert p.stt.prompt == "Names: Wolf Den" and status[0]["kind"] == "question", status
# the indicator, as it happens: a tap listens and goes idle; a hold listens, transcribes, then goes idle
assert states == ["listening", "idle", "listening", "transcribing", "idle"], states
print("ptt loop OK", p.rec.calls)
# a press that arrives while the last recording is still being turned into text is ignored until the key is
# let go again: no second recording, no second listening state; a fresh press after idle works as before
class SlowSTT:
    name = "slow"
    def __init__(self): self.gate = threading.Event()
    def transcribe(self, path, prompt=""): self.gate.wait(5); return "and the next question"
slow = SlowSTT(); p.stt = slow
p.user32.down = True; until("listening again", lambda: len(states) >= 6)
time.sleep(0.25); p.user32.down = False
until("slow transcription starts", lambda: len(states) >= 7 and states[-1] == "transcribing")
p.user32.down = True; time.sleep(0.3)                      # pressed during transcription: ignored
assert len(states) == 7 and p.rec.calls == ["start", "stop", "start", "stop", "start", "stop"], (states, p.rec.calls)
slow.gate.set()                                             # transcription ends while the key is still held
until("idle with the key still held", lambda: len(states) >= 8)
time.sleep(0.25)                                            # holding through idle must not start recording
assert len(states) == 8 and len(p.rec.calls) == 6, (states, p.rec.calls)
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
p2.keys = ptt.parse_key("F8"); p2.user32 = FakeUser32(); p2.rec = FakeRec()
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
assert p2.rec.calls == ["start", "stop"], p2.rec.calls
held.release.set()
until("the held idle lands", lambda: held.states == ["listening", "transcribing", "idle"])
p2.user32.down = False; time.sleep(0.15)      # the skipped press needs a fresh one after the gate opens
p2.user32.down = True
until("a fresh press listens after the gate", lambda: len(held.states) == 4 and held.states[-1] == "listening")
p2.user32.down = False
print("ptt idle published before the gate opens OK")

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
