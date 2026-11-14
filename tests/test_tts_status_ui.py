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
import time

import types
import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.insert(0, os.path.join(HERE, "golden"))
import glideconfig  # noqa: E402
from probe_dashboard import edge  # noqa: E402
import app as companion_app  # noqa: E402
import health as health_mod  # noqa: E402
import dashboard as dashboard_mod  # noqa: E402
from coach import Coach, CoachDeps  # noqa: E402
from glidedb import GlideDB  # noqa: E402

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
with open(os.path.join(HERE, "golden", "fixtures", "dashboard_init.json"), encoding="utf-8") as fh:
    date_coach_init = json.load(fh)
date_coach_hub = dashboard_mod.Hub()
date_coach_hub.snapshot, date_coach_hub.messages = date_coach_init["state"], date_coach_init["messages"]
date_coach_server = dashboard_mod.serve(date_coach_hub, 0, token="date-coach-test")

saved_keys = {"OPENAI_API_KEY": "sk-proj-1234", "GROQ_API_KEY": "gsk-1234"}

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
        if self.path == "/date-coach-url":
            return self.send_data("http://127.0.0.1:%d" % date_coach_server.server_address[1])
        if self.path == "/overlay.html":
            return self.send_data(overlay_html, content_type="text/html; charset=utf-8")
        if self.path == "/app.css":
            return self.send_data(app_css, content_type="text/css; charset=utf-8")
        if self.path == "/glide.css":
            return self.send_data(glide_css, content_type="text/css; charset=utf-8")
        if self.path == "/date-time-display.js":
            with open(os.path.join(SHARED_STATIC, "date-time-display.js"), encoding="utf-8") as fh:
                return self.send_data(fh.read(), content_type="text/javascript; charset=utf-8")
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
        if channel == "config:set":
            glideconfig.patch((), args[0], tmp)
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
            if args[0] == "windows":
                return self.send_data([
                    {"id": "Xenia (Russian)", "name": "Xenia (Russian)", "identity": "token:xenia", "isDefault": True},
                    {"id": "Microsoft Zira", "name": "Microsoft Zira", "identity": "token:zira", "isDefault": False},
                ])
            if args[0] == "openai":
                return self.send_data([{"id": "coral", "name": "coral"}, {"id": "alloy", "name": "alloy"}])
            return self.send_data([{"id": "ru-RU-DmitryNeural", "name": "ru-RU-DmitryNeural · Male"}])
        if channel == "speech:models":
            return self.send_data([
                {"name": "tiny.en", "mb": 74, "note": "Fastest and smallest", "downloaded": False, "shipped": False},
                {"name": "base.en", "mb": 141, "note": "Fast; fine for short commands", "downloaded": True, "shipped": False},
                {"name": "small", "mb": 464, "note": "Multilingual", "downloaded": True, "shipped": False},
            ])
        if channel in ("keys:list", "keys:set"):
            if channel == "keys:set":
                env_name, val = args[0], args[1]
                if val == "AIza-delayed-save":
                    time.sleep(0.12)
                if val:
                    saved_keys[env_name] = val
                else:
                    saved_keys.pop(env_name, None)
            envs = [
                ("ANTHROPIC_API_KEY", "Anthropic (Claude)", "model", None),
                ("GEMINI_API_KEY", "Google Gemini", "model", None),
                ("OPENROUTER_API_KEY", "OpenRouter", "model", None),
                ("OPENAI_API_KEY", "OpenAI", "model", ["Models", "Voice", "STT", "Memory"]),
                ("TAVILY_API_KEY", "Tavily", "search", None),
                ("BRAVE_API_KEY", "Brave Search", "search", None),
                ("ELEVENLABS_API_KEY", "ElevenLabs", "voice", None),
                ("AZURE_SPEECH_KEY", "Azure Speech", "voice", None),
                ("GOOGLE_TTS_API_KEY", "Google Cloud TTS", "voice", None),
                ("GROQ_API_KEY", "Groq", "speech", ["STT", "Endpoint"]),
                ("DEEPGRAM_API_KEY", "Deepgram", "speech", None),
            ]
            status = [{
                "env": e,
                "saved": e in saved_keys,
                "fromEnv": False,
                "hint": ("…" + saved_keys[e][-4:]) if e in saved_keys else "",
            } for e, _, _, _ in envs]
            status.append({
                "env": "GLIDE_ENDPOINT_PROXY_KEY",
                "saved": "GLIDE_ENDPOINT_PROXY_KEY" in saved_keys,
                "fromEnv": False,
                "hint": ("…" + saved_keys["GLIDE_ENDPOINT_PROXY_KEY"][-4:]) if "GLIDE_ENDPOINT_PROXY_KEY" in saved_keys else "",
            })
            if channel == "keys:set":
                return self.send_data(status)
            return self.send_data({
                "known": [{"env": e, "label": l, "help": l, "kind": k, **({"scopes": sc} if sc else {})} for e, l, k, sc in envs],
                "status": status,
            })
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
_stub_app.catalog = None
import providers  # noqa: E402

_gemini_provider = providers.OpenAICompatProvider("gemini", {"tune_thinking": True}, "")
_stub_app.coach.llm.provider_for = lambda _m: _gemini_provider
for _model, _fast_effort, _deep_effort, _thinking in (
    ("claude-sonnet-5-5", "low", "medium", "between_tools"),
    ("claude-haiku-4-5", None, None, None),
    ("gemini:gemini-3-flash", "minimal", None, None),
    ("gemini:gemini-2.5-pro", "low", None, None),
    ("ep:openai:gpt-6-luna", None, None, None),
    ("anthropic/claude-sonnet-5.5", None, None, None),
):
    _auto = companion_app.App.on_models(_stub_app, reasoning_model=_model)["automatic"]
    assert _auto["fast"]["effort"] == _fast_effort and _auto["deep"]["effort"] == _deep_effort, _auto
    assert _auto["fast"].get("thinking") == _thinking, _auto
_gemini_provider.accepted("gemini:gemini-3-flash", {"reasoning_effort": "low"})
_auto = companion_app.App.on_models(_stub_app, reasoning_model="gemini:gemini-3-flash")["automatic"]
assert _auto["fast"]["effort"] == "low" and _auto["deep"]["effort"] is None, _auto
_gemini_provider.accepted("gemini:gemini-3-flash", {})
_auto = companion_app.App.on_models(_stub_app, reasoning_model="gemini:gemini-3-flash")["automatic"]
assert _auto["fast"]["effort"] is None, _auto
_auto = companion_app.App.on_models(_stub_app, reasoning_model="gemini:gemini-2.5-pro")["automatic"]
assert _auto["fast"]["effort"] == "low", _auto
_cfg.update(fast_model="gemini:gemini-3-flash", deep_model="claude-sonnet-5-5",
            fast_reasoning_effort="auto", deep_reasoning_effort="auto", tune_thinking=True)
_gemini_provider.accepted("gemini:gemini-3-flash", {"reasoning_effort": "low"})
_ms = companion_app.App._model_status(_stub_app)
assert _ms["tune_thinking"] is True and _ms["automatic_reasoning"]["fast"]["effort"] == "low", _ms
assert _ms["automatic_reasoning"]["deep"]["effort"] == "medium", _ms
_gemini_provider.accepted("gemini:gemini-3-flash", {})
assert companion_app.App._model_status(_stub_app)["automatic_reasoning"]["fast"]["effort"] is None
_cfg["deep_reasoning_effort"] = "high"
_ms = companion_app.App._model_status(_stub_app)
assert _ms["deep_reasoning"] == "high" and "deep" not in _ms["automatic_reasoning"], _ms
_cfg["tune_thinking"] = False
assert companion_app.App._model_status(_stub_app)["automatic_reasoning"] == {}
# Game-link check boundary: shared between Status (/health) and overlay state.coach.link_issue.
def _link_app(*, sim=False, frames=0, last_frame_t=0.0, strip_other=None, decoded=10, attempts=10):
    return types.SimpleNamespace(
        args=types.SimpleNamespace(sim=sim, app=True),
        state=types.SimpleNamespace(frames=frames),
        last_frame_t=last_frame_t,
        strip_other=strip_other,
        decoded=decoded,
        attempts=attempts,
    )

assert health_mod.link_check(_link_app(sim=True), now=500)["short"] is None
assert health_mod.link_check(_link_app(frames=0), now=500)["short"] == "No game data"
assert health_mod.link_check(_link_app(frames=12, last_frame_t=255.0), now=500.0)["short"] == "Game data stopped 4m ago"
assert health_mod.link_check(_link_app(frames=12, last_frame_t=495.0), now=500.0)["short"] is None
assert health_mod.link_check(_link_app(frames=12, last_frame_t=495.0, decoded=4, attempts=10), now=500.0)["short"] == "Patchy data (40%)"
assert health_mod.link_check(_link_app(frames=12, last_frame_t=100.0, strip_other={"version": 99, "t": 200.0}), now=500.0)["short"] == "Addon mismatch"

# Engine thinking lifecycle: question followed by queued ANNOUNCE must clear thinking back to None once drained.
_thinking_events = []
_coach_msgs = []
_coach = Coach(
    {"enabled": False},
    GlideDB("/nonexistent.db"),
    _coach_msgs.append,
    offline_ok=True,
    deps=CoachDeps(on_thinking=_thinking_events.append),
)
_coach.ask("Where next?", voice=True)
_coach.announce_update("available", "0.5.0", "Update", ["Faster voices"])
assert _coach.thinking == "question" and _thinking_events == ["question"], _thinking_events
_coach._drain_asks()
assert _coach.thinking is None and _thinking_events == ["question", None], (_coach.thinking, _thinking_events)


class _Server(http.server.ThreadingHTTPServer):
    request_queue_size = 64


server = _Server(("127.0.0.1", 0), functools.partial(Handler, directory=compiled))
thread = threading.Thread(target=server.serve_forever, daemon=True)
try:
    glideconfig.patch((), {
        "companion": {
            "voice": {"language": "ru"},
            "llm": {
                "endpoints": [
                    {"id": "proxy", "label": "Proxy", "base_url": "https://proxy.example.com/v1", "key_env": "OPENAI_API_KEY"},
                ],
            },
        },
    }, tmp)
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
    date_coach_server.shutdown()
    date_coach_server.server_close()
    thread.join(timeout=2)
    shutil.rmtree(tmp, ignore_errors=True)
