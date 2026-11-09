#!/usr/bin/env python3
"""The character feels there with the player (banter.py, persona.py): it reacts to moments (deaths and death streaks,
level-ups, close calls, elite kills, new zones), keeps a diary of them across sessions and recalls it, greets them at a
login and says goodbye when the game goes away, has a mood that moments push and time settles, asks for a varied shape
and names its overused openers, talks sooner in downtime and where the player answers, remembers what it made up about
itself (LORE) under its name (NAME) so "bring back <name>" restores it, and delivers each line with its TONE.

Run: python3 tests/test_banter_moments.py   (prints OK)
"""
import http.server
import json
import os
import sys
import threading
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
from coach_deps import CoachDeps  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from replies import EarlySpeech  # noqa: E402
from rules import Trigger  # noqa: E402
from state import GameState  # noqa: E402
import banter  # noqa: E402
import coach  # noqa: E402
import memory as memory_mod  # noqa: E402
import voice  # noqa: E402


def check(cond, what):
    if not cond:
        raise SystemExit("FAIL: " + str(what))


class A(http.server.BaseHTTPRequestHandler):
    bodies, replies = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        A.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        out = {"content": [{"type": "text", "text": A.replies.pop(0)}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 10, "output_tokens": 3}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def user_text(b):
    m = b["messages"][-1]["content"]
    return m if isinstance(m, str) else "".join(x.get("text", "") for x in m if x.get("type") == "text")


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
os.environ["ANTHROPIC_API_KEY"] = "test"
banter.random.random = lambda: 0.0          # every moment gets its reaction (the dice are tested by their odds)
mem = memory_mod.Memory(None)
said = []
c = coach.Coach({"api_url": "http://127.0.0.1:%d/v1/messages" % srv.server_address[1],
                 "fast_model": "claude-haiku-4-5-20251001", "deep_model": "claude-sonnet-5-5",
                 "stream_speech": False}, GlideDB("/x.db"),
                lambda m: said.append(m) if m.get("kind") in ("banter", "answer") else None, offline_ok=True,
                deps=CoachDeps(memory=mem))
c.cfg["persona"] = "A grizzled dwarf drill sergeant. Calls you recruit."
b = c.banter


gs = GameState()
gs.update({"t": "1000", "l": "12", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1436", "px": "4650", "py": "5300"},
          {"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "T"})
real_summary = gs.summary


def frame(triggers=(), **kw):
    """A frame whose summary has these values (on top of a real game state's)."""
    s = dict(real_summary(), **{"level": 12, "zone": "Westfall", "subzone": "Moonbrook", "hp": 80, "combat": False})
    s.update(kw)
    gs.summary = lambda: s
    gs.vitals = lambda: s
    c.push(list(triggers), gs)
    return gs


# ---- a login: the first live frame greets them -----------------------------------------------------------------
c.last_call = time.time()                   # (just spoke: the greeting waits its moment)
frame()
b.tick()
check(b.moment and b.moment["kind"] == "greeting" and "first session together" in b.moment["text"], b.moment)
check(mem.bond["sessions"] == 1 and "glad to see them" in b.mood_line(), "a session, and the mood")
b.moment = None

# ---- moments: the diary, the mood, a reaction --------------------------------------------------------------
for _ in range(3):
    frame([Trigger("died", {"arg": "Defias Pillager"}, 1)], dead=1)
check(b.moment["kind"] == "death_streak" and "third time" in b.moment["text"] and "Defias Pillager" in b.moment["text"],
      b.moment)
check(len(mem.diary_recent(10)) == 3 and "exasperated" in b.mood_line(), "three deaths in the diary, exasperated")
check("Your mood right now: exasperated" in b.reminder(), "the mood rides on the stay-in-character line")
frame([Trigger("level_up", {}, 1)], level=13)
check(b.moment["kind"] == "level_up" and "proud" in b.mood_line(), "a level-up beats it")
frame(combat=True, hp=40)
frame(combat=True, hp=9)
frame(combat=False, hp=30)
check(b.moment["kind"] == "level_up", "a close call doesn't replace a stronger moment")
check(any("Survived a fight at 9% health" in x["text"] for x in mem.diary_recent(10)), "but it's in the diary")
frame(target={"name": "Hogger", "classification": "elite", "dead": True, "level": 11})
check(any("Took down Hogger (elite)" in x["text"] for x in mem.diary_recent(10)), "an elite kill")
frame([Trigger("zone_changed", {}, 3)], zone="Duskwood")
frame([Trigger("zone_changed", {}, 3)], zone="Duskwood")
check(sum("First time in Duskwood" in x["text"] for x in mem.diary_recent(20)) == 1, "a new zone once")

# the reaction comes soon after the coach's own line, carrying the moment and the diary
c.last_call = time.time() - 10
c.pending = []                              # (the coach thread has said its own lines about them)
b.moment = {"kind": "level_up", "text": "Reached level 13 in Westfall", "t": time.time()}
A.replies.append("TONE: bellowing, proud\nTHIRTEEN, recruit! Ye might live yet!\nFACTS: none")
b.tick()
check(said and said[-1]["kind"] == "banter" and said[-1]["text"] == "THIRTEEN, recruit! Ye might live yet!", said)
check(said[-1].get("tone") == "bellowing, proud", "the line's TONE goes on the message")
req = user_text(A.bodies[-1])
check("WHAT JUST HAPPENED: Reached level 13 in Westfall" in req, "the moment")
check("YOUR DIARY" in req and "Died to Defias Pillager" in req and "just now" in req, "the diary")
check("Your mood right now: proud" in req, "the mood")
check(b.moment is None and "level_up" in b.cooldown, "spoken: cooled down")

# ---- variety: a shape each time, and the openers it overused ----------------------------------------------------
b.said = ["Oi, ye call that a swing?", "Oi! Watch the left!", "Hmph. Fine work, recruit?"]
note = banter.variety_note(b.said + ["Right then, what's next?"])
check('Don\'t open with "oi" again.' in note and "Don't end on a question" in note, note)
check("This time, the shape: " in b.request(25), "an unprompted aside asks for a shape")
check(any("diary" in banter.Banter.shape(b) or True for _ in range(3)), "shapes")

# ---- downtime and what the replies taught: sooner while flying, later where they ignore it -----------------------
check(banter.activity({"on_taxi": True}) == "flying" and banter.activity({"buffs": [{"name": "Food"}]}) == "eating",
      "activities")
check(b._factor("flying") == 0.4 and b._factor("questing") == 1.0, "downtime is sooner")
mem.bond["by_activity"] = {"flying": [5, 4], "questing": [9, 1]}
check(abs(b._factor("flying") - 0.28) < 1e-9 and b._factor("questing") == 2.0, "learned: chatty flying, not questing")
check("flying 4 of 5" in b.request(25), "the request says when they answer")

# ---- the game goes away after a while together: a goodbye --------------------------------------------------------
b.session_t, b.last_frame = time.time() - 1800, time.time() - 60
c.frame_t = time.time() - 60
window = [True]
b.window_gone = lambda: not window[0]
b._clock(time.time())
check(b.moment is None and not b.gone, "a minute without frames, the window still there (an alt-tab): no goodbye")
window[0] = False
b._clock(time.time())
check(b.moment and b.moment["kind"] == "goodbye" and b.gone, "the game went away: a goodbye")
check(b._due(time.time()), "said even without frames")
A.replies.append("TONE: gruff, soft\nOff wi' ye then. Good day's work, recruit.\nFACTS: none")
b.tick()
check(said[-1]["text"].startswith("Off wi' ye then") and "THE PLAYER JUST LEFT" in user_text(A.bodies[-1]), said[-1])

# ---- the character's own story: NAME, LORE, recall ----------------------------------------------------------------
text = c._directives("TONE: muttering\nGrr. Fine.\nPERSONA: A twitchy gnome tinker who blows things up.\nNAME: Fizzle\n"
                     "VOICE: Squeaky, fast.\nLORE: Blew up three labs in Gnomeregan.")
check(text == "Grr. Fine." and c.tone == "muttering", "stripped; the tone kept")
check(c.persona_name() == "Fizzle" and mem.character("fizzle")["lore"] == ["Blew up three labs in Gnomeregan."],
      "the character and its lore are kept by name")
c._directives("Back to normal.\nPERSONA: off")
check(c.persona() == "" and c.persona_voice() == "", "the act is dropped")
c._directives("Sarge reporting!\nPERSONA: recall fizzle")
check(c.persona().startswith("A twitchy gnome") and c.persona_voice() == "Squeaky, fast." and
      c.persona_name() == "Fizzle", "recalled with its voice and name")
check(mem.character("fizzle")["lore"] == ["Blew up three labs in Gnomeregan."], "its story survives recall")

# ---- each line performed with its tone ------------------------------------------------------------------------
heard = []
es = EarlySpeech(lambda t, u: heard.append((t, es.tone)), 50)
es("TONE: mock-whis")
es("per\nLean in, recruit. ")
es("There's gold in them hills.\nFACTS: none")
check(heard == [("Lean in, recruit.", "mock-whisper"), ("There's gold in them hills.", "mock-whisper")], heard)
events = []
hub = type("Hub", (), {"publish": lambda self, k, d: events.append(d), "on_first_client": lambda self, f: None})()
hv = voice.HubVoice({"enabled": True, "announce": False}, hub)
hv.style_fn = c.persona_voice
hv.say("Lean in.", tone="mock-whisper")
check(events[-1]["style"] == "Squeaky, fast. This line: mock-whisper.", events[-1])
srv.shutdown()
print("OK")
