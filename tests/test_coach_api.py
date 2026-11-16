#!/usr/bin/env python3
"""Coach answers and errors through local HTTP, including effort refusal without a silent downgrade."""
import json, os, sys, threading, time, http.server
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
from coach import Coach
from glidedb import GlideDB
from state import GameState

mode = {"v": "reject"}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        H.bodies.append(b)
        if mode["v"] == "reject":
            out = b'{"type":"error","error":{"message":"effort not supported"}}'
            self.send_response(400); self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        if mode["v"] == "empty":
            out = json.dumps({"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens", "usage": {"output_tokens": 600}}).encode()
        else:
            out = json.dumps({"content": [{"type": "text", "text": "Battle Shout is up for 1:40."}], "usage": {}}).encode()
        self.send_response(200); self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)
H.bodies = []
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ANTHROPIC_API_KEY"] = "test"
msgs = []
c = Coach({"api_url": "http://127.0.0.1:%d/v1/messages" % port, "fast_model": "claude-opus-5-5", "deep_model": "claude-opus-5-5"},
          GlideDB("/x.db"), msgs.append)
c.state = GameState()
c.start()
def wait(n):
    for _ in range(80):
        if len([m for m in msgs if m["kind"] in ("answer", "error")]) >= n: return
        time.sleep(0.1)
c.ask("do I have battle shout", voice=True); wait(1)
assert len(H.bodies) == 1 and H.bodies[0]["output_config"]["effort"] == "low", H.bodies
assert any(m["kind"] == "error" and "effort not supported" in m["text"] for m in msgs), msgs
mode["v"] = "answer"
c.ask("do I have battle shout", voice=True); wait(2)
assert H.bodies[1]["output_config"]["effort"] == "low" and "thinking" not in H.bodies[1], H.bodies
ans = [m for m in msgs if m["kind"] == "answer"]
assert ans and ans[0]["text"].startswith("Battle Shout") and ans[0]["speak"], msgs
mode["v"] = "empty"
c.ask("and now?", voice=True); wait(3)
err = [m for m in msgs if m["kind"] == "error"]
assert any("no answer text" in m["text"] for m in err), msgs
print("OK")

# length cap: whole sentences while they fit, a run-on first sentence is cut
from replies import trim_words as _trim_words
long = "Recast Battle Shout before pulling. Then accept Tools for Steelgrill from Tharek, 70 yards northwest. Weaponsmith Grawn is just past him if you want prices."
assert _trim_words(long, 18) == "Recast Battle Shout before pulling. Then accept Tools for Steelgrill from Tharek, 70 yards northwest.", _trim_words(long, 18)
runon = " ".join(["word"] * 40)
assert len(_trim_words(runon, 18).split()) == 18
assert _trim_words("Equip Ragged Cloak (back).", 18) == "Equip Ragged Cloak (back)."
print("trim OK")

# ---------------------------------------------------------------------------------------------------------
# knowledge layer: tool-use loop, FACTS line, grounding log (fake API; data/kb.db when present)
import tempfile
from rules import Trigger
KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "kb.db")
if not os.path.exists(KB_PATH):
    print("SKIP: knowledge layer part (no data/kb.db); the rest passed")
    raise SystemExit(77)


def utext(msg):
    """A message's text whether it was sent as a string or as content blocks (cache markers make it blocks)."""
    c = msg["content"]
    return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")


class T(http.server.BaseHTTPRequestHandler):
    mode, bodies = "kill_xp", []
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        T.bodies.append(b)
        last = b["messages"][-1]["content"]
        has_result = isinstance(last, list) and any(x.get("type") == "tool_result" for x in last)
        forced = (b.get("tool_choice") or {}).get("type") == "none"
        if T.mode == "callout":
            out = {"content": [{"type": "text", "text": "Sit at the campfire for a minute.\nFACTS: q:96608"}],
                   "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 12}}
        elif T.mode == "greedy" and not forced:         # always asks for another tool until forced to answer
            n = len(T.bodies)
            out = {"content": [{"type": "tool_use", "id": "toolu_g%d" % n, "name": "kill_xp", "input": {"mob_level": 6}}],
                   "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 20}}
        elif not has_result and not forced:
            out = {"content": [{"type": "text", "text": "Checking."},
                               {"type": "tool_use", "id": "toolu_1", "name": "kill_xp", "input": {"mob_level": 7}}],
                   "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 20}}
        else:
            out = {"content": [{"type": "text", "text": "A level 7 mob gives you 80 XP.\nFACTS: p:xp.kill.base_mul, q:999999999"}],
                   "stop_reason": "end_turn", "usage": {"input_tokens": 150, "output_tokens": 18}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), T)
port2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()

glog = os.path.join(tempfile.mkdtemp(), "grounding.jsonl")
msgs2 = []
kc = Coach({"api_url": "http://127.0.0.1:%d/v1/messages" % port2, "fast_model": "claude-haiku-4-5-20251001",
            "deep_model": "claude-opus-5-5", "grounding_log": glog}, GlideDB("/x.db"), msgs2.append,
           kb=KB_PATH, offline_ok=True)
assert kc.pack is not None and kc.pack.ok, kc.kb_reason
gs = GameState()
core = {"t": "1000", "l": "7", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1426", "px": "4650", "py": "5300",
        "st": "5", "c": "1", "tg": "1125", "tl": "6", "tr": "2", "qn": "1", "qh": "17096"}
for page in ({"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "Testwarrior"},
             {"pg": "G", "gn": "05-11 Dun Morogh", "gg": "RestedXP Forever Guide (A)"},
             {"pg": "T", "tn": "Crag Boar"}, {"pg": "Q0", "q": "96608:0:0/1"},
             {"pg": "S0", "i": "5", "el": "go:1426:46.7:53.9,complete:96608", "tx": "0/1 Sit near the campfire"}, {}):
    gs.update(core, page)
res = kc.answer_once("how much XP does a level 7 mob give me?", state=gs)
ans = res["answer"]
assert ans and ans["text"] == "A level 7 mob gives you 80 XP.", (ans, msgs2)          # FACTS line stripped
assert len(T.bodies) == 2, len(T.bodies)
b1, b2 = T.bodies
assert b1["model"] == "claude-opus-5-5" and b1["tool_choice"] == {"type": "auto"}            # typed -> deep
assert [t["name"] for t in b1["tools"]][:2] == ["quest_info", "creature_info"] and len(b1["tools"]) == 22
assert b1["tools"][-1] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 2}, \
    "typed: web search (Opus 5.5: the filtering version)"
# prompt caching: one marker at the end of the system prompt covers tools + system (deep path: 1-hour TTL);
# a second marker on the newest message lets the next round of the tool loop read the conversation back
assert not any(t.get("cache_control") for t in b1["tools"])
assert b1["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}, b1["system"][-1].get("cache_control")
assert b1["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
assert utext(b1["messages"][0]).startswith("REFERENCE (") and "\n\nKNOWLEDGE (" in utext(b1["messages"][0]) \
    and '"q:96608"' in utext(b1["messages"][0])


def markers(body):
    """Every cache_control in a request (the API allows 4)."""
    n = sum(1 for b in body.get("system") or () if b.get("cache_control"))
    n += sum(1 for t in body.get("tools") or () if t.get("cache_control"))
    for m in body["messages"]:
        if isinstance(m["content"], list):
            n += sum(1 for b in m["content"] if isinstance(b, dict) and b.get("cache_control"))
    return n


# the REFERENCE part of the first message carries its own marker (stable while the step lasts), the live part not
assert b1["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"} and \
    b1["messages"][0]["content"][0]["text"].startswith("REFERENCE (")


def lookahead_steps(body):
    txt = utext(body["messages"][0])
    assert "UPCOMING RESTEDXP STEPS:\n" in txt, txt[:300]
    look = json.loads(txt.split("UPCOMING RESTEDXP STEPS:\n", 1)[1].split("\n\n", 1)[0])
    return look["steps"]


typed_steps = lookahead_steps(b1)                      # typed: the current step + the next 40
assert typed_steps[0].get("current") and typed_steps[0]["n"] == 5 and len(typed_steps) == 41, len(typed_steps)
assert [st["n"] for st in typed_steps[:3]] == [5, 6, 7], [st["n"] for st in typed_steps[:3]]
assert "FACTS:" in b1["system"][0]["text"] and "GLOSSARY" in b1["system"][0]["text"]
assert b2["messages"][1]["role"] == "assistant" and b2["messages"][1]["content"][1]["type"] == "tool_use"
assert [bool(b.get("cache_control")) for b in b2["messages"][0]["content"]] == [True, False], \
    "the first message keeps only its REFERENCE marker; the newest message carries the tail marker"
assert all(markers(b) <= 4 for b in (b1, b2)), [markers(b) for b in (b1, b2)]
assert b2["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
tr = b2["messages"][2]["content"][0]
assert tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_1" and not tr.get("is_error"), tr
r = json.loads(tr["content"])
assert r["xp"] == 80 and r["your_level"] == 7 and r["mob_level"] == 7, r                      # the tool really ran
assert ans["tool_calls"] == 1 and ans["tools"] == ["kill_xp"] and "1 tool call" in ans["model"], ans
assert kc.q_rounds == {"typed": 2} and kc.look_tokens_per_step and 20 < kc.look_tokens_per_step < 200, \
    (kc.q_rounds, kc.look_tokens_per_step)        # dashboard cost inputs: rounds per question, tokens per step
assert ans["facts"] == ["p:xp.kill.base_mul", "q:999999999"] and ans["facts_unknown"] == ["q:999999999"], ans
assert kc.stats["tool_calls"] == 1 and kc.stats["cited"] == 1 and kc.stats["unsupported"] == 1, kc.stats
rec = [json.loads(l) for l in open(glog, encoding="utf-8")]
assert len(rec) == 1 and rec[0]["kind"] == "answer" and rec[0]["unknown_ids"] == ["q:999999999"], rec
assert rec[0]["had_numbers"] is True and rec[0]["facts"] == ["p:xp.kill.base_mul", "q:999999999"], rec
assert set(rec[0]) == {"t", "kind", "text", "facts", "unknown_ids", "had_numbers"}, rec
print("tool loop OK")

# voice: fast model, at most 2 tool calls, then the final answer is forced (tool_choice none)
T.mode, T.bodies = "greedy", []
res = kc.answer_once("is a level 6 boar worth it?", voice=True)
voice_steps = lookahead_steps(T.bodies[0])             # spoken: the current step + the next 10
assert len(voice_steps) == 11 and voice_steps[0].get("current") and voice_steps[0]["n"] == 5, len(voice_steps)
assert res["answer"] and res["answer"]["text"] == "A level 7 mob gives you 80 XP.", res
assert len(T.bodies) == 3 and all(b["model"] == "claude-haiku-4-5-20251001" for b in T.bodies), len(T.bodies)
assert [b["tool_choice"]["type"] for b in T.bodies] == ["auto", "auto", "none"], [b["tool_choice"] for b in T.bodies]
assert res["answer"]["tool_calls"] == 2 and res["usage"]["output_tokens"] == 58, res
assert "push-to-talk" in utext(T.bodies[0]["messages"][0])
assert [b["system"][-1]["cache_control"] for b in T.bodies] == [{"type": "ephemeral"}] * 3, "fast path: 5 minutes"
assert [bool(isinstance(b["messages"][-1]["content"], list) and b["messages"][-1]["content"][-1].get("cache_control"))
        for b in T.bodies] == [True, True, False], "no marker on the forced last answer (nothing reads it)"
voice_tools = T.bodies[0]["tools"]
print("voice caps OK")

# live callout: one shot (no tools), the knowledge block in the message, FACTS stripped and verified
T.mode, T.bodies = "callout", []
m = kc._respond([Trigger("step_changed", {"from": 4, "to": 5}, 2)])
assert m and m["kind"] == "callout" and m["text"] == "Sit at the campfire for a minute." and m["facts"] == ["q:96608"]
# callouts send the questions' tools (tool_choice none) so both share one cached tools + system prefix
cb = T.bodies[0]
assert cb["tools"] == voice_tools and cb["tool_choice"] == {"type": "none"}, cb.get("tool_choice")
assert cb["system"][-1]["cache_control"] == {"type": "ephemeral"}
cbc = cb["messages"][0]["content"]
assert cbc[0]["text"].startswith("REFERENCE (") and cbc[0]["cache_control"] == {"type": "ephemeral"} and \
    "cache_control" not in cbc[1] and cbc[1]["text"].startswith("KNOWLEDGE ("), \
    "a callout caches its REFERENCE part (the next callout on this step reads it back), never the live part"
assert markers(cb) <= 4
k = kc.context(triggers=["step_changed"])["knowledge"]
assert k["step"]["n"] == 5 and k["target"]["name"] == "Crag Boar" and k["heuristics"], k
assert kc.stats["cited"] == 3 and kc.stats["unsupported"] == 2 and len(open(glog, encoding="utf-8").readlines()) == 3, kc.stats
assert kc._directives("Go.\nWATCH: Rend\nFACTS: q:1, s:11-12 Elwynn (Dwarf/Gnome)/5", with_facts=True) == \
    ("Go.", ["q:1", "s:11-12 Elwynn (Dwarf/Gnome)/5"], True)
assert kc._directives("Go. FACTS: none") == "Go."
assert [e["type"] for e in kc.recent][-1] == "message"
# the glide.db reader and the knowledge package live side by side under their own names (no shadowing)
import glidedb  # noqa: E402
import knowledge.api  # noqa: E402
assert glidedb.GlideDB is GlideDB and hasattr(knowledge, "__path__") and kc.api is knowledge.api
print("callout + directives OK")
print("OK")
