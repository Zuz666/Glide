"""Reviewed OpenCode native effort controls, intersected with that gateway's catalog.

Sources: https://opencode.ai/docs/console/models/#endpoints
https://platform.claude.com/docs/en/build-with-claude/effort
https://developers.openai.com/api/docs/guides/reasoning
https://ai.google.dev/gemini-api/docs/generate-content/thinking
https://docs.x.ai/developers/model-capabilities/text/reasoning
https://api-docs.deepseek.com/guides/thinking_mode
https://docs.z.ai/guides/capabilities/thinking
https://platform.kimi.ai/docs/api/models-overview.md
An API family and a reasoning boolean alone never authorize native effort fields.
"""
import opencode

_STANDARD = ("low", "medium", "high")
_EXTENDED = (*_STANDARD, "xhigh")
_FULL = (*_EXTENDED, "max")
_GROUPS = (
    ("anthropic", _FULL, (
        "claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5", "claude-opus-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-fable-5-2", "claude-fable-5-1", "claude-fable-5")),
    ("anthropic", (*_STANDARD, "max"), ("claude-opus-4-6", "claude-sonnet-4-6")),
    ("anthropic", _STANDARD, ("claude-opus-4-5",)),
    ("anthropic", (), ("claude-haiku-4-5", "claude-sonnet-4-5")),
    ("responses", ("minimal", *_STANDARD), ("gpt-5", "gpt-5-nano")),
    ("responses", _STANDARD, ("gpt-5-codex", "gpt-5.1-codex", "gpt-5.1-codex-mini", "grok-4.5")),
    ("responses", ("none", *_STANDARD), ("gpt-5.1",)),
    ("responses", _EXTENDED, (
        "gpt-5.1-codex-max", "gpt-5.2-codex", "gpt-5.3-codex-spark", "grok-4.6", "grok-4.7")),
    ("responses", ("none", *_EXTENDED), (
        "gpt-5.2", "gpt-5.3-codex", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5")),
    ("responses", ("medium", "high", "xhigh"), ("gpt-5.4-pro", "gpt-5.5-pro")),
    ("responses", ("none", *_FULL), (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol", "gpt-6-luna")),
    ("responses", _FULL, ("gpt-6-astra", "gpt-6.1-sol")),
    ("gemini", _STANDARD, ("gemini-4-carbon", "gemini-4-argon", "gemini-3.8-flash", "gemini-3.7-flash")),
    ("gemini", _EXTENDED, ("gemini-4-hydrogen",)),
    ("gemini", ("minimal", *_STANDARD), ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite")),
    ("chat", ("low", "high", "max"), ("deepseek-v4.1-flash", "glm-5.3", "glm-5.3-flash", "kimi-k3")),
    ("chat", ("high", "max"), ("deepseek-v4-pro", "glm-5.2")),
)
_PROFILES = {model: (transport, levels) for transport, levels, models in _GROUPS for model in models}
_API = {"anthropic": "messages", "responses": "responses", "gemini": "generate-content", "chat": "chat-completions"}


def transport(model):
    """Native API for an exact reviewed ID; unknown IDs stay translated Chat with no explicit controls."""
    return _PROFILES.get(model.removeprefix(opencode.PREFIX), ("chat", ()))[0]


def describe(model, entry=None):
    """One capability projection for selection, validation and provider request construction."""
    native = model.removeprefix(opencode.PREFIX)
    profile = _PROFILES.get(native)
    api = profile[0] if profile else "anthropic" if opencode.claude_model(model) else "chat"
    levels = []
    known = False
    if not isinstance(entry, dict) or entry.get("id") != model:
        note = "OpenCode capability metadata is unavailable; refresh the model list or use Auto."
    elif entry.get("excluded_reason"):
        note = "This OpenCode model is excluded: " + entry["excluded_reason"]
    else:
        metadata = entry.get("reasoning")
        if not isinstance(metadata, dict):
            note = "OpenCode effort metadata is unavailable; refresh the model list or use Auto."
        elif metadata.get("supported") is False or (metadata.get("supported") is True and not metadata.get("effort")):
            known = True
            note = "OpenCode advertises no effort levels for this model; thinking toggles or budgets are separate controls."
        elif not profile:
            note = "OpenCode native effort mapping is not verified for this model; use Auto."
            if native == "mistral-large-4":
                note = "OpenCode and vendor effort contracts conflict for this model; use Auto until resolved."
        elif not isinstance(entry.get("api"), dict) or not isinstance(entry["api"].get(_API[api]), dict):
            note = "OpenCode native API metadata is missing or conflicts with the reviewed route; refresh or use Auto."
        elif metadata.get("supported") is not True or not isinstance(metadata.get("effort"), list):
            note = "OpenCode effort metadata is unavailable; refresh the model list or use Auto."
        else:
            known = True
            levels = [level for level in profile[1] if level in metadata["effort"]]
            note = ("OpenCode model-specific native effort. Explicit effort bypasses all automatic tuning; "
                    "thinking keeps its native default and the output token limit is unchanged.")
            if not levels:
                note = "No advertised OpenCode effort matches this model's reviewed controls; use Auto."
            elif native == "claude-haiku-5-5":
                note += " Manual low leaves adaptive thinking on; Fast Auto tuning requests low with thinking disabled."
        if entry.get("pricing_stale"):
            note += " Catalog is stale; these are the last successfully imported capabilities."
    return {"levels": levels, "note": note, "transport": api, "known": known}


def options(model, effort):
    """The explicit native field; caller validates catalog availability before dispatch."""
    api, levels = _PROFILES.get(model.removeprefix(opencode.PREFIX), ("chat", ()))
    if effort not in levels:
        raise ValueError("Unverified OpenCode effort %r for %s" % (effort, model))
    if api == "anthropic":
        return {"output_config": {"effort": effort}}
    if api == "responses":
        return {"reasoning": {"effort": effort}}
    if api == "gemini":
        return {"generationConfig": {"thinkingConfig": {"thinkingLevel": effort}}}
    return {"reasoning_effort": effort}
