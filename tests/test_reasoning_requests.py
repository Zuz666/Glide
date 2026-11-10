#!/usr/bin/env python3
"""Reasoning selections through real local HTTP: roles, tool turns, validation and no paid fallback probes."""
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
from llm_client import LLMClient  # noqa: E402
import tuning  # noqa: E402


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

        def local_open(request, *args, **kwargs):
            # Keep the production endpoint identity for routing, but never allow an external request.
            path = urllib.parse.urlsplit(request.full_url).path
            local_request = urllib.request.Request(self.local + path, data=request.data,
                                                  headers=request.headers, method=request.method)
            return real_open(local_request, *args, **kwargs)

        self.network = patch("urllib.request.urlopen", side_effect=local_open)
        self.network.start()
        self.addCleanup(self.network.stop)
        self.env = patch.dict(os.environ, {key: "" for key in (
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")})
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

    def test_custom_chat_auto_does_not_force_none_or_change_api(self):
        client = self.client(endpoints=[{"id": "openai", "base_url": self.local + "/v1"}])

        def respond(path, body):
            if path != "/v1/chat/completions" or "reasoning_effort" in body or "max_tokens" in body:
                return 400, {"error": {"message": "Chat endpoint rejects forced reasoning or old token limit"}}
            return 200, self.chat("Turn in the quest.")

        self.respond = respond
        self.assertEqual(client.call_once(client.cfg["fast_model"], "Where?", 600)[0], "Turn in the quest.")
        self.assertEqual(self.requests[0][1]["max_completion_tokens"], 600)

    def test_explicit_rejection_is_reported_without_retrying_another_level(self):
        client = self.client(endpoints=[{"id": "openai", "base_url": self.local + "/v1"}],
                             fast_reasoning_effort="high")
        self.respond = lambda path, body: (400, {"error": {"message": "Unsupported reasoning_effort high; use low"}})
        self.assertIsNone(client.call_once(client.cfg["fast_model"], "Where?", 600)[0])
        self.assertEqual(len(self.requests), 1, "an explicit rejected level must not trigger a paid fallback probe")
        self.assertTrue(any("Unsupported reasoning_effort high" in m["text"] for m in self.errors))

    def test_gemini_auto_cache_is_separate_from_deep_and_explicit_choices(self):
        client = self.client(fast_model="gemini:gemini-3.8-flash", deep_model="gemini:gemini-3.8-flash")

        def respond(path, body):
            if body.get("reasoning_effort") == "minimal":
                return 400, {"error": {"message": "Use low"}}
            return 200, self.chat("Continue.")

        self.respond = respond
        model = client.cfg["fast_model"]
        self.assertEqual(client.call_once(model, "Fast", 600)[0], "Continue.")
        self.assertEqual(client.call_once(model, "Deep", 1200, deep=True)[0], "Continue.")
        client.cfg["fast_reasoning_effort"] = "high"
        self.assertEqual(client.call_once(model, "Explicit", 600)[0], "Continue.")
        client.cfg["fast_reasoning_effort"] = "auto"
        self.assertEqual(client.call_once(model, "Automatic again", 600)[0], "Continue.")
        self.assertEqual([b.get("reasoning_effort") for _, b in self.requests], ["minimal", "low", None, "high", "low"])

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


if __name__ == "__main__":
    unittest.main()
