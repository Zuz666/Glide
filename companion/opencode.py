"""OpenCode pay-as-you-go transport, catalog metadata and conservative tariff accounting.

Catalog rates are USD per million tokens, never reported request charges. Unknown
conditions and cache retention durations deliberately produce no estimate.
"""
import copy
import math
import urllib.request
from typing import Any

PREFIX = "opencode:"
KEY_ENV = "OPENCODE_API_KEY"
BASE_URL = "https://opencode.ai/inference/openai/v1"
MODELS_URL = BASE_URL + "/models"
CHAT_URL = BASE_URL + "/chat/completions"
MESSAGES_URL = "https://opencode.ai/inference/anthropic/v1/messages"
RESPONSES_URL = BASE_URL + "/responses"
GEMINI_URL = "https://opencode.ai/inference/google/v1beta/models/"
META_BLOCK = "opencode_meta"


class ResponseError(RuntimeError):
    """A received but unusable response still carries its billable usage."""

    def __init__(self, message, usage):
        super().__init__(message)
        self.usage = usage


# Native Messages controls, not assumptions about the gateway's Chat translation.
# https://opencode.ai/docs/console/models/#endpoints
# https://platform.claude.com/docs/en/build-with-claude/thinking#configuring-thinking
# https://platform.claude.com/docs/en/build-with-claude/effort#effort-levels
# (automatic effort supported, Fast thinking mode); explicit choices live in opencode_reasoning.
CLAUDE_POLICIES = {
    "claude-fable-5-2": (True, None),
    "claude-fable-5-1": (True, None),
    "claude-fable-5": (True, None),
    "claude-opus-5-5": (True, "adaptive"),
    "claude-opus-5": (True, "adaptive"),
    "claude-opus-4-8": (True, "disabled"),
    "claude-opus-4-7": (True, "disabled"),
    "claude-opus-4-6": (True, "disabled"),
    "claude-opus-4-5": (True, "disabled"),
    "claude-opus-4-5-20251101": (True, "disabled"),
    "claude-sonnet-5-5": (True, "between_tools"),
    "claude-sonnet-5": (True, "disabled"),
    "claude-sonnet-4-6": (True, "disabled"),
    "claude-sonnet-4-5": (False, None),
    "claude-sonnet-4-5-20250929": (False, None),
    "claude-haiku-5-5": (True, "disabled"),
    "claude-haiku-4-5": (False, None),
    "claude-haiku-4-5-20251001": (False, None),
}


def claude_model(model):
    """Only verified native IDs select Messages; catalog URLs and aliases never do."""
    native = model.removeprefix(PREFIX) if isinstance(model, str) and model.startswith(PREFIX) else None
    return native if native in CLAUDE_POLICIES else None


def model_options(model, deep):
    """The verified native Claude low-latency policy; other gateway models remain default."""
    native = claude_model(model)
    if native is None:
        return {}
    supported, thinking = CLAUDE_POLICIES[native]
    if not supported:
        return {}
    opts = {"output_config": {"effort": "medium" if deep else "low"}}
    if not deep and thinking:
        opts["thinking"] = {"type": thinking}
    return opts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def open_request(req, timeout):
    """Never forward credentials or accept responses through a redirected origin."""
    return _opener.open(req, timeout=timeout)


def number(value):
    """Finite nonnegative monetary/token value, preserving a genuine zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _rate(value):
    return number(value)


def catalog_entry(model):
    """Import discovery without treating catalog URLs as credential destinations."""
    mid = model["id"]
    cost = model.get("cost") or {}
    text = cost.get("text")
    pricing = copy.deepcopy(text) if isinstance(text, dict) else None
    if pricing is not None and "multipliers" in cost:
        pricing["multipliers"] = copy.deepcopy(cost["multipliers"])
    inp = (text or {}).get("input") or {}
    out = (text or {}).get("output") or {}
    cache = inp.get("cache") or {}
    capabilities = model.get("capabilities") or {}
    tools = capabilities.get("tools")
    row = {"id": PREFIX + mid, "provider": "opencode", "name": model.get("name") or mid,
           "context": (model.get("limit") or {}).get("context"),
           "tools": tools.get("supported") if isinstance(tools, dict) else None,
           "price": {"in": _rate(inp.get("price")), "out": _rate(out.get("price")),
                     "cache_read": _rate(cache.get("read")), "cache_write": _rate(cache.get("write"))}
                    if isinstance(text, dict) else None,
           "pricing": pricing, "reasoning": copy.deepcopy(capabilities.get("reasoning")),
           "api": copy.deepcopy(model.get("api"))}
    # Preserve discovery metadata without treating native controls as translated Chat controls.
    modalities = model.get("modalities") or {}
    outputs, inputs = modalities.get("output"), modalities.get("input")
    kind = model.get("type")
    if kind not in (None, "chat", "text"):
        row["excluded_reason"] = "not a conversational text model"
    elif (outputs is not None and "text" not in outputs) or (inputs is not None and "text" not in inputs):
        row["excluded_reason"] = "does not support text conversation"
    elif mid == "big-pickle" or mid.endswith("-free") or (
            _rate(inp.get("price")) == 0 and _rate(out.get("price")) == 0):
        row["excluded_reason"] = "free chat models are restricted to OpenCode"
    elif kind is None or outputs is None:
        row["unverified"] = True
    return row


def normalize_usage(raw, api="chat"):
    """Gateway billed counts take priority; otherwise normalize the actual native API's token envelope."""
    raw = raw or {}
    meta = raw.get("opencode") or {}
    tokens = meta.get("tokens")
    if not isinstance(tokens, dict) and api == "responses":
        raw = {**raw, "prompt_tokens": raw.get("input_tokens"), "completion_tokens": raw.get("output_tokens"),
               "prompt_tokens_details": raw.get("input_tokens_details"),
               "completion_tokens_details": raw.get("output_tokens_details")}
        raw.pop("input_tokens", None)
        raw.pop("output_tokens", None)
    elif not isinstance(tokens, dict) and api == "gemini":
        visible, thought = number(raw.get("candidatesTokenCount")), number(raw.get("thoughtsTokenCount", 0))
        raw = {**raw, "prompt_tokens": raw.get("promptTokenCount"),
               "completion_tokens": visible + thought if visible is not None and thought is not None else None,
               "prompt_tokens_details": {"cached_tokens": raw.get("cachedContentTokenCount", 0)},
               "completion_tokens_details": {"reasoning_tokens": raw.get("thoughtsTokenCount", 0)}}
    details = raw.get("prompt_tokens_details") or {}
    durations = {}
    usage: dict[str, Any]
    if isinstance(tokens, dict):
        counts = {name: number(tokens.get(name)) for name in ("input", "output", "reasoning", "cache_read")}
        for duration in ("5m", "1h", "30m", "12h"):
            if "cache_write_" + duration in tokens:
                value = number(tokens["cache_write_" + duration])
                if value is not None:
                    durations[duration] = value
        written = number(tokens.get("cache_write"))
        if written is None:
            written = sum(durations.values())
        usage = {"input_tokens": counts["input"] or 0, "output_tokens": counts["output"] or 0,
                 "cache_read_input_tokens": counts["cache_read"] or 0,
                 "cache_creation_input_tokens": written}
        if counts["input"] is None or counts["output"] is None:
            usage["tokens_unknown"] = True
        supported = {"input", "output", "reasoning", "cache_read", "cache_write",
                     "cache_write_5m", "cache_write_1h", "cache_write_30m", "cache_write_12h"}
        if any((key in supported and number(value) is None) or
               (key.startswith("cache_write_") and key not in supported) for key, value in tokens.items()):
            usage["tokens_unknown"] = True
        if counts["reasoning"] is not None:
            usage["reasoning_tokens"] = counts["reasoning"]
    elif "input_tokens" in raw or "output_tokens" in raw:
        # Messages input_tokens already excludes cache reads and writes, unlike Chat prompt_tokens.
        inp, out = number(raw.get("input_tokens")), number(raw.get("output_tokens"))
        cached = number(raw.get("cache_read_input_tokens")) or 0
        written = number(raw.get("cache_creation_input_tokens")) or 0
        usage = {"input_tokens": inp or 0, "output_tokens": out or 0,
                 "cache_read_input_tokens": cached, "cache_creation_input_tokens": written}
        if inp is None or out is None:
            usage["tokens_unknown"] = True
        creation = raw.get("cache_creation") or {}
        for duration in ("5m", "1h"):
            field = "ephemeral_" + duration + "_input_tokens"
            value = number(creation.get(field))
            if value is not None:
                durations[duration] = value
        for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if field in raw and number(raw[field]) is None:
                usage["tokens_unknown"] = True
    else:
        total, output = number(raw.get("prompt_tokens")), number(raw.get("completion_tokens"))
        cached = number(details.get("cached_tokens")) or 0
        written = number(details.get("cache_write_tokens")) or 0
        usage = {"input_tokens": max(0, (total or 0) - cached - written), "output_tokens": output or 0,
                 "cache_read_input_tokens": cached, "cache_creation_input_tokens": written}
        if total is None or output is None or cached + written > total:
            usage["tokens_unknown"] = True
        wdetails = details.get("cache_write_tokens_details") or {}
        for duration in ("5m", "1h", "30m", "12h"):
            value = number(wdetails.get("ttl_" + duration + "_tokens"))
            if value is not None:
                durations[duration] = value
        reasoning = number((raw.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        if reasoning is not None:
            usage["reasoning_tokens"] = reasoning
        if any(key in details and number(details[key]) is None for key in ("cached_tokens", "cache_write_tokens")):
            usage["tokens_unknown"] = True
    if usage.get("reasoning_tokens", 0) > usage["output_tokens"]:
        usage["tokens_unknown"] = True
    if durations:
        usage["cache_write_tokens_by_duration"] = durations
        usage["cache_creation"] = {"ephemeral_" + k + "_input_tokens": v for k, v in durations.items()}
        if sum(durations.values()) > usage["cache_creation_input_tokens"]:
            usage["tokens_unknown"] = True
    charge = number(raw.get("cost"))
    estimated = meta.get("estimated") is True
    if charge is None:
        charge = number(meta.get("list_cost"))
        estimated = True
    if charge is not None:
        usage["cost_usd"] = charge
        usage["cost_estimated"] = estimated
    return usage


def _condition(when, context):
    if not isinstance(when, dict) or set(when) != {"context"}:
        return None
    comparisons = when["context"]
    if not isinstance(comparisons, dict) or not comparisons:
        return None
    matches = []
    for op, threshold in comparisons.items():
        threshold = number(threshold)
        if threshold is None or op not in ("gt", "gte", "lt", "lte", "eq"):
            return None
        matches.append({"gt": context > threshold, "gte": context >= threshold,
                        "lt": context < threshold, "lte": context <= threshold,
                        "eq": context == threshold}[op])
    return all(matches)


def _select_rate(tariff, context):
    if not isinstance(tariff, dict):
        return None
    selected = {k: copy.deepcopy(v) for k, v in tariff.items() if k != "overrides"}
    overrides = tariff.get("overrides", [])
    if not isinstance(overrides, list):
        return None
    for override in overrides:
        if not isinstance(override, dict):
            return None
        applies = _condition(override.get("when"), context)
        if applies is None:
            return None
        if applies:
            for key, value in override.items():
                if key == "cache" and isinstance(value, dict):
                    selected[key] = {**(selected.get(key) or {}), **value}
                elif key != "when":
                    selected[key] = value
    return selected


def estimate_cost(entry, usage):
    """Return a tariff estimate, or None rather than invent missing rates/conditions."""
    pricing = (entry or {}).get("pricing")
    if not isinstance(pricing, dict) or not isinstance(usage, dict) or usage.get("tokens_unknown"):
        return None
    # Date, peak-hour and other multipliers require facts this interface does not have.
    if pricing.get("multipliers") or set(pricing) - {"input", "output", "multipliers"}:
        return None
    counts = []
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = number(usage.get(key, 0))
        if value is None:
            return None
        counts.append(value)
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    fresh, output, cached, written = counts
    context = fresh + cached + written
    inp, out = _select_rate(pricing.get("input"), context), _select_rate(pricing.get("output"), context)
    if inp is None or out is None or set(inp) - {"price", "cache"} or set(out) - {"price"}:
        return None
    if _rate(inp.get("price")) is None or _rate(out.get("price")) is None:
        return None
    cache = inp.get("cache") or {}
    if not isinstance(cache, dict) or set(cache) - {"read", "write"}:
        return None
    total = 0.0
    for count, rate in ((fresh, inp.get("price")), (output, out.get("price")), (cached, cache.get("read"))):
        if count:
            rate = _rate(rate)
            if rate is None:
                return None
            total += count * rate
    if written:
        rate = cache.get("write")
        durations = usage.get("cache_write_tokens_by_duration")
        if durations is None:
            durations = {k[len("ephemeral_"):-len("_input_tokens")]: v
                         for k, v in (usage.get("cache_creation") or {}).items()
                         if k.startswith("ephemeral_") and k.endswith("_input_tokens")}
        if not isinstance(durations, dict) or not durations or any(number(v) is None for v in durations.values()):
            return None
        if sum(durations.values()) != written:
            return None
        for duration, count in durations.items():
            if not count:
                continue
            # Scalar writes are the standard short-lived rate; a long-lived write
            # needs an explicit duration rate, never an upstream-vendor multiplier.
            applicable = rate.get(duration) if isinstance(rate, dict) else rate if duration == "5m" else None
            applicable = _rate(applicable)
            if applicable is None:
                return None
            total += count * applicable
    result = total / 1e6
    return result if math.isfinite(result) else None
