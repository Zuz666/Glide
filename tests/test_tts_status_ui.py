#!/usr/bin/env python3
"""Real session-bar, Settings navigation, and game-overlay DOM in headless Edge via CDP.

Verifies the 48 px single-tier session bar, stacked Fast/Deep buttons, independent STT and
reserved-width TTS chips, direct Settings section/field scrolling, sessionBarOrder migration,
0 px horizontal neighbor shift across TtsStatus transitions, and 0 px vertical height bounce
on the click-through game overlay (#step + #tts) in both expanded and collapsed modes.
Selected-model effort previews and status consume real App model projections, including
verified Fast-low / Deep-medium requests, unavailable and unknown targets, tuning Off,
and explicit overrides independent of the tuning switch. No model requests are made.
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
import urllib.parse

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
title_proof = {}
if os.environ.get("GLIDE_STEP_TITLE_PROOF"):
    with open(os.environ["GLIDE_STEP_TITLE_PROOF"], encoding="utf-8") as fh:
        title_proof = json.load(fh)
    for key in ("steps", "level", "overlay_step", "guide", "guide_group", "step",
                "step_source_index", "step_fingerprint", "step_match", "step_match_reason"):
        if key in title_proof:
            date_coach_init["state"][key] = title_proof[key]
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
        if self.path == "/step-missing" and "missing" in title_proof:
            date_coach_hub.snapshot = {**date_coach_init["state"], **title_proof["missing"]}
            return self.send_data(True)
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
        if self.path == "/reasoning-settings":
            return self.send_data({"saved": glideconfig.read(tmp)[0]["companion"]["llm"],
                                   "running": _settings_app.coach.cfg})
        if self.path.startswith("/reasoning-state-fixture?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self.send_data({"models": _status_fixtures[query.get("scenario", ["automatic"])[0]]})
        if self.path.startswith("/reasoning-fixture?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fixture = _reasoning_fixtures.get(query.get("scenario", ["default"])[0], {})
            if query.get("scenario", ["default"])[0] == "unavailable":
                return self.send_data({"error": "catalog refresh failed"}, 503)
            model = query.get("reasoning_model", [""])[0]
            if model not in fixture:
                return self.send_data({"error": "unknown reasoning fixture"}, 404)
            return self.send_data(fixture[model])
        return super().do_GET()

    def do_POST(self):
        if self.path == "/reasoning-save-fixture":
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if request.get("seed"):
                legacy = {"fast_model": "opencode:claude-haiku-5-5", "fast_reasoning_effort": "max",
                          "deep_model": "opencode:claude-opus-4-5", "deep_reasoning_effort": "max"}
                _saved_reasoning_runtime.update(_settings_app.coach.cfg)
                glideconfig.patch(("companion", "llm"), legacy, tmp)
                _settings_app.coach.cfg.update(legacy)
            if "low" in request:
                _native_catalog["opencode:claude-haiku-5-5"]["reasoning"]["effort"] = (
                    ["low"] if request["low"] else []) + ["medium", "high", "xhigh", "max"]
            if request.get("restore"):
                _settings_app.coach.cfg.clear()
                _settings_app.coach.cfg.update(_saved_reasoning_runtime)
            return self.send_data({"ok": True})
        if self.path != "/ipc":
            return self.send_data({"error": "unknown route"}, 404)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        channel, args = request["channel"], request["args"]
        if channel == "config:get":
            return self.send_data(glideconfig.read(tmp)[0])
        if channel == "config:set":
            tunable = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            try:
                companion_app.App.on_settings(_settings_app, {"save": args[0], **tunable})
            except ValueError as exc:
                return self.send_data({"error": str(exc)}, 400)
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


# Catalog-shaped capability evidence feeds the same App projections as a real model list.
_native_catalog = {
    "opencode:" + model: {"id": "opencode:" + model, "reasoning": {"supported": True, "effort": levels},
                         "api": {api: {}}}
    for model, api, levels in (
        ("claude-haiku-5-5", "messages", ["low", "medium", "high", "xhigh", "max"]),
        ("claude-opus-4-6", "messages", ["low", "medium", "high", "max"]),
        ("claude-opus-4-5", "messages", ["low", "medium", "high"]),
        ("claude-haiku-4-5", "messages", []),
        ("gpt-5.5-pro", "responses", ["medium", "high", "xhigh"]),
        ("gemini-3.8-flash", "generate-content", ["low", "medium", "high"]),
        ("gemini-3.6-flash", "generate-content", ["minimal", "low", "medium", "high"]),
        ("deepseek-v4.1-flash", "chat-completions", ["low", "high", "max"]),
        ("kimi-k3", "chat-completions", ["low", "high", "max"]),
        ("mistral-large-4", "chat-completions", ["none", "high"]),
        ("qwen3.8-max", "chat-completions", ["low", "medium", "xhigh"]),
    )
}
_capability_catalog = {
    **_native_catalog,
    "claude-sonnet-5-5": {
        "id": "claude-sonnet-5-5", "capabilities": {"effort": {"supported": True,
            "low": {"supported": True}, "medium": {"supported": True}, "high": {"supported": True}}},
    },
    "anthropic/claude-sonnet-5.5": {
        "id": "anthropic/claude-sonnet-5.5",
        "reasoning": {"supported_efforts": ["low", "medium", "high"]},
    },
}
_preview_cfg = {"tune_thinking": True, "endpoints": [
    {"id": "official", "base_url": "https://api.openai.com/v1"},
    {"id": "proxy", "base_url": "https://proxy.example.com/v1"},
]}
_preview_app = types.SimpleNamespace(
    catalog=types.SimpleNamespace(raw_get=_capability_catalog.get),
    coach=types.SimpleNamespace(
        cfg=_preview_cfg, model_name=lambda model: model,
        localization=types.SimpleNamespace(language="ru"),
        llm=types.SimpleNamespace(available=lambda _model: True, provider_for=lambda _model: object()),
    ),
    ptt=None,
)
_preview_models = (
    "claude-sonnet-5-5", "claude-haiku-4-5", "gemini:gemini-3-flash", "gemini:gemini-2.5-pro",
    "gemini:gemini-2.0-flash", "anthropic/claude-sonnet-5.5",
    "ep:official:gpt-5.5-pro", "ep:proxy:gpt-5.5",
    *_native_catalog, "opencode:unverified-model",
)
_reasoning_fixtures = {"default": {
    model: companion_app.App.on_models(_preview_app, reasoning_model=model)
    for model in _preview_models
}}
for _model, _fast, _deep, _fast_reason, _deep_reason in (
    ("claude-sonnet-5-5", "low", "medium", "automatic", "automatic"),
    ("claude-haiku-4-5", None, None, "unsupported", "unsupported"),
    ("gemini:gemini-3-flash", "low", "medium", "automatic", "automatic"),
    ("gemini:gemini-2.5-pro", "low", "medium", "automatic", "automatic"),
    ("gemini:gemini-2.0-flash", None, None, "unsupported", "unsupported"),
    ("opencode:claude-haiku-5-5", "low", "medium", "automatic", "automatic"),
    ("opencode:gemini-3.8-flash", "low", "medium", "automatic", "automatic"),
    ("opencode:gpt-5.5-pro", None, "medium", "unsupported", "automatic"),
    ("opencode:kimi-k3", "low", None, "automatic", "unsupported"),
    ("anthropic/claude-sonnet-5.5", "low", "medium", "automatic", "automatic"),
    ("ep:official:gpt-5.5-pro", None, "medium", "unsupported", "automatic"),
    ("ep:proxy:gpt-5.5", None, None, "unknown", "unknown"),
    ("opencode:unverified-model", None, None, "unknown", "unknown"),
):
    _preview = _reasoning_fixtures["default"][_model]
    for _role, _effort, _reason in (("fast", _fast, _fast_reason), ("deep", _deep, _deep_reason)):
        _resolved = _preview["automatic"][_role]
        assert _resolved["effort"] == _effort and _resolved["reason"] == _reason, _preview
        assert _resolved["source"] == ("automatic" if _effort else "provider_default"), _preview
        assert _resolved["profile"] == _preview["reasoning"], _preview
        assert "thinking" not in _resolved, _preview
_haiku_preview = _reasoning_fixtures["default"]["opencode:claude-haiku-5-5"]
assert _haiku_preview["reasoning"]["levels"] == ["low", "medium", "high", "xhigh", "max"], _haiku_preview
assert not _reasoning_fixtures["default"]["opencode:unverified-model"]["reasoning"]["levels"]

# Status state publishes automatic resolution only for On + Auto roles. Preview ignores
# saved tuning/effort so Settings can apply its unsaved Off/explicit drafts locally.
_status_fixtures = {}
for _scenario, _fast_model, _deep_model, _tuning, _fast_effort, _deep_effort in (
    ("automatic", "gemini:gemini-3-flash", "opencode:claude-haiku-5-5", True, "auto", "auto"),
    ("unsupported", "opencode:gpt-5.5-pro", "opencode:kimi-k3", True, "auto", "auto"),
    ("unknown", "ep:proxy:gpt-5.5", "opencode:unverified-model", True, "auto", "auto"),
    ("off", "gemini:gemini-3-flash", "opencode:claude-haiku-5-5", False, "auto", "auto"),
    ("explicit", "gemini:gemini-3-flash", "opencode:claude-haiku-5-5", True, "low", "medium"),
    ("explicit_off", "gemini:gemini-3-flash", "opencode:claude-haiku-5-5", False, "low", "medium"),
):
    _preview_cfg.update(fast_model=_fast_model, deep_model=_deep_model, tune_thinking=_tuning,
                        fast_reasoning_effort=_fast_effort, deep_reasoning_effort=_deep_effort,
                        voice_question_model="deep")
    _status = companion_app.App._model_status(_preview_app)
    _status_fixtures[_scenario] = _status
    assert _status["fast_reasoning"] == _fast_effort and _status["deep_reasoning"] == _deep_effort, _status
    assert _status["voice_question"] == "deep" and _status["tune_thinking"] is _tuning, _status
    if _scenario in ("off", "explicit", "explicit_off"):
        assert _status["automatic_reasoning"] == {}, _status
    else:
        for _role, _model in (("fast", _fast_model), ("deep", _deep_model)):
            assert (_status["automatic_reasoning"][_role]
                    == _reasoning_fixtures["default"][_model]["automatic"][_role]), _status
    _preview = companion_app.App.on_models(_preview_app, reasoning_model="opencode:claude-haiku-5-5")
    assert _preview["automatic"]["fast"]["effort"] == "low", _preview
    assert _preview["automatic"]["deep"]["effort"] == "medium", _preview
for _scenario, _entry in (
    ("metadata_missing", {"id": "opencode:claude-haiku-5-5", "api": {"messages": {}}}),
    ("route_conflict", {**_native_catalog["opencode:claude-haiku-5-5"], "api": {"responses": {}}}),
    ("removed", {**_native_catalog["opencode:claude-haiku-5-5"],
                 "reasoning": {"supported": True, "effort": ["low", "medium", "high"]}}),
    ("stale", {**_native_catalog["opencode:claude-haiku-5-5"], "pricing_stale": True}),
):
    _preview_app.catalog.raw_get = lambda model, entry=_entry: entry if model == entry["id"] else _native_catalog.get(model)
    _reasoning_fixtures[_scenario] = {
        "opencode:claude-haiku-5-5": companion_app.App.on_models(
            _preview_app, reasoning_model="opencode:claude-haiku-5-5"),
    }

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
    companion_app.ROOT = tmp
    _settings_app = types.SimpleNamespace(
        args=types.SimpleNamespace(sim=True),
        coach=types.SimpleNamespace(cfg=glideconfig.load(tmp)["llm"], announce_models=lambda _: None),
        catalog=types.SimpleNamespace(get=_native_catalog.get, raw_get=_native_catalog.get),
    )
    _saved_reasoning_runtime = {}
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
