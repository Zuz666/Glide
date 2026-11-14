#!/usr/bin/env python3
"""Recognition language selection without network, microphone, or model downloads."""
import io
import hashlib
import json
import os
import sys
import shutil
import struct
import subprocess
import types
import unittest
import urllib.error
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
import ptt
from ptt_audio import MAX_PCM_BYTES, Recording
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
        self.audio = []
        self.configuration = kwargs

    def transcribe(self, audio, **kwargs):
        self.audio.append(audio)
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
                stt.transcribe(Recording(b"\x00\x00"))
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
                stt.transcribe(Recording(b"\x00\x00"))
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

    def test_known_pcm_samples_reach_whisper_without_decoding_or_source_mutation(self):
        import numpy as np
        pcm = struct.pack("<7h", -32768, -16384, -1, 0, 1, 16384, 32767)
        recording = Recording(pcm)
        stt = ptt.WhisperSTT("small", language="de")
        prompt = "Names: Goldshire " * 80
        with patch("builtins.open", side_effect=AssertionError("audio file access")):
            self.assertEqual(stt.transcribe(recording, prompt), "selected language")
        audio = stt.model.audio[-1]
        self.assertEqual(audio.dtype, np.dtype("float32"))
        self.assertEqual(audio.shape, (7,))
        np.testing.assert_array_equal(audio, [-1.0, -0.5, -0.000030517578125, 0.0,
                                              0.000030517578125, 0.5, 0.999969482421875])
        self.assertEqual(stt.model.calls[-1]["initial_prompt"], prompt[:600])
        self.assertEqual(stt.model.calls[-1]["beam_size"], 1)
        self.assertTrue(stt.model.calls[-1]["vad_filter"])
        self.assertEqual(stt.model.configuration["device"], "cpu")
        self.assertEqual(stt.model.configuration["compute_type"], "int8")
        audio[:] = 0
        self.assertEqual(recording.pcm, pcm)
        self.assertEqual(recording.waveform()[0], -1.0)
        self.assertEqual(recording.wav_bytes()[-len(pcm):], pcm)


class RecordingTests(unittest.TestCase):
    def test_rejects_empty_misaligned_oversize_and_mutable_audio(self):
        for pcm in (b"", b"\x00", b"\x00" * (MAX_PCM_BYTES + 2)):
            with self.subTest(size=len(pcm)), self.assertRaises(ValueError):
                Recording(pcm)
        for pcm in (bytearray(b"\x00\x00"), "question.wav", None):
            with self.subTest(type=type(pcm)), self.assertRaises(TypeError):
                Recording(pcm)

    def test_source_is_immutable_and_silence_is_valid(self):
        import dataclasses
        recording = Recording(b"\x00\x00" * 1600)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            recording.pcm = b"\x01\x00"
        self.assertTrue((recording.waveform() == 0).all())
        self.assertEqual(len(Recording(b"\x00" * MAX_PCM_BYTES).pcm), MAX_PCM_BYTES)


class OnlineLanguageTests(unittest.TestCase):
    def test_each_provider_receives_selected_language_and_its_aliases(self):
        keys = {"OPENAI_API_KEY": "fake", "GROQ_API_KEY": "fake", "DEEPGRAM_API_KEY": "fake"}
        recording = Recording(b"\x00\x00")
        with patch.dict(os.environ, keys):
            for engine in ("openai", "groq", "deepgram"):
                for selected, expected in (("de-DE", "de"), ("fil", "tl"), ("nb", "no"), ("jw", "jv")):
                    with self.subTest(engine=engine, selected=selected):
                        response = {"results": {"channels": [{"alternatives": [{"transcript": "recognized"}]}]}}
                        if engine != "deepgram":
                            response = {"text": "recognized"}
                        with patch.object(ptt.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
                            stt = ptt.OnlineSTT(engine, language=selected)
                            stt.transcribe(recording)
                        request = send.call_args.args[0]
                        if engine == "deepgram":
                            self.assertEqual(parse_qs(urlparse(request.full_url).query)["language"], [expected])
                        else:
                            self.assertIn(('name="language"\r\n\r\n%s\r\n' % expected).encode(), request.data)

    def test_provider_rejection_is_explicit_and_not_retried_as_english(self):
        keys = {"OPENAI_API_KEY": "fake", "GROQ_API_KEY": "fake", "DEEPGRAM_API_KEY": "fake"}
        recording = Recording(b"\x00\x00")
        with patch.dict(os.environ, keys):
            for engine in ("openai", "groq", "deepgram"):
                with self.subTest(engine=engine):
                    rejection = urllib.error.HTTPError("https://unused.invalid", 400, "Bad Request", {},
                                                       io.BytesIO(b"Unsupported language: zu"))
                    with patch.object(ptt.urllib.request, "urlopen", side_effect=rejection) as send:
                        with self.assertRaisesRegex(RuntimeError, "Unsupported language: zu"):
                            ptt.OnlineSTT(engine, language="zu").transcribe(recording)
                    self.assertEqual(send.call_count, 1)


class WindowsLanguageTests(unittest.TestCase):
    def test_installed_recognizer_selection_uses_base_language_and_alias(self):
        for selected, expected in (("de-DE", "de"), ("fil", "tl"), ("nb", "no")):
            with self.subTest(selected=selected):
                result = types.SimpleNamespace(returncode=0, stdout=b"recognized", stderr=b"")
                with patch.object(ptt.subprocess, "run", return_value=result) as run:
                    ptt.WindowsSTT(language=selected).transcribe(Recording(b"\x00\x00"))
                environment = run.call_args.kwargs["env"]
                self.assertEqual(environment["GLIDE_PTT_LANGUAGE"], language_code(selected))
                self.assertIn(expected, environment["GLIDE_PTT_RECOGNIZER_LANGUAGES"].split(";"))

    def test_missing_installed_language_is_reported_without_english_retry(self):
        result = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"No Windows speech recognizer installed for fr.")
        with patch.object(ptt.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "No Windows speech recognizer installed for fr"):
                ptt.WindowsSTT(language="fr").transcribe(Recording(b"\x00\x00"))
        self.assertEqual(run.call_count, 1)

    def test_wav_uses_binary_stdin_and_utf8_output_without_audio_environment(self):
        pcm = struct.pack("<6h", 0, -32768, 32767, 13, 10, -1)
        recording = Recording(pcm)
        result = types.SimpleNamespace(returncode=0, stdout=" вопрос \n".encode(), stderr=b"")
        with patch.object(ptt.subprocess, "run", return_value=result) as run, \
                patch("builtins.open", side_effect=AssertionError("audio file access")):
            self.assertEqual(ptt.WindowsSTT(language="ru").transcribe(recording), "вопрос")
        args, options = run.call_args
        self.assertEqual(args[0][-1], ptt.PS_STT)
        self.assertEqual(args[0][-2], "-Command")
        self.assertEqual(options["input"][-len(pcm):], pcm)
        self.assertEqual(options["input"], recording.wav_bytes())
        self.assertEqual(options["timeout"], 30)
        self.assertNotIn("text", options)
        self.assertNotIn("encoding", options)
        self.assertNotIn("stdin", options)
        self.assertNotIn("GLIDE_PTT_WAV", options["env"])
        self.assertEqual(recording.pcm, pcm)

    def test_timeout_nonzero_exit_and_empty_result_keep_distinct_outcomes(self):
        recording = Recording(b"\x00\x00" * 1600)
        with patch.object(ptt.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                ptt.WindowsSTT().transcribe(recording)
        for error, expected in (("ошибка".encode(), "ошибка"), (b"", "Windows speech recognition failed")):
            result = types.SimpleNamespace(returncode=1, stdout=b"", stderr=error)
            with self.subTest(error=error), patch.object(ptt.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, expected):
                    ptt.WindowsSTT().transcribe(recording)
        result = types.SimpleNamespace(returncode=0, stdout=b" \r\n", stderr=b"")
        with patch.object(ptt.subprocess, "run", return_value=result):
            self.assertEqual(ptt.WindowsSTT().transcribe(recording), "")

    def test_invalid_recording_cannot_reach_subprocess_or_upload(self):
        with patch.object(ptt.subprocess, "run") as run, \
                patch.object(ptt.urllib.request, "urlopen") as send, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}):
            for pcm in (b"", b"\x01", b"\x00" * (MAX_PCM_BYTES + 2)):
                for transcribe in (ptt.WindowsSTT().transcribe, ptt.OnlineSTT("openai").transcribe):
                    with self.subTest(size=len(pcm), transcribe=transcribe), self.assertRaises(ValueError):
                        transcribe(Recording(pcm))
            run.assert_not_called()
            send.assert_not_called()


@unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell"),
                     "Windows PowerShell is required for the real binary-pipe checks")
class WindowsMemoryStreamTests(unittest.TestCase):
    def test_adapter_delivers_binary_audio_and_eof_to_a_powershell_child(self):
        # Substitute only the external recognizer with a binary receive/checksum probe.
        # The adapter still constructs and communicates with a real PowerShell process.
        script = r"""
        $ErrorActionPreference = 'Stop'
        $stream = [Console]::OpenStandardInput()
        $memory = New-Object System.IO.MemoryStream
        $hash = [System.Security.Cryptography.SHA256]::Create()
        try {
          $stream.CopyTo($memory)
          [Console]::Out.Write([BitConverter]::ToString($hash.ComputeHash($memory.ToArray())).Replace('-', '').ToLowerInvariant())
        } finally { $hash.Dispose(); $memory.Dispose(); $stream.Dispose() }
        """
        recording = Recording(bytes(range(256)) * 32)
        real_run = subprocess.run
        def child(command, **options):
            return real_run(command[:-1] + [script], **options)
        with patch.object(ptt.subprocess, "run", side_effect=child):
            digest = ptt.WindowsSTT().transcribe(recording)
        self.assertEqual(digest, hashlib.sha256(recording.wav_bytes()).hexdigest())

    def test_fixed_helper_rejects_empty_malformed_and_oversize_binary_input(self):
        good = Recording(b"\x00\x00").wav_bytes()
        bad_rate = bytearray(good)
        struct.pack_into("<I", bad_rate, 24, 8000)
        for payload in (b"", b"\x00\xff", bytes(bad_rate), good[:-1],
                        b"\x00" * (MAX_PCM_BYTES + 45)):
            with self.subTest(size=len(payload)):
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ptt.PS_STT],
                    input=payload, capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertRegex(result.stderr.decode("utf-8", "replace"), "WAV|audio limit")

    def test_real_system_speech_accepts_memory_wav_silence_or_reports_missing_recognizer(self):
        try:
            text = ptt.WindowsSTT(language="en").transcribe(Recording(b"\x00\x00" * 1600))
        except RuntimeError as error:
            if "No Windows speech recognizer installed for en." in str(error):
                self.skipTest(str(error))
            raise
        self.assertEqual(text, "")



if __name__ == "__main__":
    unittest.main()
