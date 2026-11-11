#!/usr/bin/env python3
"""Real session-bar, Settings navigation, and game-overlay DOM in headless Edge via CDP.

Verifies the 48 px single-tier session bar, stacked Fast/Deep buttons, independent STT and
reserved-width TTS chips, direct Settings section/field scrolling, sessionBarOrder migration,
0 px horizontal neighbor shift across TtsStatus transitions, and 0 px vertical height bounce
on the click-through game overlay (#step + #tts) in both expanded and collapsed modes.
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
import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.insert(0, os.path.join(HERE, "golden"))
import glideconfig  # noqa: E402
from probe_dashboard import edge  # noqa: E402
import app as companion_app  # noqa: E402

exe = edge()
if not exe or not desktop_ts.available():
    print("SKIP: needs Edge/Chrome, Node and desktop/node_modules")
    sys.exit(77)

compiled = desktop_ts.renderer()
DESK_RENDERER = os.path.join(ROOT, "desktop", "src", "renderer")
SHARED_STATIC = os.path.join(ROOT, "companion", "static")

with open(os.path.join(SHARED_STATIC, "icons.svg"), encoding="utf-8") as fh:
    sprite = fh.read()
with open(os.path.join(DESK_RENDERER, "index.html"), encoding="utf-8") as fh:
    index_html = (
        fh.read()
        .replace("<!--ICONS-->", sprite)
        .replace('<script type="module" src="app.js"></script>', "")
    )
with open(os.path.join(DESK_RENDERER, "overlay.html"), encoding="utf-8") as fh:
    overlay_html = (
        fh.read()
        .replace("<!--ICONS-->", sprite)
        .replace('<script type="module" src="overlay.js"></script>', "")
    )
with open(os.path.join(DESK_RENDERER, "app.css"), encoding="utf-8") as fh:
    app_css = fh.read()
with open(os.path.join(SHARED_STATIC, "glide.css"), encoding="utf-8") as fh:
    glide_css = fh.read()

tmp = tempfile.mkdtemp(prefix="glide-tts-status-ui-")
app_settings = {
    "muted": False,
    "speech": {"provider": "edge", "edge": {"voice": "ru-RU-DmitryNeural"}, "volume": 0.8},
    "overlay": {"enabled": True, "collapsed": False, "opacity": 0.85, "fontSize": 13},
}
resize_heights = []


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
            return self.send_data(index_html, content_type="text/html; charset=utf-8")
        if self.path == "/overlay.html":
            return self.send_data(overlay_html, content_type="text/html; charset=utf-8")
        if self.path == "/app.css":
            return self.send_data(app_css, content_type="text/css; charset=utf-8")
        if self.path == "/glide.css":
            return self.send_data(glide_css, content_type="text/css; charset=utf-8")
        if self.path.startswith("/fonts/"):
            font_path = os.path.join(SHARED_STATIC, self.path.lstrip("/"))
            if os.path.isfile(font_path):
                with open(font_path, "rb") as fh:
                    raw = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
        if self.path == "/resizes":
            return self.send_data(resize_heights)
        return super().do_GET()

    def do_POST(self):
        if self.path != "/ipc":
            return self.send_data({"error": "unknown route"}, 404)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        channel, args = request["channel"], request["args"]
        if channel == "config:get":
            return self.send_data(glideconfig.read(tmp)[0])
        if channel == "app:settings:get":
            return self.send_data(app_settings)
        if channel == "app:settings:set":
            patch = args[0] or {}
            for k, v in patch.items():
                if isinstance(v, dict) and isinstance(app_settings.get(k), dict):
                    app_settings[k] = {**app_settings[k], **v}
                else:
                    app_settings[k] = v
            return self.send_data(app_settings)
        if channel == "engine:get":
            return self.send_data({"state": "stopped", "detail": "", "port": 0, "url": None, "token": ""})
        if channel == "tts:status:get":
            return self.send_data({
                "provider": "edge",
                "voiceLabel": "Edge · Dmitry",
                "fullVoice": "Edge-TTS (ru-RU-DmitryNeural)",
                "phase": "ready",
                "workerState": "ready",
                "readySlots": 6,
                "totalSlots": 6,
                "updatedAt": 1000,
            })
        if channel == "overlay:resize":
            resize_heights.append(args[0])
            return self.send_data(None)
        if channel == "tts:voices":
            return self.send_data([{"id": "ru-RU-DmitryNeural", "name": "ru-RU-DmitryNeural · Male"}])
        return self.send_data(None)


# Engine boundary: App._model_status exposes live reasoning efforts and spoken-question role routing.
_cfg = {
    "fast_model": "ep:openai:gpt-6-luna",
    "deep_model": "ep:openai:gpt-6.1-terra",
    "fast_reasoning_effort": "low",
    "deep_reasoning_effort": "medium",
    "voice_question_model": "deep",
}
_stub_app = types.SimpleNamespace(
    coach=types.SimpleNamespace(
        cfg=_cfg,
        model_name=lambda m: {"ep:openai:gpt-6-luna": "gpt-6-luna (OpenAI)",
                              "ep:openai:gpt-6.1-terra": "gpt-6.1-terra (OpenAI)"}.get(m, m),
        localization=types.SimpleNamespace(language="ru"),
        llm=types.SimpleNamespace(available=lambda _m: True, provider_for=lambda _m: object()),
    ),
    ptt=None,
)
_ms = companion_app.App._model_status(_stub_app)
assert _ms["fast_reasoning"] == "low" and _ms["deep_reasoning"] == "medium" and _ms["voice_question"] == "deep", _ms
_cfg["fast_reasoning_effort"] = "auto"
assert companion_app.App._model_status(_stub_app)["fast_reasoning"] == "auto"

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=compiled))
thread = threading.Thread(target=server.serve_forever, daemon=True)
try:
    glideconfig.patch((), {"companion": {"voice": {"language": "ru"}}}, tmp)
    thread.start()
    result = subprocess.run(
        [
            desktop_ts.node,
            os.path.join(HERE, "tts_status_ui_cdp.mjs"),
            exe,
            os.path.join(tmp, "profile"),
            "http://127.0.0.1:%d" % server.server_port,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    shutil.rmtree(tmp, ignore_errors=True)
