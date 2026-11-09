#!/usr/bin/env python3
"""Recognition language selection without network, microphone, or model downloads."""
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
import ptt
from speech_language import language_code


class LanguageCodeTests(unittest.TestCase):
    def test_normalizes_base_codes_without_clamping_to_english(self):
        for value, expected in (("RU-ru", "ru"), (" de-DE ", "de"), ("fr_CA", "fr"),
                                ("zh-Hant-TW", "zh"), ("fil-PH", "fil"), ("jv", "jv"),
                                ("nb", "nb"), ("haw", "haw"), ("xx", "xx")):
            with self.subTest(value=value):
                self.assertEqual(language_code(value), expected)

    def test_missing_or_invalid_values_use_english(self):
        for value in (None, "", "english", "de-", "en;exit", "12", [], 3, "рус"):
            with self.subTest(value=value):
                self.assertEqual(language_code(value), "en")


class FakeWhisperModel:
    supported_languages = ["en", "ru", "de", "fr", "tl", "jw", "no", "ar", "zh"]

    def __init__(self, path, **kwargs):
        self.path = path
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter([types.SimpleNamespace(text=" selected language ")]), None


class WhisperLanguageTests(unittest.TestCase):
    def setUp(self):
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = FakeWhisperModel
        self.modules = patch.dict(sys.modules, {"faster_whisper": module})
        self.modules.start()
        self.environment = patch.dict(os.environ, {"GLIDE_WHISPER_DIRS": ""})
        self.environment.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.environment.stop)

    def test_non_english_replaces_english_only_model_and_transcribes_selected_language(self):
        for language in ("ru", "de"):
            with self.subTest(language=language):
                stt = ptt.WhisperSTT("tiny.en", language=language)
                self.assertEqual(stt.model.path, "small")
                with patch.object(stt, "load_wav", return_value=object()):
                    stt.transcribe("unused.wav")
                self.assertEqual(stt.model.calls[-1]["language"], language)

    def test_missing_model_uses_small_for_non_english(self):
        with patch.object(ptt.settings, "default", return_value="tiny.en"):
            self.assertEqual(ptt.WhisperSTT(None, language="fr").model.path, "small")

    def test_english_model_and_user_multilingual_choices_are_preserved(self):
        self.assertEqual(ptt.WhisperSTT("tiny.en", language="en").model.path, "tiny.en")
        for model in ("base", "medium", "large-v3"):
            self.assertEqual(ptt.WhisperSTT(model, language="de").model.path, model)

    def test_provider_aliases_reach_actual_transcription(self):
        for selected, expected in (("fil", "tl"), ("jv", "jw"), ("nb", "no")):
            with self.subTest(selected=selected):
                stt = ptt.WhisperSTT("small", language=selected)
                with patch.object(stt, "load_wav", return_value=object()):
                    stt.transcribe("unused.wav")
                self.assertEqual(stt.model.calls[-1]["language"], expected)

    def test_unsupported_language_or_disguised_english_model_fails_explicitly(self):
        for selected, supported in (("zu", FakeWhisperModel.supported_languages), ("de", ["en"])):
            with self.subTest(selected=selected), patch.object(FakeWhisperModel, "supported_languages", supported):
                with self.assertRaisesRegex(ValueError, selected):
                    ptt.WhisperSTT("custom-model", language=selected)

    def test_desktop_waits_for_multilingual_model_without_starting_download(self):
        utils = types.ModuleType("faster_whisper.utils")
        def unavailable(model, *, local_files_only):
            self.assertEqual(model, "small")
            self.assertTrue(local_files_only)
            raise FileNotFoundError(model)
        utils.download_model = unavailable
        with patch.dict(sys.modules, {"faster_whisper.utils": utils}), \
                patch.dict(os.environ, {"GLIDE_WHISPER_DIRS": "unused-model-directory"}), \
                patch.object(ptt, "whisper_model_path", side_effect=lambda model: model):
            with self.assertRaisesRegex(ptt.ModelNotDownloaded, "small"):
                ptt.WhisperSTT("tiny.en", language="fr")


class OnlineLanguageTests(unittest.TestCase):
    def test_each_provider_receives_selected_language_and_its_aliases(self):
        keys = {"OPENAI_API_KEY": "fake", "GROQ_API_KEY": "fake", "DEEPGRAM_API_KEY": "fake"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, keys):
            path = os.path.join(directory, "question.wav")
            with open(path, "wb") as wav:
                wav.write(b"RIFF fake audio")
            for engine in ("openai", "groq", "deepgram"):
                for selected, expected in (("de-DE", "de"), ("fil", "tl"), ("nb", "no"), ("jw", "jv")):
                    with self.subTest(engine=engine, selected=selected):
                        response = {"results": {"channels": [{"alternatives": [{"transcript": "recognized"}]}]}}
                        if engine != "deepgram":
                            response = {"text": "recognized"}
                        with patch.object(ptt.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
                            stt = ptt.OnlineSTT(engine, language=selected)
                            stt.transcribe(path)
                        request = send.call_args.args[0]
                        if engine == "deepgram":
                            self.assertEqual(parse_qs(urlparse(request.full_url).query)["language"], [expected])
                        else:
                            self.assertIn(('name="language"\r\n\r\n%s\r\n' % expected).encode(), request.data)

    def test_provider_rejection_is_explicit_and_not_retried_as_english(self):
        keys = {"OPENAI_API_KEY": "fake", "GROQ_API_KEY": "fake", "DEEPGRAM_API_KEY": "fake"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, keys):
            path = os.path.join(directory, "question.wav")
            with open(path, "wb") as wav:
                wav.write(b"RIFF fake audio")
            for engine in ("openai", "groq", "deepgram"):
                with self.subTest(engine=engine):
                    rejection = urllib.error.HTTPError("https://unused.invalid", 400, "Bad Request", {},
                                                       io.BytesIO(b"Unsupported language: zu"))
                    with patch.object(ptt.urllib.request, "urlopen", side_effect=rejection) as send:
                        with self.assertRaisesRegex(RuntimeError, "Unsupported language: zu"):
                            ptt.OnlineSTT(engine, language="zu").transcribe(path)
                    self.assertEqual(send.call_count, 1)


class WindowsLanguageTests(unittest.TestCase):
    def test_installed_recognizer_selection_uses_base_language_and_alias(self):
        for selected, expected in (("de-DE", "de"), ("fil", "tl"), ("nb", "no")):
            with self.subTest(selected=selected):
                result = types.SimpleNamespace(returncode=0, stdout="recognized", stderr="")
                with patch.object(ptt.subprocess, "run", return_value=result) as run:
                    ptt.WindowsSTT(language=selected).transcribe("question.wav")
                environment = run.call_args.kwargs["env"]
                self.assertEqual(environment["GLIDE_PTT_LANGUAGE"], language_code(selected))
                self.assertIn(expected, environment["GLIDE_PTT_RECOGNIZER_LANGUAGES"].split(";"))

    def test_missing_installed_language_is_reported_without_english_retry(self):
        result = types.SimpleNamespace(returncode=1, stdout="", stderr="No Windows speech recognizer installed for fr.")
        with patch.object(ptt.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "No Windows speech recognizer installed for fr"):
                ptt.WindowsSTT(language="fr").transcribe("question.wav")
        self.assertEqual(run.call_count, 1)



if __name__ == "__main__":
    unittest.main()
