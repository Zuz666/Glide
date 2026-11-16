#!/usr/bin/env python3
"""OpenCode catalog, conditional tariff and fixed-origin tool-loop contracts. No live API."""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import models  # noqa: E402
import openai_compat  # noqa: E402
import opencode  # noqa: E402
import providers  # noqa: E402
import reasoning  # noqa: E402


TARIFF = {
    "input": {"price": 0.2, "cache": {"read": 0.02, "write": {"30m": 0.25, "12h": 0.5}},
              "overrides": [{"when": {"context": {"gt": 272000}}, "price": 0.4,
                             "cache": {"read": 0.04, "write": {"30m": 0.5, "12h": 1}}}]},
    "output": {"price": 1.2, "overrides": [{"when": {"context": {"gt": 272000}}, "price": 1.8}]},
}


def chat(mid, tariff=None):
    return {"id": mid, "type": "chat", "name": mid, "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": {"tools": {"supported": True}, "reasoning": {"supported": True, "effort": ["high"]}},
            "limit": {"context": 1050000}, "cost": {"text": copy.deepcopy(tariff or TARIFF)},
            "api": {"chat-completions": {"base_url": "https://attacker.invalid/v1"}}}


class CatalogTests(unittest.TestCase):
    def test_authenticated_discovery_keeps_raw_exclusions_and_unknown_prices(self):
        paid = chat("vendor/paid")
        unknown = chat("new-paid")
        unknown.pop("cost")
        free = chat("big-pickle", {"input": {"price": 0}, "output": {"price": 0}})
        specialty = {**chat("decision"), "type": "system-one"}
        image = {**chat("image"), "modalities": {"input": ["text"], "output": ["image"]}}
        calls = []

        def getter(url, headers):
            calls.append((url, headers))
            return {"data": [paid, unknown, free, specialty, image]}

        self.assertEqual(models.fetch_opencode(None, getter=getter), [])
        self.assertEqual(calls, [])
        with tempfile.TemporaryDirectory() as root:
            catalog = models.Catalog(os.path.join(root, "models.json"), opencode_key="draft-key",
                                     fetchers={"anthropic": lambda k: [], "gemini": lambda k: [],
                                               "openrouter": lambda k: [],
                                               "opencode": lambda k: models.fetch_opencode(k, getter=getter)})
            catalog.refresh()
            offered = {m["id"]: m for m in catalog.entries() if m["provider"] == "opencode"}
            self.assertEqual(set(offered), {"opencode:vendor/paid", "opencode:new-paid"})
            self.assertEqual(offered["opencode:vendor/paid"]["pricing"], TARIFF)
            self.assertIsNone(offered["opencode:new-paid"]["price"])
            self.assertIsNone(catalog.get("opencode:decision"))
            self.assertTrue(catalog.raw_get("opencode:decision")["excluded_reason"])
            self.assertTrue(catalog.raw_get("opencode:big-pickle")["excluded_reason"])
        self.assertEqual(calls, [(opencode.MODELS_URL, {"authorization": "Bearer draft-key"})])
        self.assertEqual(models.provider("opencode:vendor/paid"), "opencode")
        self.assertFalse(openai_compat.is_openrouter("opencode:vendor/paid"))

    def test_failed_refresh_and_reload_preserve_successful_import_and_rates(self):
        attempts = 0

        def getter(url, headers):
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                raise OSError("offline")
            return {"data": [chat("paid")]}

        fetchers = {"anthropic": lambda k: [], "gemini": lambda k: [], "openrouter": lambda k: [],
                    "opencode": lambda k: models.fetch_opencode(k, getter=getter)}
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "models.json")
            catalog = models.Catalog(path, opencode_key="test-key", fetchers=fetchers)
            with patch("models.time.time", return_value=1000):
                catalog.refresh()
                self.assertEqual(catalog.get("opencode:paid")["pricing_updated"], 1000)
                self.assertFalse(catalog.get("opencode:paid")["pricing_stale"])
            with patch("models.time.time", return_value=2000):
                status = catalog.refresh()
            self.assertIn("offline", status["errors"]["opencode"])
            self.assertEqual(status["provider_updated"]["opencode"], 1000)
            restored = models.Catalog(path).get("opencode:paid")
            self.assertEqual(restored["pricing_updated"], 1000)
            self.assertTrue(restored["pricing_stale"])
            self.assertEqual(restored["pricing"], TARIFF)

    def test_successful_rates_expire_in_memory_and_on_reload_until_refreshed(self):
        fetchers = {"anthropic": lambda k: [], "gemini": lambda k: [], "openrouter": lambda k: [],
                    "opencode": lambda k: models.fetch_opencode(k, getter=lambda url, headers: {"data": [chat("paid")]})}
        with tempfile.TemporaryDirectory() as root, patch("models.time.time", return_value=1000) as clock:
            path = os.path.join(root, "models.json")
            catalog = models.Catalog(path, opencode_key="test-key", fetchers=fetchers)
            catalog.refresh()
            self.assertFalse(catalog.get("opencode:paid")["pricing_stale"])
            clock.return_value = 1000 + models.MAX_AGE_S + 1
            self.assertTrue(catalog.get("opencode:paid")["pricing_stale"])
            self.assertTrue(catalog.status()["provider_stale"]["opencode"])
            restored = models.Catalog(path, opencode_key="test-key", fetchers=fetchers)
            self.assertTrue(restored.get("opencode:paid")["pricing_stale"])
            self.assertEqual(restored.get("opencode:paid")["pricing_updated"], 1000)
            restored.refresh()
            self.assertFalse(restored.get("opencode:paid")["pricing_stale"])

    def test_injected_catalog_without_new_fetcher_stays_offline(self):
        fetchers = {"anthropic": lambda k: [], "gemini": lambda k: [], "openrouter": lambda k: []}
        with tempfile.TemporaryDirectory() as root, patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            catalog = models.Catalog(os.path.join(root, "models.json"), opencode_key="test-key", fetchers=fetchers)
            self.assertIn("opencode", catalog.refresh()["errors"])
            catalog = models.Catalog(os.path.join(root, "missing.json"), fetchers={**fetchers,
                                      "opencode": lambda k: self.fail("missing key invoked fetcher")})
            self.assertIn("OPENCODE_API_KEY", catalog.refresh()["errors"]["opencode"])


class CostTests(unittest.TestCase):
    def test_cached_writes_reads_and_context_surcharge_have_literal_totals(self):
        usage = {"input_tokens": 200000, "output_tokens": 10000, "reasoning_tokens": 2000,
                 "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 100000,
                 "cache_write_tokens_by_duration": {"30m": 50000, "12h": 50000}}
        self.assertAlmostEqual(opencode.estimate_cost({"pricing": TARIFF}, usage), 0.177)
        short = {**usage, "input_tokens": 50000, "cache_read_input_tokens": 20000,
                 "cache_creation_input_tokens": 30000,
                 "cache_write_tokens_by_duration": {"30m": 10000, "12h": 20000}}
        self.assertAlmostEqual(opencode.estimate_cost({"pricing": TARIFF}, short), 0.0349)

    def test_unknown_conditions_durations_and_missing_prices_are_not_zero(self):
        usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 10}
        self.assertIsNone(opencode.estimate_cost({"pricing": TARIFF}, usage))
        unsupported = copy.deepcopy(TARIFF)
        unsupported["input"]["overrides"].append({"when": {"peak_hours": True}, "price": 9})
        self.assertIsNone(opencode.estimate_cost({"pricing": unsupported}, {"input_tokens": 100, "output_tokens": 10}))
        self.assertIsNone(opencode.estimate_cost({"pricing": None}, usage))
        self.assertIsNone(opencode.estimate_cost({"pricing": {"input": {"price": 1}, "output": {}}}, usage))
        self.assertIsNone(opencode.estimate_cost({"pricing": {"input": {}, "output": {}}},
                                               {"input_tokens": 0, "output_tokens": 0}))
        timed = copy.deepcopy(TARIFF)
        timed["multipliers"] = [{"when": {"date": {"gte": "2027-01-01"}}, "factor": 2}]
        self.assertIsNone(opencode.estimate_cost({"pricing": timed}, {"input_tokens": 100, "output_tokens": 10}))
        scalar = {"input": {"price": 1, "cache": {"write": 1.25}}, "output": {"price": 5}}
        self.assertIsNone(opencode.estimate_cost({"pricing": scalar},
                                               {**usage, "cache_write_tokens_by_duration": {"1h": 10}}))

    def test_canonical_billed_tokens_override_wire_totals_without_double_counting(self):
        normalized = opencode.normalize_usage({"prompt_tokens": 999999, "completion_tokens": 20000,
            "opencode": {"tokens": {"input": 200000, "output": 10000, "reasoning": 2000,
                                      "cache_read": 100000, "cache_write_30m": 50000, "cache_write_12h": 50000}}})
        self.assertEqual(normalized["input_tokens"], 200000)
        self.assertEqual(normalized["output_tokens"], 10000)
        self.assertEqual(normalized["reasoning_tokens"], 2000)
        self.assertAlmostEqual(opencode.estimate_cost({"pricing": TARIFF}, normalized), 0.177)
        fallback = opencode.normalize_usage({"prompt_tokens": 100, "completion_tokens": 20,
                                            "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
                                            "completion_tokens_details": {"reasoning_tokens": 5}})
        self.assertEqual(fallback["input_tokens"], 50)
        self.assertEqual(fallback["output_tokens"], 20)
        self.assertIsNone(opencode.estimate_cost({"pricing": TARIFF}, fallback))

    def test_reported_zero_estimate_invalid_charge_and_missing_usage_remain_distinct(self):
        provider = providers.OpenCodeProvider({}, "test-key")
        zero = provider.parse({"usage": {"cost": 0, "opencode": {"list_cost": 5}}}, "opencode:paid")["usage"]
        self.assertEqual(zero["cost_usd"], 0)
        self.assertIs(zero["cost_estimated"], False)
        estimated = provider.parse({"usage": {"cost": 0.25, "opencode": {"estimated": True}}}, "opencode:paid")["usage"]
        self.assertEqual(estimated["cost_usd"], 0.25)
        self.assertIs(estimated["cost_estimated"], True)
        for value in (float("nan"), float("inf"), -1, True, "not-money"):
            self.assertNotIn("cost_usd", provider.parse({"usage": {"cost": value}}, "opencode:paid")["usage"])
        listed = provider.parse({"usage": {"opencode": {"list_cost": 0.5}}}, "opencode:paid")["usage"]
        self.assertEqual(listed["cost_usd"], 0.5)
        self.assertIs(listed["cost_estimated"], True)
        self.assertIsNone(opencode.estimate_cost({"pricing": TARIFF}, provider.parse({}, "opencode:paid")["usage"]))

    def test_native_messages_usage_keeps_exclusive_input_cache_durations_and_charge(self):
        provider = providers.OpenCodeProvider({}, "test-key")
        parsed = provider.parse({"type": "message", "model": "claude-haiku-5-5", "content": [],
                                 "usage": {"input_tokens": 100, "output_tokens": 30,
                                           "cache_read_input_tokens": 50, "cache_creation_input_tokens": 20,
                                           "cache_creation": {"ephemeral_5m_input_tokens": 15,
                                                              "ephemeral_1h_input_tokens": 5}, "cost": 0}}, "opencode:claude-haiku-5-5")
        usage = parsed["usage"]
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cache_read_input_tokens"], 50)
        self.assertEqual(usage["cache_creation_input_tokens"], 20)
        self.assertEqual(usage["cache_write_tokens_by_duration"], {"5m": 15, "1h": 5})
        self.assertEqual((usage["cost_usd"], usage["cost_estimated"]), (0, False))
        self.assertNotIn("tokens_unknown", usage)
        estimate = provider.parse({"type": "message", "content": [], "usage": {
            "input_tokens": 100, "output_tokens": 30, "opencode": {"list_cost": 0.005}}}, "opencode:claude-haiku-5-5")["usage"]
        self.assertEqual((estimate["cost_usd"], estimate["cost_estimated"]), (0.005, True))


class ProviderTests(unittest.TestCase):
    def test_fixed_origin_native_id_and_two_round_private_reasoning_continuity(self):
        provider = providers.OpenCodeProvider({"opencode_url": "https://attacker.invalid"}, "test-key")
        reply = {"choices": [{"finish_reason": "tool_calls", "message": {
            "reasoning_content": "private reasoning", "reasoning_details": [{"type": "encrypted", "data": "opaque"}],
            "tool_calls": [{"id": "t1", "function": {"name": "lookup", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0}}
        requests = []

        def urlopen(req, timeout):
            requests.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
            return io.BytesIO(json.dumps(reply if len(requests) == 1 else {
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5}}).encode())

        body = {"model": "opencode:vendor/paid", "max_tokens": 50,
                "messages": [{"role": "user", "content": "question"}],
                "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}
        with patch("opencode.open_request", side_effect=urlopen):
            req, stream = provider.request(body, {}, None)
            first = provider.parse(provider.fetch(req, stream, None, 10), body["model"])
            body["messages"] += [{"role": "assistant", "content": first["content"]},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result"}]}]
            req, stream = provider.request(body, {}, None)
            final = provider.parse(provider.fetch(req, stream, None, 10), body["model"])
        self.assertEqual(final["content"], [{"type": "text", "text": "answer"}])
        self.assertEqual([r[0] for r in requests], [opencode.CHAT_URL, opencode.CHAT_URL])
        self.assertEqual([r[1] for r in requests], ["Bearer test-key", "Bearer test-key"])
        self.assertEqual(requests[1][2]["model"], "vendor/paid")
        self.assertEqual(requests[1][2]["messages"][1]["reasoning_content"], "private reasoning")
        self.assertEqual(requests[1][2]["messages"][1]["reasoning_details"], [{"type": "encrypted", "data": "opaque"}])
        self.assertEqual(requests[1][2]["messages"][2], {"role": "tool", "tool_call_id": "t1", "content": "result"})
        for flavor, model in (("openrouter", "vendor/model"), ("gemini", "gemini:test"), ("endpoint", "ep:local:test")):
            wire = openai_compat.to_openai({**body, "model": model}, flavor)
            self.assertNotIn("private reasoning", json.dumps(wire))
            self.assertNotIn("opaque", json.dumps(wire))
        self.assertEqual(provider.options(body["model"], None), {})
        native_req, _ = providers.AnthropicProvider({}, "native-test-key").request(
            {**body, "model": "claude-sonnet-5-5"}, {}, None)
        self.assertNotIn("private reasoning", native_req.data.decode())
        self.assertNotIn("opaque", native_req.data.decode())
        self.assertNotIn(opencode.META_BLOCK, native_req.data.decode())
        self.assertEqual(reasoning.describe(body["model"], {}, {"reasoning": {"effort": ["high"]}})["levels"], [])

    def test_native_thinking_is_private_to_gateway_model_and_native_transport(self):
        provider = providers.OpenCodeProvider({"opencode_url": "https://attacker.invalid"}, "test-key")
        thought = {"type": "thinking", "thinking": "", "signature": "gateway-only"}
        redacted = {"type": "redacted_thinking", "data": "gateway-redacted"}
        tool = {"type": "tool_use", "id": "t1", "name": "lookup", "input": {}}
        first = provider.parse({"type": "message", "model": "claude-haiku-5-5",
                                "content": [thought, redacted, tool], "stop_reason": "tool_use"}, "opencode:claude-haiku-5-5")
        body = {"model": "opencode:claude-haiku-5-5", "max_tokens": 50,
                "messages": [{"role": "assistant", "content": first["content"]}]}
        req, stream = provider.request(body, provider.options(body["model"], "medium"), None)
        self.assertEqual(req.full_url, "https://opencode.ai/inference/anthropic/v1/messages")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
        self.assertFalse(stream)
        self.assertEqual(json.loads(req.data)["messages"][0]["content"], [thought, redacted, tool])
        native_req, _ = providers.AnthropicProvider({}, "native-key").request(
            {**body, "model": "claude-haiku-5-5"}, {}, None)
        changed_model, _ = provider.request({**body, "model": "opencode:claude-sonnet-5-5"}, {}, None)
        foreign_blocks = [{"type": "thinking", "thinking": "", "signature": "foreign-native"},
                          {"type": "openai_meta", "reasoning_details": ["foreign-router"]},
                          {"type": "openai_responses_output", "items": ["foreign-responses"]},
                          {**tool, "extra_content": {"google": {"thought_signature": "foreign-google"}}}]
        imported, _ = provider.request({**body, "messages": [{"role": "assistant", "content": foreign_blocks}]},
                                      {}, None)
        for other in (native_req, changed_model, imported):
            self.assertNotIn("gateway-only", other.data.decode())
            self.assertNotIn("gateway-redacted", other.data.decode())
        self.assertNotIn("foreign-", imported.data.decode())
        for flavor, model in (("openrouter", "vendor/model"), ("gemini", "gemini:test"),
                              ("endpoint", "ep:local:test"), ("opencode", "opencode:unknown")):
            wire = openai_compat.to_openai({**body, "model": model}, flavor)
            self.assertNotIn("gateway-only", json.dumps(wire))
            self.assertNotIn("gateway-redacted", json.dumps(wire))

    def test_missing_key_and_vendor_secret_host_protection(self):
        with self.assertRaisesRegex(ValueError, "OPENCODE_API_KEY"):
            providers.OpenCodeProvider({}, None).request({"model": "opencode:paid"}, {}, None)
        self.assertEqual(openai_compat.endpoint_key_env("evil", "https://attacker.invalid/v1", "OPENCODE_API_KEY"),
                         "GLIDE_ENDPOINT_EVIL_KEY")
        self.assertEqual(openai_compat.endpoint_key_env("oc", opencode.BASE_URL, "OPENCODE_API_KEY"), "OPENCODE_API_KEY")


if __name__ == "__main__":
    unittest.main()
