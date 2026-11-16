#!/usr/bin/env python3
"""Reasoning selections through real local HTTP: roles, tool turns, validation and no paid fallback probes."""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
from llm_client import LLMClient  # noqa: E402
import tuning  # noqa: E402
import models  # noqa: E402
import opencode  # noqa: E402
import reasoning  # noqa: E402


class ReasoningRequests(unittest.TestCase):
    def setUp(self):
        self.requests, self.errors = [], []
        self.respond = lambda path, body: (200, self.chat("Ready."))
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, body))
                status, data = owner.respond(self.path, body)
                data = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.local = "http://127.0.0.1:%d" % self.server.server_address[1]
        real_open = urllib.request.urlopen

        def local_open(request, timeout=None):
            # Keep the production endpoint identity for routing, but never allow an external request.
            path = urllib.parse.urlsplit(request.full_url).path
            local_request = urllib.request.Request(self.local + path, data=request.data,
                                                  headers=request.headers, method=request.method)
            return real_open(local_request, timeout=timeout)

        self.network = patch("urllib.request.urlopen", side_effect=local_open)
        self.network.start()
        self.addCleanup(self.network.stop)
        gateway = patch("opencode.open_request", side_effect=local_open)
        gateway.start()
        self.addCleanup(gateway.stop)
        self.env = patch.dict(os.environ, {key: "" for key in (
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
            "OPENCODE_API_KEY")})
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def chat(text):
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    @staticmethod
    def response(items):
        return {"object": "response", "status": "completed", "output": items,
                "usage": {"input_tokens": 10, "output_tokens": 2}}

    @staticmethod
    def text_item(text):
        return {"id": "msg_answer", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}

    def client(self, **changes):
        cfg = {"endpoints": [{"id": "openai", "base_url": "https://api.openai.com/v1"}],
               "fast_model": "ep:openai:gpt-6.1-sol", "deep_model": "ep:openai:gpt-6.1-sol", **changes}
        client = LLMClient(cfg, emit=self.errors.append)
        return client

    def gateway_client(self, native, api, levels, effort="high"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway_rows = [{
            "id": native, "type": "chat", "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": {"tools": {"supported": True}, "reasoning": {"supported": True, "effort": levels}},
            "api": {api: {"base_url": "https://attacker.invalid"}},
            "cost": {"text": {"input": {"price": 1, "cache": {"read": 0.1, "write": 2}},
                              "output": {"price": 4}}},
        }]
        catalog = models.Catalog(os.path.join(temporary.name, "models.json"), opencode_key="fixture",
                                 fetchers={"anthropic": lambda _: [], "gemini": lambda _: [],
                                           "openrouter": lambda _: [],
                                           "opencode": lambda _: [opencode.catalog_entry(row) for row in self.gateway_rows]})
        catalog.refresh()
        model = "opencode:" + native
        client = self.client(fast_model=model, deep_model=model, fast_reasoning_effort=effort)
        client.providers["opencode"].key = "isolated-fixture-key"
        client.catalog = catalog
        return client

    def test_sol_tool_turn_keeps_effort_and_encrypted_state_when_settings_change(self):
        client = self.client(fast_reasoning_effort="low", deep_reasoning_effort="xhigh")
        model = client.cfg["fast_model"]
        original = [{"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque-state"},
                    {"id": "fc_1", "type": "function_call", "call_id": "call_quest", "name": "quest",
                     "arguments": '{"id":42}', "status": "completed"}]
        expected = iter(("low", "low", "high", "xhigh"))

        def respond(path, body):
            effort = next(expected)
            if path != "/v1/responses" or body.get("reasoning") != {"effort": effort}:
                return 400, {"error": {"message": "Wrong endpoint or reasoning effort"}}
            if len(self.requests) == 1:
                return 200, self.response(original)
            if len(self.requests) == 2:
                replay = body["input"][1:]
                if replay != [*original, {"type": "function_call_output", "call_id": "call_quest", "output": '{"east":80}'}]:
                    return 400, {"error": {"message": "Lost or duplicated reasoning/tool continuity"}}
            return 200, self.response([self.text_item("Go east.")])

        calls = []

        def tool(name, args, ctx, budget):
            calls.append((name, args))
            client.cfg["fast_reasoning_effort"] = "high"  # UI edit while this turn is running
            return {"east": 80}

        self.respond, client.run_tool = respond, tool
        answer, meta = client.call_tools(model, [{"role": "user", "content": "Where?"}],
                                        [{"name": "quest", "input_schema": {"type": "object"}}], 1, 10)
        self.assertEqual((answer, calls, meta["tool_calls"]), ("Go east.", [("quest", {"id": 42})], 1))
        self.assertEqual(client.call_once(model, "Next?", 600)[0], "Go east.")
        self.assertEqual(client.call_once(model, "Review", 1200, deep=True)[0], "Go east.")
        self.assertEqual(client.path_stats["fast"]["calls"], 3)
        self.assertEqual(client.path_stats["deep"]["calls"], 1)

    def test_invalid_sol_effort_is_rejected_before_request_or_settings_mutation(self):
        client = self.client(fast_reasoning_effort="none")
        answer, _ = client.call_once(client.cfg["fast_model"], "Where?", 600)
        self.assertIsNone(answer)
        self.assertEqual(self.requests, [])
        self.assertTrue(any("none" in m["text"] and "low" in m["text"] for m in self.errors))
        before = dict(client.cfg)
        with self.assertRaises(ValueError):
            tuning.apply(client.cfg, {"fast_reasoning_effort": "none", "deep_model": "ep:openai:gpt-6-luna"})
        self.assertEqual(client.cfg, before)
        tuning.apply(client.cfg, {"fast_reasoning_effort": "low"})
        self.assertEqual(client.cfg["fast_reasoning_effort"], "low")

    def test_origin_isolation_separates_compatible_chat_from_official_openai_chat_and_responses(self):
        # Nonofficial compatible origin keeps /v1/chat/completions with max_tokens even for OpenAI-like names.
        compat_client = self.client(endpoints=[{"id": "openai", "base_url": self.local + "/v1"}])

        def respond_compat(path, body):
            if path != "/v1/chat/completions" or "reasoning_effort" in body or "max_completion_tokens" in body:
                return 400, {"error": {"message": "Compatible server expects plain Chat and max_tokens"}}
            return 200, self.chat("Turn in the quest.")

        self.respond = respond_compat
        self.assertEqual(compat_client.call_once("ep:openai:gpt-6.1-sol", "Where?", 600)[0], "Turn in the quest.")
        self.assertEqual(self.requests[-1][1]["max_tokens"], 600)
        self.assertEqual(compat_client.call_once("ep:openai:gpt-6.1-sol", "Review", 600, deep=True)[0], "Turn in the quest.")

        # Official OpenAI non-reasoning (gpt-4.1-2025-04-14) and unclassified IDs use /v1/chat/completions + max_completion_tokens.
        official = self.client(fast_model="ep:openai:gpt-4.1-2025-04-14", deep_model="ep:openai:gpt-6-private")

        def respond_official_chat(path, body):
            if path != "/v1/chat/completions" or "max_tokens" in body or body.get("max_completion_tokens") != 600:
                return 400, {"error": {"message": "Official Chat requires max_completion_tokens"}}
            if "reasoning_effort" in body:
                return 400, {"error": {"message": "Auto must omit reasoning_effort"}}
            return 200, self.chat("Go north.")

        self.respond = respond_official_chat
        self.assertEqual(official.call_once("ep:openai:gpt-4.1-2025-04-14", "Where?", 600)[0], "Go north.")
        self.assertEqual(official.call_once("ep:openai:gpt-6-private", "Where?", 600, deep=True)[0], "Go north.")

    def test_luna_56_and_pro_models_route_translation_and_tools_through_responses_with_cost_accounting(self):
        client = self.client(fast_model="ep:openai:gpt-5.6-luna", deep_model="ep:openai:gpt-5.5-pro",
                             deep_reasoning_effort="high")

        def respond(path, body):
            if path != "/v1/responses" or "max_tokens" in body or "max_completion_tokens" in body:
                return 400, {"error": {"message": "Expected stateless Responses with max_output_tokens"}}
            if body["model"] == "gpt-5.6-luna":
                if body.get("reasoning") != {"effort": "low"}:
                    return 400, {"error": {"message": "Automatic Luna 5.6 must request low"}}
                return 200, {
                    "object": "response", "status": "completed", "model": "gpt-5.6-luna",
                    "output": [self.text_item("Модель переключена.")],
                    "usage": {
                        "input_tokens": 3_000, "output_tokens": 50,
                        "input_tokens_details": {"cached_tokens": 1_000, "cache_write_tokens": 1_000,
                                                 "cache_write_tokens_details": {"ttl_30m_tokens": 1_000, "ttl_12h_tokens": 0}},
                    },
                }
            if body["model"] == "gpt-5.5-pro" and body.get("reasoning") == {"effort": "high"}:
                return 200, self.response([self.text_item("Cleared.")])
            return 400, {"error": {"message": "Unexpected model or effort"}}

        self.respond = respond
        self.assertEqual(client.translate("Model switched.", "ru"), "Модель переключена.")
        self.assertEqual(client.call_once("ep:openai:gpt-5.5-pro", "Review", 900, deep=True)[0], "Cleared.")
        # Luna 5.6 ($0.20/M in, $0.02/M read, $0.25/M 30m write, $1.20/M out):
        # (1000*0.2 + 1000*0.02 + 1000*0.25 + 50*1.2)/1e6 = 0.00053
        # Pro 5.5 ($30/M in, $180/M out): (10*30 + 2*180)/1e6 = 0.00066 -> total = 0.00119
        self.assertAlmostEqual(client.stats["cost_usd"], 0.00119, places=8)
        self.assertEqual(client.stats["in_tokens"], 3_010)
        self.assertEqual(client.stats["cache_write_tokens"], 1_000)

    def test_excluded_official_model_is_blocked_before_network_io(self):
        client = self.client(fast_model="ep:openai:tts-1")
        answer, _ = client.call_once("ep:openai:tts-1", "Hello", 600)
        self.assertIsNone(answer)
        self.assertEqual(self.requests, [])
        self.assertEqual(client.stats["errors"], 1)
        self.assertTrue(any("tts-1" in m["text"] and "speech synthesis" in m["text"] for m in self.errors))

    def test_explicit_rejection_is_reported_without_retrying_another_level(self):
        client = self.client(endpoints=[{"id": "openai", "base_url": self.local + "/v1"}],
                             fast_reasoning_effort="high")
        self.respond = lambda path, body: (400, {"error": {"message": "Unsupported reasoning_effort high; use low"}})
        self.assertIsNone(client.call_once(client.cfg["fast_model"], "Where?", 600)[0])
        self.assertEqual(len(self.requests), 1, "an explicit rejected level must not trigger a paid fallback probe")
        self.assertTrue(any("Unsupported reasoning_effort high" in m["text"] for m in self.errors))

    def test_gemini_automatic_roles_and_explicit_choices_are_independent(self):
        client = self.client(fast_model="gemini:gemini-3.8-flash", deep_model="gemini:gemini-3.8-flash")
        model = client.cfg["fast_model"]
        for deep in (False, True):
            self.assertEqual(client.call_once(model, "Automatic", 600, deep=deep)[0], "Ready.")
        client.cfg["fast_reasoning_effort"] = "high"
        self.assertEqual(client.call_once(model, "Explicit", 600)[0], "Ready.")
        client.cfg.update(fast_reasoning_effort="auto", tune_thinking=False)
        self.assertEqual(client.call_once(model, "Off", 600)[0], "Ready.")
        self.assertEqual([b.get("reasoning_effort") for _, b in self.requests], ["low", "medium", "high", None])

    def test_opencode_haiku_fast_deep_and_off_use_native_messages(self):
        model = "opencode:claude-haiku-5-5"
        client = self.gateway_client("claude-haiku-5-5", "messages", ["low", "medium"], effort="auto")

        def respond(path, body):
            self.assertEqual(path, "/inference/anthropic/v1/messages")
            self.assertEqual(body["model"], "claude-haiku-5-5")
            self.assertNotIn("reasoning_effort", body)
            return 200, {"type": "message", "model": body["model"], "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "East."}],
                         "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0}}

        self.respond = respond
        for deep in (False, True):
            self.assertEqual(client.call_once(model, "Where?", 600, deep=deep)[0], "East.")
        client.cfg["tune_thinking"] = False
        self.assertEqual(client.call_once(model, "Off", 600)[0], "East.")
        client.cfg["fast_reasoning_effort"] = "high"
        self.assertIsNone(client.call_once(model, "Unsupported explicit", 600)[0])
        fast, deep, off = [body for _, body in self.requests]
        self.assertEqual(fast["output_config"], {"effort": "low"})
        self.assertNotIn("thinking", fast)
        self.assertEqual(deep["output_config"], {"effort": "medium"})
        self.assertNotIn("thinking", deep)
        self.assertNotIn("output_config", off)
        self.assertNotIn("thinking", off)
        self.assertEqual(client.stats["cost_coverage"]["reported"], 3)
        client.cfg["fast_reasoning_effort"] = "auto"
        client.cfg["tune_thinking"] = True
        self.assertEqual(client.call_once(model, "On again", 600)[0], "East.")
        self.assertEqual(self.requests[-1][1]["output_config"], {"effort": "low"})

    def test_automatic_refusal_is_one_attempt_and_never_changes_future_policy(self):
        client = self.gateway_client("claude-haiku-5-5", "messages", ["low", "medium"], effort="auto")
        model = client.cfg["fast_model"]
        self.respond = lambda *_: (400, {"error": {"message": "Unsupported effort"}})
        for deep in (False, False, True):
            before = len(self.requests)
            self.assertIsNone(client.call_once(model, "Rejected", 600, deep=deep)[0])
            self.assertEqual(len(self.requests), before + 1)
        self.assertEqual([body["output_config"]["effort"] for _, body in self.requests], ["low", "low", "medium"])
        self.assertEqual(client.stats["calls"], 0)
        self.assertEqual(client.stats["errors"], 3)

    def test_opencode_unverified_families_and_unknown_claude_aliases_stay_plain_chat(self):
        client = self.client()
        client.providers["opencode"].key = "isolated-fixture-key"

        def respond(path, body):
            self.assertEqual(path, "/inference/openai/v1/chat/completions")
            for field in ("reasoning", "reasoning_effort", "thinking", "output_config"):
                self.assertNotIn(field, body)
            return 200, self.chat("Continue.")

        self.respond = respond
        for native in ("gemini-3.8-flash", "gpt-6.1-sol", "vendor/paid", "claude-haiku-5-5-custom"):
            for deep in (False, True):
                self.assertEqual(client.call_once("opencode:" + native, "Where?", 600, deep=deep)[0], "Continue.")
                self.assertEqual(self.requests[-1][1]["model"], native)

    def test_opencode_native_tool_round_preserves_private_thinking_and_billed_categories(self):
        model = "opencode:claude-haiku-5-5"
        client = self.gateway_client("claude-haiku-5-5", "messages", ["low", "medium"], effort="auto")
        thought = {"type": "thinking", "thinking": "", "signature": "gateway-only-signature"}
        tool = {"type": "tool_use", "id": "call_quest", "name": "quest", "input": {"id": 42}}

        def respond(path, body):
            self.assertEqual(path, "/inference/anthropic/v1/messages")
            self.assertNotIn("thinking", body)
            self.assertEqual(body["output_config"], {"effort": "medium"})
            if len(self.requests) == 1:
                content, stop = [thought, tool], "tool_use"
            else:
                self.assertEqual(body["messages"][-2]["content"], [thought, tool])
                self.assertEqual(body["messages"][-1]["content"][0]["tool_use_id"], "call_quest")
                content, stop = [{"type": "text", "text": "East."}], "end_turn"
            return 200, {"type": "message", "model": "claude-haiku-5-5",
                         "content": content, "stop_reason": stop, "usage": {
                             "input_tokens": 999, "output_tokens": 999, "cost": 0.012,
                             "opencode": {"tokens": {"input": 100, "output": 30, "reasoning": 20,
                                                    "cache_read": 50, "cache_write_5m": 10}}}}

        self.respond = respond
        client.run_tool = lambda *args: {"east": 80}
        answer, meta = client.call_tools(model, [{"role": "user", "content": "Where?"}],
                                        [{"name": "quest", "input_schema": {"type": "object"}}], 1, 10, deep=True)
        self.assertEqual((answer, meta["tool_calls"]), ("East.", 1))
        self.assertEqual(client.stats["uncached_in_tokens"], 200)
        self.assertEqual(client.stats["cache_read_tokens"], 100)
        self.assertEqual(client.stats["cache_write_tokens"], 20)
        self.assertEqual(client.stats["out_tokens"], 60)
        self.assertAlmostEqual(client.stats["cost_usd"], 0.024)

    def test_native_effort_preserves_schema_without_foreign_reasoning_metadata(self):
        client = self.client(fast_reasoning_effort="high", stream_speech=False)
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        model = "claude-opus-5-5"

        def respond(path, body):
            if body.get("thinking") or body.get("output_config") != {"effort": "high", "format": {
                    "type": "json_schema", "schema": schema}}:
                return 400, {"error": {"message": "Explicit effort lost schema or inherited automatic thinking"}}
            if "openai_responses_output" in json.dumps(body):
                return 400, {"error": {"message": "Foreign reasoning metadata"}}
            return 200, {"content": [{"type": "text", "text": '{"answer":"East"}'}], "stop_reason": "end_turn"}

        self.respond = respond
        self.assertEqual(client.call_once(model, "Where?", 600, schema=schema)[0], '{"answer":"East"}')
        body = {"model": model, "max_tokens": 600, "messages": [{"role": "assistant", "content": [
            {"type": "text", "text": "Earlier answer"},
            {"type": "openai_responses_output", "items": [{"encrypted_content": "private"}]}]}]}
        data, _ = client.post(body, {"output_config": {"format": {"type": "json_schema", "schema": schema}}}, "fast")
        self.assertEqual(data["content"][0]["text"], '{"answer":"East"}')

    def test_opencode_explicit_controls_reach_each_native_api_without_automatic_thinking(self):
        cases = (
            ("claude-haiku-5-5", "messages", "xhigh", "/anthropic/v1/messages"),
            ("gpt-6.1-sol", "responses", "max", "/openai/v1/responses"),
            ("gemini-4-hydrogen", "generate-content", "xhigh",
             "/google/v1beta/models/gemini-4-hydrogen:generateContent"),
            ("grok-4.7", "responses", "xhigh", "/openai/v1/responses"),
            ("glm-5.3", "chat-completions", "max", "/openai/v1/chat/completions"),
            ("deepseek-v4-pro", "chat-completions", "max", "/openai/v1/chat/completions"),
        )
        for native, api, effort, route in cases:
            with self.subTest(model=native):
                client = self.gateway_client(native, api, [effort], effort)
                model = client.cfg["fast_model"]

                def respond(path, body, route=route, api=api, native=native, effort=effort):
                    self.assertEqual(path, "/inference" + route)
                    self.assertEqual(body["model"] if api != "generate-content" else native, native)
                    self.assertNotIn("thinking", body)
                    if api == "messages":
                        self.assertEqual(body["output_config"], {"effort": effort})
                        self.assertEqual(body["max_tokens"], 600)
                        return 200, {"type": "message", "content": [{"type": "text", "text": "East."}],
                                     "stop_reason": "end_turn", "usage": {"cost": 0}}
                    if api == "responses":
                        self.assertEqual(body["reasoning"], {"effort": effort})
                        self.assertEqual(body["max_output_tokens"], 600)
                        self.assertIs(body["store"], False)
                        return 200, {**self.response([self.text_item("East.")]), "usage": {"cost": 0}}
                    if api == "generate-content":
                        self.assertEqual(body["generationConfig"],
                                         {"maxOutputTokens": 600, "thinkingConfig": {"thinkingLevel": effort}})
                        return 200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "East."}]},
                                                     "finishReason": "STOP"}], "usageMetadata": {"cost": 0}}
                    self.assertEqual(body["reasoning_effort"], effort)
                    self.assertEqual(body["max_tokens"], 600)
                    return 200, {**self.chat("East."), "usage": {"cost": 0}}

                self.respond = respond
                self.assertEqual(client.call_once(model, "Where?", 600)[0], "East.", self.errors)
                self.assertEqual(client.stats["cost_coverage"]["reported"], 1)

    def test_automatic_native_roles_and_missing_exact_targets(self):
        cases = (
            ("gemini-3.8-flash", "generate-content", ["low", "medium"], ("low", "medium")),
            ("gpt-5.5-pro", "responses", ["medium", "high"], (None, "medium")),
            ("kimi-k3", "chat-completions", ["low", "high"], ("low", None)),
        )
        for native, api, levels, expected in cases:
            with self.subTest(model=native):
                self.requests.clear()
                client = self.gateway_client(native, api, levels, effort="auto")

                def respond(path, body):
                    if ":generateContent" in path:
                        return 200, {"candidates": [{"content": {"parts": [{"text": "Ready."}]}, "finishReason": "STOP"}]}
                    if path.endswith("/responses"):
                        return 200, self.response([self.text_item("Ready.")])
                    return 200, self.chat("Ready.")

                self.respond = respond
                for deep, level in enumerate(expected):
                    self.assertEqual(client.call_once(client.cfg["fast_model"], "Where?", 600, deep=bool(deep))[0], "Ready.")
                    path, body = self.requests[-1]
                    if level is None:
                        self.assertTrue(path.endswith("/chat/completions"))
                        self.assertNotIn("reasoning_effort", body)
                    elif api == "generate-content":
                        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": level})
                    elif api == "responses":
                        self.assertEqual(body["reasoning"], {"effort": level})
                    else:
                        self.assertEqual(body["reasoning_effort"], level)

    def test_openrouter_automatic_uses_only_its_advertised_levels(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        row = {"id": "vendor/coach", "provider": "openrouter",
               "reasoning": {"supported_efforts": ["low", "medium", "high"]}}
        catalog = models.Catalog(os.path.join(temporary.name, "models.json"),
                                 fetchers={"anthropic": lambda _: [], "gemini": lambda _: [],
                                           "openrouter": lambda _: [row], "opencode": lambda _: []})
        catalog.refresh()
        client = self.client(fast_model=row["id"], deep_model=row["id"])
        client.catalog = catalog
        for deep in (False, True):
            self.assertEqual(client.call_once(row["id"], "Where?", 600, deep=deep)[0], "Ready.")
        self.assertEqual([body["reasoning"] for _, body in self.requests], [{"effort": "low"}, {"effort": "medium"}])
        client.cfg["tune_thinking"] = False
        self.assertEqual(client.call_once(row["id"], "Off", 600)[0], "Ready.")
        self.assertNotIn("reasoning", self.requests[-1][1])

    def test_official_automatic_roles_and_pro_missing_low(self):
        client = self.client()
        self.respond = lambda *_: (200, self.response([self.text_item("Ready.")]))
        for model in ("ep:openai:gpt-6.1-sol", "ep:openai:gpt-5.5-pro"):
            for deep in (False, True):
                self.assertEqual(client.call_once(model, "Where?", 600, deep=deep)[0], "Ready.")
        self.assertEqual([body.get("reasoning") for _, body in self.requests],
                         [{"effort": "low"}, {"effort": "medium"}, None, {"effort": "medium"}])
        self.assertTrue(all(path == "/v1/responses" for path, _ in self.requests))

    def test_official_auto_tool_turn_freezes_transport_limits_and_effort_after_origin_edit(self):
        client = self.client()
        model = client.cfg["fast_model"]
        original = [{"id": "r", "type": "reasoning", "encrypted_content": "private"},
                    {"type": "function_call", "call_id": "q", "name": "quest", "arguments": "{}"}]

        def respond(path, body):
            self.assertEqual(path, "/v1/responses")
            self.assertEqual(body["max_output_tokens"], 600)
            self.assertEqual(body["reasoning"], {"effort": "low"})
            if len(self.requests) == 1:
                return 200, self.response(original)
            self.assertEqual(body["input"][1:3], original)
            return 200, self.response([self.text_item("Ready.")])

        def tool(*_):
            client.cfg["endpoints"][0]["base_url"] = self.local + "/v1"
            client.cfg.update(tune_thinking=False, fast_reasoning_effort="high")
            return {}

        self.respond, client.run_tool = respond, tool
        text, meta = client.call_tools(model, [{"role": "user", "content": "Where?"}],
                                       [{"name": "quest", "input_schema": {"type": "object"}}], 1, 10)
        self.assertEqual((text, meta["tool_calls"]), ("Ready.", 1))
        self.respond = lambda *_: (200, self.chat("New origin."))
        self.assertEqual(client.call_once(model, "Next?", 600)[0], "New origin.")
        route, body = self.requests[-1]
        self.assertEqual(route, "/v1/chat/completions")
        self.assertEqual(body["max_tokens"], 600)
        self.assertEqual(body["reasoning_effort"], "high")

    def test_opencode_tool_turn_freezes_effort_and_replays_native_reasoning_after_catalog_edit(self):
        cases = [(native, api, effort) for native, api in (
            ("gpt-6.1-sol", "responses"), ("gemini-3.8-flash", "generate-content"),
            ("kimi-k3", "chat-completions")) for effort in ("high", "auto")]
        for native, api, effort in cases:
            with self.subTest(model=native, effort=effort):
                self.requests.clear()
                client = self.gateway_client(native, api, ["low", "high"], effort=effort)
                selected = "low" if effort == "auto" else "high"
                model = client.cfg["fast_model"]
                original = [{"type": "reasoning", "id": "r1", "encrypted_content": "gateway-ciphertext"},
                            {"type": "function_call", "id": "f1", "call_id": "q1",
                             "name": "quest", "arguments": '{"id":42}'}]
                parts = [{"text": "", "thought": True, "thoughtSignature": "signed-empty"},
                         {"functionCall": {"name": "quest", "args": {"id": 42}}, "thoughtSignature": "signed-call"}]

                def respond(path, body, api=api, original=original, parts=parts, selected=selected):
                    first = len(self.requests) == 1
                    if api == "responses":
                        self.assertEqual(path, "/inference/openai/v1/responses")
                        self.assertEqual(body["reasoning"], {"effort": selected})
                        if not first:
                            self.assertEqual(body["input"][1:3], original)
                            self.assertEqual(body["input"][-1]["call_id"], "q1")
                        return 200, self.response(original if first else [self.text_item("East.")])
                    if api == "generate-content":
                        self.assertIn(":generateContent", path)
                        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": selected})
                        if not first:
                            self.assertEqual(body["contents"][-2]["parts"], parts)
                            self.assertEqual(body["contents"][-1]["parts"][0]["functionResponse"]["name"], "quest")
                        return 200, {"candidates": [{"content": {"role": "model", "parts": parts if first else [{"text": "East."}]},
                                                     "finishReason": "STOP"}],
                                     "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}
                    self.assertEqual(path, "/inference/openai/v1/chat/completions")
                    self.assertEqual(body["reasoning_effort"], selected)
                    if first:
                        return 200, {"choices": [{"finish_reason": "tool_calls", "message": {
                            "content": None, "reasoning_content": "gateway-private", "tool_calls": [
                                {"id": "q1", "type": "function", "function": {"name": "quest", "arguments": '{"id":42}'}}]}}]}
                    self.assertEqual(body["messages"][-2]["reasoning_content"], "gateway-private")
                    self.assertEqual(body["messages"][-1]["tool_call_id"], "q1")
                    return 200, self.chat("East.")

                def run_tool(name, args, ctx, budget, client=client):
                    self.assertEqual((name, args), ("quest", {"id": 42}))
                    client.cfg.update(fast_reasoning_effort="low", tune_thinking=False)
                    self.gateway_rows[0]["capabilities"]["reasoning"]["effort"] = []
                    self.gateway_rows[0]["api"] = {"chat-completions": {}}
                    client.catalog.refresh()
                    return {"east": 80}

                self.respond, client.run_tool = respond, run_tool
                text, meta = client.call_tools(model, [{"role": "user", "content": "Where?"}],
                                               [{"name": "quest", "input_schema": {"type": "object"}}], 1, 10)
                self.assertEqual((text, meta["tool_calls"]), ("East.", 1), self.errors)
                self.assertEqual(len(self.requests), 2)
                if effort != "auto":
                    self.assertIsNone(client.call_once(model, "Next turn?", 600)[0])
                    self.assertEqual(len(self.requests), 2, "Removed explicit level must fail before the next request")
                else:
                    client.cfg["fast_reasoning_effort"] = "auto"
                    self.respond = lambda *_: (200, self.chat("Default."))
                    self.assertEqual(client.call_once(model, "Next turn?", 600)[0], "Default.")
                    route, body = self.requests[-1]
                    self.assertEqual(route, "/inference/openai/v1/chat/completions")
                    self.assertNotIn("reasoning_effort", body)

    def test_automatic_opencode_empty_answer_retry_keeps_native_effort_and_increases_budget(self):
        client = self.gateway_client("gemini-3.8-flash", "generate-content", ["low", "medium"], effort="auto")

        def respond(path, body):
            self.assertIn(":generateContent", path)
            self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
            first = len(self.requests) == 1
            return 200, {"candidates": [{"content": {"parts": [] if first else [{"text": "East."}]},
                                         "finishReason": "MAX_TOKENS" if first else "STOP"}],
                         "usageMetadata": {"candidatesTokenCount": 600 if first else 2}}

        self.respond = respond
        self.assertEqual(client.call_once(client.cfg["fast_model"], "Where?", 600)[0], "East.")
        self.assertEqual([body["generationConfig"]["maxOutputTokens"] for _, body in self.requests], [600, 2400])

    def test_official_chat_tool_turn_freezes_limit_field_when_origin_settings_change(self):
        model = "ep:openai:gpt-4.1"
        client = self.client(fast_model=model)

        def respond(path, body):
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(body["max_completion_tokens"], 600)
            self.assertNotIn("max_tokens", body)
            self.assertNotIn("reasoning_effort", body)
            if len(self.requests) == 1:
                return 200, {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": None, "tool_calls": [
                        {"id": "q", "type": "function", "function": {"name": "quest", "arguments": "{}"}}]}}]}
            self.assertEqual(body["messages"][-1]["tool_call_id"], "q")
            return 200, self.chat("Ready.")

        def tool(*_):
            client.cfg["endpoints"][0]["base_url"] = self.local + "/v1"
            return {}

        self.respond, client.run_tool = respond, tool
        self.assertEqual(client.call_tools(model, [{"role": "user", "content": "Where?"}],
                                          [{"name": "quest", "input_schema": {"type": "object"}}], 1, 10)[0], "Ready.")
        self.respond = lambda *_: (200, self.chat("Compatible."))
        self.assertEqual(client.call_once(model, "Next?", 600)[0], "Compatible.")
        self.assertEqual(self.requests[-1][1]["max_tokens"], 600)

    def test_automatic_anthropic_effort_keeps_schema_and_reports_refusal_once(self):
        client = self.client(stream_speech=False)
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        self.respond = lambda *_: (400, {"error": {"message": "Requested output configuration refused"}})
        self.assertIsNone(client.call_once("claude-opus-5-5", "Where?", 600, schema=schema)[0])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1]["output_config"], {
            "effort": "low", "format": {"type": "json_schema", "schema": schema}})
        self.assertNotIn("thinking", self.requests[0][1])

    def test_schema_only_refusal_recovers_without_changing_automatic_effort(self):
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        for enabled in (False, True):
            with self.subTest(tuning=enabled):
                self.requests.clear()
                client = self.client(stream_speech=False, tune_thinking=enabled)

                def respond(path, body):
                    if body.get("output_config", {}).get("format"):
                        return 400, {"error": {"message": "output_config.format: json_schema is unsupported"}}
                    return 200, {"type": "message", "content": [{"type": "text", "text": "East."}],
                                 "stop_reason": "end_turn", "usage": {}}

                self.respond = respond
                self.assertEqual(client.call_once("claude-opus-5-5", "Where?", 600, schema=schema)[0], "East.")
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(self.requests[0][1]["output_config"]["format"]["schema"], schema)
                self.assertNotIn("format", self.requests[1][1].get("output_config", {}))
                for _, body in self.requests:
                    self.assertEqual(body.get("output_config", {}).get("effort"), "low" if enabled else None)
                self.assertEqual(client.stats["errors"], 0)

    def test_opencode_explicit_rejection_and_exhausted_budget_do_not_downgrade_or_retry(self):
        client = self.gateway_client("claude-haiku-5-5", "messages", ["high"])
        model = client.cfg["fast_model"]
        self.respond = lambda *_: (400, {"error": {"message": "Effort rejected"}})
        self.assertIsNone(client.call_once(model, "Rejected?", 600)[0])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(client.cfg["fast_reasoning_effort"], "high")
        self.respond = lambda *_: (200, {"type": "message", "content": [], "stop_reason": "max_tokens",
                                        "usage": {"input_tokens": 10, "output_tokens": 600, "cost": 0.003}})
        self.assertFalse(client.call_once(model, "Budget?", 600)[0])
        self.assertEqual(len(self.requests), 2, "Explicit effort must not trigger a larger-budget paid retry")
        self.assertEqual([body["max_tokens"] for _, body in self.requests], [600, 600])
        self.assertAlmostEqual(client.stats["cost_usd"], 0.003)

    def test_opencode_cached_capabilities_survive_refresh_failure_but_removal_blocks_atomic_save(self):
        client = self.gateway_client("gpt-6.1-sol", "responses", ["low", "high"])
        model = client.cfg["fast_model"]
        fetcher = client.catalog.fetchers["opencode"]

        def unavailable(_):
            raise OSError("isolated catalog failure")

        client.catalog.fetchers["opencode"] = unavailable
        client.catalog.refresh()
        caps = reasoning.describe(model, client.cfg, client.catalog.get(model))
        self.assertEqual(caps["levels"], ["low", "high"])
        self.assertTrue(client.catalog.get(model)["pricing_stale"])
        self.respond = lambda *_: (200, self.response([self.text_item("East.")]))
        self.assertEqual(client.call_once(model, "Use cached capabilities", 600)[0], "East.")
        client.catalog.fetchers["opencode"] = fetcher
        self.gateway_rows[0]["capabilities"]["reasoning"]["effort"] = ["low"]
        client.catalog.refresh()
        before = dict(client.cfg)
        with self.assertRaises(ValueError):
            tuning.apply(client.cfg, {"fast_reasoning_effort": "high", "deep_model": "opencode:claude-haiku-5-5"},
                         catalog=client.catalog)
        self.assertEqual(client.cfg, before)
        self.assertIsNone(client.call_once(model, "No stale high", 600)[0])
        self.assertEqual(len(self.requests), 1)

    def test_opencode_native_usage_envelopes_keep_cache_reasoning_and_charge_coverage(self):
        cases = (
            ("gpt-6.1-sol", "responses",
             {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 40},
              "output_tokens_details": {"reasoning_tokens": 5}}, 20, 0.000144),
            ("gemini-3.8-flash", "generate-content",
             {"promptTokenCount": 100, "cachedContentTokenCount": 40,
              "candidatesTokenCount": 20, "thoughtsTokenCount": 5}, 25, 0.000164),
        )
        for native, api, usage, output, estimate in cases:
            with self.subTest(model=native):
                client = self.gateway_client(native, api, ["high"])
                current_usage = dict(usage)

                def respond(path, body, api=api, current_usage=current_usage):
                    if api == "responses":
                        return 200, {**self.response([self.text_item("East.")]), "usage": current_usage}
                    return 200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "East."}]},
                                                 "finishReason": "STOP"}], "usageMetadata": current_usage}

                self.respond = respond
                model = client.cfg["fast_model"]
                self.assertEqual(client.call_once(model, "Estimate", 600)[0], "East.")
                self.assertEqual(client.stats["uncached_in_tokens"], 60)
                self.assertEqual(client.stats["cache_read_tokens"], 40)
                self.assertEqual(client.stats["out_tokens"], output)
                self.assertAlmostEqual(client.stats["cost_usd"], estimate)
                current_usage["cost"] = 0
                client.call_once(model, "Reported zero", 600)
                current_usage.update(cost=0.02, opencode={"estimated": True,
                    "tokens": {"input": 7, "output": 9, "reasoning": 5, "cache_read": 3, "cache_write_5m": 2}})
                client.call_once(model, "Estimated billed categories", 600)
                self.assertEqual(client.stats["out_tokens"], output * 2 + 9)
                self.assertEqual(client.stats["cache_write_tokens"], 2)
                self.assertAlmostEqual(client.stats["cost_usd"], estimate + 0.02)
                current_usage.clear()
                client.call_once(model, "Unknown usage", 600)
                self.assertEqual(client.stats["cost_coverage"],
                                 {"reported": 1, "estimated": 2, "unknown": 1, "stale_estimates": 0})

    def test_opencode_refusals_and_malformed_replies_keep_reported_charges_without_retry(self):
        cases = (
            ("claude-haiku-5-5", "messages",
             {"type": "message", "stop_reason": "refusal", "content": [], "usage": {"cost": 0.02}}),
            ("gpt-6.1-sol", "responses",
             {**self.response([{"type": "message", "content": [{"type": "refusal", "refusal": "Blocked"}]}]),
              "usage": {"cost": 0.02}}),
            ("gemini-3.8-flash", "generate-content",
             {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"cost": 0.02}}),
            ("kimi-k3", "chat-completions",
             {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}],
              "usage": {"cost": 0.02}}),
            ("gpt-6.1-sol", "responses",
             {**self.response([{"type": "function_call", "call_id": "q", "name": "quest", "arguments": "{"}]),
              "usage": {"cost": 0.02}}),
            ("claude-haiku-5-5", "messages",
             {"type": "message", "content": [None], "usage": {"cost": 0.02}}),
            ("gpt-6.1-sol", "responses",
             {**self.response([None]), "usage": {"cost": 0.02}}),
            ("gemini-3.8-flash", "generate-content",
             {"candidates": [None], "usageMetadata": {"cost": 0.02}}),
            ("kimi-k3", "chat-completions",
             {"choices": [None], "usage": {"cost": 0.02}}),
        )
        for native, api, response in cases:
            with self.subTest(model=native, response=response):
                client = self.gateway_client(native, api, ["high"])
                self.respond = lambda *_, response=response: (200, response)
                before = len(self.requests)
                self.assertIsNone(client.call_once(client.cfg["fast_model"], "Answer?", 600)[0])
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(client.stats["cost_usd"], 0.02)
                self.assertEqual(client.stats["cost_coverage"]["reported"], 1)
                self.assertEqual(client.stats["errors"], 1)


if __name__ == "__main__":
    unittest.main()
