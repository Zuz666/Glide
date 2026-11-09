#!/usr/bin/env python3
"""Multilingual coach delivery, provider accounting, English fallback, streaming, startup and authenticated samples.

All provider fetches are in-memory stand-ins; any accidental real network request fails immediately.
Run: python tests/test_localization.py
"""
import http.client
import json
import os
import re
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.append(ROOT)
for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(key, None)

import app  # noqa: E402
import dashboard  # noqa: E402
import voice  # noqa: E402
from coach import Coach, CoachDeps  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from localization import LocalizedText, Localizer  # noqa: E402
from state import GameState  # noqa: E402


class Provider:
    name, label, quiet_fallback = "endpoint", "Test endpoint", True

    def __init__(self, translated="Bereit.", answer="Gehe nach Norden."):
        self.translated, self.answer = translated, answer
        self.bodies = []
        self.failure = None

    def option_sets(self, model, opts, deep):
        return [{}]

    def caching(self, model):
        return "implicit"

    def request(self, body, attempt, on_text):
        self.bodies.append(body)
        return body, bool(on_text)

    def fetch(self, body, stream, on_text, timeout):
        if self.failure:
            raise self.failure
        content = body["messages"][0]["content"]
        translating = isinstance(content, str) and content.startswith("{") and "text" in json.loads(content)
        text = self.translated if translating else self.answer + "\nFACTS: q:123"
        if on_text:
            on_text(text + "\n")
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 17, "output_tokens": 5}}

    def parse(self, data):
        return data

    def accepted(self, model, attempt):
        pass


def make_coach(language="de", speak=None, emit=None, enabled=True):
    usage, messages = [], []
    cfg = {"enabled": enabled, "fast_model": "ep:test:fake", "deep_model": "ep:test:fake",
           "endpoints": [{"id": "test", "base_url": "http://127.0.0.1:1/v1"}]}
    coach = Coach(cfg, GlideDB("/missing-localization-test.db"), emit or messages.append, offline_ok=True,
                  deps=CoachDeps(language=language, speak_fn=speak, usage_fn=usage.append))
    provider = Provider()
    coach.llm.providers["ep:test"] = provider
    return coach, provider, messages, usage


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("Real network is forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_two_languages_use_real_client_and_accounting(self):
        for language, translated in (("ru-RU", "Готово."), ("de-DE", "Bereit.")):
            with self.subTest(language=language):
                coach, provider, messages, usage = make_coach(language)
                provider.translated = translated
                message = {"kind": "info", "text": "Glide voice ready.", "speak": True, "model": "ep:test:fake"}
                coach.emit(message)
                self.assertEqual(message["text"], translated)
                self.assertEqual(message["original_text"], "Glide voice ready.")
                self.assertEqual(message["model"], "ep:test:fake")
                self.assertEqual(len(provider.bodies), 1)
                self.assertRegex(provider.bodies[0]["system"][0]["text"], r"\b" + language[:2] + r"\b")
                self.assertEqual(coach.stats["calls"], 1)
                self.assertEqual(usage[0]["path"], "fast")
                self.assertEqual(usage[0]["in"], 17)
                self.assertIs(messages[-1], message)
                before = len(provider.bodies)
                coach.localization.message(message)
                self.assertEqual(len(provider.bodies), before, "prepared text is not translated twice")

    def test_unavailable_means_english_and_no_request(self):
        for reason in ("disabled", "missing-key", "suspended", "backoff", "missing-fast-endpoint"):
            with self.subTest(reason=reason):
                coach, provider, _messages, _usage = make_coach(enabled=reason != "disabled")
                if reason == "missing-key":
                    coach.llm.keys["endpoint"] = ""
                elif reason == "suspended":
                    coach.llm.suspended, coach.llm.suspended_for = "API key rejected", "endpoint"
                elif reason == "backoff":
                    coach.llm.backoff_until = time.time() + 100
                elif reason == "missing-fast-endpoint":
                    coach.cfg["fast_model"] = "ep:absent:fake"  # the available deep model must not be charged instead
                self.assertEqual(coach.localize("Original English."), {"text": "Original English.", "language": "en"})
                self.assertEqual(provider.bodies, [])
        coach, provider, _messages, _usage = make_coach("en")
        self.assertEqual(coach.localize("English."), {"text": "English.", "language": "en"})
        self.assertEqual(provider.bodies, [])

    def test_translator_failure_reports_diagnostics_without_recursion(self):
        coach, provider, messages, _usage = make_coach()
        provider.failure = RuntimeError("socket diagnostic 127.0.0.1:1 /v1")
        result = coach.localize("Still English.")
        self.assertEqual(result, {"text": "Still English.", "language": "en"})
        self.assertEqual(len(provider.bodies), 1)
        self.assertTrue(any("socket diagnostic 127.0.0.1:1 /v1" in m["text"] for m in messages))
        self.assertEqual(coach.stats["errors"], 1)
        self.assertGreater(coach.llm.backoff_until, time.time())

    def test_empty_translation_preserves_english_original(self):
        coach, provider, messages, _usage = make_coach()
        provider.translated = ""
        self.assertEqual(coach.localize("English sample."), {"text": "English sample.", "language": "en"})
        self.assertEqual(len(provider.bodies), 1)

    def test_streamed_native_answer_is_not_retranslated_or_spoken_twice(self):
        spoken = []
        coach, provider, _messages, _usage = make_coach(speak=lambda text, kind, urgent: spoken.append(text))
        question = "Where is NPC:123? Keep my question unchanged."
        result = coach.answer_once(question, state=GameState(), voice=True)
        answer = result["answer"]
        self.assertEqual(answer["text"], "Gehe nach Norden.")
        self.assertIsInstance(answer["text"], LocalizedText)
        self.assertEqual(spoken, ["Gehe nach Norden."])
        self.assertEqual(answer["spoken"], "Gehe nach Norden.")
        final_voice = types.SimpleNamespace(say=lambda text, **kw: spoken.append(text))
        app.App._say(types.SimpleNamespace(voice=final_voice), answer)
        self.assertEqual(spoken, ["Gehe nach Norden."])
        self.assertEqual(len(provider.bodies), 1)
        self.assertTrue(any(re.search(r"\bde\b", block["text"]) for block in provider.bodies[0]["system"]))
        self.assertEqual(next(x["text"] for x in coach.convo if x["who"] == "player"), question)
        self.assertNotIn("FACTS", answer["text"])

    def test_all_deterministic_message_kinds_are_prepared_but_user_text_is_not(self):
        coach, provider, _messages, _usage = make_coach()
        for kind in ("answer", "callout", "review", "briefing", "reminder", "banter", "info", "error", "status", "model"):
            message = {"kind": kind, "text": "English notice.", "id": "q:123", "usage": {"tokens": 17}}
            coach.localization.message(message)
            self.assertEqual(message["text"], "Bereit.")
            self.assertEqual(message["id"], "q:123")
            self.assertEqual(message["usage"], {"tokens": 17})
        before = len(provider.bodies)
        question = {"kind": "question", "text": "🎙 My original question?", "model": "stt-id"}
        coach.localization.message(question)
        self.assertEqual(question["text"], "🎙 My original question?")
        self.assertEqual(len(provider.bodies), before)

    def test_async_delivery_never_blocks_capture_and_uses_one_prepared_text(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        class BlockingClient:
            def translate(self, text, language):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test translator timed out")
                return "Bereit."

        localizer = Localizer(BlockingClient(), "de")
        delivered = []
        try:
            localizer.deliver({"kind": "reminder", "text": "Ready.", "speak": True},
                              lambda message: (delivered.append(message), done.set()))
            self.assertTrue(entered.wait(2))
            self.assertFalse(done.is_set(), "delivery waits, caller does not")
        finally:
            release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(delivered[0]["text"], "Bereit.")
        self.assertEqual(delivered[0]["original_text"], "Ready.")

    def test_briefing_translates_only_objective_before_early_speech(self):
        spoken = []
        coach, provider, _messages, _usage = make_coach(speak=lambda text, kind, urgent: spoken.append(text))
        coach.state = GameState()
        provider.translated = "Neue Quest: Quest:123. Besiege Eber."
        infos = [{"id": 123, "name": "Quest:123", "objective_text": "Kill boars."}]
        with patch.object(coach.briefings, "_brief_infos", return_value=infos):
            message = coach.briefings.brief([123])
        self.assertEqual(message["text"], "Neue Quest: Quest:123. Besiege Eber. Gehe nach Norden.")
        self.assertEqual(spoken, ["Neue Quest: Quest:123. Besiege Eber.", "Gehe nach Norden."])
        app.App._say(types.SimpleNamespace(voice=types.SimpleNamespace(
            say=lambda text, **kw: spoken.append(text))), message)
        self.assertEqual(len(spoken), 2, "final delivery does not repeat either part")
        self.assertEqual(len(provider.bodies), 2, "objective translation plus one native explanation")

    def test_app_displays_and_speaks_identical_prepared_text(self):
        coach, provider, _messages, _usage = make_coach()
        displayed, spoken, logged = [], [], []
        done = threading.Event()
        running = app.App.__new__(app.App)
        running.coach, running.overlay, running._flags = coach, None, {}
        running.msg_history, running._held = [], []
        running._hist_lock, running._ptt_down = threading.Lock(), False
        running.hub = types.SimpleNamespace(publish=lambda kind, message: displayed.append((kind, message)))
        running.voice = types.SimpleNamespace(say=lambda text, **kw: (spoken.append(text), done.set()))
        running.write_log = logged.append
        running.on_message({"kind": "reminder", "text": "Ready.", "speak": True})
        self.assertTrue(done.wait(2))
        self.assertEqual(spoken, ["Bereit."])
        self.assertEqual(displayed[0][1]["text"], spoken[0])
        self.assertEqual(running.msg_history[0]["text"], spoken[0])
        self.assertEqual(logged[0]["msg"]["original_text"], "Ready.")
        self.assertEqual(len(provider.bodies), 1)

    def test_hub_and_standalone_startup_share_coach_translator(self):
        coach, provider, _messages, _usage = make_coach()
        for output in ("hub", "standalone"):
            with self.subTest(output=output):
                said, done, hooks = [], threading.Event(), []
                if output == "hub":
                    narrator = voice.HubVoice({"enabled": True, "announce": True},
                                               types.SimpleNamespace(on_first_client=hooks.append), announce=False)
                else:
                    narrator = voice.Voice.__new__(voice.Voice)
                    narrator.cfg, narrator.enabled = {"announce": True}, True
                narrator.say = lambda text, said=said, done=done: (said.append(text), done.set())
                narrator.announce(coach.localize_text)
                if output == "hub":
                    self.assertEqual(said, [])
                    hooks[0]()
                self.assertTrue(done.wait(2))
                self.assertEqual(said, ["Bereit."])
        self.assertEqual(len(provider.bodies), 2)

    def test_localize_route_requires_token_and_returns_effective_language(self):
        coach, provider, _messages, _usage = make_coach()
        fake = types.SimpleNamespace(coach=coach)
        handler = dashboard.post_json(lambda body: app.App.on_localize(fake, body))
        server = dashboard.serve(dashboard.Hub(), 0, {("POST", "/localize"): handler}, token="test-token")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def request(body, token=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            headers = {"X-Glide-Token": token} if token is not None else {}
            connection.request("POST", "/localize", json.dumps(body), headers)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result

        self.assertEqual(request({"text": "Ready."})[0], 403)
        self.assertEqual(provider.bodies, [])
        self.assertEqual(request({"text": "Ready."}, "test-token"),
                         (200, {"ok": True, "text": "Bereit.", "language": "de"}))
        coach.llm.enabled = False
        self.assertEqual(request({"text": "Ready."}, "test-token"),
                         (200, {"ok": True, "text": "Ready.", "language": "en"}))
        self.assertEqual(request({"text": 123}, "test-token")[0], 400)


if __name__ == "__main__":
    unittest.main()
