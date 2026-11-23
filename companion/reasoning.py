"""Documented reasoning controls for a model's actual API; no probes or fallback requests."""
import openai_capabilities
import openai_compat

# Schema vocabulary is not a promise that every compatible server accepts every value.
# https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
_COMPAT_LEVELS = openai_capabilities.COMPAT_LEVELS
_STANDARD = ("low", "medium", "high")
_EXTENDED = (*_STANDARD, "xhigh")
# These are Google's OpenAI-compatibility values, not native thinkingLevel names/budgets.
# https://ai.google.dev/gemini-api/docs/openai#thinking
# https://ai.google.dev/gemini-api/docs/thinking?generate-content=true#thinking-levels
_GEMINI_LEVELS = ("minimal", *_STANDARD)
_GEMINI_PROFILES = {
    "gemini-2.5-flash": ("none", *_GEMINI_LEVELS),
    "gemini-2.5-flash-lite": ("none", *_GEMINI_LEVELS),
    "gemini-2.5-pro": _GEMINI_LEVELS,
    "gemini-3-flash": _GEMINI_LEVELS,
    "gemini-3.1-flash-lite": _GEMINI_LEVELS,
    "gemini-3.5-flash-lite": _GEMINI_LEVELS,
    "gemini-3.6-flash": _GEMINI_LEVELS,
    "gemini-3.8-flash": _STANDARD,
    "gemini-4-carbon": _GEMINI_LEVELS,
    "gemini-4-argon": _GEMINI_LEVELS,
    "gemini-2.0-flash": (),
    "gemini-2.0-flash-lite": (),
}


# Native effort is distinct from thinking mode; none/minimal are not native effort values.
# https://platform.claude.com/docs/en/build-with-claude/effort
# https://platform.claude.com/docs/en/models/opus-5-5/migration-guide (all five levels)
_CLAUDE_PROFILES = {
    "claude-opus-4-5": _STANDARD,
    "claude-opus-4-5-20251101": _STANDARD,
    "claude-opus-5-5": (*_EXTENDED, "max"),
    "claude-haiku-4-5": (),
    "claude-haiku-4-5-20251001": (),
    "claude-sonnet-4-5": (),
    "claude-sonnet-4-5-20250929": (),
}


def _result(levels, note, transport, known, limit_field=None, suitability=None, reason=None):
    out = {"levels": list(levels), "note": note, "transport": transport, "known": known}
    if limit_field is not None:
        out["limit_field"] = limit_field
    if suitability is not None:
        out["suitability"] = suitability
    if reason is not None:
        out["reason"] = reason
    return out

def _catalog_entry(model, entry):
    """Ignore entries belonging to another selection, including a stale model-picker entry."""
    return entry if isinstance(entry, dict) and entry.get("id", model) == model else {}


def _openrouter(entry):
    # Missing/null are deliberately different in the gateway schema.
    # https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#model-reasoning-capabilities
    metadata = entry.get("reasoning")
    if not isinstance(metadata, dict):
        return _result((), "OpenRouter uses gateway reasoning.effort, not the native upstream API contract. "
                       "Supported model levels are not advertised here; refresh the model list or use auto.",
                       "chat", False)
    if "supported_efforts" not in metadata:
        return _result((), "OpenRouter does not expose effort selection for this model; use auto.", "chat", True)
    advertised = metadata["supported_efforts"]
    if advertised is not None and not isinstance(advertised, list):
        return _result((), "OpenRouter effort metadata is unavailable; refresh the model list or use auto.",
                       "chat", False)
    levels = [level for level in _COMPAT_LEVELS if advertised is None or level in advertised]
    if metadata.get("mandatory") is True:
        levels = [level for level in levels if level != "none"]
    return _result(levels, "OpenRouter gateway effort levels from the model catalog; native upstream levels "
                   "and semantics may differ.", "chat", True)


def _anthropic(entry):
    # Read nested .supported booleans, never the truthiness of capability objects.
    # https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/effort_capability.py
    capabilities = entry.get("capabilities")
    effort = capabilities.get("effort") if isinstance(capabilities, dict) else None
    if not isinstance(effort, dict) or not isinstance(effort.get("supported"), bool):
        return None
    if effort["supported"] is False:
        return _result((), "The Claude model catalog reports no native effort controls.", "anthropic", True)
    levels = [level for level in (*_EXTENDED, "max")
              if isinstance(effort.get(level), dict) and effort[level].get("supported") is True]
    return _result(levels, "Native Claude effort levels from the model catalog; thinking mode remains separate.",
                   "anthropic", True)


def describe(model, cfg, entry=None) -> dict:
    """Return explicit choices (excluding auto), documentation confidence and tool-capable transport."""
    entry = _catalog_entry(model, entry)
    if openai_compat.is_endpoint(model):
        contract = openai_capabilities.describe_endpoint_model(model, cfg, entry)
        return _result(contract.efforts, contract.note, contract.transport, contract.known,
                       limit_field=contract.limit_field, suitability=contract.suitability, reason=contract.reason)
    if openai_compat.is_openrouter(model):
        return _openrouter(entry)
    if openai_compat.is_gemini(model):
        name = model[len(openai_compat.GEMINI_PREFIX):]
        levels = _GEMINI_PROFILES.get(name)
        if levels is None:
            return _result((), "Supported Gemini levels are not advertised for this model; use auto.", "chat", False)
        if not levels:
            note = "This model does not support reasoning effort controls."
        elif name in ("gemini-4-carbon", "gemini-4-argon"):
            note = "Google's compatibility API maps minimal to low; thinking cannot be disabled."
        elif "none" in levels:
            note = "Google's compatibility API maps effort to a thinking budget; none disables thinking."
        else:
            note = "Google's compatibility effort levels; thinking cannot be disabled."
        return _result(levels, note, "chat", True)
    advertised = _anthropic(entry)
    if advertised is not None:
        return advertised
    levels = _CLAUDE_PROFILES.get(model)
    if levels is None:
        return _result((), "Supported effort levels are not advertised here for this Claude model; "
                       "refresh the model list or use auto.",
                       "anthropic", False)
    note = ("Native Claude output effort; thinking mode is a separate setting."
            if levels else "This Claude model does not support effort controls.")
    return _result(levels, note, "anthropic", True)


def validate(model, cfg, value, entry=None) -> None:
    """Reject invalid explicit settings without changing them or falling back to another effort."""
    if isinstance(value, str) and value == "auto":
        return
    choices = describe(model, cfg, entry)
    if isinstance(value, str) and value in choices["levels"]:
        return
    allowed = ", ".join(["auto", *choices["levels"]])
    raise ValueError("Reasoning effort %r is not supported for %s. Allowed values: %s. %s"
                     % (value, model, allowed, choices["note"]))
