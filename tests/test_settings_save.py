#!/usr/bin/env python3
"""The desktop app saves config.json through the running engine (POST /settings {"save": patch}), so the engine's
own writes (dashboard model picks) and the app's never cross: the patch merges as the app's config:set did (None
removes a key), lands even in a simulated run, goes with live changes in one request, and a bad one is refused.
The app falls back to writing the file itself only when no engine takes it (desktop/src/main/app/config.ts).

Run: python3 tests/test_settings_save.py   (prints OK)
"""
import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import app as glide_app  # noqa: E402
import glideconfig  # noqa: E402

tmp = tempfile.mkdtemp(prefix="glide-settings-save-")
p = os.path.join(tmp, "config.json")
glide_app.ROOT = tmp
announced = []


def fake_app(sim):
    coach = types.SimpleNamespace(cfg={"fast_model": "a-model", "deep_model": "b-model"},
                                  announce_models=announced.append, describe_models=lambda: "models")
    return types.SimpleNamespace(args=types.SimpleNamespace(sim=sim), coach=coach, catalog=None)


try:
    with open(p, "w", encoding="utf-8-sig") as fh:
        json.dump({"wow_dir": "C:/WoW", "companion": {"llm": {"fast_model": "a-model", "web_search": True}}}, fh)
    # the app's Settings save: a patch of the whole file
    a = fake_app(sim=True)
    out = glide_app.App.on_settings(a, {"save": {"companion": {"llm": {"web_search": None}, "ptt": {"key": "F9"}},
                                                 "wow_dir": "D:/WoW"}})
    assert out == {"saved": True}, out
    assert json.load(open(p, encoding="utf-8")) == {"wow_dir": "D:/WoW", "companion": {
        "llm": {"fast_model": "a-model"}, "ptt": {"key": "F9"}}}
    assert a.coach.cfg == {"fast_model": "a-model", "deep_model": "b-model"}, "a save alone changes nothing live"

    # with a live change in the same request: both
    a = fake_app(sim=False)
    out = glide_app.App.on_settings(a, {"save": {"companion": {"voice": {"rate": 2}}}, "fast_model": "c-model"})
    assert out == {"fast_model": "c-model", "saved": True}, out
    saved = json.load(open(p, encoding="utf-8"))
    assert saved["companion"]["voice"] == {"rate": 2} and saved["companion"]["llm"]["fast_model"] == "c-model", saved
    assert a.coach.cfg["fast_model"] == "c-model" and announced, announced

    # the old flat body still works and still says whether it saved
    assert glide_app.App.on_settings(fake_app(sim=True), {"deep_model": "d-model"}) == {"deep_model": "d-model",
                                                                                        "saved": False}

    # Save and restart sends only the nested save patch, not a live model change.
    old = {"fast_model": "ep:openai:gpt-6-luna", "fast_reasoning_effort": "none",
           "deep_model": "ep:openai:gpt-6-luna", "deep_reasoning_effort": "low"}
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"companion": {"llm": old}}, fh)
    a = fake_app(sim=False)
    a.coach.cfg.update(old)
    glide_app.App.on_settings(a, {"save": {"companion": {"llm": {"fast_model": "ep:openai:gpt-6.1-sol"}}}})
    restarted = glideconfig.load(tmp)["llm"]
    assert restarted["fast_model"] == "ep:openai:gpt-6.1-sol"
    assert restarted["fast_reasoning_effort"] == "auto", restarted
    assert restarted["deep_reasoning_effort"] == "low"
    assert a.coach.cfg["fast_model"] == old["fast_model"], "save-only changes apply at restart, not mid-turn"
    # A level explicitly selected together with the new model belongs to that model.
    glide_app.App.on_settings(a, {"save": {"companion": {"llm": {
        "fast_model": "ep:openai:gpt-6-luna", "fast_reasoning_effort": "low"}}}})
    assert glideconfig.load(tmp)["llm"]["fast_reasoning_effort"] == "low"

    # A combined save cannot leave invalid live choices persisted for the next restart.
    a = fake_app(sim=False)
    a.coach.cfg.update({"fast_model": "ep:openai:gpt-6.1-sol", "fast_reasoning_effort": "low"})
    a.coach.cfg["endpoints"] = [{"id": "openai", "base_url": "https://api.openai.com/v1"}]
    before = open(p, encoding="utf-8").read()
    running_before = dict(a.coach.cfg)
    try:
        glide_app.App.on_settings(a, {
            "save": {"companion": {"llm": {"fast_reasoning_effort": "none"}, "ptt": {"key": "F10"}}},
            "fast_reasoning_effort": "none"})
        raise AssertionError("accepted unsupported reasoning effort")
    except ValueError:
        pass
    assert open(p, encoding="utf-8").read() == before
    assert a.coach.cfg == running_before

    # Use default removes the override on disk and applies that exact default without a restart.
    default_model = glide_app.settings.default("llm.fast_model")
    glide_app.App.on_settings(a, {
        "save": {"companion": {"llm": {"fast_model": None, "fast_reasoning_effort": None}}},
        "fast_model": default_model, "fast_reasoning_effort": "auto"})
    saved = json.load(open(p, encoding="utf-8"))["companion"]["llm"]
    assert "fast_model" not in saved and "fast_reasoning_effort" not in saved
    assert a.coach.cfg["fast_model"] == default_model and a.coach.cfg["fast_reasoning_effort"] == "auto"
    assert glideconfig.load(tmp)["llm"]["fast_model"] == default_model

    # A failed combined write leaves the running selection untouched too.
    a = fake_app(sim=False)
    running_before = dict(a.coach.cfg)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"wow_dir": ,}')
    try:
        glide_app.App.on_settings(a, {"save": {"wow_dir": "E:/WoW"}, "fast_model": "c-model"})
        raise AssertionError("saved over an unreadable config.json")
    except ValueError:
        pass
    assert a.coach.cfg == running_before
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(before)
    # a bad patch is refused (POST /settings answers 400 with the reason) and the file stays as it is
    before = open(p, encoding="utf-8").read()
    for bad in ({"save": [1]}, {"save": "x"}):
        try:
            glide_app.App.on_settings(fake_app(sim=False), bad)
            raise SystemExit("took %r" % bad)
        except ValueError:
            pass
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"wow_dir": ,}')
    try:
        glide_app.App.on_settings(fake_app(sim=False), {"save": {"wow_dir": "E:/WoW"}})
        raise SystemExit("saved over an unreadable config.json")
    except ValueError:
        pass
    assert open(p, encoding="utf-8").read() == '{"wow_dir": ,}' and before

finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("OK")
