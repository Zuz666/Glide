#!/usr/bin/env python3
"""A character that stays itself (Phase 2 of the coach's social side): a new character gets sample lines and its own
story threads (once, in the background), and lines the player answered become favourites; they anchor every call as
"HOW YOU SOUND", with its story threads, who the player is to it (ROLE) and the scene being played (SCENE); phrases it
keeps repeating are named to rest; greetings mention its own life while the player was away.

Run: python3 tests/test_character_depth.py   (prints OK)
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
from state import GameState  # noqa: E402
import coach  # noqa: E402
import memory as memory_mod  # noqa: E402
import persona  # noqa: E402


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
mem = memory_mod.Memory(None)
saved = {}
c = coach.Coach({"api_url": "http://127.0.0.1:%d/v1/messages" % srv.server_address[1],
                 "fast_model": "claude-haiku-4-5-20251001", "deep_model": "claude-sonnet-5-5",
                 "stream_speech": False}, GlideDB("/x.db"), lambda m: None, offline_ok=True,
                deps=CoachDeps(memory=mem, on_toggle=lambda k, v: saved.__setitem__(k, v)))
gs = GameState()
gs.update({"t": "1000", "l": "12", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1429", "px": "4650", "py": "5300"},
          {"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "T"})
c.state = gs
b = c.banter

# ---- a new character: its sample lines and its own story, once, in the background ------------------------------
c._directives("Aye!\nPERSONA: A grizzled dwarf drill sergeant from Ironforge. Calls you recruit.\nNAME: Sarge")
A.replies.append(json.dumps({"samples": ["Move it, recruit!", "Ye fight like a kobold with a candle.",
                                         "Good. Now do it again, faster.", "Back in '98 we did this without maps.",
                                         "What's yer story, recruit?"],
                             "plot": ["Wants to see Ironforge's Great Forge relit.", "Hides a letter from his son."]}))
b._seed()
for _ in range(100):
    if (mem.character("sarge") or {}).get("samples"):
        break
    time.sleep(0.05)
ch = mem.character("sarge")
check(ch["samples"][0] == "Move it, recruit!" and len(ch["plot"]) == 2, ch)
check("A grizzled dwarf drill sergeant from Ironforge. Calls you recruit." in user_text(A.bodies[-1]),
      "the seed request carries the character's brief")
n = len(A.bodies)
b._seed()
check(len(A.bodies) == n, "seeded once")

# ---- the character keeps its role and scene ------------------------------------------------------------------
infos = []
c._emit_raw = lambda m: infos.append(m) if m.get("kind") == "info" else None
c._directives("Right, recruit.\nROLE: Their drill sergeant; calls them recruit.\nSCENE: We're bounty hunters after "
              "Hogger; the reward is a keg of Thunderbrew.")
ch = mem.character("sarge")
check(ch["role"].startswith("Their drill sergeant") and ch["scene"].startswith("We're bounty hunters"), ch)
check([m["text"] for m in infos] == ["Scene: We're bounty hunters after Hogger; the reward is a keg of Thunderbrew."],
      infos)
c._directives("Still hunting.\nSCENE: We're bounty hunters after Hogger, the reward a keg of Thunderbrew.\n"
              "ROLE: Their drill sergeant, calls them recruit.")
check(len(infos) == 1 and mem.character("sarge")["role"].startswith("Their drill sergeant; calls"),
      "said again in other words: nothing changes, nothing shown")
c._directives("A plain answer.\nRole: tank or healer, your pick")
check(mem.character("sarge")["role"].startswith("Their drill sergeant"), "an answer's own 'Role:' line is the answer's")
c._directives("Bounty's in, recruit.\nSCENE: ended, they dropped it")
check(mem.character("sarge")["scene"] == "" and infos[-1]["text"] == "Scene ended", "the scene ends")
c._directives("No scene.\nSCENE: end")
check(infos[-1]["text"] == "Scene ended" and len([m for m in infos if m["text"] == "Scene ended"]) == 1,
      "ending no scene shows nothing")
# the scene is the character's: another character has its own (none), coming back brings it back
c._directives("Ahoy.\nSCENE: A heist at the Stockade.")
c._directives("New act.\nPERSONA: A sarcastic butler.\nNAME: Jeeves")
check(c.main_name() == "Jeeves" and not mem.character("Jeeves").get("scene"), "Jeeves has no scene")
c._directives("Sarge again!\nPERSONA: recall Sarge")
check(c.main_name() == "Sarge" and mem.character("Sarge")["scene"] == "A heist at the Stockade.",
      "Sarge's scene is his")
ch = mem.characters["sarge"]
ch["scene_t"] = time.time() - coach.SCENE_S - 1
check(not any("A heist at the Stockade." in block["text"] for block in c._system_blocks()),
      "a scene not played for hours has lapsed")
mem.characters["sarge"]["scene"] = ""

# ---- a line the player answered becomes a favourite of the one who said it; worn phrases spare the names ----------
c.cfg["persona"], c.cfg["persona_name"] = mem.character("sarge")["brief"], "Sarge"
b.awaiting = (c._now(), "questing", "Ye fight like a troll with a hangover, recruit.", "Sarge")
c.convo.append({"t": c._now() + 1, "who": "player", "kind": "voice", "text": "hey!"})
b._reaction()
check("Ye fight like a troll with a hangover, recruit." in mem.character("sarge")["favorites"], "a favourite")
b.awaiting = (c._now(), "questing", "Arr, that one's mine.", "Cap")      # said by another character before a switch
c.convo.append({"t": c._now() + 1, "who": "player", "kind": "voice", "text": "ha"})
b._reaction()
check("Arr, that one's mine." not in mem.character("sarge")["favorites"], "not Sarge's: not his favourite")
for i in range(4):
    c.emit({"kind": "banter", "speak": True, "text": "By my beard, recruit, ye'll turn in Wolves Across the Border."})
    c.emit({"kind": "callout", "speak": True, "text": "Turn in the quest %d, recruit, by my beard." % i})
worn = b.worn()
check("by my beard" in worn, worn)
check(not any(w in g for g in worn for w in ("turn", "quest")), "the coaching's own words are never worn: %r" % worn)
check(not any("across" in g or "border" in g or "wolves" in g for g in worn), "names are never worn: %r" % worn)
check(len([g for g in worn if "beard" in g]) == 1, "one of overlapping phrases: %r" % worn)
check(len(mem.character("sarge")["lines"]) == 4, "only the character's own lines (asides, chats) are counted")

# ---- the first lore stays; the seed knows what the character already said -------------------------------------
for i in range(40):
    mem.character_lore("Sarge", " ".join("fact%dw%d" % (i, k) for k in range(5)))      # distinct facts
lore = mem.character("sarge")["lore"]
check(len(lore) == 24, len(lore))
check("fact0w0" in " ".join(lore[:8]) and "fact39w0" in lore[-1] and "fact20w0" not in " ".join(lore), lore[:9])
req = persona.seed_request("A grizzled dwarf.", ["Lost his beard to a rocket."], "Their drill sergeant")
check("Lost his beard to a rocket." in req and "Their drill sergeant" in req,
      "the seed request carries existing lore and the player's role")

srv.shutdown()
print("OK")
