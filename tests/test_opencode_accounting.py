#!/usr/bin/env python3
"""OpenCode request charges, tariff estimates and incomplete sessions through local HTTP."""
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
import opencode  # noqa: E402
import models  # noqa: E402


class OpenCodeAccounting(unittest.TestCase):
    def setUp(self):
        self.logs, self.errors = [], []
        self.catalog_status = self.chat_status = 200
        self.redirect_to = None
        self.leaks, self.posts = [], 0
        self.usage = {"prompt_tokens": 10, "completion_tokens": 2}
        self.model = {
            "id": "accounting-model", "name": "Accounting model", "type": "chat",
            "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": {"tools": {"supported": True}},
            "limit": {"context": 100000, "output": 1000},
            "cost": {"text": {
                "input": {
                    "price": 2, "cache": {"read": 0.2, "write": {"5m": 2.5, "1h": 4}},
                    "overrides": [{"when": {"context": {"gt": 10000}}, "price": 3,
                                   "cache": {"read": 0.3, "write": {"5m": 3.75, "1h": 6}}}],
                },
                "output": {"price": 4, "overrides": [
                    {"when": {"context": {"gt": 10000}}, "price": 6}]},
            }},
            "api": {"chat-completions": {"base_url": "https://opencode.ai/inference/openai/v1"}},
        }
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = json.dumps(data).encode()
                redirect = owner.redirect_to if self.path != "/exfiltration" else None
                self.send_response(302 if redirect else status)
                if redirect:
                    self.send_header("Location", redirect)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/exfiltration":
                    owner.leaks.append(self.headers.get("Authorization"))
                self.reply(owner.catalog_status, {"data": [owner.model]})

            def do_POST(self):
                owner.posts += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if owner.chat_status != 200:
                    return self.reply(owner.chat_status, {"error": {"message": "Models Request permission required"}})
                self.reply(200, {"model": body["model"], "choices": [
                    {"finish_reason": "stop", "message": {"content": "Go north."}}],
                    "usage": owner.usage})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.local = local = "http://127.0.0.1:%d" % server.server_address[1]
        real_open = opencode.open_request

        def local_open(request, *args, **kwargs):
            # Redirect only the external HTTP seam; production routing still sees its real origin.
            path = urllib.parse.urlsplit(request.full_url).path
            request = urllib.request.Request(local + path, data=request.data,
                                             headers=request.headers, method=request.get_method())
            return real_open(request, *args, **kwargs)

        forbidden = patch("urllib.request.urlopen", side_effect=AssertionError("unexpected external HTTP"))
        forbidden.start()
        self.addCleanup(forbidden.stop)
        network = patch("opencode.open_request", side_effect=local_open)
        network.start()
        self.addCleanup(network.stop)
        credentials = {key: "" for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                                           "GOOGLE_API_KEY", "OPENROUTER_API_KEY")}
        credentials["OPENCODE_API_KEY"] = "isolated-fixture-key"
        environment = patch.dict(os.environ, credentials)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.catalog = models.Catalog(os.path.join(temporary.name, "models.json"),
                                      opencode_key="isolated-fixture-key",
                                      fetchers={"anthropic": lambda key: [], "gemini": lambda key: [],
                                                "openrouter": lambda key: [], "opencode": models.fetch_opencode})
        self.catalog.refresh()
        self.client = LLMClient({"fast_model": "opencode:accounting-model",
                                 "deep_model": "opencode:accounting-model"},
                                emit=self.errors.append, catalog=self.catalog, usage_fn=self.logs.append)

    def answer(self, model="opencode:accounting-model"):
        text, _ = self.client.call_once(model, "Where next?", 600)
        self.assertEqual(text, "Go north.", self.errors)

    def test_reported_zero_is_known_and_takes_priority_over_imported_rates(self):
        self.usage["cost"] = 0
        self.answer()
        self.assertEqual(self.logs[-1]["cost"], 0)
        self.assertEqual(self.logs[-1]["cost_basis"], "reported")
        self.assertEqual(self.client.stats["cost_coverage"],
                         {"reported": 1, "estimated": 0, "unknown": 0, "stale_estimates": 0})
        self.usage["cost"] = 0.4
        self.answer()
        self.assertAlmostEqual(self.client.stats["cost_usd"], 0.4)
        self.assertEqual(self.client.path_stats["fast"]["cost_coverage"]["reported"], 2)

    def test_provider_estimated_amount_is_not_relabelled_as_a_charge(self):
        self.usage.update(cost=0.05, opencode={"estimated": True})
        self.answer()
        self.assertEqual(self.logs[-1]["cost_basis"], "estimated")
        self.assertEqual(self.logs[-1]["cost"], 0.05)
        self.assertEqual(self.client.stats["cost_coverage"],
                         {"reported": 0, "estimated": 1, "unknown": 0, "stale_estimates": 0})

    def test_failed_catalog_refresh_retains_conditional_tariff_and_marks_its_estimate(self):
        self.catalog_status = 503
        self.catalog.refresh()
        self.usage = {
            "prompt_tokens": 15000, "completion_tokens": 500,
            "opencode": {"tokens": {"input": 4000, "output": 500, "reasoning": 100,
                                    "cache_read": 10000, "cache_write_5m": 1000, "cache_write_1h": 0}},
        }
        self.answer()
        # Context > 10k: 4k*3 + 10k*.3 + 1k*3.75 + .5k*6 dollars per million.
        self.assertAlmostEqual(self.client.stats["cost_usd"], 0.02175)
        self.assertEqual(self.logs[-1]["cost_basis"], "estimated")
        self.assertIs(self.logs[-1]["cost_stale"], True)
        self.assertEqual(self.client.stats["cost_coverage"],
                         {"reported": 0, "estimated": 1, "unknown": 0, "stale_estimates": 1})
        self.assertEqual(self.client.stats["in_tokens"], 15000)
        self.assertEqual(self.client.stats["out_tokens"], 500)

    def test_unpriced_answer_is_delivered_but_does_not_claim_a_complete_session_total(self):
        self.usage["cost"] = 0.03
        self.answer()
        del self.usage["cost"]
        self.answer("opencode:unpriced-model")
        self.assertIsNone(self.logs[-1]["cost"])
        self.assertEqual(self.logs[-1]["cost_basis"], "unknown")
        self.assertAlmostEqual(self.client.stats["cost_usd"], 0.03)
        self.assertEqual(self.client.stats["cost_coverage"],
                         {"reported": 1, "estimated": 0, "unknown": 1, "stale_estimates": 0})
        self.assertEqual(self.client.path_stats["fast"]["cost_coverage"]["unknown"], 1)

    def test_catalog_and_model_redirects_never_forward_the_key(self):
        self.redirect_to = self.local.replace("127.0.0.1", "localhost") + "/exfiltration"
        status = self.catalog.refresh()
        self.assertIn("302", status["errors"]["opencode"])
        self.assertTrue(self.catalog.get("opencode:accounting-model")["pricing_stale"])
        text, _ = self.client.call_once("opencode:accounting-model", "Where next?", 600)
        self.assertFalse(text)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.leaks, [])

    def test_read_only_key_holds_automatic_work_but_allows_an_explicit_retry(self):
        self.assertNotIn("opencode", self.catalog.status()["errors"])
        self.chat_status = 403
        text, _ = self.client.call_once("opencode:accounting-model", "Where next?", 600)
        self.assertFalse(text)
        # Automatic translations share the provider hold with coach callouts.
        self.assertIsNone(self.client.translate("Go north.", "ru"))
        self.assertFalse(self.client.available("opencode:accounting-model"))
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.client.stats["calls"], 0)
        # A player's explicit question can retry after permissions are corrected.
        self.chat_status = 200
        self.answer()
        self.assertEqual(self.posts, 2)
        self.assertTrue(self.client.available("opencode:accounting-model"))


if __name__ == "__main__":
    unittest.main()
