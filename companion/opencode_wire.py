"""Pure OpenCode native codecs; opaque continuation belongs to one gateway model/API.

Only canonical text and tools cross a model boundary. Signed Claude blocks, full
Responses output, Gemini Parts and Chat reasoning stay in scoped opencode_meta.
No codec selects a URL, makes a request, or changes the caller's output budget.
"""
import copy
import hashlib
import json

import opencode
import openai_compat
import openai_responses

_TRANSPORTS = ("anthropic", "responses", "gemini", "chat")
_PUBLIC_FIELDS = {
    "text": ("type", "text", "cache_control", "citations"),
    "tool_use": ("type", "id", "name", "input", "cache_control"),
    "tool_result": ("type", "tool_use_id", "content", "is_error", "cache_control"),
    "image": ("type", "source", "cache_control"),
    "document": ("type", "source", "title", "context", "citations", "cache_control"),
}


def _native(model, transport):
    if not isinstance(model, str) or not model.startswith(opencode.PREFIX) or model == opencode.PREFIX:
        raise ValueError("OpenCode codecs require an opencode: model")
    if transport not in _TRANSPORTS:
        raise ValueError("Unknown OpenCode native transport: %s" % transport)
    return model[len(opencode.PREFIX):]


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or ()
                   if isinstance(b, dict) and b.get("type") == "text")


def _public(block):
    fields = _PUBLIC_FIELDS.get(block.get("type"))
    if fields is None:
        return None
    clean = {key: copy.deepcopy(block[key]) for key in fields if key in block}
    if block.get("type") == "tool_result" and isinstance(clean.get("content"), list):
        clean["content"] = [item for b in clean["content"] if isinstance(b, dict)
                            if (item := _public(b)) is not None]
    return clean


def _meta(model, transport, **payload):
    return {"type": opencode.META_BLOCK, "model": model, "transport": transport, **copy.deepcopy(payload)}


def _scoped(block, model, transport):
    return (block.get("type") == opencode.META_BLOCK
            and block.get("model") == model and block.get("transport") == transport)


def _history(body, transport):
    messages = []
    for message in body.get("messages") or ():
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if message.get("role") == "assistant" and _scoped(block, body["model"], transport):
                    if transport == "anthropic":
                        thinking = block.get("anthropic_thinking")
                        if isinstance(thinking, dict) and thinking.get("type") in ("thinking", "redacted_thinking"):
                            blocks.append(copy.deepcopy(thinking))
                    else:
                        blocks.append(block)
                elif (public := _public(block)) is not None:
                    blocks.append(public)
            content = blocks
        messages.append({"role": message.get("role"), "content": content})
    return messages


def _merge(target, values):
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _format(body, options):
    return ((options.get("output_config") or {}).get("format")
            or (body.get("output_config") or {}).get("format"))


def _json_format(fmt):
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") != "json_schema" or not isinstance(fmt.get("schema"), dict):
        raise ValueError("OpenCode structured output requires a JSON schema object")
    # Do not turn optional properties into required ones, or guess a stricter schema.
    return {"type": "json_schema", "name": fmt.get("name") or "response", "schema": copy.deepcopy(fmt["schema"]),
            "strict": fmt.get("strict", False)}


def _responses(body, messages):
    # Reuse tool/system conversion, but never disguise gateway state as direct OpenAI metadata.
    out = openai_responses.to_responses(dict(body, messages=[]))
    items = out["input"]
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        blocks = content or []
        if role == "assistant":
            original = next((b for b in blocks if _scoped(b, body["model"], "responses")), None)
            if original is not None:
                items.extend(copy.deepcopy(original.get("items") or []))
                continue
            for block in blocks:
                if block.get("type") == "text":
                    items.append({"role": "assistant", "content": block.get("text", "")})
                elif block.get("type") == "tool_use":
                    items.append({"type": "function_call", "call_id": block.get("id"), "name": block.get("name"),
                                  "arguments": json.dumps(block.get("input") or {})})
        else:
            for block in blocks:
                if block.get("type") == "tool_result":
                    result = block.get("content")
                    items.append({"type": "function_call_output", "call_id": block.get("tool_use_id"),
                                  "output": result if isinstance(result, str) else json.dumps(result)})
            text = _text(blocks)
            if text:
                items.append({"role": role, "content": text})
    choice = body.get("tool_choice") or {}
    if body.get("tools") and choice.get("type") == "tool":
        out["tool_choice"] = {"type": "function", "name": choice["name"]}
    return out


def _gemini(body, messages):
    contents, calls = [], {}
    for message in messages:
        role = "model" if message["role"] == "assistant" else "user"
        content = message["content"]
        if isinstance(content, str):
            contents.append({"role": role, "parts": [{"text": content}]})
            continue
        blocks = content or []
        original = next((b for b in blocks if role == "model" and _scoped(b, body["model"], "gemini")), None)
        if original is not None:
            native_content = copy.deepcopy(original["gemini_content"])
            native_calls = [part["functionCall"] for part in native_content.get("parts") or []
                            if isinstance(part, dict) and "functionCall" in part]
            ids = original.get("tool_ids") or []
            if len(ids) != len(native_calls):
                raise ValueError("OpenCode Gemini continuation has mismatched tool identities")
            for tool_id, call in zip(ids, native_calls):
                calls[tool_id] = call
            contents.append(native_content)
            continue
        parts = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                parts.append({"text": block.get("text", "")})
            elif kind == "tool_use" and role == "model":
                call = {"id": block.get("id"), "name": block.get("name"), "args": block.get("input") or {}}
                calls[block.get("id")] = call
                parts.append({"functionCall": call})
            elif kind == "tool_result":
                tool_id = block.get("tool_use_id")
                if tool_id not in calls:
                    raise ValueError("OpenCode Gemini tool result has no matching function call")
                call, result = calls[tool_id], block.get("content")
                response = {"name": call["name"], "response":
                            {"error": result} if block.get("is_error") else
                            result if isinstance(result, dict) else {"output": result}}
                if "id" in call:
                    response["id"] = call["id"]
                parts.append({"functionResponse": response})
        if parts:
            contents.append({"role": role, "parts": parts})
    out = {"contents": contents, "generationConfig": {"maxOutputTokens": body.get("max_tokens")}}
    if body.get("system"):
        system = body["system"]
        out["systemInstruction"] = {"parts": [{"text": system}] if isinstance(system, str) else
                                    [{"text": b.get("text", "")} for b in system
                                     if isinstance(b, dict) and b.get("type") == "text"]}
    if body.get("tools"):
        out["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t.get("description", ""),
             "parametersJsonSchema": copy.deepcopy(t.get("input_schema") or {"type": "object"})}
            for t in body["tools"]]}]
        choice = body.get("tool_choice") or {}
        config: dict[str, str | list[str]] = {
            "mode": {"auto": "AUTO", "none": "NONE", "any": "ANY", "tool": "ANY"}.get(choice.get("type", "auto"), "AUTO")}
        if choice.get("type") == "tool":
            config["allowedFunctionNames"] = [choice["name"]]
        out["toolConfig"] = {"functionCallingConfig": config}
    return out


def encode(body, transport, options):
    """Canonical Messages body -> one non-streaming native request, without network effects."""
    native = _native(body.get("model"), transport)
    messages = _history(body, transport)
    options = options or {}
    allowed = {"anthropic": {"output_config", "thinking"}, "responses": {"reasoning", "text", "output_config"},
               "gemini": {"generationConfig", "output_config"}, "chat": {"reasoning_effort", "response_format", "output_config"}}
    if set(options) - allowed[transport]:
        raise ValueError("Controls do not belong to OpenCode %s transport" % transport)
    if transport != "anthropic" and set(options.get("output_config") or {}) - {"format"}:
        raise ValueError("Claude effort does not belong to OpenCode %s transport" % transport)
    if transport == "anthropic":
        out = {key: copy.deepcopy(body[key]) for key in
               ("max_tokens", "system", "tools", "tool_choice", "temperature", "top_p", "top_k", "stop_sequences",
                "metadata") if key in body}
        out.update(model=native, messages=messages)
        if (body.get("output_config") or {}).get("format"):
            out["output_config"] = {"format": copy.deepcopy(body["output_config"]["format"])}
    elif transport == "responses":
        out = _responses(body, messages)
        out["model"] = native
    elif transport == "gemini":
        out = _gemini(body, messages)
    else:
        out = openai_compat.to_openai(dict(body, messages=messages), "opencode")
        choice = body.get("tool_choice") or {}
        if body.get("tools") and choice.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    _merge(out, {key: value for key, value in options.items() if transport == "anthropic" or key != "output_config"})
    fmt = _format(body, options)
    if fmt and transport != "anthropic":
        native_format = _json_format(fmt)
        if transport == "responses":
            out.setdefault("text", {})["format"] = native_format
        elif transport == "chat":
            out["response_format"] = ({"type": "json_object"} if native_format["type"] == "json_object" else
                                      {"type": "json_schema", "json_schema":
                                       {key: value for key, value in native_format.items() if key != "type"}})
        else:
            out["generationConfig"]["responseMimeType"] = "application/json"
            if "schema" in native_format:
                out["generationConfig"]["responseJsonSchema"] = native_format["schema"]
    # Native effort and structured-output controls cannot replace the requested cap.
    if transport == "gemini":
        out["generationConfig"]["maxOutputTokens"] = body.get("max_tokens")
    else:
        out[{"anthropic": "max_tokens", "responses": "max_output_tokens", "chat": "max_tokens"}[transport]] = body.get("max_tokens")
    return out


def _error(data):
    if not isinstance(data, dict):
        raise RuntimeError("OpenCode returned a non-object response")
    if data.get("error") or data.get("type") == "error" or data.get("object") == "error":
        raise RuntimeError(json.dumps(data.get("error") or data)[:300])


def _arguments(value):
    try:
        args = json.loads(value) if isinstance(value, str) else value
    except ValueError as exc:
        raise RuntimeError("OpenCode returned invalid function arguments") from exc
    if not isinstance(args, dict):
        raise RuntimeError("OpenCode function arguments must be an object")
    return args


def _tool(tool_id, name, args):
    if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str) or not name:
        raise RuntimeError("OpenCode returned a function call without an identity or name")
    return {"type": "tool_use", "id": tool_id, "name": name, "input": _arguments(args)}


def _decode_responses(data, model):
    # The shared decoder handles native status/cap semantics. Gateway provenance stays private here.
    for item in data.get("output") or []:
        if item.get("type") == "message" and any(part.get("type") == "refusal" for part in item.get("content") or []):
            raise RuntimeError("OpenCode Responses refused the request")
        if item.get("type") == "function_call":
            _tool(item.get("call_id"), item.get("name"), item.get("arguments", "{}"))
    if (data.get("incomplete_details") or {}).get("reason") == "content_filter":
        raise RuntimeError("OpenCode Responses refused the request: content_filter")
    parsed = openai_responses.from_responses(data)
    assert parsed is not None  # _error already established a dictionary response.
    content = [b for b in parsed["content"] if b.get("type") != openai_responses.META_BLOCK]
    if data.get("output"):
        content.append(_meta(model, "responses", items=data["output"]))
    return content, parsed["stop_reason"]


def _decode_chat(data, model):
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    if message.get("refusal") or choice.get("finish_reason") == "content_filter":
        raise RuntimeError("OpenCode Chat refused the request")
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        _tool(call.get("id"), fn.get("name"), fn.get("arguments", "{}"))
    parsed = openai_compat.from_openai(data, "opencode")
    assert parsed is not None  # _error already established a dictionary response.
    content = parsed["content"]
    reasoning = {key: message[key] for key in ("reasoning_content", "reasoning_details") if key in message}
    if reasoning:
        content.append(_meta(model, "chat", **reasoning))
    stop = "max_tokens" if choice.get("finish_reason") == "length" else parsed["stop_reason"]
    return content, stop


def _decode_gemini(data, model):
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason") not in (None, "BLOCK_REASON_UNSPECIFIED"):
        raise RuntimeError("OpenCode Gemini refused the prompt: %s" % feedback["blockReason"])
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    finish = candidate.get("finishReason")
    if finish not in (None, "STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"):
        raise RuntimeError("OpenCode Gemini stopped: %s" % finish)
    native_content = candidate.get("content", {"role": "model", "parts": []})
    if not isinstance(native_content, dict):
        raise RuntimeError("OpenCode Gemini returned invalid content")
    parts = native_content.get("parts") or []
    # A response ID distinguishes repeated identical tool calls in successive steps;
    # the deterministic content digest also works for older replies without one.
    identity = json.dumps([model, data.get("responseId"), native_content], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    content, ids = [], []
    for index, part in enumerate(parts):
        if "text" in part and not part.get("thought"):
            content.append({"type": "text", "text": part["text"]})
        if "functionCall" in part:
            call = part["functionCall"]
            tool_id = call["id"] if "id" in call else "gemini_" + digest + "_" + str(index)
            content.append(_tool(tool_id, call.get("name"), call.get("args", {})))
            ids.append(tool_id)
    if "content" in candidate:
        content.append(_meta(model, "gemini", gemini_content=native_content, tool_ids=ids))
    stop = "max_tokens" if finish == "MAX_TOKENS" else "tool_use" if ids else "end_turn"
    return content, stop


def _decode_content(data, model, transport):
    _error(data)
    if transport == "anthropic":
        if data.get("stop_reason") == "refusal":
            raise RuntimeError("OpenCode Claude refused the request")
        content = []
        for block in data.get("content") or []:
            if block.get("type") in ("thinking", "redacted_thinking"):
                content.append(_meta(model, transport, anthropic_thinking=block))
            elif block.get("type") == "tool_use":
                content.append(_tool(block.get("id"), block.get("name"), block.get("input", {})))
            elif (public := _public(block)) is not None:
                content.append(public)
        stop = data.get("stop_reason") or ("tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn")
    elif transport == "responses":
        content, stop = _decode_responses(data, model)
    elif transport == "gemini":
        content, stop = _decode_gemini(data, model)
    else:
        content, stop = _decode_chat(data, model)
    return content, stop


def decode(data, model, transport):
    """Decode native history; retain billing even when refusal/schema errors prevent an answer."""
    _native(model, transport)
    raw_usage = data.get("usageMetadata" if transport == "gemini" else "usage") if isinstance(data, dict) else None
    usage = opencode.normalize_usage(raw_usage, api=transport)
    try:
        content, stop = _decode_content(data, model, transport)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise opencode.ResponseError(str(error), usage) from error
    reported = data.get("modelVersion" if transport == "gemini" else "model")
    return {"content": content, "stop_reason": stop, "usage": usage, "model": reported}
