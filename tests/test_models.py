#!/usr/bin/env python3
"""Model selector: the catalog (Anthropic + OpenRouter lists, prices, tool support, cache), the OpenRouter request /
response translation, the coach's tool loop through a mock OpenRouter, the missing-key path, cost tracking, model
settings, and the dashboard /models endpoint."""
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.append(ROOT)
import models  # noqa: E402
import openai_compat  # noqa: E402
import tuning  # noqa: E402

# ---- catalog --------------------------------------------------------------------------------------------
pages = {None: {"data": [{"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5", "lifecycle": "active",
                          "max_input_tokens": 1000000},
                         {"id": "claude-old", "display_name": "Old", "lifecycle": "retired"}],
                "has_more": True, "last_id": "claude-old"},
         "claude-old": {"data": [{"id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5",
                                  "lifecycle": "active"}], "has_more": False}}


def fake_get(url, headers):
    if "anthropic" in url:
        assert headers["x-api-key"] == "ak"
        after = url.split("after_id=")[1] if "after_id=" in url else None
        return pages[after]
    return {"data": [
        {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "context_length": 1048576,
         "pricing": {"prompt": "0.0000003", "completion": "0.0000025", "input_cache_read": "0.000000075"},
         "supported_parameters": ["tools", "tool_choice", "max_tokens"], "architecture": {"output_modalities": ["text"]}},
        {"id": "some/no-tools", "name": "No Tools", "pricing": {"prompt": "0.0000001", "completion": "0.0000001"},
         "supported_parameters": ["max_tokens"]},
        {"id": "img/gen", "name": "Image", "pricing": {"prompt": "0"}, "architecture": {"output_modalities": ["image"]}}]}


ant = models.fetch_anthropic("ak", getter=fake_get)
assert [m["id"] for m in ant] == ["claude-sonnet-5-5", "claude-haiku-4-5-20251001"], ant
assert ant[0]["price"] == {"in": 2.0, "out": 10.0, "cache_read": 0.1, "cache_write": 2.5} and ant[0]["context"] == 1000000
orl = models.fetch_openrouter(getter=fake_get)
assert [m["id"] for m in orl] == ["google/gemini-2.5-flash", "some/no-tools"], orl          # image model dropped
assert orl[0]["price"] == {"in": 0.3, "out": 2.5, "cache_read": 0.075, "cache_write": None} and orl[0]["tools"] is True
assert orl[1]["tools"] is False
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "models.json")
cat = models.Catalog(path, "ak", None, fetchers={"anthropic": lambda k: models.fetch_anthropic(k, getter=fake_get),
                                                 "openrouter": lambda k: models.fetch_openrouter(k, getter=fake_get)})
st = cat.refresh()
assert st["count"] == 4 and st["errors"] == {"gemini": "no GEMINI_API_KEY"}, st
assert st["keys"] == {"anthropic": True, "gemini": False, "openrouter": False}, st
assert cat.price("google/gemini-2.5-flash")["in"] == 0.3 and cat.price("claude-opus-5-5")["in"] == 4.0
assert cat.supports_tools("some/no-tools") is False and cat.supports_tools("unknown/x") is True
assert models.Catalog(path).get("google/gemini-2.5-flash")["name"] == "Gemini 2.5 Flash", "cached on disk"


def broken(k):
    raise OSError("offline")


cat2 = models.Catalog(path, None, None, fetchers={"anthropic": broken, "openrouter": broken})
st = cat2.refresh()
assert "openrouter" in st["errors"] and cat2.get("google/gemini-2.5-flash"), "a failing provider keeps its old list"
assert cat2.get("claude-sonnet-5-5") is not None
cat3 = models.Catalog(os.path.join(tmp, "m3.json"), None, None,
                      fetchers={"anthropic": lambda k: [], "openrouter": lambda k: []})
st = cat3.refresh()
assert cat3.get("claude-opus-5-5") and "no ANTHROPIC_API_KEY" in st["errors"]["anthropic"], st
assert abs(models.cost_usd({"in": 2.0, "out": 10.0, "cache_read": 0.2}, {"input_tokens": 1000, "output_tokens": 100,
                                                                         "cache_read_input_tokens": 5000}) - 0.004) < 1e-9

# ---- translation ---------------------------------------------------------------------------------------
body = {"model": "anthropic/claude-sonnet-4.5", "max_tokens": 600,
        "system": [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "how far?"},
                     {"role": "assistant", "content": [{"type": "text", "text": "checking"},
                                                       {"type": "tool_use", "id": "t1", "name": "eta", "input": {"target_level": 30}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{\"s\":1}"}]}],
        "tools": [{"name": "eta", "description": "d", "input_schema": {"type": "object", "properties": {}},
                   "cache_control": {"type": "ephemeral"}}],
        "tool_choice": {"type": "none"}}
o = openai_compat.to_openai(body)
assert o["messages"][0] == {"role": "system", "content": [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]}
assert o["messages"][1] == {"role": "user", "content": "how far?"}
assert o["messages"][2]["tool_calls"][0] == {"id": "t1", "type": "function",
                                             "function": {"name": "eta", "arguments": "{\"target_level\": 30}"}}
assert o["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "{\"s\":1}"}
assert o["tools"] == [{"type": "function", "function": {"name": "eta", "description": "d",
                                                        "parameters": {"type": "object", "properties": {}}}}]
assert o["tool_choice"] == "none" and o["usage"] == {"include": True}
assert openai_compat.to_openai(dict(body, model="meta/llama"))["messages"][0] == {"role": "system", "content": "SYS"}
r = openai_compat.from_openai({"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "eta", "arguments": "{\"target_level\": 31}"}}]}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 1000}, "cost": 0.0012}})
assert r["stop_reason"] == "tool_use" and r["content"] == [{"type": "tool_use", "id": "c1", "name": "eta", "input": {"target_level": 31}}]
assert r["usage"] == {"input_tokens": 200, "output_tokens": 30, "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 0, "cost_usd": 0.0012}, r["usage"]
assert openai_compat.fatal(402, "Insufficient credits") == "out of API credits"
assert openai_compat.fatal(401, "No auth credentials found") == "API key rejected" and openai_compat.fatal(429, "rate") is None

# ---- coach through a mock OpenRouter ------------------------------------------------------------------------
from coach import Coach  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from state import GameState  # noqa: E402


class H(http.server.BaseHTTPRequestHandler):
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        H.bodies.append((self.headers.get("authorization"), b))
        if b.get("tools") and b.get("tool_choice") == "auto" and len(H.bodies) == 1:
            out = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "kill_xp", "arguments": "{\"mob_level\": 7}"}}]}}],
                "usage": {"prompt_tokens": 9000, "completion_tokens": 20, "cost": 0.0021}}
        else:
            out = {"choices": [{"finish_reason": "stop", "message": {"content": "A level 7 mob gives you 80 XP.\nFACTS: p:xp.kill.base_mul"}}],
                   "usage": {"prompt_tokens": 9500, "completion_tokens": 25, "prompt_tokens_details": {"cached_tokens": 8000}}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()  # shutdown() returns at once
os.environ["OPENROUTER_API_KEY"] = "or-test"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
KB = os.path.join(ROOT, "data", "kb.db")
msgs = []
kc = Coach({"openrouter_url": "http://127.0.0.1:%d/api/v1/chat/completions" % port, "fast_model": "google/gemini-2.5-flash",
            "deep_model": "google/gemini-2.5-flash"}, GlideDB("/x.db"), msgs.append,
           kb=KB if os.path.exists(KB) else None, offline_ok=True)
kc.llm.catalog = cat
assert kc.enabled and kc.llm.keys["openrouter"] == "or-test" and not kc.llm.keys["anthropic"]
gs = GameState()
core = {"t": "1000", "l": "7", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1426", "px": "4650", "py": "5300", "st": "5"}
for page in ({"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "T"},
             {"pg": "G", "gn": "05-11 Dun Morogh", "gg": "RestedXP Forever Guide (A)"}, {}):
    gs.update(core, page)
res = kc.answer_once("how much XP does a level 7 mob give me?", state=gs)
ans = res["answer"]
assert ans and ans["text"] == "A level 7 mob gives you 80 XP.", (ans, msgs)
auth, first = H.bodies[0]
assert auth == "Bearer or-test" and first["model"] == "google/gemini-2.5-flash" and "thinking" not in first
assert first["messages"][0]["role"] == "system" and first["messages"][-1]["role"] == "user"
if kc.pack is not None:
    assert len(H.bodies) == 2 and ans["tools"] == ["kill_xp"], H.bodies
    second = H.bodies[1][1]
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_1" and second["messages"][-1]["role"] == "tool"
    assert abs(kc.stats["cost_usd"] - (0.0021 + (1500 * 0.3 + 25 * 2.5 + 8000 * 0.075) / 1e6)) < 1e-9, kc.stats["cost_usd"]
    assert kc.path_stats["deep"]["calls"] == 2 and kc.path_stats["deep"]["cache_read"] == 8000, kc.path_stats
# caching via OpenRouter: Google models get the system marker without the Anthropic-only 1-hour TTL
cache_markers = [block["cache_control"] for message in first["messages"] if message["role"] == "system"
                 for block in message["content"] if "cache_control" in block]
assert cache_markers and all(marker == {"type": "ephemeral"} for marker in cache_markers), first["messages"][0]
assert kc.llm.caching("google/gemini-2.5-flash") == "explicit" and kc.llm.caching("openai/gpt-5") == "implicit"
assert kc.llm.cache_marker("anthropic/claude-sonnet-5-5", True) == {"type": "ephemeral", "ttl": "1h"}
assert kc.llm.cache_marker("gemini:gemini-2.5-flash", True) is None and kc.llm.cache_marker("claude-haiku-4-5", False) == \
    {"type": "ephemeral"}
o_or = openai_compat.to_openai({"model": "google/gemini-2.5-pro", "max_tokens": 5, "messages": [],
                             "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]})
assert o_or["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
# prices: Sonnet 5.5 reads at 0.05x; 1-hour cache writes cost 2x input
assert models.anthropic_price("claude-sonnet-5-5")["cache_read"] == 0.10
assert models.anthropic_price("claude-sonnet-5")["cache_read"] == 0.20
u1h = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1000,
       "cache_creation": {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600}}
assert abs(models.cost_usd(models.anthropic_price("claude-sonnet-5-5"), u1h) -
           (100 * 2 + 10 * 10 + 400 * 2.5 + 600 * 4.0) / 1e6) < 1e-12
# a model without tool support: one plain call, no tools sent
H.bodies.clear()
kc.cfg["deep_model"] = "some/no-tools"
res = kc.answer_once("anything?", state=gs)
assert len(H.bodies) == 1 and "tools" not in H.bodies[0][1], H.bodies
# no key for the provider: offline answer that says which key is missing
kc.cfg["deep_model"] = "claude-opus-5-5"
res = kc.answer_once("anything?", state=gs)
assert "No ANTHROPIC_API_KEY for claude-opus-5-5" in res["answer"]["text"], res["answer"]
assert "offline" in res["answer"]["model"]

# ---- Gemini (direct) -----------------------------------------------------------------------------------------
gpages = {None: {"models": [
    {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "inputTokenLimit": 1048576,
     "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-tts", "displayName": "TTS", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "p2"},
          "p2": {"models": [{"name": "models/gemini-2.5-flash-lite", "displayName": "Gemini 2.5 Flash-Lite",
                             "supportedGenerationMethods": ["generateContent"]}]}}


def gget(url, headers):
    assert headers["x-goog-api-key"] in ("gk", "gm-test")
    return gpages["p2" if "pageToken=p2" in url else None]


gem = models.fetch_gemini("gk", getter=gget)
assert [m["id"] for m in gem] == ["gemini:gemini-2.5-flash", "gemini:gemini-3.8-flash", "gemini:gemini-2.5-flash-lite"], gem
assert gem[0]["price"] == {"in": 0.3, "out": 2.5, "cache_read": 0.03, "cache_write": None} and gem[0]["context"] == 1048576
assert gem[2]["price"]["in"] == 0.1 and gem[1]["price"]["in"] == 0.75, "longest prefix wins (flash-lite vs flash)"
assert models.provider("gemini:gemini-2.5-flash") == "gemini" and models.provider("google/gemini-2.5-flash") == "openrouter"
assert models.fetch_gemini(None) == []
og = openai_compat.to_openai(dict(body, model="gemini:gemini-2.5-flash"), "gemini")
assert og["model"] == "gemini-2.5-flash" and "usage" not in og and og["messages"][0] == {"role": "system", "content": "SYS"}
assert openai_compat.gemini_effort("gemini:gemini-2.5-flash", False) == {"reasoning_effort": "none"}
assert openai_compat.gemini_effort("gemini:gemini-3.8-flash", False) == {"reasoning_effort": "minimal"}
assert openai_compat.gemini_effort("gemini:gemini-3.8-flash", True) == {}
assert openai_compat.gemini_fatal(400, "API key not valid. Please pass a valid API key.") == "API key rejected"
assert openai_compat.gemini_fatal(429, "RESOURCE_EXHAUSTED quota") is None


class G(http.server.BaseHTTPRequestHandler):
    bodies = []
    busy = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        G.bodies.append((self.headers.get("authorization"), b))
        if G.busy:
            G.busy -= 1
            data = json.dumps([{"error": {"code": 503, "message": "This model is currently experiencing high demand.",
                                          "status": "UNAVAILABLE"}}]).encode()
            self.send_response(503)
        elif b.get("reasoning_effort") == "minimal":
            data = json.dumps([{"error": {"code": 400, "message": "Invalid reasoning_effort"}}]).encode()
            self.send_response(400)
        else:
            data = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "Turn in Harmony in Balance."}}],
                               "usage": {"prompt_tokens": 6000, "completion_tokens": 9}}).encode()
            self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv3 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), G)
port3 = srv3.server_address[1]
threading.Thread(target=srv3.serve_forever, args=(0.02,), daemon=True).start()  # shutdown() returns at once
os.environ["GEMINI_API_KEY"] = "gm-test"
gmsgs = []
gc = Coach({"gemini_url": "http://127.0.0.1:%d/v1beta/openai/chat/completions" % port3, "fast_model": "gemini:gemini-3.8-flash",
            "deep_model": "gemini:gemini-3.8-flash"}, GlideDB("/x.db"), gmsgs.append, offline_ok=True)
gc.llm.catalog = models.Catalog(os.path.join(tmp, "g.json"), None, None, gemini_key="gm-test",
                            fetchers={"anthropic": lambda k: [], "openrouter": lambda k: [],
                                      "gemini": lambda k: models.fetch_gemini(k, getter=gget)})
gc.llm.catalog.refresh()
assert gc.llm.keys["gemini"] == "gm-test" and gc.enabled
gc.state = gs
res = gc.answer_once("what now?", state=gs, voice=True)
assert res["answer"] and res["answer"]["text"] == "Turn in Harmony in Balance.", (res, gmsgs)
(a1, b1), (a2, b2) = G.bodies[0], G.bodies[1]
assert a1 == "Bearer gm-test" and b1["model"] == "gemini-3.8-flash" and b1["reasoning_effort"] == "minimal", b1
assert "cache_control" not in json.dumps(b1), "Gemini caches implicitly: no markers sent"
assert b2["reasoning_effort"] == "low" and "usage" not in b2, "a rejected effort level falls back to low"
assert not any("refused" in m.get("text", "") for m in gmsgs), "the Gemini effort fallback is silent"
assert abs(gc.stats["cost_usd"] - (6000 * 0.75 + 9 * 3.75) / 1e6) < 1e-9, gc.stats["cost_usd"]
# the accepted level is remembered: the next call is one request
G.bodies.clear()
gc.answer_once("and then?", state=gs, voice=True)
assert len(G.bodies) == 1 and G.bodies[0][1]["reasoning_effort"] == "low", G.bodies
# a busy model (503) gets one quick retry; a second 503 is reported as Gemini, not Claude
G.bodies.clear()
G.busy = 1
gc.cfg["busy_retry_s"] = 0.05
res = gc.answer_once("again?", state=gs, voice=True)
assert len(G.bodies) == 2 and res["answer"]["text"] == "Turn in Harmony in Balance.", (G.bodies, res)
G.bodies.clear()
G.busy = 2
gmsgs.clear()
gc.answer_once("again?", state=gs, voice=True)
errs = [m["text"] for m in gmsgs if m.get("kind") == "error"]
assert len(G.bodies) == 2 and errs and errs[0].startswith("Gemini API 503 (gemini:gemini-3.8-flash): This model is "
                                                           "currently experiencing high demand."), errs
assert not any("Claude" in m.get("text", "") for m in gmsgs), gmsgs
gc.llm.backoff_until, gc.llm.fails = 0, 0
# switching models is announced (spoken), and a suspension of the old provider no longer blocks the new one
gc.llm.suspended, gc.llm.suspended_for = "out of API credits", "anthropic"
gc.cfg["fast_model"] = "claude-haiku-4-5-20251001"
assert "no ANTHROPIC_API_KEY set" in gc.describe_models()
gmsgs.clear()
gc.cfg["fast_model"] = "gemini:gemini-2.5-flash"
gc.announce_models({"fast_model": ("claude-haiku-4-5-20251001", "gemini:gemini-2.5-flash")})
ann = gmsgs[-1]
assert ann["speak"] and ann["text"].startswith("Model switched. Callouts and spoken questions now use Gemini 2.5 Flash "
                                               "via Gemini API"), ann
assert gc.suspended is None, "the old provider's suspension is cleared on a switch away from it"
gc.announce_models({"fast_model": ("gemini:gemini-2.5-flash", "gemini:gemini-2.5-flash")})
assert gmsgs[-1] is ann, "no announcement when nothing changed"
gc.announce_models({"fast_model": ("x", "claude-opus-5-5"), "deep_model": ("y", "claude-opus-5-5")})
assert gmsgs[-1]["text"].startswith("Model switched. All coaching now uses Claude Opus 5.5 via Claude API") or \
    gmsgs[-1]["text"].startswith("Model switched. All coaching now uses opus"), gmsgs[-1]
assert "No ANTHROPIC_API_KEY is set" in gmsgs[-1]["text"], gmsgs[-1]
gc.cfg["fast_model"] = "gemini:gemini-3.8-flash"
# a suspension from another provider doesn't block this one's callouts
gc.llm.suspended, gc.llm.suspended_for = "out of API credits", "anthropic"
G.bodies.clear()
gc._respond([], question="still there?")
assert len(G.bodies) == 1 and gc.suspended == "out of API credits", "anthropic stays suspended; gemini is called"
gc.llm.suspended, gc.llm.suspended_for = None, None
gc.announce_models({}, startup=True)
assert gmsgs[-1]["text"].startswith("Coach models (saved selection) - callouts and spoken questions: ") and \
    not gmsgs[-1]["speak"], gmsgs[-1]
# Gemini 3 thought signatures: kept from the function call and sent back with it on the next round
resp = openai_compat.from_openai({"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{\"q\": \"x\"}"},
     "extra_content": {"google": {"thought_signature": "SIG1"}}},
    {"id": "c2", "type": "function", "function": {"name": "kill_xp", "arguments": "{}"}}]}}]})
assert resp["content"][0]["extra_content"] == {"google": {"thought_signature": "SIG1"}} and resp["stop_reason"] == "tool_use"
conv = {"model": "gemini:gemini-3.8-flash", "max_tokens": 50, "messages": [
    {"role": "user", "content": "q"}, {"role": "assistant", "content": resp["content"]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{}"},
                                 {"type": "tool_result", "tool_use_id": "c2", "content": "{}"}]}]}
calls = openai_compat.to_openai(conv, "gemini")["messages"][1]["tool_calls"]
assert calls[0]["extra_content"] == {"google": {"thought_signature": "SIG1"}} and "extra_content" not in calls[1], calls
# a call whose signature wasn't kept gets Google's placeholder (first call of the step only)
del conv["messages"][1]["content"][0]["extra_content"]
calls = openai_compat.to_openai(conv, "gemini")["messages"][1]["tool_calls"]
assert calls[0]["extra_content"]["google"]["thought_signature"] == openai_compat.DUMMY_SIGNATURE and "extra_content" not in calls[1]
# OpenRouter: reasoning_details ride along with the tool calls; nothing extra for a plain answer
r2 = openai_compat.from_openai({"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
    "reasoning_details": [{"type": "reasoning.encrypted", "data": "abc"}], "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]}}]})
o2 = openai_compat.to_openai({"model": "google/gemini-3-flash", "max_tokens": 9, "messages": [
    {"role": "user", "content": "q"}, {"role": "assistant", "content": r2["content"]}]})
assert o2["messages"][1]["reasoning_details"] == [{"type": "reasoning.encrypted", "data": "abc"}], o2["messages"][1]
assert "extra_content" not in o2["messages"][1]["tool_calls"][0], "no Gemini placeholder through OpenRouter"
assert openai_compat._text(r2["content"]) == ""

# the whole tool loop against a mock that insists on signatures (as Gemini 3 does)
class S(http.server.BaseHTTPRequestHandler):
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        S.bodies.append(b)
        prior = [m for m in b["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
        if any(not (m["tool_calls"][0].get("extra_content") or {}).get("google", {}).get("thought_signature")
               for m in prior):
            data, code = json.dumps([{"error": {"code": 400, "message": "Function call is missing a thought_signature "
                                                "in functionCall parts."}}]).encode(), 400
        elif b.get("tools") and b.get("tool_choice") == "auto" and not prior:
            data, code = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "kill_xp", "arguments": "{\"mob_level\": 7}"},
                 "extra_content": {"google": {"thought_signature": "REAL"}}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5}}).encode(), 200
        else:
            data, code = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "80 XP."}}],
                                     "usage": {"prompt_tokens": 100, "completion_tokens": 5}}).encode(), 200
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if os.path.exists(KB):
    srv4 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), S)
    port4 = srv4.server_address[1]
    threading.Thread(target=srv4.serve_forever, args=(0.02,), daemon=True).start()  # shutdown() returns at once
    smsgs = []
    sc = Coach({"gemini_url": "http://127.0.0.1:%d/x" % port4, "fast_model": "gemini:gemini-3.8-flash",
                "deep_model": "gemini:gemini-3.8-flash"}, GlideDB("/x.db"), smsgs.append, kb=KB, offline_ok=True)
    res = sc.answer_once("how much XP does a level 7 mob give me?", state=gs)
    assert res["answer"] and res["answer"]["text"].startswith("80 XP"), (res, smsgs)
    assert len(S.bodies) == 2 and S.bodies[1]["messages"][2]["tool_calls"][0]["extra_content"] == \
        {"google": {"thought_signature": "REAL"}}, S.bodies[-1]["messages"][:3]
    assert not [m for m in smsgs if m.get("kind") == "error"], smsgs
    srv4.shutdown()
srv3.shutdown()
os.environ.pop("GEMINI_API_KEY", None)

# ---- settings + endpoint ---------------------------------------------------------------------------------
live = {"fast_model": "claude-haiku-4-5-20251001"}
cfg_path = os.path.join(tmp, "config.json")
json.dump({"companion": {"llm": {"deep_model": "claude-opus-5-5"}}}, open(cfg_path, "w"))
applied = tuning.apply(live, {"fast_model": "google/gemini-2.5-flash"}, cfg_path)
assert applied == {"fast_model": "google/gemini-2.5-flash"} and live["fast_model"] == "google/gemini-2.5-flash"
assert json.load(open(cfg_path))["companion"]["llm"] == {"deep_model": "claude-opus-5-5", "fast_model": "google/gemini-2.5-flash"}
try:
    tuning.apply(live, {"deep_model": "bad model; rm -rf"}, cfg_path)
    raise AssertionError("bad id accepted")
except ValueError:
    pass
assert tuning.input_price("google/gemini-2.5-flash", cat) == 0.3
prof = tuning.profile(kc)
assert set(prof) == {"fast", "deep"} and prof["fast"]["measured"] is False and prof["fast"]["cache_read"] > 0

# Official OpenAI catalog projection filters cached & fresh specialty/shut-down IDs, attaches first-party prices/context,
# marks unclassified entries unverified, and leaves third-party compatible origins untouched.
oai_cache = os.path.join(tmp, "oai_cached.json")
json.dump({
    "updated": 1, "errors": {},
    "models": [
        {"id": "ep:openai:tts-1", "provider": "endpoint", "endpoint": "openai", "name": "tts-1 (OpenAI)", "context": None, "tools": None, "price": None},
        {"id": "ep:openai:gpt-4o-mini-tts", "provider": "endpoint", "endpoint": "openai", "name": "gpt-4o-mini-tts (OpenAI)", "context": None, "tools": None, "price": None},
        {"id": "ep:openai:gpt-5-search-api", "provider": "endpoint", "endpoint": "openai", "name": "gpt-5-search-api (OpenAI)", "context": None, "tools": None, "price": None},
        {"id": "ep:openai:gpt-5", "provider": "endpoint", "endpoint": "openai", "name": "gpt-5 (OpenAI)", "context": None, "tools": None, "price": None, "shutdown_date": "2020-01-01"},
        {"id": "ep:openai:gpt-5.6-luna", "provider": "endpoint", "endpoint": "openai", "name": "gpt-5.6-luna (OpenAI)", "context": None, "tools": None, "price": None},
        {"id": "ep:openai:future-model-x", "provider": "endpoint", "endpoint": "openai", "name": "future-model-x (OpenAI)", "context": None, "tools": None, "price": None},
        {"id": "ep:local:tts-1", "provider": "endpoint", "endpoint": "local", "name": "tts-1 (Local)", "context": None, "tools": None, "price": None},
    ],
}, open(oai_cache, "w", encoding="utf-8"))
oai_cat = models.Catalog(oai_cache, endpoints={
    "openai": {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "local": {"id": "local", "label": "Local", "base_url": "http://127.0.0.1:11434/v1"},
})
projected = {m["id"]: m for m in oai_cat.entries()}
assert set(projected) == {"ep:openai:gpt-5.6-luna", "ep:openai:future-model-x", "ep:local:tts-1"}, projected
assert oai_cat.status()["count"] == 3, oai_cat.status()
assert projected["ep:openai:gpt-5.6-luna"]["context"] == 1_050_000 and projected["ep:openai:gpt-5.6-luna"]["price"]["in"] == 0.2
assert not projected["ep:openai:gpt-5.6-luna"].get("unverified")
assert projected["ep:openai:future-model-x"].get("unverified") is True
assert not projected["ep:local:tts-1"].get("unverified") and projected["ep:local:tts-1"]["price"] is None
assert oai_cat.price("ep:openai:gpt-6.1-terra") == {"in": 1.0, "out": 5.0, "cache_read": 0.05, "cache_write": 1.25}
# Mutating endpoint origin immediately re-projects entries(), status()["count"], and strips stale unverified flags.
oai_cat.endpoints["openai"] = {"id": "openai", "label": "OpenAI", "base_url": "http://127.0.0.1:11434/v1"}
reprojected = {m["id"]: m for m in oai_cat.entries()}
assert "ep:openai:tts-1" in reprojected and oai_cat.status()["count"] == 7, reprojected
assert not reprojected["ep:openai:future-model-x"].get("unverified")

from dashboard import Hub, get_json, serve  # noqa: E402
import dashboard  # noqa: E402
dashboard.SHUTDOWN_POLL_S = 0.02     # shutdown() returns at once instead of after up to 0.5 s
calls = []
srv2 = serve(Hub(), 0, {("GET", "/models"): get_json(lambda q: calls.append(q.get("refresh") == "1")
                                                       or {"models": cat.entries()})})
port2 = srv2.server_address[1]
d = json.load(urllib.request.urlopen("http://127.0.0.1:%d/models" % port2))
assert len(d["models"]) == 4 and calls == [False]
json.load(urllib.request.urlopen("http://127.0.0.1:%d/models?refresh=1" % port2))
assert calls == [False, True]
page = urllib.request.urlopen("http://127.0.0.1:%d/" % port2).read().decode()
srv2.shutdown()
srv.shutdown()
# the desktop app's Settings page has the model picker, and the picker reads this /models route
picker = open(os.path.join(ROOT, "desktop", "src", "renderer", "settings", "model-picker.ts"), encoding="utf-8").read()
assert "/models" in picker and "?refresh=1" in picker, "the model picker no longer reads /models"
import desktop_schema  # noqa: E402  (the Settings page schema as the page gets it)
specials = desktop_schema.specials()
if specials is None:
    print("SKIP: the Settings page check (needs node and desktop/node_modules); the rest passed")
    raise SystemExit(77)
assert "model-picker" in specials, "the model picker is in the desktop app's Settings"
print("OK")
