#!/usr/bin/env python3
"""An empty model never reaches a provider: a model path left empty (its Settings box cleared and saved) is the other
path's model, at start (glideconfig.load) and when changed live (tuning.apply); both empty are the defaults. Before,
an empty deep model was sent as a Claude model ("Claude API 400 (): model: String should have at least 1 character")
by a player who had switched the fast model to OpenRouter.

Run: python3 tests/test_model_fill.py   (prints OK)
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import glideconfig  # noqa: E402
import models  # noqa: E402
import settings  # noqa: E402
import tuning  # noqa: E402


def check(cond, what):
    if not cond:
        raise SystemExit("FAIL: " + str(what))


OR = "cognitivecomputations/dolphin-mistral-24b-venice-edition"
d = tempfile.mkdtemp(prefix="glide-model-fill-")
try:
    def load(llm):
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"companion": {"llm": llm}}, fh)
        return glideconfig.load(d)["llm"]

    # the player's config: the fast model on OpenRouter, the deep one cleared
    llm = load({"fast_model": OR, "deep_model": ""})
    check(llm["fast_model"] == OR and llm["deep_model"] == OR and models.provider(llm["deep_model"]) == "openrouter", llm)
    llm = load({"fast_model": "  ", "deep_model": "claude-sonnet-5-5"})
    check(llm["fast_model"] == llm["deep_model"] == "claude-sonnet-5-5", llm)
    llm = load({"fast_model": "", "deep_model": ""})
    check(llm["fast_model"] == settings.default("llm.fast_model") and
          llm["deep_model"] == settings.default("llm.deep_model"), "both empty: the defaults")
    llm = load({"fast_model": "", "deep_model": None})        # (not set: its default, which the empty one takes)
    check(llm["fast_model"] == llm["deep_model"] == settings.default("llm.deep_model"), llm)
    llm = load({"fast_model": OR, "deep_model": "google/gemini-2.5-flash"})
    check(llm["deep_model"] == "google/gemini-2.5-flash", "set ones are kept")
    check(glideconfig.load(os.path.join(d, "nowhere"))["llm"]["deep_model"] == settings.default("llm.deep_model"),
          "no config: the defaults")

    # changed live: empty takes the other path's model (and saves it); a bad id is still refused
    cfg = {"fast_model": OR, "deep_model": "claude-sonnet-5-5"}
    path = os.path.join(d, "config.json")
    got = tuning.apply(cfg, {"deep_model": ""}, path)
    check(got == {"deep_model": OR} and cfg["deep_model"] == OR, (got, cfg))
    saved = json.load(open(path, encoding="utf-8"))["companion"]["llm"]
    check(saved["deep_model"] == OR, saved)
    try:
        tuning.apply(cfg, {"fast_model": "a"}, None)
        check(False, "a bad id is refused")
    except ValueError:
        pass

    # clearing a model resets stale effort, validates explicit effort against the inherited model,
    # rejects inheriting excluded OpenAI/OpenCode models, and never mutates cfg if saving fails
    sol = "ep:openai:gpt-6.1-sol"
    cfg_r = {
        "endpoints": [{"id": "openai", "base_url": "https://api.openai.com/v1"}],
        "fast_model": sol,
        "deep_model": "ep:openai:gpt-6-luna",
        "deep_reasoning_effort": "none",
    }
    got = tuning.apply(cfg_r, {"deep_model": ""}, path)
    check(got == {"deep_model": sol, "deep_reasoning_effort": "auto"}
          and cfg_r["deep_model"] == sol and cfg_r["deep_reasoning_effort"] == "auto", (got, cfg_r))
    try:
        tuning.apply(cfg_r, {"deep_model": "", "deep_reasoning_effort": "none"}, path)
        check(False, "explicit effort unsupported by inherited model must be rejected")
    except ValueError:
        pass
    got = tuning.apply(cfg_r, {"deep_model": "", "deep_reasoning_effort": "low"}, path)
    check(got == {"deep_model": sol, "deep_reasoning_effort": "low"}, got)

    cfg_ex = {
        "endpoints": [{"id": "openai", "base_url": "https://api.openai.com/v1"}],
        "fast_model": "ep:openai:tts-1",
        "deep_model": sol,
    }
    before_json = open(path, encoding="utf-8").read()
    try:
        tuning.apply(cfg_ex, {"deep_model": ""}, path)
        check(False, "must reject inheriting an excluded official OpenAI model")
    except ValueError as exc:
        check("speech synthesis" in str(exc), exc)
    check(cfg_ex["deep_model"] == sol and open(path, encoding="utf-8").read() == before_json, (cfg_ex, before_json))

    bad_cat = type("Cat", (), {
        "raw_get": staticmethod(lambda m: {"id": m, "excluded_reason": "subscription tier"}
                                if m == "opencode:bad-model" else None),
    })()
    cfg_oc = {"fast_model": "opencode:bad-model", "deep_model": OR}
    try:
        tuning.apply(cfg_oc, {"deep_model": ""}, path, catalog=bad_cat)
        check(False, "must reject inheriting an excluded OpenCode model")
    except ValueError as exc:
        check("subscription tier" in str(exc), exc)
    check(cfg_oc["deep_model"] == OR, cfg_oc)

    # failed save leaves live cfg untouched; clearing both roles fills manifest defaults
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{bad json")
    cfg_atomic = {"fast_model": OR, "deep_model": "claude-sonnet-5-5"}
    try:
        tuning.apply(cfg_atomic, {"deep_model": ""}, path)
        check(False, "expected ValueError on unreadable config.json")
    except ValueError:
        pass
    check(cfg_atomic["deep_model"] == "claude-sonnet-5-5", cfg_atomic)
    got = tuning.apply(dict(cfg_atomic), {"fast_model": "", "deep_model": ""}, None)
    check(got == {"fast_model": settings.default("llm.fast_model"),
                  "deep_model": settings.default("llm.deep_model")}, got)
finally:
    shutil.rmtree(d, ignore_errors=True)
print("OK")
