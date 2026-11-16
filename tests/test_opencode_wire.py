#!/usr/bin/env python3
"""Offline public-codec regressions for native gateway reasoning and tool continuation."""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import openai_compat  # noqa: E402
import openai_responses  # noqa: E402
from opencode_wire import decode, encode  # noqa: E402

MODELS = {"anthropic": "opencode:claude-haiku-5-5", "responses": "opencode:gpt-6.1-sol",
          "gemini": "opencode:gemini-4-carbon", "chat": "opencode:deepseek-v4.1-flash"}
SCHEMA = {"type": "object", "properties": {"zone": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
          "required": [], "additionalProperties": False}
TOOLS = [{"name": "route", "description": "Find a route", "input_schema": SCHEMA},
         {"name": "quest", "description": "Find a quest", "input_schema": {"type": "object"}}]


def body(transport, messages=None, **extra):
    return {"model": MODELS[transport], "max_tokens": 512, "system": [{"type": "text", "text": "Be brief."}],
            "tools": copy.deepcopy(TOOLS), "messages": messages or [{"role": "user", "content": "Which route?"}], **extra}


def roundtrip(value):
    return json.loads(json.dumps(value))


def visible(reply):
    return [block for block in reply["content"] if block["type"] in ("text", "tool_use")]


def uses(reply):
    return [block for block in reply["content"] if block["type"] == "tool_use"]


def results(reply):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block["id"],
                                           "content": "  result " + str(index) + "\n"}
                                          for index, block in enumerate(uses(reply))]}


def response_fixture():
    return {"model": "untrusted-echo", "status": "completed", "output": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "  encrypted\nopaque  "},
        {"type": "message", "id": "msg_1", "role": "assistant", "phase": "commentary", "status": "completed",
         "content": [{"type": "output_text", "text": "Checking.", "annotations": []}]},
        {"type": "function_call", "id": "fc_route", "call_id": "call_route", "name": "route",
         "arguments": '{ "zone": 7 }', "status": "completed"},
        {"type": "function_call", "id": "fc_quest", "call_id": "call_quest", "name": "quest",
         "arguments": '{"quest":42}', "status": "completed"}]}


def gemini_fixture():
    return {"modelVersion": "untrusted-echo", "responseId": "response-a", "candidates": [{"finishReason": "STOP",
        "content": {"role": "model", "parts": [
            {"text": "private thought", "thought": True, "thoughtSignature": "  thought-signature\n"},
            {"text": "", "thoughtSignature": "  empty-text-signature  "},
            {"functionCall": {"name": "route", "args": {"zone": 7}}, "thoughtSignature": "  signature-a\n"},
            {"functionCall": {"id": "native-quest", "name": "quest", "args": {"quest": 42}}},
            {}, {"text": "Checking."}, {"thoughtSignature": "  final-signature  "}]}}]}


def claude_fixture():
    return {"type": "message", "model": "untrusted-echo", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "", "signature": "  signed-empty\n"},
        {"type": "redacted_thinking", "data": "  redacted\nopaque  "},
        {"type": "text", "text": "Checking."},
        {"type": "thinking", "thinking": "private thought", "signature": " signed-middle "},
        {"type": "tool_use", "id": "call_route", "name": "route", "input": {"zone": 7}}]}


def chat_fixture():
    return {"model": "untrusted-echo", "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Checking.", "reasoning_content": "  private thought\n",
        "reasoning_details": [{"type": "reasoning.encrypted", "data": "  encrypted\n", "index": 0},
                              {"type": "reasoning.text", "text": "", "signature": "signature"}],
        "tool_calls": [{"id": "call_route", "type": "function", "function": {"name": "route", "arguments": '{"zone":7}'},
                        "extra_content": {"google": {"thought_signature": "foreign-tool-extra"}}},
                       {"id": "call_quest", "type": "function", "function": {"name": "quest", "arguments": '{}'}}]}}]}


class NativeWireTests(unittest.TestCase):
    def test_reported_identity_does_not_replace_requested_continuation_scope(self):
        for transport, fixture in (("anthropic", claude_fixture), ("responses", response_fixture),
                                   ("gemini", gemini_fixture), ("chat", chat_fixture)):
            native = fixture()
            reply = decode(native, MODELS[transport], transport)
            self.assertEqual(reply["model"], "untrusted-echo")
            metadata = [block for block in reply["content"] if block["type"] == "opencode_meta"]
            self.assertTrue(metadata)
            self.assertTrue(all(block["model"] == MODELS[transport] for block in metadata))
            native.pop("modelVersion" if transport == "gemini" else "model")
            self.assertIsNone(decode(native, MODELS[transport], transport)["model"])

    def test_responses_stateless_parallel_continuation_preserves_output_once(self):
        native = response_fixture()
        reply = decode(native, MODELS["responses"], "responses")
        self.assertEqual(reply["model"], native["model"])
        self.assertEqual(reply["stop_reason"], "tool_use")
        self.assertEqual(uses(reply), [
            {"type": "tool_use", "id": "call_route", "name": "route", "input": {"zone": 7}},
            {"type": "tool_use", "id": "call_quest", "name": "quest", "input": {"quest": 42}}])
        conversation = [{"role": "user", "content": "Which route?"}, {"role": "assistant", "content": reply["content"]}, results(reply)]
        request = encode(roundtrip(body("responses", conversation)), "responses", {"reasoning": {"effort": "max"}})
        self.assertEqual(request["input"], [
            {"role": "user", "content": "Which route?"}, *native["output"],
            {"type": "function_call_output", "call_id": "call_route", "output": "  result 0\n"},
            {"type": "function_call_output", "call_id": "call_quest", "output": "  result 1\n"}])
        self.assertFalse(request["store"])
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])
        self.assertNotIn("previous_response_id", request)
        self.assertEqual(request["max_output_tokens"], 512)
        self.assertEqual(request["reasoning"], {"effort": "max"})
        self.assertNotIn("encrypted", json.dumps(visible(reply)))

    def test_responses_final_phase_replays_without_tools_or_visible_duplicates(self):
        native = {"status": "completed", "output": [{"type": "message", "role": "assistant", "phase": "final_answer",
                   "id": "msg_final", "content": [{"type": "output_text", "text": "Go east.", "annotations": []}]}]}
        reply = decode(native, MODELS["responses"], "responses")
        request = encode(body("responses", [{"role": "assistant", "content": roundtrip(reply["content"])}]), "responses", {})
        self.assertEqual(request["input"], native["output"])
        self.assertEqual(visible(reply), [{"type": "text", "text": "Go east."}])

    def test_gemini_parallel_calls_restore_exact_parts_and_native_result_identity(self):
        native = gemini_fixture()
        reply = decode(native, MODELS["gemini"], "gemini")
        again = decode(roundtrip(native), MODELS["gemini"], "gemini")
        self.assertEqual(uses(reply), uses(again))
        self.assertEqual(len({b["id"] for b in uses(reply)}), 2)
        self.assertEqual(uses(reply)[1]["id"], "native-quest")
        self.assertEqual(visible(reply)[0], {"type": "text", "text": ""})
        self.assertNotIn("private thought", json.dumps(visible(reply)))
        conversation = [{"role": "user", "content": "Which route?"}, {"role": "assistant", "content": reply["content"]}, results(reply)]
        request = encode(roundtrip(body("gemini", conversation)), "gemini",
                         {"generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}})
        self.assertEqual(request["contents"][1], native["candidates"][0]["content"])
        self.assertEqual(request["contents"][2], {"role": "user", "parts": [
            {"functionResponse": {"name": "route", "response": {"output": "  result 0\n"}}},
            {"functionResponse": {"id": "native-quest", "name": "quest", "response": {"output": "  result 1\n"}}}]})
        self.assertEqual(request["generationConfig"], {"maxOutputTokens": 512, "thinkingConfig": {"thinkingLevel": "high"}})
        self.assertEqual(request["systemInstruction"], {"parts": [{"text": "Be brief."}]})
        self.assertNotIn("skip_thought_signature_validator", json.dumps(request))
        self.assertNotIn("model", request)  # The requested native model belongs to the fixed URL path.

    def test_gemini_sequential_steps_keep_each_signature_and_distinct_synthetic_ids(self):
        first = gemini_fixture()
        second = copy.deepcopy(first)
        second["responseId"] = "response-b"
        first_reply = decode(first, MODELS["gemini"], "gemini")
        second_reply = decode(second, MODELS["gemini"], "gemini")
        self.assertNotEqual(uses(first_reply)[0]["id"], uses(second_reply)[0]["id"])
        history = [{"role": "user", "content": "Which route?"},
                   {"role": "assistant", "content": first_reply["content"]}, results(first_reply),
                   {"role": "assistant", "content": second_reply["content"]}, results(second_reply)]
        request = encode(roundtrip(body("gemini", history)), "gemini", {})
        self.assertEqual(request["contents"][1], first["candidates"][0]["content"])
        self.assertEqual(request["contents"][3], second["candidates"][0]["content"])
        self.assertEqual(request["contents"][4]["parts"][0]["functionResponse"]["name"], "route")

    def test_gemini_final_signed_empty_parts_replay_without_calls(self):
        for parts in ([], [{}], [{"text": "", "thoughtSignature": " signed "}],
                      [{"thought": True, "text": "", "thoughtSignature": " empty-thought "}],
                      [{"text": "Go east.", "thoughtSignature": " signed-answer "}]):
            with self.subTest(parts=parts):
                native = {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}]}
                reply = decode(native, MODELS["gemini"], "gemini")
                request = encode(body("gemini", [{"role": "assistant", "content": roundtrip(reply["content"])}]), "gemini", {})
                self.assertEqual(request["contents"], [native["candidates"][0]["content"]])
                self.assertEqual(reply["stop_reason"], "end_turn")

    def test_gemini_structured_results_and_errors_keep_parallel_order(self):
        reply = decode(gemini_fixture(), MODELS["gemini"], "gemini")
        calls = uses(reply)
        history = [{"role": "assistant", "content": reply["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": calls[0]["id"], "content": {"zone": 7}},
            {"type": "tool_result", "tool_use_id": calls[1]["id"], "is_error": True, "content": "not found"},
            {"type": "text", "text": "Use what is available."}]}]
        request = encode(body("gemini", history), "gemini", {})
        self.assertEqual(request["contents"][1]["parts"], [
            {"functionResponse": {"name": "route", "response": {"zone": 7}}},
            {"functionResponse": {"id": "native-quest", "name": "quest", "response": {"error": "not found"}}},
            {"text": "Use what is available."}])
        with self.assertRaisesRegex(ValueError, "matching function call"):
            encode(body("gemini", [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "missing", "content": "x"}]}]), "gemini", {})

    def test_claude_signed_redacted_empty_and_interleaved_blocks_survive_exactly(self):
        native = claude_fixture()
        reply = decode(native, MODELS["anthropic"], "anthropic")
        self.assertEqual(visible(reply), [{"type": "text", "text": "Checking."},
                                        {"type": "tool_use", "id": "call_route", "name": "route", "input": {"zone": 7}}])
        conversation = [{"role": "assistant", "content": reply["content"]}, results(reply)]
        request = encode(roundtrip(body("anthropic", conversation)), "anthropic", {"output_config": {"effort": "low"}})
        self.assertEqual(request["messages"][0]["content"], native["content"])
        self.assertEqual(request["messages"][1], results(reply))
        self.assertEqual(request["model"], "claude-haiku-5-5")
        self.assertEqual(request["output_config"], {"effort": "low"})
        self.assertNotIn("thinking", request)
        self.assertEqual(request["max_tokens"], 512)

    def test_chat_replays_whole_reasoning_representation_and_no_extra_content(self):
        native = chat_fixture()
        reply = decode(native, MODELS["chat"], "chat")
        self.assertNotIn("private thought", json.dumps(visible(reply)))
        self.assertNotIn("foreign-tool-extra", json.dumps(reply))
        conversation = [{"role": "assistant", "content": reply["content"]}, results(reply)]
        request = encode(roundtrip(body("chat", conversation)), "chat", {"reasoning_effort": "max"})
        assistant = request["messages"][1]
        self.assertEqual(assistant["reasoning_content"], native["choices"][0]["message"]["reasoning_content"])
        self.assertEqual(assistant["reasoning_details"], native["choices"][0]["message"]["reasoning_details"])
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["call_route", "call_quest"])
        self.assertEqual(request["messages"][2:], [
            {"role": "tool", "tool_call_id": "call_route", "content": "  result 0\n"},
            {"role": "tool", "tool_call_id": "call_quest", "content": "  result 1\n"}])
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertEqual(request["max_tokens"], 512)

    def test_chat_reasoning_is_preserved_on_final_and_empty_reasoning_messages(self):
        for reasoning in ({"reasoning_content": "  final reasoning  ", "reasoning_details": []},
                          {"reasoning_content": "", "reasoning_details": None}):
            native = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Go east.", **reasoning}}]}
            reply = decode(native, MODELS["chat"], "chat")
            request = encode(body("chat", [{"role": "assistant", "content": roundtrip(reply["content"])}]), "chat", {})
            for field, value in reasoning.items():
                self.assertEqual(request["messages"][1][field], value)

    def test_all_private_state_is_isolated_by_requested_model_and_transport(self):
        fixtures = {"anthropic": claude_fixture(), "responses": response_fixture(),
                    "gemini": gemini_fixture(), "chat": chat_fixture()}
        opaque = ("signed-empty", "redacted\\nopaque", "encrypted\\nopaque", "thought-signature", "private thought", "reasoning_details")
        for source, fixture in fixtures.items():
            reply = decode(fixture, MODELS[source], source)
            history = [{"role": "assistant", "content": roundtrip(reply["content"])}]
            for target in MODELS:
                with self.subTest(source=source, target=target):
                    model = MODELS[target] if target != source else MODELS[source] + "-other"
                    request = encode(body(target, history, model=model), target, {})
                    serialized = json.dumps(request)
                    for secret in opaque:
                        self.assertNotIn(secret, serialized)
                    self.assertNotIn("untrusted-echo", serialized)
                    # Public calls are retained, not duplicated, even when opaque state is unusable.
                    if target == "responses":
                        self.assertEqual(len([i for i in request["input"] if i.get("type") == "function_call"]), len(uses(reply)))
                    elif target == "gemini":
                        self.assertEqual(len([p for c in request["contents"] for p in c["parts"] if "functionCall" in p]), len(uses(reply)))
            direct_body = body(source, history, model="ep:direct:model")
            for direct in (openai_compat.to_openai(direct_body, "endpoint"), openai_responses.to_responses(direct_body)):
                for secret in opaque:
                    self.assertNotIn(secret, json.dumps(direct))

    def test_foreign_direct_provider_metadata_and_raw_thinking_never_become_gateway_state(self):
        blocks = [{"type": "openai_meta", "reasoning_details": [{"text": "foreign-meta"}]},
                  {"type": "openai_responses_output", "items": [{"type": "reasoning", "encrypted_content": "foreign-output"}]},
                  {"type": "opencode_meta", "reasoning_content": "unscoped-gateway"},
                  {"type": "thinking", "thinking": "direct-thinking", "signature": "direct-signature"},
                  {"type": "redacted_thinking", "data": "direct-redacted"},
                  {"type": "tool_use", "id": "public-call", "name": "route", "input": {},
                   "extra_content": {"google": {"thought_signature": "foreign-tool-signature"}}},
                  {"type": "text", "text": "Public answer."}]
        for transport in MODELS:
            request = encode(body(transport, [{"role": "assistant", "content": blocks}]), transport, {})
            serialized = json.dumps(request)
            for secret in ("foreign-", "unscoped-gateway", "direct-thinking", "direct-signature", "direct-redacted", "extra_content"):
                self.assertNotIn(secret, serialized)
            self.assertIn("public-call", serialized)
            self.assertIn("Public answer.", serialized)

    def test_native_effort_options_preserve_tools_and_output_caps(self):
        controls = {"anthropic": {"output_config": {"effort": "low"}}, "responses": {"reasoning": {"effort": "high"}},
                    "gemini": {"generationConfig": {"thinkingConfig": {"thinkingLevel": "medium"}, "maxOutputTokens": 99999}},
                    "chat": {"reasoning_effort": "high"}}
        for transport, options in controls.items():
            with self.subTest(transport=transport):
                canonical = body(transport, max_tokens=64, tool_choice={"type": "none"})
                original = copy.deepcopy(canonical)
                request = encode(canonical, transport, options)
                self.assertEqual(canonical, original)
                self.assertEqual(request["generationConfig"]["maxOutputTokens"] if transport == "gemini" else
                                 request["max_output_tokens" if transport == "responses" else "max_tokens"], 64)
                self.assertEqual(len(request["tools"][0]["functionDeclarations"]) if transport == "gemini" else len(request["tools"]), 2)
                self.assertNotIn("thinking", request)
                if transport != "anthropic":
                    self.assertNotIn("output_config", request)
        auto = encode(body("anthropic"), "anthropic", {"output_config": {"effort": "low"}, "thinking": {"type": "disabled"}})
        self.assertEqual(auto["thinking"], {"type": "disabled"})
        for transport in MODELS:
            plain = encode(body(transport), transport, {})
            self.assertNotIn("reasoning_effort", plain)
            self.assertNotIn("reasoning", plain)
            self.assertNotIn("output_config", plain)
            self.assertNotIn("thinkingConfig", plain.get("generationConfig", {}))

    def test_json_schema_translation_keeps_optional_nullable_properties_and_effort(self):
        controls = {"anthropic": {"output_config": {"effort": "high"}}, "responses": {"reasoning": {"effort": "high"}},
                    "gemini": {"generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}}, "chat": {"reasoning_effort": "high"}}
        for transport, options in controls.items():
            canonical = body(transport, output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
            request = encode(canonical, transport, options)
            with self.subTest(transport=transport):
                if transport == "anthropic":
                    self.assertEqual(request["output_config"], {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}})
                elif transport == "responses":
                    self.assertEqual(request["text"]["format"], {"type": "json_schema", "name": "response", "schema": SCHEMA, "strict": False})
                    self.assertFalse(request["tools"][0]["strict"])
                elif transport == "chat":
                    self.assertEqual(request["response_format"], {"type": "json_schema", "json_schema": {"name": "response", "schema": SCHEMA, "strict": False}})
                else:
                    self.assertEqual(request["generationConfig"]["responseMimeType"], "application/json")
                    self.assertEqual(request["generationConfig"]["responseJsonSchema"], SCHEMA)
                    self.assertEqual(request["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"], SCHEMA)
                self.assertEqual(SCHEMA["required"], [])
        merged = encode(body("anthropic"), "anthropic", {"output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}})
        self.assertEqual(merged["output_config"]["format"]["schema"], SCHEMA)

    def test_named_tool_choice_is_native_in_every_family(self):
        expected = {"anthropic": {"type": "tool", "name": "route"}, "responses": {"type": "function", "name": "route"},
                    "chat": {"type": "function", "function": {"name": "route"}},
                    "gemini": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["route"]}}}
        for transport in MODELS:
            request = encode(body(transport, tool_choice={"type": "tool", "name": "route"}), transport, {})
            self.assertEqual(request["toolConfig" if transport == "gemini" else "tool_choice"], expected[transport])

    def test_output_cap_stop_wins_even_with_partial_tools_or_only_hidden_reasoning(self):
        fixtures = {"anthropic": dict(claude_fixture(), stop_reason="max_tokens"),
                    "responses": dict(response_fixture(), status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
                    "gemini": gemini_fixture(), "chat": chat_fixture()}
        fixtures["gemini"]["candidates"][0]["finishReason"] = "MAX_TOKENS"
        fixtures["chat"]["choices"][0]["finish_reason"] = "length"
        for transport, fixture in fixtures.items():
            self.assertEqual(decode(fixture, MODELS[transport], transport)["stop_reason"], "max_tokens")
        only_thoughts = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"role": "model", "parts": [
            {"thought": True, "text": "private thought", "thoughtSignature": "signed"}]}}]}
        reply = decode(only_thoughts, MODELS["gemini"], "gemini")
        self.assertEqual(reply["stop_reason"], "max_tokens")
        self.assertEqual(visible(reply), [])

    def test_gateway_billed_usage_is_used_on_all_native_shapes(self):
        raw = {"cost": 0, "opencode": {"tokens": {"input": 10, "output": 8, "reasoning": 5,
                                                  "cache_read": 7, "cache_write_5m": 4}}}
        for transport, native in (("anthropic", claude_fixture()), ("responses", response_fixture()),
                                  ("gemini", gemini_fixture()), ("chat", chat_fixture())):
            native["usageMetadata" if transport == "gemini" else "usage"] = copy.deepcopy(raw)
            usage = decode(native, MODELS[transport], transport)["usage"]
            self.assertEqual(usage["input_tokens"], 10)
            self.assertEqual(usage["output_tokens"], 8)  # Already includes the five reasoning tokens.
            self.assertEqual(usage["reasoning_tokens"], 5)
            self.assertEqual(usage["cache_read_input_tokens"], 7)
            self.assertEqual(usage["cache_creation_input_tokens"], 4)
            self.assertEqual(usage["cost_usd"], 0)
            self.assertFalse(usage["cost_estimated"])

    def test_api_errors_and_native_refusals_are_not_empty_successes(self):
        for transport in MODELS:
            for error in ({"error": {"code": "bad_request", "message": "effort refused"}}, {"type": "error"}, {"object": "error"}):
                with self.subTest(transport=transport, error=error), self.assertRaises(RuntimeError):
                    decode(error, MODELS[transport], transport)
        refusals = [
            ("anthropic", {"type": "message", "stop_reason": "refusal", "content": []}),
            ("responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No."}]}]}),
            ("responses", {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}),
            ("responses", {"status": "failed"}), ("responses", {"status": "queued"}),
            ("chat", {"choices": [{"finish_reason": "stop", "message": {"refusal": "No."}}]}),
            ("chat", {"choices": [{"finish_reason": "content_filter", "message": {}}]}),
            ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}}),
            ("gemini", {"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]}),
            ("gemini", {"candidates": [{"finishReason": "MISSING_THOUGHT_SIGNATURE"}]}),
        ]
        for transport, refusal in refusals:
            with self.subTest(transport=transport, refusal=refusal), self.assertRaises(RuntimeError):
                decode(refusal, MODELS[transport], transport)

    def test_malformed_tool_arguments_are_not_replaced_by_guessed_empty_inputs(self):
        fixtures = [
            ("anthropic", {"content": [{"type": "tool_use", "id": "call", "name": "route", "input": []}]}),
            ("responses", {"status": "completed", "output": [{"type": "function_call", "call_id": "call", "name": "route", "arguments": "{"}]}),
            ("responses", {"status": "completed", "output": [{"type": "function_call", "id": "fc_not_call_id", "name": "route", "arguments": "{}"}]}),
            ("chat", {"choices": [{"message": {"tool_calls": [{"id": "call", "function": {"name": "route", "arguments": "[]"}}]}}]}),
            ("gemini", {"candidates": [{"content": {"parts": [{"functionCall": {"name": "route", "args": []}}]}}]}),
        ]
        for transport, fixture in fixtures:
            with self.subTest(transport=transport), self.assertRaises(RuntimeError):
                decode(fixture, MODELS[transport], transport)

    def test_wrong_origin_model_or_native_controls_are_rejected(self):
        for model in ("claude-haiku-5-5", "ep:openai:gpt-6.1-sol", "gemini:gemini-4-carbon", "opencode:"):
            with self.assertRaises(ValueError):
                encode(body("chat", model=model), "chat", {})
            with self.assertRaises(ValueError):
                decode({}, model, "chat")
        with self.assertRaises(ValueError):
            encode(body("chat"), "unreviewed", {})
        with self.assertRaises(ValueError):
            encode(body("responses"), "responses", {"output_config": {"effort": "high"}})
        with self.assertRaises(ValueError):
            encode(body("chat"), "chat", {"thinking": {"type": "disabled"}})
        for transport in ("responses", "gemini", "chat"):
            with self.assertRaises(ValueError):
                encode(body(transport), transport, {"output_config": {"format": {"type": "json_schema"}}})

    def test_final_options_do_not_inherit_stale_body_thinking_controls(self):
        canonical = body("anthropic", thinking={"type": "disabled"},
                         output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}})
        request = encode(canonical, "anthropic", {"output_config": {"effort": "low"}})
        self.assertNotIn("thinking", request)
        self.assertEqual(request["output_config"], {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
        plain = encode(canonical, "anthropic", {})
        self.assertNotIn("thinking", plain)
        self.assertEqual(plain["output_config"], {"format": {"type": "json_schema", "schema": SCHEMA}})


if __name__ == "__main__":
    unittest.main()
