#!/usr/bin/env python3
"""Pure Responses conversion: stateless reasoning, parallel tools, accounting and failures; no API calls."""
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import openai_compat  # noqa: E402
from openai_responses import from_responses, to_responses  # noqa: E402


# The engine runs normalized tool_use blocks, then appends their results to the same conversation.
original_items = [
    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-private-reasoning"},
    {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "phase": "commentary",
     "content": [{"type": "output_text", "text": "Checking both routes.", "annotations": []}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_route", "name": "route", "arguments": '{"zone":7}',
     "status": "completed"},
    {"type": "function_call", "id": "fc_2", "call_id": "call_quest", "name": "quest", "arguments": '{"quest":42}',
     "status": "completed"},
]
reply = from_responses({"model": "gpt-6.1-sol", "status": "completed", "output": original_items})
assert reply["stop_reason"] == "tool_use", reply
uses = [block for block in reply["content"] if block["type"] == "tool_use"]
assert uses == [{"type": "tool_use", "id": "call_route", "name": "route", "input": {"zone": 7}},
                {"type": "tool_use", "id": "call_quest", "name": "quest", "input": {"quest": 42}}], uses
assert [block for block in reply["content"] if block["type"] == "text"] == [
    {"type": "text", "text": "Checking both routes."}], reply
metadata = [block for block in reply["content"] if block["type"] == "openai_responses_output"]
assert metadata == [{"type": "openai_responses_output", "items": original_items}], metadata
conversation = [{"role": "user", "content": "Which route?"},
                {"role": "assistant", "content": reply["content"]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": uses[0]["id"], "content": '{"distance":120}'},
                    {"type": "tool_result", "tool_use_id": uses[1]["id"], "content": {"error": "not available"},
                     "is_error": True},
                    {"type": "text", "text": "Use what is available."}]}]
# Exercise the JSON boundary rather than relying on shared object identity to preserve the items.
body = json.loads(json.dumps({"model": "ep:openai:gpt-6.1-sol", "messages": conversation, "max_tokens": 512}))
request = to_responses(body, "low")
assert request["model"] == "gpt-6.1-sol" and request["max_output_tokens"] == 512, request
assert request["store"] is False and request["include"] == ["reasoning.encrypted_content"], request
assert request["reasoning"] == {"effort": "low"}, request
assert request["input"] == [{"role": "user", "content": "Which route?"}, *original_items,
                            {"type": "function_call_output", "call_id": "call_route", "output": '{"distance":120}'},
                            {"type": "function_call_output", "call_id": "call_quest",
                             "output": '{"error": "not available"}'},
                            {"role": "user", "content": "Use what is available."}], request
assert "previous_response_id" not in request and "stream" not in request, request
# Existing Chat adapters should see only the normalized blocks, never the private Responses payload.
for flavor in ("openrouter", "gemini", "endpoint"):
    chat = openai_compat.to_openai(body, flavor)
    assert "opaque-private-reasoning" not in json.dumps(chat), chat
    assert len(chat["messages"][1]["tool_calls"]) == 2, chat


# Ordinary text remains usable without any tool definitions or opaque state.
plain = {"model": "ep:openai:model:variant", "max_tokens": 64,
         "system": [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}],
         "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello."}]},
                      {"role": "assistant", "content": "Hi."}, {"role": "user", "content": "Next?"}]}
plain_request = to_responses(plain)
assert plain_request["model"] == "model:variant" and plain_request["instructions"] == "Be brief.", plain_request
assert plain_request["input"] == [{"role": "user", "content": "Hello."},
                                  {"role": "assistant", "content": "Hi."},
                                  {"role": "user", "content": "Next?"}], plain_request
assert "reasoning" not in plain_request and "tools" not in plain_request, plain_request
assert "reasoning" not in to_responses(plain, "auto"), plain_request
for effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
    assert to_responses(plain, effort)["reasoning"] == {"effort": effort}, effort
text_items = [{"type": "message", "id": "msg_final", "role": "assistant", "status": "completed", "content": [
    {"type": "output_text", "text": "Go east. ", "annotations": []},
    {"type": "output_text", "text": "Then turn north.", "annotations": []}]}]
text_reply = from_responses({"model": "gpt-6.1-sol", "status": "completed", "output": text_items,
                             "usage": {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 90},
                                       "output_tokens": 25, "output_tokens_details": {"reasoning_tokens": 20}}})
assert text_reply["stop_reason"] == "end_turn" and text_reply["model"] == "gpt-6.1-sol", text_reply
assert "".join(block["text"] for block in text_reply["content"] if block["type"] == "text") == \
    "Go east. Then turn north.", text_reply
assert text_reply["usage"] == {"input_tokens": 30, "output_tokens": 25,
                               "cache_read_input_tokens": 90, "cache_creation_input_tokens": 0}, text_reply
text_replay = to_responses({**plain, "messages": [{"role": "assistant", "content": text_reply["content"]}]})
assert text_replay["input"] == text_items, text_replay
assert from_responses({"status": "completed", "output": []})["usage"] == {
    "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
clamped = from_responses({"status": "completed", "usage": {"input_tokens": 4,
                                                         "input_tokens_details": {"cached_tokens": 8}}})
assert clamped["usage"]["input_tokens"] == 0 and clamped["usage"]["cache_read_input_tokens"] == 8, clamped


# Function schemas remain permissive; optional fields must not become required during migration.
schema = {"type": "object", "properties": {"zone": {"type": "integer"}}, "required": []}
tooled = {**plain, "tools": [{"name": "route", "description": "Find a route", "input_schema": schema}]}
for canonical, wire in (("auto", "auto"), ("none", "none"), ("any", "required")):
    tool_request = to_responses({**tooled, "tool_choice": {"type": canonical}})
    assert tool_request["tool_choice"] == wire, tool_request
    assert tool_request["tools"] == [{"type": "function", "name": "route", "description": "Find a route",
                                      "parameters": schema, "strict": False}], tool_request
assert schema == {"type": "object", "properties": {"zone": {"type": "integer"}}, "required": []}, schema
# History without Responses metadata still reconstructs function calls using canonical call IDs.
legacy = to_responses({**plain, "messages": [{"role": "assistant", "content": [
    {"type": "text", "text": "Checking."},
    {"type": "tool_use", "id": "old_call", "name": "route", "input": {"zone": 7}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old_call",
                                  "content": [{"type": "text", "text": "Found."}]}]}]})
assert legacy["input"] == [{"role": "assistant", "content": "Checking."},
                           {"type": "function_call", "call_id": "old_call", "name": "route",
                            "arguments": '{"zone": 7}'},
                           {"type": "function_call_output", "call_id": "old_call",
                            "output": '[{"type": "text", "text": "Found."}]'}], legacy


# Hidden reasoning can exhaust the original budget before any answer; never report end_turn/tool_use here.
for items in ([original_items[0]], text_items, [original_items[0], original_items[2]]):
    incomplete = from_responses({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                 "output": items, "usage": {"input_tokens": 75, "output_tokens": 64}})
    assert incomplete["stop_reason"] == "max_tokens" and incomplete["usage"]["output_tokens"] == 64, incomplete


for failed, detail in (({"status": "failed", "error": {"code": "server_error", "message": "Model failed"}},
                        "Model failed"),
                       ({"status": "failed"}, "failed"),
                       ({"status": "cancelled"}, "cancelled"),
                       ({"status": "error"}, "error"),
                       ({"object": "error", "message": "Request rejected"}, "Request rejected"),
                       ({"error": {"message": "Unsupported effort low"}}, "Unsupported effort low"),
                       ({"status": "completed", "error": {"message": "Not actually successful"}},
                        "Not actually successful"),
                       ({"status": "in_progress"}, "in_progress"),
                       ({"status": "queued"}, "queued")):
    try:
        from_responses({"output": text_items, **failed})
    except RuntimeError as error:
        assert detail in str(error), error
    else:
        raise AssertionError("Responses error was disguised as a successful answer: %r" % failed)

print("openai_responses: OK")
