#!/usr/bin/env python3
"""Real Settings renderer + DOM, with IPC at an isolated file-backed boundary.

Use default must not write before Save, Revert restores the saved value, and a rejected Save keeps the reset
pending. The successful retry removes the override. No Electron install, player data, audio or model API is used.
Run: python tests/test_settings_defaults.py (needs Node, TypeScript and Edge/Chrome; otherwise skips with 77).
"""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import urllib.parse

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.insert(0, os.path.join(HERE, "golden"))
import glideconfig  # noqa: E402
import app as glide_app  # noqa: E402
import models as models_mod  # noqa: E402
from probe_dashboard import edge  # noqa: E402

exe = edge()
if not exe or not desktop_ts.available():
    print("SKIP: needs Edge/Chrome, Node and desktop/node_modules")
    sys.exit(77)
compiled = desktop_ts.renderer()
with open(os.path.join(ROOT, "desktop", "src", "renderer", "index.html"), encoding="utf-8") as fh:
    html = fh.read().replace('<script type="module" src="app.js"></script>', "")

tmp = tempfile.mkdtemp(prefix="glide-settings-defaults-")
attempts = []
reject = True
cat_path = os.path.join(tmp, "models.json")
with open(cat_path, "w", encoding="utf-8") as _cfh:
    json.dump({
        "updated": 1000, "errors": {},
        "models": [
            {"id": "ep:openai:whisper-1", "provider": "endpoint", "endpoint": "openai", "name": "whisper-1 (OpenAI)", "context": None, "tools": None, "price": None},
            {"id": "ep:openai:gpt-5.6-luna", "provider": "endpoint", "endpoint": "openai", "name": "gpt-5.6-luna (OpenAI)", "context": None, "tools": None, "price": None},
            {"id": "ep:openai:future-model-x", "provider": "endpoint", "endpoint": "openai", "name": "future-model-x (OpenAI)", "context": None, "tools": None, "price": None},
        ],
    }, _cfh)
_ep = {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"}
_catalog = models_mod.Catalog(cat_path, endpoints={"openai": _ep})
_stub_app = types.SimpleNamespace(
    catalog=_catalog,
    coach=types.SimpleNamespace(
        cfg={"endpoints": [_ep], "fast_model": "ep:openai:tts-1", "deep_model": "ep:openai:gpt-6.1-terra"},
        stats={"cost_usd": 0.0},
        path_stats={},
    ),
)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_data(self, value, status=200, content_type="application/json"):
        body = (json.dumps(value) if content_type == "application/json" else value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.send_data(html, content_type="text/html; charset=utf-8")
        if self.path == "/probe":
            return self.send_data({"attempts": len(attempts), "config": glideconfig.read(tmp)[0]})
        if self.path.startswith("/models"):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            return self.send_data(glide_app.App.on_models(
                _stub_app, refresh=q.get("refresh") == "1", reasoning_model=q.get("reasoning_model")))
        return super().do_GET()

    def do_POST(self):
        global reject
        if self.path != "/ipc":
            return self.send_data({"error": "unknown route"}, 404)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        channel, args = request["channel"], request["args"]
        if channel == "config:get":
            return self.send_data(glideconfig.read(tmp)[0])
        if channel == "app:settings:get":
            return self.send_data({"muted": True, "speech": {"volume": 0}})
        if channel == "config:set":
            attempts.append(args[0])
            if reject:
                reject = False
                return self.send_data({"error": "Test save rejected"}, 503)
            return self.send_data(glideconfig.patch((), args[0], tmp))
        return self.send_data({"error": "Unexpected IPC: " + channel}, 400)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=compiled))
thread = threading.Thread(target=server.serve_forever, daemon=True)
try:
    glideconfig.patch((), {"companion": {"llm": {"voice_question_model": "deep"}}}, tmp)
    thread.start()
    result = subprocess.run([desktop_ts.node, os.path.join(HERE, "settings_defaults_cdp.mjs"), exe,
                             os.path.join(tmp, "profile"), "http://127.0.0.1:%d/" % server.server_port],
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(attempts) == 2, attempts  # failed Save and its retry, never reset or Revert
    assert "voice_question_model" not in glideconfig.read(tmp)[0]["companion"]["llm"]
    print(result.stdout.strip())
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    shutil.rmtree(tmp, ignore_errors=True)
