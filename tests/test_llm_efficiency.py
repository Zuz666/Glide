#!/usr/bin/env python3
"""How the coach talks to the model: the streamed reply rebuilt exactly (and spoken sentence by sentence while it
arrives, only the rest said at the end), the user message split into a cached REFERENCE part and a live part that
are shrunk by structure (never cut mid-JSON), fixed place text with the live distances in NEARBY, what each callout
topic gets, WoW's $ codes cleaned out of quest text, and the user cache marker kept for OpenRouter's Claude models.

Run: python3 tests/test_llm_efficiency.py   (prints OK)
"""
import http.server
import io
import json
import os
import sys
import threading
import types
import urllib.error

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import coach  # noqa: E402
from coach import Coach  # noqa: E402
from prompt_context import fit as _fit, compact_json as _json, progress_text as _progress_text  # noqa: E402
from providers import read_stream  # noqa: E402
from replies import EarlySpeech  # noqa: E402
from glidedb import GlideDB, wow_text  # noqa: E402
from state import GameState  # noqa: E402
import openai_compat  # noqa: E402


def sse(events):
    """A streamed reply as the lines urllib hands back."""
    out = []
    for e in events:
        out += [b"event: " + e["type"].encode() + b"\n", b"data: " + json.dumps(e).encode() + b"\n", b"\n"]
    return out


# ---- read_stream: the same message dict a plain request returns --------------------------------------------------
EVENTS = [
    {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "content": [],
                                          "stop_reason": None, "usage": {"input_tokens": 120,
                                                                         "cache_read_input_tokens": 9000,
                                                                         "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Short answer."}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Turn in Supplies to "}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Tannok, 20 yards east. "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "citations_delta",
                                                         "citation": {"url": "https://x.example", "title": "X"}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Then train.\nFACTS: q:1"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "kill_xp",
                                                                  "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"mob_le'}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'vel": 7}'}},
    {"type": "content_block_stop", "index": 2},
    {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "toolu_2", "name": "x",
                                                                  "input": {}}},
    {"type": "content_block_stop", "index": 3},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 61}},
    {"type": "message_stop"},
]
chunks = []
msg = read_stream(iter(sse(EVENTS)), chunks.append)
assert msg["stop_reason"] == "tool_use" and msg["usage"] == {"input_tokens": 120, "cache_read_input_tokens": 9000,
                                                             "output_tokens": 61}, msg
th, tx, tu, tu2 = msg["content"]
assert th == {"type": "thinking", "thinking": "Short answer.", "signature": "sig=="}, th
assert tx["text"] == "Turn in Supplies to Tannok, 20 yards east. Then train.\nFACTS: q:1" and \
    tx["citations"] == [{"url": "https://x.example", "title": "X"}], tx
assert tu == {"type": "tool_use", "id": "toolu_1", "name": "kill_xp", "input": {"mob_level": 7}}, tu
assert tu2["input"] == {}, tu2
assert "".join(chunks) == tx["text"], chunks
try:
    read_stream(iter(sse(EVENTS[:2] + [{"type": "error", "error": {"type": "overloaded_error",
                                                                   "message": "Overloaded"}}])), None, "u")
    raise AssertionError("an error event must raise")
except urllib.error.HTTPError as e:
    assert e.code == 529 and "overloaded_error" in e.read().decode(), e
print("read_stream OK")

# ---- EarlySpeech: whole sentences while they arrive, never past the trim limit, never the FACTS / SKIP lines ------
said = []
sp = EarlySpeech(lambda t, u: said.append((t, u)), 10, urgent=True)
for piece in ("Turn in Supplies", " to Tannok, 20 yards east.", " Then train", " at Thran.\nFACTS: q:1, s:2"):
    sp(piece)
assert said == [("Turn in Supplies to Tannok, 20 yards east.", True)], said      # "Then train..." would pass 10 words
assert sp.spoken == "Turn in Supplies to Tannok, 20 yards east." and sp.said
said.clear()
sp = EarlySpeech(lambda t, u: said.append(t), 30)
for piece in ("Go east.", " Kill boars.\n", "FACTS: q:1."):
    sp(piece)
assert said == ["Go east.", "Kill boars."], said
said.clear()
sp = EarlySpeech(lambda t, u: said.append(t), 30)
sp("SKIP. ")
assert said == [] and not sp.said
sp = EarlySpeech(lambda t, u: said.append(t), 40, prefix="New quest: X. RestedXP: kill 8 boars.")
sp("They roam the hills north. ")
assert sp.spoken == "New quest: X. RestedXP: kill 8 boars. They roam the hills north.", sp.spoken
print("early speech OK")

# ---- the app says only what wasn't said yet ---------------------------------------------------------------------
import app  # noqa: E402
voice_said = []
# Bypass startup: exercise real delivery without reading player config or starting services.
fake = app.App.__new__(app.App)
fake.coach, fake.overlay, fake._ptt_down = None, None, False
fake._hist_lock, fake.msg_history, fake._held = threading.Lock(), [], []
fake.hub = types.SimpleNamespace(publish=lambda *a: None)
fake.write_log = lambda o: None
fake._flags = {"voice": True}
fake.voice = types.SimpleNamespace(
    say=lambda t, interrupt=False, tone="", first=True: voice_said.append((t, interrupt)))
fake.on_message({"kind": "answer", "text": "Go east.\nKill boars there.", "speak": True, "urgent": True,
                 "spoken": "Go east."})
fake.on_message({"kind": "answer", "text": "Go east.", "speak": True, "spoken": "Go east."})
fake.on_message({"kind": "answer", "text": "Something else.", "speak": True, "spoken": "Let me check."})
fake.on_message({"kind": "callout", "text": "Rebuff.", "speak": True, "urgent": True})
assert voice_said == [("Kill boars there.", False), ("Something else.", False), ("Rebuff.", True)], voice_said
fake._flags = {"voice": False}                        # /coach voice off: early speech too, except answers
voice_said.clear()
fake.speak_early("Rebuff.", "callout")
fake.speak_early("Ten yards.", "answer", True)
assert voice_said == [("Ten yards.", True)], voice_said
print("app speech OK")

# ---- _fit: shrink by structure, least important first; a text cut only as the very last resort -----------------
d = {"keep": "x" * 50, "items": list(range(200))}
txt = _fit(d, 300, [lambda o: bool(o["items"]) and (o["items"].pop() or True)])
assert len(txt) <= 300 and json.loads(txt)["keep"] == "x" * 50, txt          # still valid JSON
assert _fit({"a": "y" * 400}, 100, []).endswith("..."), "no step left: the old cut"
assert _json({"w": "Elwynn · Goldshire"}) == '{"w":"Elwynn · Goldshire"}'     # no · escapes
assert _progress_text({"complete": True, "objectives": [[1, 1]]}) == "done"
assert _progress_text({"complete": False, "objectives": [[3, 8], [0, 1]]}) == "3/8, 0/1"
print("fit OK")

# ---- WoW text codes ---------------------------------------------------------------------------------------------
assert wow_text("weeds!$B$BBring me a feast, $n.") == "weeds! Bring me a feast, adventurer."
assert wow_text("Thank you, $Gsir:madam;. A $c like you") == "Thank you, sir. A adventurer like you"
assert wow_text("No codes here.") == "No codes here." and wow_text(None) is None
print("wow text OK")

# ---- OpenRouter keeps the user message's cache breakpoint for Claude models (and joins it for the rest) ----------
body = {"model": "anthropic/claude-sonnet-5.5", "max_tokens": 50, "system": [{"type": "text", "text": "S"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "REF\n\n", "cache_control": {"type": "ephemeral"}},
                                                  {"type": "text", "text": "LIVE"}]}]}
u = openai_compat.to_openai(body)["messages"][-1]
assert u["content"] == [{"type": "text", "text": "REF\n\n", "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": "LIVE"}], u
u = openai_compat.to_openai(dict(body, model="meta-llama/llama-4"))["messages"][-1]
assert u == {"role": "user", "content": "REF\n\nLIVE"}, u
print("openrouter OK")

# ---- the user message: REFERENCE (stable while the player moves) + live, topics, nothing cut ---------------------
KB = os.path.join(ROOT, "data", "kb.db")
if not os.path.exists(KB):
    print("SKIP: prompt layout part (no data/kb.db); the rest passed")
    raise SystemExit(77)


class A(http.server.BaseHTTPRequestHandler):
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        A.bodies.append(b)
        text = "Sit at the campfire for a minute. The fire is right by Eric.\nFACTS: q:96608"
        if b.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            ev = [{"type": "message_start", "message": {"content": [], "usage": {"input_tokens": 10}}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
            ev += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w}}
                   for w in (text[:20], text[20:41], text[41:])]
            ev += [{"type": "content_block_stop", "index": 0},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 30}}]
            for line in sse(ev):
                self.wfile.write(line)
            return
        out = json.dumps({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                          "usage": {"input_tokens": 10, "output_tokens": 30}}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()  # shutdown() returns at once
os.environ["ANTHROPIC_API_KEY"] = "test"
msgs, spoken = [], []
c = Coach({"api_url": "http://127.0.0.1:%d/v1/messages" % port, "fast_model": "claude-sonnet-5-5",
           "deep_model": "claude-opus-5-5", "grounding_log": ""}, GlideDB(os.path.join(ROOT, "data", "glide.db")),
          msgs.append, kb=KB, offline_ok=True)
assert c.pack is not None, c.kb_reason
gs = GameState()
core = {"t": "1000", "l": "7", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1426", "px": "4650", "py": "5300",
        "st": "5", "c": "1", "tg": "1125", "tl": "6", "tr": "2", "qn": "1", "qh": "17096", "h": "40"}
pages = ({"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "Testwarrior"},
         {"pg": "G", "gn": "05-11 Dun Morogh", "gg": "RestedXP Forever Guide (A)"},
         {"pg": "T", "tn": "Crag Boar"}, {"pg": "Q0", "q": "96608:0:0/1"},
         {"pg": "S0", "i": "5", "el": "go:1426:46.7:53.9,complete:96608", "tx": "0/1 Sit near the campfire"}, {})
for page in pages:
    gs.update(core, page)
c.state = gs
from rules import Trigger  # noqa: E402


def user_of(body):
    cont = body["messages"][0]["content"]
    assert isinstance(cont, list) and len(cont) == 2 and cont[0].get("cache_control") == {"type": "ephemeral"}
    return cont[0]["text"], cont[1]["text"]


c._respond([Trigger("step_changed", {"from": 4, "to": 5}, 2)])
ref1, live1 = user_of(A.bodies[-1])
assert ref1.startswith("REFERENCE (") and live1.startswith("KNOWLEDGE (") and "GAME STATE (live):" in live1
assert '"rxp_steps"' in live1 and '"rxp_steps"' not in ref1, "route steps change with the step: live part"
ref_json = json.loads(ref1.split("\n", 1)[1])
assert "96608" in ref_json["quests"] and "progress" not in ref_json["quests"]["96608"], ref_json["quests"]
assert '"quest_progress":{"The Great Outdoors":"0/1"}' in live1, live1[:2000]
assert "units_note" not in live1 and "buffs_note" not in live1 and "Data conventions" in A.bodies[-1]["system"][0]["text"]
# the player walks and turns: REFERENCE is byte-identical (the cache reads it back), NEARBY follows the player
core2 = dict(core, px="4700", py="5250", f="300")
gs.update(core2, {})
c._respond([Trigger("stuck_on_step", {"minutes": 3}, 2)])
ref2, live2 = user_of(A.bodies[-1])
assert ref2 == ref1, "REFERENCE changed while only the position did"
assert "NEARBY (live" in live2 and "current step waypoint:" in live2, live2[-1500:]
# a buff callout: no route, target or heuristics; a combat callout: the target, no route
gs.update(core, {})
c._respond([Trigger("buff_expiring", {"buff": "Battle Shout", "seconds": 10}, 2)])
_, live3 = user_of(A.bodies[-1])
assert '"rxp_steps"' not in live3 and '"heuristics"' not in live3 and "NEARBY" not in live3, live3[:1500]
c._respond([Trigger("low_health_in_combat", {"hp": 30}, 1)])
_, live4 = user_of(A.bodies[-1])
assert '"target"' in live4 and '"rxp_steps"' not in live4 and "NEARBY" not in live4, live4[:1500]
assert '"rationale"' not in live4, "callouts get the heuristics' advice only"
print("prompt layout OK")

# ---- a spoken answer streams: the first sentence is said before the reply is done, the app says the rest -------
c.deps.speak_fn = lambda t, kind, urgent: spoken.append((t, kind, urgent))
msgs.clear()
res = c.answer_once("where is the campfire?", voice=True)
ans = res["answer"]
assert A.bodies[-1].get("stream") is True, "a spoken answer is streamed"
assert ans["text"] == "Sit at the campfire for a minute. The fire is right by Eric.", ans
assert spoken[0] == ("Sit at the campfire for a minute.", "answer", True), spoken
assert ans["spoken"].startswith("Sit at the campfire for a minute."), ans
assert ans["facts"] == ["q:96608"], ans
# without a speech hook nothing streams (evals, tests): the plain request
c.deps.speak_fn = None
c.answer_once("where is the campfire?", voice=True)
assert "stream" not in A.bodies[-1]
srv.shutdown()
print("OK")
