#!/usr/bin/env python3
"""Reasoning controls describe the selected API, not just a model-name prefix (no network)."""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import reasoning  # noqa: E402
import models  # noqa: E402


def endpoint(url="https://api.openai.com/v1", eid="work"):
    return {"endpoints": [{"id": eid, "base_url": url}]}


def rejected(model, cfg, value, *allowed):
    try:
        reasoning.validate(model, cfg, value)
    except ValueError as exc:
        message = str(exc)
        assert model in message and "auto" in message, message
        assert all(level in message for level in allowed), message
    else:
        raise AssertionError("accepted unsupported reasoning effort %r for %s" % (value, model))


# The actual 6.1 Sol server rejects none; the endpoint's user-chosen id is not its identity.
model = "ep:work:gpt-6.1-sol"
caps = reasoning.describe(model, endpoint())
assert caps["levels"] == ["low", "medium", "high", "xhigh"], caps
assert caps["known"] and caps["transport"] == "responses" and caps["note"], caps
for value in ("auto", "low", "medium", "high", "xhigh"):
    assert reasoning.validate(model, endpoint(), value) is None
for value in ("none", "max", "minimal", "HIGH", " high ", "", None, False, 1, []):
    rejected(model, endpoint(), value, "low", "medium", "high", "xhigh")

# Distinct documented models retain their own sets; all named GPT-6 tool loops use Responses.
for name in ("gpt-6-luna", "gpt-6-sol", "gpt-6.1-terra"):
    caps = reasoning.describe("ep:work:" + name, endpoint())
    assert caps["levels"] == ["none", "low", "medium", "high", "xhigh", "max"], caps
    assert caps["known"] and caps["transport"] == "responses", caps
astra = reasoning.describe("ep:work:gpt-6-astra", endpoint())
assert astra["levels"] == ["low", "medium", "high", "xhigh", "max"], astra
assert astra["transport"] == "responses"
assert reasoning.describe("ep:work:gpt-5", endpoint())["levels"] == ["minimal", "low", "medium", "high"]
assert reasoning.describe("ep:work:gpt-5.1", endpoint())["levels"] == ["none", "low", "medium", "high"]
assert reasoning.describe("ep:work:gpt-5.6-terra", endpoint())["transport"] == "chat"

# Normalized endpoint config, not an id such as 'openai', identifies the official API.
normalized = endpoint("  https://api.openai.com/v1///  ", " WORK ")
assert reasoning.describe(model, normalized)["transport"] == "responses"
assert reasoning.describe(model, endpoint("https://API.OPENAI.COM:443/v1"))["known"]
for url in ("https://api.openai.com.evil.example/v1", "https://example.com/api.openai.com/v1",
            "https://api.openai.com@evil.example/v1", "https://api.openai.com:8443/v1",
            "http://api.openai.com/v1", "https://api.openai.com/proxy/v1",
            "https://api.openai.com/v1?proxy=1", "https://api.openai.com/v1#proxy",
            "https://other.example/v1", "https://api.openai.com:bad/v1"):
    caps = reasoning.describe("ep:openai:gpt-6.1-sol", endpoint(url, "openai"))
    assert caps["transport"] == "chat" and not caps["known"], (url, caps)
assert reasoning.describe("ep:missing:gpt-6.1-sol", endpoint())["transport"] == "chat"

# Unknown compatible servers/model aliases are not forced into OpenAI's Responses transport.
custom = endpoint("http://127.0.0.1:1234/v1", "openai")
for unknown_model, cfg in (("ep:openai:gpt-6.1-sol", custom), ("ep:work:gpt-6-private", endpoint()),
                           ("ep:work:gpt-6-luna-custom", endpoint())):
    unknown = reasoning.describe(unknown_model, cfg)
    assert unknown["transport"] == "chat" and not unknown["known"], unknown
    assert "auto" not in unknown["levels"] and "low" in unknown["levels"], unknown
    assert "not advertised" in unknown["note"] and "auto" in unknown["note"].lower(), unknown
    reasoning.validate(unknown_model, cfg, "auto")
    rejected(unknown_model, cfg, "invented", "low")

# A known non-reasoning model offers no explicit control, rather than a fake 'none' option.
for name in ("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"):
    model = "ep:work:" + name
    caps = reasoning.describe(model, endpoint())
    assert caps["known"] and caps["levels"] == [] and caps["transport"] == "chat", caps
    reasoning.validate(model, endpoint(), "auto")
    rejected(model, endpoint(), "none")
    rejected(model, endpoint(), "low")

# Google compatibility has model-scoped levels: 'minimal' is not an off switch.
for model in ("gemini:gemini-2.5-pro", "gemini:gemini-3-flash", "gemini:gemini-3.1-flash-lite"):
    caps = reasoning.describe(model, {})
    assert caps["levels"] == ["minimal", "low", "medium", "high"] and caps["known"], caps
    assert caps["transport"] == "chat"
    rejected(model, {}, "none", "minimal", "low", "medium", "high")
    rejected(model, {}, "xhigh")
assert reasoning.describe("gemini:gemini-2.5-flash", {})["levels"] == ["none", "minimal", "low", "medium", "high"]
assert reasoning.describe("gemini:gemini-3.8-flash", {})["levels"] == ["low", "medium", "high"]
for model in ("gemini:private-alias", "claude-private-alias", "claude-sonnet-5-5", "claude-opus-4-6"):
    caps = reasoning.describe(model, {})
    assert not caps["known"] and caps["levels"] == [], caps
    reasoning.validate(model, {}, "auto")
    rejected(model, {}, "low")

# Native Claude does not accept OpenAI's 'none'/'minimal' effort vocabulary.
assert reasoning.describe("claude-opus-4-5-20251101", {})["levels"] == ["low", "medium", "high"]
assert reasoning.describe("claude-opus-5-5", {})["levels"] == ["low", "medium", "high", "xhigh", "max"]
assert reasoning.describe("claude-opus-5-5", {})["transport"] == "anthropic"
for model in ("claude-opus-4-5-20251101", "claude-opus-5-5"):
    rejected(model, {}, "none", "low", "medium", "high")
    rejected(model, {}, "minimal")
for model in ("claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"):
    caps = reasoning.describe(model, {})
    assert caps["known"] and not caps["levels"] and caps["transport"] == "anthropic", caps
    rejected(model, {}, "low")

# OpenRouter's gateway contract is not inferred from the model's native upstream contract.
for model in ("openai/gpt-6.1-sol", "anthropic/claude-opus-4-5", "google/gemini-3-flash"):
    caps = reasoning.describe(model, {})
    assert caps["transport"] == "chat" and caps["levels"] == [] and not caps["known"], caps
    assert "gateway" in caps["note"] and "not advertised" in caps["note"], caps
    reasoning.validate(model, {}, "auto")
    rejected(model, {}, "low")

# Per-model API metadata is authoritative: false capability objects must not become enabled options.
native = {"id": "claude-sonnet-5-5", "capabilities": {"effort": {
    "supported": True, "low": {"supported": True}, "medium": {"supported": False},
    "high": {"supported": True}, "max": {"supported": False}}}}
entry = models.fetch_anthropic("test", getter=lambda *_: {"data": [native]})[0]
assert reasoning.describe(native["id"], {}, entry)["levels"] == ["low", "high"]
reasoning.validate(native["id"], {}, "high", entry)
try:
    reasoning.validate(native["id"], {}, "medium", entry)
    raise AssertionError("A false capability object enabled medium")
except ValueError:
    pass
disabled = {**entry, "capabilities": {"effort": {"supported": False, "high": {"supported": True}}}}
assert reasoning.describe(native["id"], {}, disabled)["levels"] == []
assert reasoning.describe("claude-private-alias", {}, entry)["levels"] == [], "another model's metadata cannot apply"

# OpenRouter's null allowlist differs from an absent one, and mandatory reasoning cannot be disabled.
router = {"id": "vendor/model", "reasoning": {"supported_efforts": ["none", "high"], "mandatory": True}}
entry = models.fetch_openrouter(getter=lambda *_: {"data": [router]})[0]
assert reasoning.describe(router["id"], {}, entry)["levels"] == ["high"]
reasoning.validate(router["id"], {}, "high", entry)
unrestricted = {**entry, "reasoning": {"supported_efforts": None}}
assert "none" in reasoning.describe(router["id"], {}, unrestricted)["levels"]
assert "xhigh" in reasoning.describe(router["id"], {}, unrestricted)["levels"]
assert reasoning.describe(router["id"], {}, {**entry, "reasoning": {}})["levels"] == []

print("reasoning capabilities: ok")
