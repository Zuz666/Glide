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
import tempfile
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
import speech_markup  # noqa: E402
from rules import Trigger  # noqa: E402
import coach_tools  # noqa: E402
import reminders  # noqa: E402


class Provider:
    name, label, quiet_fallback = "endpoint", "Test endpoint", True

    def __init__(self, translated="Bereit.", answer="Gehe nach Norden."):
        self.translated, self.answer = translated, answer
        self.bodies = []
        self.failure = None
        self.chunks = None

    def option_sets(self, model, opts, deep, effort="auto"):
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
            for chunk in self.chunks if self.chunks is not None else (text + "\n",):
                on_text(chunk)
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 17, "output_tokens": 5}}

    def parse(self, data):
        return data

    def accepted(self, model, attempt, deep=False):
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
        with open(os.path.join(ROOT, "desktop", "src", "shared", "engine.ts"), encoding="utf-8") as source:
            schema = re.search(r"^export interface CoachMessage \{\n(.*?)^\}", source.read(), re.S | re.M)
        self.assertIsNotNone(schema)
        self.message_fields = set(re.findall(r"^  (\w+)\??:", schema.group(1), re.M))

    def speech_session(self, *, profile="standard", mode="ai", features=("rate", "pitch", "pauses"),
                       provider="windows", enabled=True):
        coach, remote, messages, usage = make_coach("en", enabled=enabled)
        queued, displayed = [], []
        running = app.App.__new__(app.App)
        running.coach, running.overlay, running._flags = coach, None, {}
        running.cfg = {"voice": {"markup_mode": mode, "markup_profile": profile}}
        running.msg_history, running._held = [], []
        running._hist_lock, running._ptt_down = threading.Lock(), False
        def publish(kind, message):
            if kind == "message":
                self.assertLessEqual(set(message), self.message_fields, "public message keys must be declared")
            displayed.append((kind, message))
        running.hub = types.SimpleNamespace(publish=publish)
        running.write_log = lambda message: None
        destination = {"provider": provider, "identity": "test-token", "features": list(features)}
        running.voice = types.SimpleNamespace(
            say=lambda text, **kw: queued.append({"text": text, **kw}),
            markup_destination=lambda: destination)
        coach.deps.speak_fn = running.speak_early
        coach.deps.speech_policy_fn = lambda: speech_markup.policy(running.cfg["voice"], destination)
        coach._emit_raw = lambda message: (messages.append(message), running.on_message(message))
        return coach, remote, running, queued, displayed

    def test_native_hints_preserve_literal_words_and_stream_across_every_header_boundary(self):
        header = 'TONE: calm\nSPEECH: ' + json.dumps([
            ["😀 & <tag>", 1, {"rate": "slow", "pitch": "high", "after_ms": 200}]]) + "\n"
        body = "Use 😀 & <tag> now. Then leave."
        for split in range(len(header) + 1):
            with self.subTest(split=split):
                coach, remote, running, queued, _displayed = self.speech_session(profile="light")
                remote.answer = header + body
                remote.chunks = [header[:split], header[split:], "Use 😀 & <tag> now. ", "Then leave.\n"]
                answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
                self.assertEqual(answer["text"], body)
                self.assertEqual([line["text"] for line in queued], ["Use 😀 & <tag> now.", "Then leave."])
                self.assertEqual(queued[0]["markup"]["spans"], [
                    {"start": 4, "end": 13, "rate": "slow", "pitch": "high", "after_ms": 200}])
                self.assertEqual(coach.history[-1], body)
                self.assertEqual(coach.convo[-1]["text"], body)
                self.assertEqual(remote.bodies[0]["max_tokens"], 664)
                self.assertEqual(len(remote.bodies), 1)

    def test_broken_duplicate_late_and_excessive_headers_preserve_framed_answer(self):
        body = "Go east. Stay safe."
        cases = (
            "SPEECH: not-json\n" + body,
            'SPEECH: [["Go",1,{"rate":"slow"}]]\nSPEECH: []\n' + body,
            'Go east.\nSPEECH: [["Stay",1,{"rate":"slow"}]]\nStay safe.',
            'SPEECH: [["Go",1,{"rate":"slow"}],["Stay",1,{"rate":"fast"}]]\n' + body,
            "SPEECH: " + " " * 600 + "[]\n" + body,
            body + '\nSPEECH: [["Stay",',
            body,
        )
        for response in cases:
            with self.subTest(response=response[:60]):
                coach, remote, running, queued, _displayed = self.speech_session(profile="light")
                remote.answer = response
                remote.chunks = [response + "\n"]
                answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
                self.assertEqual(speech_markup.canonical(answer["text"]), body)
                self.assertEqual(" ".join(line["text"] for line in queued), body)
                self.assertTrue(all("markup" not in line for line in queued))
                self.assertEqual(len(remote.bodies), 1, "unusable hints never authorize repair requests")

    def test_provider_failure_is_not_reported_again_as_an_empty_markup_answer(self):
        coach, provider, messages, usage = make_coach("en")
        provider.failure = RuntimeError("local provider unavailable")
        result = coach.answer_once("What next?", state=GameState(), voice=True)
        self.assertIsNone(result["answer"])
        errors = [message for message in messages if message.get("kind") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("local provider unavailable", errors[0]["text"])
        self.assertEqual(len(provider.bodies), 1)
        self.assertNotIn("The model returned no answer text.", [message["text"] for message in messages])

    def test_delivery_metadata_is_not_published_or_retained_as_conversation_history(self):
        coach, remote, running, queued, displayed = self.speech_session(features=("rate",))
        logged = []
        running.write_log = logged.append
        message = {"kind": "callout", "text": "Go now.", "speak": True,
                   "markup": {"text": "Go now.", "spans": [{"start": 0, "end": 2, "rate": "slow"}]},
                   "speech_context": {"kind": "instruction", "text": "Go now."}}
        running.on_message(message)
        self.assertTrue(queued[0].get("markup"))
        public = [data for kind, data in displayed if kind == "message"]
        self.assertTrue(public)
        for item in [*public, *running.msg_history, *(entry["msg"] for entry in logged)]:
            self.assertEqual(item["text"], "Go now.")
            self.assertNotIn("markup", item)
            self.assertNotIn("speech_context", item)

    def test_completed_ordinary_header_prefix_words_and_inline_metadata_keep_framed_words(self):
        for response, expected, chunks in (
                ("To\nrecover, eat first.", "To recover, eat first.", ["To", "\nrecover, eat first.\n"]),
                ("Tone\nmatters here.", "Tone matters here.", ["Tone\n", "matters here.\n"]),
                ("Move north. SPEECH: []\nThen stop.", "Move north. Then stop.",
                 ["Move north. SPE", "ECH: []\nThen stop.\n"]),
                ("Move north. SPEECH: [broken\nThen stop.", "Move north. Then stop.",
                 ["Move north. SPEECH: [broken\n", "Then stop.\n"])):
            with self.subTest(response=response):
                coach, remote, running, queued, displayed = self.speech_session()
                remote.answer, remote.chunks = response, chunks
                answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
                self.assertEqual(speech_markup.canonical(answer["text"]), expected)
                self.assertEqual(" ".join(line["text"] for line in queued), expected)
                self.assertEqual(speech_markup.canonical(coach.history[-1]), expected)
                self.assertEqual(speech_markup.canonical(coach.convo[-1]["text"]), expected)
                self.assertEqual(len(remote.bodies), 1)

    def test_automatic_control_limit_preserves_every_word_and_late_ai_effect(self):
        body = " ".join(["A."] * 129)
        for early in (False, True):
            with self.subTest(early=early):
                coach, remote, running, queued, displayed = self.speech_session(features=("rate", "boundaries"))
                coach.cfg["answer_words"] = 150
                remote.answer = 'SPEECH: [["A",129,{"rate":"slow"}]]\n' + body
                if not early:
                    coach.deps.speak_fn = None
                answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
                self.assertEqual(answer["text"], body)
                self.assertEqual(" ".join(line["text"] for line in queued), body)
                self.assertTrue(all(len(line.get("markup", {}).get("spans", [])) <= 128 for line in queued))
                self.assertTrue(any(span.get("rate") == "slow" for line in queued
                                    for span in line.get("markup", {}).get("spans", [])))

    def test_ptt_during_app_destination_discovery_cancels_or_refuses_without_replay(self):
        for barge in (False, True):
            with self.subTest(barge=barge):
                coach, remote, running, queued, displayed = self.speech_session()
                running._barge_timer = None
                destination = {"provider": "windows", "identity": "test-token", "features": ["rate"]}
                running.voice = voice.HubVoice(
                    {**running.cfg["voice"], "enabled": True, "markup_destination": destination},
                    running.hub, announce=False)
                entered, release = threading.Event(), threading.Event()
                original_destination = running.voice.markup_destination

                def destination_after_discovery(entered=entered, release=release, original_destination=original_destination):
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test did not release voice discovery")
                    return original_destination()

                running.voice.markup_destination = destination_after_discovery
                remote.answer = "Go east. Stay safe."
                results, failures = [], []

                def answer(results=results, coach=coach, failures=failures):
                    try:
                        results.append(coach.answer_once("What next?", state=GameState(), voice=True)["answer"])
                    except Exception as error:
                        failures.append(error)

                worker = threading.Thread(target=answer, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    running._ptt_down, running._ptt_down_t = True, time.time()
                    if barge:
                        running._barge()
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(failures)
                self.assertFalse(any(kind == "speak" for kind, message in displayed))
                running._ptt_release()
                spoken = [message["text"] for kind, message in displayed if kind == "speak"]
                self.assertEqual(spoken, [] if barge else ["Go east. Stay safe."])
                self.assertEqual(results[0]["speak"], not barge)

    def test_final_delivery_cancellation_during_app_preparation_is_not_revived_on_ptt_release(self):
        coach, remote, running, queued, displayed = self.speech_session()
        running._barge_timer = None
        destination = {"provider": "windows", "identity": "test-token", "features": ["rate"]}
        running.voice = voice.HubVoice(
            {**running.cfg["voice"], "enabled": True, "markup_destination": destination},
            running.hub, announce=False)
        entered, release = threading.Event(), threading.Event()
        original_destination = running.voice.markup_destination

        def destination_after_discovery():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release voice discovery")
            return original_destination()

        running.voice.markup_destination = destination_after_discovery
        failures = []

        def deliver():
            try:
                running.on_message({"kind": "answer", "text": "Do not replay this line.", "speak": True})
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=deliver, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            running._ptt_down, running._ptt_down_t = True, time.time()
            running._barge()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        running._ptt_release()
        self.assertFalse(any(kind == "speak" for kind, message in displayed))

    def test_saved_markup_policy_applies_live_and_keeps_inflight_profile_snapshot(self):
        with tempfile.TemporaryDirectory() as home, patch.object(app, "ROOT", home):
            coach, remote, running, queued, displayed = self.speech_session(profile="expanded")
            running.args, running.catalog = types.SimpleNamespace(sim=False), None
            destination = {"provider": "windows", "identity": "test-token", "features": ["rate", "pitch"]}
            running.cfg["voice"].update({"markup_destination": destination, "markup_voices": {"test-token": ["rate"]}})
            running.voice = voice.HubVoice(
                {**running.cfg["voice"], "enabled": True}, running.hub, announce=False)
            coach.deps.speech_policy_fn = running._speech_policy
            hints = [[word, 1, {"rate": "slow"}] for word in ("Go", "east", "now", "Stay")]
            remote.answer = "SPEECH: " + json.dumps(hints) + "\nGo east now. Stay safe."
            original_fetch = remote.fetch

            def fetch_with_saved_profile(*args):
                running.on_settings({"save": {"companion": {"voice": {"markup_profile": "light"}}}})
                return original_fetch(*args)

            remote.fetch = fetch_with_saved_profile
            coach.answer_once("What next?", state=GameState(), voice=True)
            lines = [message for kind, message in displayed if kind == "speak"]
            self.assertEqual(remote.bodies[0]["max_tokens"], 856)
            self.assertEqual(sum(len(line.get("markup", {}).get("spans", [])) for line in lines), 4)
            remote.fetch = original_fetch
            running.on_settings({"save": {"companion": {"voice": {
                "markup_mode": "off", "markup_voices": {"test-token": []},
                "markup_destination": {**destination, "features": []}}}}})
            coach.answer_once("What next again?", state=GameState(), voice=True)
            self.assertEqual(remote.bodies[-1]["max_tokens"], 600)
            self.assertEqual(running.voice.cfg["markup_mode"], "off")
            self.assertEqual(running.voice.cfg["markup_voices"], {"test-token": []})
            newer = [message for kind, message in displayed if kind == "speak"][len(lines):]
            self.assertTrue(newer)
            self.assertTrue(all("markup" not in line for line in newer))
            self.assertEqual(sum(len(line.get("markup", {}).get("spans", [])) for line in lines), 4)
            running.on_settings({"save": {"companion": {"voice": {
                "markup_mode": None, "markup_profile": None, "markup_voices": None, "markup_destination": None}}}})
            self.assertEqual(running.voice.cfg["markup_mode"], "ai")
            self.assertEqual(running.voice.cfg["markup_profile"], "standard")
            self.assertEqual(running.voice.cfg["markup_voices"], {})
            self.assertEqual(running._speech_policy().allowance, 0)

    def test_failed_markup_policy_save_does_not_change_runtime_permission(self):
        with tempfile.TemporaryDirectory() as home, patch.object(app, "ROOT", home):
            coach, remote, running, queued, displayed = self.speech_session(profile="expanded")
            running.args, running.catalog = types.SimpleNamespace(sim=False), None
            running.voice.cfg = dict(running.cfg["voice"])
            with patch.object(app.glideconfig, "patch", side_effect=OSError("write refused")):
                with self.assertRaisesRegex(OSError, "write refused"):
                    running.on_settings({"save": {"companion": {"voice": {"markup_mode": "off"}}}})
            self.assertEqual(running.cfg["voice"]["markup_mode"], "ai")
            self.assertEqual(running.voice.cfg["markup_mode"], "ai")
            self.assertEqual(coach.speech_policy().allowance, 256)

    def test_occurrence_effect_filtering_overlap_and_cross_sentence_targets(self):
        cases = (
            ([["Go", 2, {"rate": "slow", "pitch": "invalid"}]],
             [{"start": 0, "end": 2, "rate": "slow"}]),
            ([["Go", 2, {"rate": "slow", "pitch": "high"}]],
             [{"start": 0, "end": 2, "rate": "slow"}]),
            ([["Missing", 1, {"rate": "slow", "pitch": "high"}]], []),
            ([["east. Go", 1, {"rate": "slow"}]], []),
            ([["Go", 2, {"rate": "slow"}], ["Go west", 1, {"rate": "fast"}]], []),
            ([["x" * 33, 1, {"rate": "slow"}]], []),
            ([["Go", 0, {"rate": "slow"}]], []),
            ([["Go", True, {"rate": "slow"}]], []),
        )
        for hints, expected in cases:
            with self.subTest(hints=hints):
                coach, remote, running, queued, _displayed = self.speech_session(features=("rate",))
                remote.answer = "SPEECH: " + json.dumps(hints) + "\nGo east. Go west."
                coach.answer_once("What next?", state=GameState(), voice=True)
                self.assertEqual([line["text"] for line in queued], ["Go east.", "Go west."])
                actual = [span for line in queued for span in line.get("markup", {}).get("spans", [])]
                self.assertEqual(actual, expected)

    def test_hint_count_precedes_effect_and_target_filtering(self):
        coach, remote, running, queued, _displayed = self.speech_session(profile="light")
        remote.answer = ('SPEECH: [["Missing",1,{"unknown":true}],["Go",1,{"rate":"slow"}]]\nGo east.')
        coach.answer_once("What next?", state=GameState(), voice=True)
        self.assertEqual([line["text"] for line in queued], ["Go east."])
        self.assertNotIn("markup", queued[0])

    def test_final_clipping_never_retargets_a_lost_occurrence(self):
        coach, remote, running, queued, _displayed = self.speech_session(features=("rate",))
        coach.cfg["answer_words"] = 10
        remote.answer = ('SPEECH: [["Go",2,{"rate":"slow"}]]\n'
                         'Go east. Keep walking north until you reach the river. Go west.')
        # Refuse early speech: the real final app path still owns the words and must clip controls with them.
        coach.deps.speak_fn = lambda *a, **kw: False
        answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
        self.assertNotIn("markup", answer)
        self.assertTrue(queued)
        self.assertTrue(all("markup" not in line for line in queued))
        self.assertNotIn("Go west.", " ".join(line["text"] for line in queued))

    def test_metadata_only_reports_no_answer_without_speaking_or_retrying(self):
        for response in ('SPEECH: [["Go",', "SPEECH: []"):
            coach, remote, running, queued, displayed = self.speech_session()
            remote.answer = response
            remote.chunks = [response]
            answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
            self.assertIsNone(answer)
            self.assertEqual(queued, [])
            self.assertEqual(len(remote.bodies), 1)
            self.assertTrue(any(message.get("kind") == "error" for kind, message in displayed))

    def test_accepted_suppressed_and_refused_early_text_have_separate_commitment(self):
        for suppressed, refused in ((False, False), (True, False), (False, True)):
            with self.subTest(suppressed=suppressed, refused=refused):
                coach, remote, running, queued, _displayed = self.speech_session()
                remote.answer = "Go east. Stay safe."
                if suppressed:
                    coach.deps.speak_fn = lambda *a, **kw: None
                if refused:
                    coach.deps.speak_fn = lambda *a, **kw: False
                answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
                if refused:
                    self.assertNotIn("spoken", answer)
                    self.assertEqual([line["text"] for line in queued], ["Go east. Stay safe."])
                else:
                    self.assertEqual(answer["spoken"], "Go east. Stay safe.")
                    self.assertEqual(len(queued), 0 if suppressed else 2)
                before = len(queued)
                running._say(dict(answer, text="Different final text.", spoken="Go east."))
                self.assertEqual(len(queued), before, "a committed prefix mismatch cannot replay the full answer")

    def test_profiles_have_exact_request_ceilings_and_inactive_routes_keep_base(self):
        for profile, expected in (("light", 664), ("standard", 728), ("expanded", 856)):
            for voice_question, base in ((True, expected), (False, expected + 2400)):
                coach, remote, running, queued, _displayed = self.speech_session(profile=profile)
                coach.answer_once("What next?", state=GameState(), voice=voice_question)
                self.assertEqual(remote.bodies[0]["max_tokens"], base)
                self.assertEqual(len(remote.bodies), 1)
        for options in ({"mode": "local"}, {"mode": "off"}, {"provider": "edge"}, {"features": ()},
                        {"features": ("boundaries",)}):
            coach, remote, running, queued, _displayed = self.speech_session(profile="expanded", **options)
            coach.answer_once("What next?", state=GameState(), voice=True)
            self.assertEqual(remote.bodies[0]["max_tokens"], 600)
            self.assertEqual(running.cfg["voice"]["markup_profile"], "expanded")

    def test_empty_answer_retry_adds_allowance_once_and_preserves_effort(self):
        coach, remote, running, queued, _displayed = self.speech_session()
        original_fetch = remote.fetch
        calls = []

        def fetch(body, stream, on_text, timeout):
            calls.append((body["max_tokens"], timeout))
            if len(calls) == 1:
                return {"content": [], "stop_reason": "max_tokens", "usage": {"output_tokens": 728}}
            return original_fetch(body, stream, on_text, timeout)

        remote.fetch = fetch
        answer = coach.answer_once("What next?", state=GameState(), voice=True)["answer"]
        self.assertEqual([budget for budget, timeout in calls], [728, 2528])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(answer["text"], "Gehe nach Norden.")

    def test_profile_changes_midstream_keep_original_header_limits(self):
        coach, remote, running, queued, _displayed = self.speech_session(profile="expanded")
        hints = [[word, 1, {"rate": "slow"}] for word in ("Go", "east", "now", "Stay")]
        remote.answer = "SPEECH: " + json.dumps(hints) + "\nGo east now. Stay safe."
        original_fetch = remote.fetch

        def fetch(body, stream, on_text, timeout):
            running.cfg["voice"]["markup_profile"] = "light"
            return original_fetch(body, stream, on_text, timeout)

        remote.fetch = fetch
        coach.answer_once("What next?", state=GameState(), voice=True)
        self.assertEqual(remote.bodies[0]["max_tokens"], 856)
        self.assertEqual(sum(len(line.get("markup", {}).get("spans", [])) for line in queued), 4)

    def test_aside_briefing_and_update_use_only_their_original_base_plus_profile(self):
        for profile, aside, update in (("light", 464, 564), ("standard", 528, 628), ("expanded", 656, 756)):
            for kind in ("aside", "briefing", "update"):
                with self.subTest(profile=profile, kind=kind):
                    coach, remote, running, queued, displayed = self.speech_session(profile=profile)
                    coach.state = GameState()
                    remote.answer = 'SPEECH: [["Go",1,{"rate":"slow"}]]\nGo north.'
                    if kind == "aside":
                        message = coach.banter.speak()
                    elif kind == "briefing":
                        infos = [{"id": 123, "name": "Quest:123", "objective_text": "Go north."}]
                        with patch.object(coach.briefings, "_brief_infos", return_value=infos):
                            message = coach.briefings.brief([123])
                    else:
                        message = coach._announce("available", "9.9", "New version", ["Better coaching."])
                    self.assertIsNotNone(message)
                    self.assertNotIn("SPEECH", message["text"])
                    self.assertEqual(len(remote.bodies), 1)
                    self.assertEqual(remote.bodies[0]["max_tokens"], update if kind == "update" else aside)
                    self.assertTrue(any("markup" in line for line in queued))

    def test_authorized_tool_rounds_receive_allowance_once_without_changing_round_caps(self):
        coach, remote, running, queued, displayed = self.speech_session()
        coach.deps.reminders = reminders.Reminders(None)
        original_fetch = remote.fetch
        calls = []

        def fetch(body, stream, on_text, timeout):
            calls.append(body["max_tokens"])
            if len(calls) == 1:
                return {"content": [{"type": "tool_use", "id": "local-tool", "name": "reminder_list",
                                      "input": {}}], "stop_reason": "tool_use", "usage": {"output_tokens": 3}}
            if len(calls) == 2:
                return {"content": [], "stop_reason": "max_tokens", "usage": {"output_tokens": 728}}
            return original_fetch(body, stream, on_text, timeout)

        remote.fetch = fetch
        text, meta = coach.llm.call_tools(
            "ep:test:fake", [{"role": "user", "content": "What next?"}],
            [tool for tool in coach_tools.REMINDER_TOOLS if tool["name"] == "reminder_list"],
            1, 12, speech_policy=coach.speech_policy())
        self.assertEqual(calls, [728, 728, 2528])
        self.assertEqual(meta["tool_calls"], 1)
        self.assertEqual(text, "Gehe nach Norden.\nFACTS: q:123")
        self.assertEqual(remote.bodies[1]["tool_choice"], {"type": "none"})
        result = remote.bodies[1]["messages"][-1]["content"][0]
        self.assertEqual(json.loads(result["content"]), {"reminders": []})
        self.assertNotIn("is_error", result)

    def test_offline_instruction_context_and_local_boundaries_are_available_without_ai(self):
        coach, remote, running, queued, displayed = self.speech_session(
            mode="local", features=("rate", "boundaries"), enabled=False)
        coach.state = GameState()
        infos = [{"id": 123, "name": "Quest:123", "objective_text": "Kill boars."}]
        with patch.object(coach.briefings, "_brief_infos", return_value=infos):
            message = coach.briefings.brief([123])
        self.assertEqual(remote.bodies, [])
        self.assertTrue(any(span.get("rate") == "slow" for line in queued
                            for span in line.get("markup", {}).get("spans", [])))
        self.assertNotIn("SPEECH", message["text"])
        queued.clear()
        running.on_message({"kind": "callout", "text": "First instruction\n\nSecond instruction.", "speak": True})
        self.assertEqual(queued[0]["text"], "First instruction\n\nSecond instruction.")
        spans = queued[0]["markup"]["spans"]
        self.assertEqual(spans[0], {"start": 0, "end": 17, "boundary": "p"})

    def test_native_pause_effects_are_independent_and_have_per_reply_limits(self):
        coach, remote, running, queued, displayed = self.speech_session(profile="expanded")
        hints = [["Go", 1, {"after_ms": 1501, "rate": "slow"}],
                 ["east", 1, {"after_ms": 1500}], ["now", 1, {"after_ms": 1500}],
                 ["Stay", 1, {"after_ms": 1000, "pitch": "high"}]]
        remote.answer = "SPEECH: " + json.dumps(hints) + "\nGo east now. Stay safe."
        remote.chunks = ["SPEECH: " + json.dumps(hints) + "\nGo east now. ", "Stay safe.\n"]
        coach.answer_once("What next?", state=GameState(), voice=True)
        spans = [span for line in queued for span in line.get("markup", {}).get("spans", [])]
        self.assertEqual(sum(span.get("after_ms", 0) for span in spans), 3000)
        self.assertEqual(spans[0], {"start": 0, "end": 2, "rate": "slow"})
        self.assertEqual(spans[-1], {"start": 0, "end": 4, "pitch": "high"})

    def test_quotation_limit_counts_unicode_scalars_not_utf16_units(self):
        for count, expected in ((32, True), (33, False)):
            with self.subTest(count=count):
                coach, remote, running, queued, displayed = self.speech_session()
                quote = "😀" * count
                remote.answer = "SPEECH: " + json.dumps([[quote, 1, {"rate": "slow"}]]) + "\n" + quote + "."
                coach.answer_once("What next?", state=GameState(), voice=True)
                self.assertEqual(queued[0]["text"], quote + ".")
                self.assertEqual("markup" in queued[0], expected)
                if expected:
                    self.assertEqual(queued[0]["markup"]["spans"], [{"start": 0, "end": 32, "rate": "slow"}])

    def test_mode_change_after_enqueue_keeps_bound_line_and_filters_new_lines(self):
        coach, remote, running, queued, displayed = self.speech_session()
        remote.answer = 'SPEECH: [["Go",1,{"rate":"slow"}],["Stay",1,{"pitch":"high"}]]\nGo east. Stay safe.'
        remote.chunks = [remote.answer.split("\n")[0] + "\nGo east. ", "Stay safe.\n"]
        original_speak = running.speak_early

        def speak(*args, **kwargs):
            result = original_speak(*args, **kwargs)
            running.cfg["voice"]["markup_mode"] = "local"
            return result

        coach.deps.speak_fn = speak
        coach.answer_once("What next?", state=GameState(), voice=True)
        self.assertEqual([line["text"] for line in queued], ["Go east.", "Stay safe."])
        self.assertEqual(queued[0]["markup"]["spans"], [{"start": 0, "end": 2, "rate": "slow"}])
        self.assertNotIn("markup", queued[1])
        self.assertEqual(remote.bodies[0]["max_tokens"], 728)
        coach.answer_once("What next again?", state=GameState(), voice=True)
        self.assertEqual(remote.bodies[-1]["max_tokens"], 600)

    def test_offline_typed_warning_not_urgency_produces_controls_without_network(self):
        coach, remote, running, queued, _displayed = self.speech_session(
            mode="local", features=("rate",), enabled=False)
        coach.state = GameState()
        warning = Trigger("low_health_in_combat", {"hp": 12}, 1)
        coach._respond([warning])
        self.assertEqual(remote.bodies, [])
        self.assertEqual(queued[0]["text"], "Health 12%. Finish or disengage now.")
        self.assertEqual(queued[0]["markup"]["spans"], [{"start": 0, "end": 11, "rate": "fast"}])
        queued.clear()
        running.on_message({"kind": "answer", "text": "An urgent voice question.", "urgent": True, "speak": True})
        running.on_message({"kind": "callout", "text": "Level up.", "urgent": True, "speak": True})
        self.assertTrue(all("markup" not in line for line in queued))

    def test_translation_discards_unreliable_controls_but_keeps_literal_final_words(self):
        coach, provider, messages, usage = make_coach("de")
        provider.translated = "Go & <wörtlich>."
        message = {"kind": "callout", "text": "Go & <literal>.", "speak": True,
                   "markup": {"text": "Go & <literal>.", "spans": [{"start": 0, "end": 2, "rate": "slow"}]},
                   "speech_context": {"kind": "instruction", "text": "Go"}}
        coach.emit(message)
        self.assertEqual(message["text"], "Go & <wörtlich>.")
        self.assertNotIn("markup", message)
        self.assertNotIn("speech_context", message)
        self.assertEqual(len(provider.bodies), 1)
        self.assertEqual(provider.bodies[0]["max_tokens"], 600, "translation gets no markup allowance")

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

    def test_settings_response_does_not_wait_for_announcement_translation(self):
        def check_response(translation_fails):
            with tempfile.TemporaryDirectory() as home:
                entered, release, replied, delivered = (threading.Event() for _ in range(4))
                messages, response, errors = [], [], []

                def emit(message):
                    messages.append(message)
                    if message.get("kind") == "info":
                        delivered.set()

                coach, provider, _messages, _usage = make_coach("de", emit=emit)
                if translation_fails:
                    provider.failure = RuntimeError("translator unavailable")
                fetch = provider.fetch

                def blocked_fetch(*args):
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("test did not release translation")
                    return fetch(*args)

                provider.fetch = blocked_fetch
                running = types.SimpleNamespace(args=types.SimpleNamespace(sim=False), coach=coach, catalog=None)
                handler = dashboard.post_json(lambda body: app.App.on_settings(running, body),
                                              lambda applied: {"applied": applied})
                server = dashboard.serve(dashboard.Hub(), 0, {("POST", "/settings"): handler}, token="test-token")
                model = "ep:test:new-model"

                def save():
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=8)
                    try:
                        connection.request("POST", "/settings", json.dumps({
                            "save": {"companion": {"llm": {"fast_model": model}}}, "fast_model": model}),
                            {"X-Glide-Token": "test-token"})
                        result = connection.getresponse()
                        response.append((result.status, json.loads(result.read())))
                    except Exception as error:
                        errors.append(error)
                    finally:
                        connection.close()
                        replied.set()

                with patch.object(app, "ROOT", home):
                    caller = threading.Thread(target=save, daemon=True)
                    caller.start()
                    try:
                        self.assertTrue(entered.wait(2), "translation reached the provider")
                        self.assertTrue(replied.wait(2), "settings confirmation must not wait for translation")
                        self.assertFalse(errors, errors)
                        self.assertEqual(response, [(200, {"ok": True, "applied": {
                            "fast_model": model, "saved": True}})])
                        self.assertEqual(app.glideconfig.load(home)["llm"]["fast_model"], model)
                        self.assertEqual(coach.cfg["fast_model"], model)
                        self.assertFalse(delivered.is_set(), "announcement remains pending")
                    finally:
                        release.set()
                        caller.join(3)
                        server.shutdown()
                        server.server_close()
                    self.assertTrue(delivered.wait(2))
                announcements = [message for message in messages if message.get("kind") == "info"]
                self.assertEqual(len(announcements), 1, "one announcement after the save")
                self.assertTrue(announcements[0]["speak"])
                if translation_fails:
                    self.assertIn("new-model", announcements[0]["text"], "failed translation keeps English")
                else:
                    self.assertEqual(announcements[0]["text"], "Bereit.")
                    self.assertIn("new-model", announcements[0]["original_text"])

        for translation_fails in (False, True):
            with self.subTest(translation_fails=translation_fails):
                check_response(translation_fails)

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
