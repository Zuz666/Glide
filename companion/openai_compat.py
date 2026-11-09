"""The OpenAI-compatible APIs for the coach (OpenRouter, Gemini direct and custom endpoints): translate the coach's
Anthropic Messages requests to OpenAI-style chat completions and the answers back, so the rest of the coach (tool
loop, billing guard, grounding) works unchanged with any model these offer (providers.OpenAICompatProvider sends
them).

A model id with a "/" (OpenRouter's "vendor/model" form, e.g. "anthropic/claude-sonnet-4.5",
"google/gemini-2.5-flash") goes to OpenRouter; plain ids ("claude-sonnet-5-5") go to Anthropic directly.
The key comes from the OPENROUTER_API_KEY environment variable (config "openrouter_key_env"); it is never stored.

Custom endpoints (config "llm" -> "endpoints": [{"id", "label", "base_url", "key_env"}]) are any other
OpenAI-compatible server: a local Ollama or LM Studio, OpenAI, Groq, Mistral... Their models are "ep:<id>:<model>",
e.g. "ep:ollama:llama3.1:8b"; the key (optional for local servers) comes from the endpoint's key_env variable.
"""
import json
import re
import urllib.parse

URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_PREFIX = "gemini:"                     # direct Gemini API models are "gemini:<id>", e.g. gemini:gemini-2.5-flash
ENDPOINT_PREFIX = "ep:"                       # custom OpenAI-compatible endpoints: "ep:<endpoint id>:<model id>"
ENDPOINT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CACHE_VENDORS = ("anthropic/", "google/")      # honour cache_control breakpoints in content parts


def is_gemini(model):
    return (model or "").startswith(GEMINI_PREFIX)


def is_endpoint(model):
    return (model or "").startswith(ENDPOINT_PREFIX)


def is_openrouter(model):
    return "/" in (model or "") and not is_gemini(model) and not is_endpoint(model)


def split_endpoint(model):
    """'ep:ollama:llama3.1:8b' -> ('ollama', 'llama3.1:8b')."""
    eid, _, mid = (model or "")[len(ENDPOINT_PREFIX):].partition(":")
    return eid, mid


# The built-in vendors' keys and the hosts they may be sent to. A custom endpoint naming one of them as its key_env
# gets it only when its base_url is that vendor's own host (an OpenAI-compatible endpoint at api.openai.com may use
# OPENAI_API_KEY); anywhere else it gets its own GLIDE_ENDPOINT_<ID>_KEY instead, so a config.json someone hands the
# player can't send their Anthropic key to a server of its own.
VENDOR_KEY_HOSTS = {
    "ANTHROPIC_API_KEY": ("api.anthropic.com",), "OPENROUTER_API_KEY": ("openrouter.ai",),
    "GEMINI_API_KEY": ("generativelanguage.googleapis.com",), "GOOGLE_API_KEY": ("generativelanguage.googleapis.com",),
    "OPENAI_API_KEY": ("api.openai.com",), "GROQ_API_KEY": ("api.groq.com",), "DEEPGRAM_API_KEY": ("api.deepgram.com",),
    "TAVILY_API_KEY": (), "BRAVE_API_KEY": (), "ELEVENLABS_API_KEY": (), "AZURE_SPEECH_KEY": (), "GOOGLE_TTS_API_KEY": (),
}
KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def endpoint_key_env(eid, url, key_env):
    """The variable an endpoint's key is read from: its key_env, unless that's another vendor's key (or not a valid
    name): then the endpoint's own GLIDE_ENDPOINT_<ID>_KEY."""
    own = "GLIDE_ENDPOINT_%s_KEY" % eid.upper().replace("-", "_")
    env = str(key_env or own)
    if not KEY_ENV_RE.match(env):
        return own
    hosts = VENDOR_KEY_HOSTS.get(env)
    if hosts is not None and (urllib.parse.urlsplit(url).hostname or "").lower() not in hosts:
        return own
    return env


def endpoints(cfg):
    """The valid custom endpoints of an "llm" config: {id: {"id", "label", "base_url", "key_env"}}."""
    out = {}
    for e in (cfg or {}).get("endpoints") or ():
        if not isinstance(e, dict):
            continue
        eid, url = str(e.get("id") or "").strip().lower(), str(e.get("base_url") or "").strip().rstrip("/")
        if ENDPOINT_ID_RE.match(eid) and url.startswith(("http://", "https://")):
            out[eid] = {"id": eid, "label": str(e.get("label") or eid), "base_url": url,
                        "key_env": endpoint_key_env(eid, url, e.get("key_env"))}
    return out


def gemini_effort(model, deep):
    """reasoning_effort for Gemini's OpenAI endpoint: live callouts and spoken answers think as little as the model
    allows (2.5 models can turn thinking off; 3.x models go to minimal); deep calls use the model's default."""
    if deep:
        return {}
    m = model[len(GEMINI_PREFIX):] if is_gemini(model) else model
    if "pro" in m:
        return {"reasoning_effort": "low"}
    return {"reasoning_effort": "none" if m.startswith("gemini-2.") else "minimal"}


META_BLOCK = "openai_meta"          # carries OpenRouter reasoning_details across a tool loop (never sent to Anthropic)
# Google's documented placeholder for a function call whose thought signature wasn't kept
DUMMY_SIGNATURE = "skip_thought_signature_validator"


def _signature(extra):
    return isinstance(extra, dict) and isinstance(extra.get("google"), dict) and extra["google"].get("thought_signature")


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or () if isinstance(b, dict) and b.get("type") == "text")


def to_openai(body, flavor="openrouter"):
    """Anthropic Messages body -> OpenAI-style chat/completions body (flavor "openrouter" or "gemini")."""
    model = body["model"]
    cache = flavor == "openrouter" and model.startswith(CACHE_VENDORS)
    if flavor == "gemini" and is_gemini(model):
        model = model[len(GEMINI_PREFIX):]
    if flavor == "endpoint" and is_endpoint(model):
        model = split_endpoint(model)[1]
    msgs = []
    system = body.get("system")
    if system:
        if cache and isinstance(system, list):
            def marker(cc):          # the 1-hour TTL is Anthropic-only; Google models via OpenRouter get 5 minutes
                return cc if model.startswith("anthropic/") else {k: v for k, v in cc.items() if k != "ttl"}
            parts = [{"type": "text", "text": b.get("text", ""), **({"cache_control": marker(b["cache_control"])}
                                                                   if b.get("cache_control") else {})}
                     for b in system if isinstance(b, dict)]
            msgs.append({"role": "system", "content": parts})
        else:
            msgs.append({"role": "system", "content": _text(system)})
    for m in body.get("messages") or ():
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        blocks = [b for b in content or () if isinstance(b, dict)]
        if role == "assistant":
            calls = []
            for b in blocks:
                if b.get("type") != "tool_use":
                    continue
                call = {"id": b.get("id"), "type": "function",
                        "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}}
                # Gemini 3 thinking models need each step's thought signature sent back with its function call
                extra = b.get("extra_content")
                if flavor == "gemini" and not calls and not _signature(extra):
                    extra = {"google": {"thought_signature": DUMMY_SIGNATURE}}
                if extra:
                    call["extra_content"] = extra
                calls.append(call)
            out = {"role": "assistant", "content": _text(blocks) or None}
            if calls:
                out["tool_calls"] = calls
            meta = next((b for b in blocks if b.get("type") == META_BLOCK), None)
            if meta and meta.get("reasoning_details") and flavor == "openrouter":
                out["reasoning_details"] = meta["reasoning_details"]      # OpenRouter: Gemini 3 / reasoning models
            msgs.append(out)
            continue
        # user turn: tool results become "tool" messages, any text a user message
        for b in blocks:
            if b.get("type") == "tool_result":
                c = b.get("content")
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                             "content": c if isinstance(c, str) else json.dumps(c)})
        txt = _text(blocks)
        if txt and cache and any(b.get("cache_control") for b in blocks if b.get("type") == "text"):
            # keep the user message's cache breakpoint (its REFERENCE part) for the vendors that honour it
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": b.get("text", ""), **({"cache_control": {"type": "ephemeral"}}
                                                              if b.get("cache_control") else {})}
                for b in blocks if b.get("type") == "text"]})
        elif txt:
            msgs.append({"role": "user", "content": txt})
    out = {"model": model, "messages": msgs, "max_tokens": body.get("max_tokens")}
    if flavor == "endpoint" and model.startswith("gpt-6"):
        # GPT-6 Chat Completions requires non-reasoning mode for the coach's function calls.
        out["max_completion_tokens"] = out.pop("max_tokens")
        out["reasoning_effort"] = "none"
    if flavor == "openrouter":
        out["usage"] = {"include": True}
    tools = body.get("tools")
    if tools:
        out["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                          "parameters": t.get("input_schema") or {"type": "object"}}}
                        for t in tools]
        tc = (body.get("tool_choice") or {}).get("type", "auto")
        out["tool_choice"] = {"auto": "auto", "none": "none", "any": "required"}.get(tc, "auto")
    return out


def from_openai(data):
    """OpenRouter chat/completions response -> Anthropic Messages-shaped dict (content blocks, stop_reason,
    usage with input/output/cache_read tokens, cost_usd when OpenRouter reports it)."""
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        raise RuntimeError(json.dumps(data["error"])[:300])
    choices = data.get("choices") or []
    msg = (choices[0].get("message") if choices else None) or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"] if isinstance(msg["content"], str) else _text(msg["content"])})
    for c in msg.get("tool_calls") or ():
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        block = {"type": "tool_use", "id": c.get("id"), "name": fn.get("name"), "input": args}
        if c.get("extra_content"):
            block["extra_content"] = c["extra_content"]           # Gemini thought signature: send it back
        content.append(block)
    if msg.get("reasoning_details") and any(b["type"] == "tool_use" for b in content):
        content.append({"type": META_BLOCK, "reasoning_details": msg["reasoning_details"]})
    finish = (choices[0].get("finish_reason") if choices else None) or ""
    stop = {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn"}.get(finish, "end_turn")
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    u = data.get("usage") or {}
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
    usage = {"input_tokens": max(0, (u.get("prompt_tokens") or 0) - cached), "output_tokens": u.get("completion_tokens") or 0,
             "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
    if u.get("cost") is not None:
        usage["cost_usd"] = float(u["cost"])
    return {"content": content, "stop_reason": stop, "usage": usage, "model": data.get("model")}


def fatal(code, detail):
    """OpenRouter errors that won't fix themselves -> short reason, else None."""
    d = (detail or "").lower()
    if code == 402 or "insufficient credits" in d or "more credits" in d:
        return "out of API credits"
    if code == 401 or "no auth credentials" in d or "invalid api key" in d or "user not found" in d:
        return "API key rejected"
    return None


def gemini_fatal(code, detail):
    """Gemini API errors that won't fix themselves -> short reason, else None (quota/rate limits back off)."""
    d = (detail or "").lower()
    if "api key not valid" in d or "api_key_invalid" in d or code == 401:
        return "API key rejected"
    if "billing" in d or "prepayment" in d or "credits" in d and "exhausted" in d:
        return "out of API credits"
    if code == 403 or "permission_denied" in d:
        return "API access denied"
    return None
