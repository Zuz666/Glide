"""Translate canonical engine messages to/from stateless, non-streaming OpenAI Responses.

Wire fields: https://developers.openai.com/api/reference/resources/responses/methods/create
Reasoning replay: https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-across-calls
The private output block belongs only in canonical history, never another provider's request.
"""
import json

from openai_compat import is_endpoint, split_endpoint

META_BLOCK = "openai_responses_output"


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or () if isinstance(b, dict) and b.get("type") == "text")


def to_responses(body, effort=None):
    """Canonical Messages body -> Responses JSON; None/auto leaves reasoning at the model default."""
    model = body["model"]
    if is_endpoint(model):
        model = split_endpoint(model)[1]
    items = []
    for message in body.get("messages") or ():
        role, content = message.get("role"), message.get("content")
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        blocks = [b for b in content or () if isinstance(b, dict)]
        if role == "assistant":
            original = next((b for b in blocks if b.get("type") == META_BLOCK), None)
            if original is not None:
                # These items already contain the normalized text/calls below, including their identities.
                # Reconstructing them would duplicate calls and lose encrypted reasoning and message phase.
                items.extend(original.get("items") or [])
                continue
            for block in blocks:
                if block.get("type") == "text":
                    items.append({"role": "assistant", "content": block.get("text", "")})
                elif block.get("type") == "tool_use":
                    items.append({"type": "function_call", "call_id": block.get("id"), "name": block.get("name"),
                                  "arguments": json.dumps(block.get("input") or {})})
            continue
        # Match the Chat adapter: tool outputs first, then any ordinary user text.
        for block in blocks:
            if block.get("type") == "tool_result":
                result = block.get("content")
                items.append({"type": "function_call_output", "call_id": block.get("tool_use_id"),
                              "output": result if isinstance(result, str) else json.dumps(result)})
        text = _text(blocks)
        if text:
            items.append({"role": role, "content": text})
    out = {"model": model, "input": items, "max_output_tokens": body.get("max_tokens"),
           "store": False, "include": ["reasoning.encrypted_content"]}
    if body.get("system"):
        out["instructions"] = _text(body["system"])
    if effort is not None and effort != "auto":
        out["reasoning"] = {"effort": effort}
    tools = body.get("tools")
    if tools:
        out["tools"] = [{"type": "function", "name": tool["name"], "description": tool.get("description", ""),
                         "parameters": tool.get("input_schema") or {"type": "object"}, "strict": False}
                        for tool in tools]
        choice = (body.get("tool_choice") or {}).get("type", "auto")
        out["tool_choice"] = {"auto": "auto", "none": "none", "any": "required"}.get(choice, "auto")
    return out


def from_responses(data):
    """Responses JSON -> canonical content/stop/usage, retaining all output items for the next turn."""
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        raise RuntimeError(json.dumps(data["error"])[:300])
    if data.get("object") == "error":
        raise RuntimeError(json.dumps(data)[:300])
    status = data.get("status")
    if status is not None and status not in ("completed", "incomplete"):
        raise RuntimeError("OpenAI Responses returned status %s" % status)
    items = data.get("output") or []
    content = []
    for item in items:
        if item.get("type") == "message":
            for part in item.get("content") or ():
                if part.get("type") == "output_text" and part.get("text"):
                    content.append({"type": "text", "text": part["text"]})
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except ValueError:
                args = {}
            # Tool results reference call_id, not the separate fc_* output item identity.
            content.append({"type": "tool_use", "id": item.get("call_id"), "name": item.get("name"), "input": args})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    if status == "incomplete" and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
        stop = "max_tokens"
    if items:
        content.append({"type": META_BLOCK, "items": items})
    usage = data.get("usage") or {}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    canonical_usage = {"input_tokens": max(0, (usage.get("input_tokens") or 0) - cached),
                       "output_tokens": usage.get("output_tokens") or 0,
                       "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
    return {"content": content, "stop_reason": stop, "usage": canonical_usage, "model": data.get("model")}
