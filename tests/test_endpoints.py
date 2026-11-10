#!/usr/bin/env python3
"""Custom OpenAI-compatible endpoints ("ep:<id>:<model>"): config parsing, routing, the catalog listing, and the coach
answering through a mock local server (no key) and a keyed one."""
import http.server
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.append(ROOT)
import models  # noqa: E402
import openai_compat  # noqa: E402

# ---- parsing and routing --------------------------------------------------------------------------------
eps = openai_compat.endpoints({"endpoints": [
    {"id": "Ollama", "label": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1/"},
    {"id": "groq", "base_url": "https://api.groq.com/openai/v1", "key_env": "MY_GROQ"},
    {"id": "bad id!", "base_url": "http://x"}, {"id": "nourl", "base_url": "ftp://x"}, "junk"]})
assert sorted(eps) == ["groq", "ollama"], eps
assert eps["ollama"]["base_url"] == "http://127.0.0.1:11434/v1" and eps["ollama"]["key_env"] == "GLIDE_ENDPOINT_OLLAMA_KEY"
assert eps["groq"]["key_env"] == "MY_GROQ" and eps["groq"]["label"] == "groq"
assert models.provider("ep:ollama:llama3.1:8b") == "endpoint" and models.provider("ep:groq:meta/llama") == "endpoint"
assert not openai_compat.is_openrouter("ep:groq:meta/llama") and openai_compat.is_openrouter("meta/llama")
assert openai_compat.split_endpoint("ep:ollama:llama3.1:8b") == ("ollama", "llama3.1:8b")
o = openai_compat.to_openai({"model": "ep:ollama:llama3.1:8b", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]},
                         "endpoint")
assert o["model"] == "llama3.1:8b" and "usage" not in o, o



# ---- mock server: /v1/models and /v1/chat/completions -------------------------------------------------------
class H(http.server.BaseHTTPRequestHandler):
    bodies = []

    def log_message(self, *a):
        pass

    def _json(self, out):
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        assert self.path == "/v1/models", self.path
        self._json({"object": "list", "data": [{"id": "llama3.1:8b"}, {"id": "qwen3:14b"}]})

    def do_POST(self):
        assert self.path == "/v1/chat/completions", self.path
        H.bodies.append((self.headers.get("authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self._json({"choices": [{"finish_reason": "stop", "message": {"content": "Kill boars east of the farm."}}],
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 9}})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()  # shutdown() returns at once

# the catalog lists the endpoint's models
tmp = tempfile.mkdtemp()
cat = models.Catalog(os.path.join(tmp, "models.json"), endpoints={"ollama": dict(eps["ollama"], key="", base_url="http://127.0.0.1:%d/v1" % port)},
                     fetchers={"anthropic": lambda k: [], "gemini": lambda k: [], "openrouter": lambda k: []})
st = cat.refresh()
ids = [m["id"] for m in cat.entries()]
assert "ep:ollama:llama3.1:8b" in ids and "ep:ollama:qwen3:14b" in ids, (ids, st)
assert st["endpoints"] == ["ollama"] and cat.get("ep:ollama:qwen3:14b")["name"] == "qwen3:14b (Ollama (local))"

# ---- the coach answers through it --------------------------------------------------------------------------
from coach import Coach  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from state import GameState  # noqa: E402

for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "GLIDE_ENDPOINT_OLLAMA_KEY"):
    os.environ.pop(k, None)
os.environ["MY_GROQ"] = "gq-test"
cfg = {"fast_model": "ep:ollama:llama3.1:8b", "deep_model": "ep:ollama:llama3.1:8b",
       "endpoints": [{"id": "ollama", "label": "Ollama (local)", "base_url": "http://127.0.0.1:%d/v1" % port},
                     {"id": "groq", "label": "Groq", "base_url": "http://127.0.0.1:%d/v1" % port, "key_env": "MY_GROQ"}]}
msgs = []
c = Coach(cfg, GlideDB("/x.db"), msgs.append, offline_ok=True)
c.llm.catalog = cat
assert c.enabled and c.llm.keys["endpoint"] and not c.llm.keys["anthropic"], c.llm.keys
gs = GameState()
gs.update({"t": "1000", "l": "7", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1426", "px": "4650", "py": "5300",
           "st": "5"}, {"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "T"})
res = c.answer_once("where do I go?", state=gs)
assert res["answer"] and res["answer"]["text"].startswith("Kill boars"), (res, msgs)
auth, body = H.bodies[-1]
assert auth is None and body["model"] == "llama3.1:8b", (auth, body["model"])   # local server: no key sent
# a keyed endpoint sends its own key
c.cfg["deep_model"] = "ep:groq:llama-3.3-70b"
res = c.answer_once("and then?", state=gs)
auth, body = H.bodies[-1]
assert auth == "Bearer gq-test" and body["model"] == "llama-3.3-70b", (auth, body["model"])
# an unknown endpoint id is reported, not sent anywhere
n = len(H.bodies)
c.cfg["deep_model"] = "ep:nope:x"
c.answer_once("again?", state=gs)
assert len(H.bodies) == n and any("No custom endpoint 'nope'" in (m.get("text") or "") for m in msgs), msgs
assert "custom endpoint" in c.describe_models()
srv.shutdown()
print("endpoints: ok")
