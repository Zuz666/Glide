"""Push-to-talk: hold a key, speak, release -> transcript -> coach question -> spoken answer.

Key state is read passively with GetAsyncKeyState (nothing is sent to the game). Audio is recorded
with Windows' built-in MCI (no dependencies). Speech-to-text:
  whisper  local faster-whisper model (pip install faster-whisper), accurate, offline after first download
  windows  Windows' built-in recognizer through PowerShell (no install, less accurate)
  auto     whisper if installed, else windows
  openai   OpenAI's transcription API (OPENAI_API_KEY; model gpt-4o-mini-transcribe)
  groq     Groq's Whisper API (GROQ_API_KEY; whisper-large-v3-turbo, very fast)
  deepgram Deepgram (DEEPGRAM_API_KEY; nova-3)
An online engine that fails (network, key, quota) falls back to the local one for that question.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import settings
from log import log, log_once
from speech_language import language_code

IS_WINDOWS = sys.platform == "win32"

VK = {"LBUTTON": 0x01, "RBUTTON": 0x02, "MBUTTON": 0x04, "MOUSE3": 0x04, "MOUSE4": 0x05, "XBUTTON1": 0x05,
      "MOUSE5": 0x06, "XBUTTON2": 0x06, "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "SHIFT": 0x10,
      "CTRL": 0x11, "ALT": 0x12, "PAUSE": 0x13, "CAPSLOCK": 0x14, "SPACE": 0x20, "PAGEUP": 0x21,
      "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24, "INSERT": 0x2D, "DELETE": 0x2E, "SCROLLLOCK": 0x91,
      "NUMLOCK": 0x90, "LSHIFT": 0xA0, "RSHIFT": 0xA1, "LCTRL": 0xA2, "RCTRL": 0xA3, "LALT": 0xA4, "RALT": 0xA5,
      "BACKQUOTE": 0xC0, "`": 0xC0}
VK.update({"F%d" % i: 0x6F + i for i in range(1, 25)})
VK.update({"NUM%d" % i: 0x60 + i for i in range(10)})
VK.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({str(d): 0x30 + d for d in range(10)})


def parse_key(name):
    """'F8', 'MOUSE5', 'CTRL+Q' -> list of virtual-key codes that must all be held."""
    codes = []
    for part in str(name).upper().replace(" ", "").split("+"):
        if part not in VK:
            raise ValueError("unknown push-to-talk key %r (examples: F8, MOUSE5, CTRL+Q)" % part)
        codes.append(VK[part])
    return codes


class Recorder:
    """Records the default microphone to a 16 kHz mono 16-bit WAV with MCI."""

    def __init__(self):
        self.winmm = ctypes.windll.winmm
        self.active = False

    def _mci(self, cmd):
        buf = ctypes.create_unicode_buffer(256)
        err = self.winmm.mciSendStringW(cmd, buf, 255, 0)
        if err:
            ebuf = ctypes.create_unicode_buffer(256)
            self.winmm.mciGetErrorStringW(err, ebuf, 255)
            raise RuntimeError("MCI '%s': %s" % (cmd.split()[0], ebuf.value))
        return buf.value

    def start(self):
        try:
            self._mci("close glideptt")
        except RuntimeError:
            pass
        self._mci("open new type waveaudio alias glideptt")
        self._mci("set glideptt time format ms bitspersample 16 channels 1 samplespersec 16000 "
                  "bytespersec 32000 alignment 2")
        self._mci("record glideptt")
        self.active = True

    def stop(self, path):
        self.active = False
        self._mci("stop glideptt")
        self._mci('save glideptt "%s"' % path)
        self._mci("close glideptt")
        return path


def whisper_model_path(model_name):
    """A model folder shipped with or downloaded by the desktop app (GLIDE_WHISPER_DIRS, ';'-separated folders that
    hold <model>/model.bin), else the name itself (faster-whisper then uses its own download cache)."""
    for d in os.environ.get("GLIDE_WHISPER_DIRS", "").split(os.pathsep):
        p = os.path.join(d, model_name) if d else ""
        if p and os.path.exists(os.path.join(p, "model.bin")):
            return p
    return model_name


class ModelNotDownloaded(Exception):
    """Under the desktop app the model isn't on disk yet: the app is downloading it, and loading it by name here
    would start a second download into faster-whisper's own cache while push-to-talk waits."""


class WhisperSTT:
    provider = "faster-whisper"

    def __init__(self, model_name, *, language="en"):
        selected_language = language_code(language)
        self.language = {"fil": "tl", "jv": "jw", "nb": "no"}.get(selected_language, selected_language)
        model_name = model_name or ("small" if self.language != "en" else settings.default("ptt.whisper_model"))
        if self.language != "en" and model_name.endswith(".en"):
            model_name = "small"
        # optional: installed by the app at runtime, not in the dev or CI environment
        from faster_whisper import WhisperModel  # pyright: ignore[reportMissingImports]  (ImportError if missing)
        path = whisper_model_path(model_name)
        if path == model_name and os.environ.get("GLIDE_WHISPER_DIRS"):
            try:
                from faster_whisper.utils import download_model  # pyright: ignore[reportMissingImports]
                path = download_model(model_name, local_files_only=True)   # faster-whisper's cache (setup-voice.cmd)
            except Exception:  # noqa: BLE001
                raise ModelNotDownloaded(model_name) from None
        self.model = WhisperModel(path, device="cpu", compute_type="int8",
                                  cpu_threads=max(2, (os.cpu_count() or 4) // 2))
        # faster-whisper silently substitutes English for an English-only model.
        # Check the loaded model too, including custom paths without a .en suffix.
        if self.language not in self.model.supported_languages:
            raise ValueError("Whisper model %s does not support language %s" % (model_name, selected_language))
        self.name = "whisper " + model_name
        self.model_id = os.path.basename(model_name.replace("\\", "/"))

    @staticmethod
    def load_wav(path):
        """WAV -> 16 kHz mono float32 array, without PyAV (some PyAV versions break faster-whisper's
        own decoder: "open() got an unexpected keyword argument 'metadata_errors'")."""
        import wave
        import numpy as np
        with wave.open(path, "rb") as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width == 2:
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 1:
            a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        if rate != 16000 and len(a):
            n = int(len(a) * 16000 / rate)
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
        return a

    def transcribe(self, path, prompt=""):
        audio = self.load_wav(path)
        segs, _ = self.model.transcribe(audio, language=self.language, beam_size=1, vad_filter=True,
                                        initial_prompt=prompt[:600] or None)
        return " ".join(s.text.strip() for s in segs).strip()


PS_STT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
  Add-Type -AssemblyName System.Speech
  $languages = $env:GLIDE_PTT_RECOGNIZER_LANGUAGES -split ';'
  $recognizer = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
    Where-Object {
      $languages -contains $_.Culture.TwoLetterISOLanguageName -or
      $languages -contains ($_.Culture.Name -split '-')[0]
    } | Sort-Object { $_.Culture.Name } | Select-Object -First 1
  if ($null -eq $recognizer) {
    throw ("No Windows speech recognizer installed for " + $env:GLIDE_PTT_LANGUAGE + ".")
  }
  $r = New-Object System.Speech.Recognition.SpeechRecognitionEngine -ArgumentList $recognizer.Id
  $r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
  $r.SetInputToWaveFile($env:GLIDE_PTT_WAV)
  $res = $r.Recognize()
  if ($res) { [Console]::Out.Write($res.Text) }
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
"""


class WindowsSTT:
    name = "windows speech"
    provider = "Windows Speech"

    def __init__(self, *, language="en"):
        self.language = language_code(language)
        alias = {"fil": "tl", "tl": "fil", "nb": "no", "no": "nb", "jv": "jw", "jw": "jv"}.get(self.language)
        self.recognizer_languages = ";".join([self.language, alias] if alias else [self.language])

    def transcribe(self, path, prompt=""):
        # the recording's path goes in the environment: -Command joins its arguments into the script's text, so a
        # path argument would have run as code (and the recognizer got no file)
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_STT],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=30,
                             env=dict(os.environ, GLIDE_PTT_WAV=path, GLIDE_PTT_LANGUAGE=self.language,
                                      GLIDE_PTT_RECOGNIZER_LANGUAGES=self.recognizer_languages),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode:
            raise RuntimeError((out.stderr or "").strip() or "Windows speech recognition failed")
        return (out.stdout or "").strip()


def _multipart(fields, file_field, path):
    """multipart/form-data body for an OpenAI-style transcription request -> (body bytes, content type)."""
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        if v:
            parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode("utf-8"))
    with open(path, "rb") as fh:
        data = fh.read()
    parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="question.wav"\r\n'
                  'Content-Type: audio/wav\r\n\r\n' % (b, file_field)).encode("utf-8") + data + b"\r\n")
    parts.append(("--%s--\r\n" % b).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + b


# online engines: (label, endpoint, key variable, default model)
ONLINE = {
    "openai": ("OpenAI", "https://api.openai.com/v1/audio/transcriptions", "OPENAI_API_KEY", "gpt-4o-mini-transcribe"),
    "groq": ("Groq", "https://api.groq.com/openai/v1/audio/transcriptions", "GROQ_API_KEY", "whisper-large-v3-turbo"),
    "deepgram": ("Deepgram", "https://api.deepgram.com/v1/listen", "DEEPGRAM_API_KEY", "nova-3"),
}


class OnlineSTT:
    """Speech-to-text through an online API (see ONLINE). Raises on failure so the caller can fall back."""

    def __init__(self, engine, model=None, timeout=15, *, language="en"):
        label, self.url, env, default = ONLINE[engine]
        self.engine, self.model = engine, model or default
        self.provider, self.model_id = label, self.model
        selected_language = language_code(language)
        # The online APIs use ISO Javanese jv; local Whisper uses its legacy jw token.
        self.language = {"fil": "tl", "jw": "jv", "nb": "no"}.get(selected_language, selected_language)
        self.key = os.environ.get(env, "").strip()
        if not self.key:
            raise ValueError("%s isn't set (Settings > AI models & keys)" % env)
        self.timeout = timeout
        self.name = "%s %s" % (label, self.model)

    def transcribe(self, path, prompt=""):
        if self.engine == "deepgram":
            from urllib.parse import urlencode
            q = [("model", self.model), ("language", self.language), ("smart_format", "true")]
            # nova-3 keyterm prompting: the game names most likely to come up
            names = [n.strip() for n in (prompt.split("Names:", 1)[-1] if "Names:" in prompt else "").split(",")]
            q += [("keyterm", n) for n in names if n][:30]
            with open(path, "rb") as fh:
                req = urllib.request.Request(self.url + "?" + urlencode(q), data=fh.read(), method="POST",
                                             headers={"authorization": "Token " + self.key,
                                                      "content-type": "audio/wav"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raise RuntimeError("%s (%s) %s: %s" % (
                    self.name, self.language, e.code, e.read().decode("utf-8", "replace")[:200])) from e
            alts = ((d.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}]
            return (alts[0].get("transcript") or "").strip()
        body, ctype = _multipart({"model": self.model, "language": self.language, "response_format": "json",
                                  "prompt": prompt[:800]}, "file", path)
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"authorization": "Bearer " + self.key, "content-type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return (json.loads(r.read().decode("utf-8")).get("text") or "").strip()
        except urllib.error.HTTPError as e:
            raise RuntimeError("%s (%s) %s: %s" % (
                self.name, self.language, e.code, e.read().decode("utf-8", "replace")[:200])) from e


def beep(freq, ms):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception as e:  # noqa: BLE001 - only the cue is lost
        log_once("ptt.beep", e)


class PushToTalk:
    on_state = None                             # set in __init__; a bare instance (the tests) has no indicator
    key_down = False                            # the key is held now ("idle" from a previous press may come later)
    transcribing = False                        # the last recording is still being turned into text

    @property
    def opt(self):
        """The ptt settings, read through settings_manifest.json (defaults, ranges)."""
        return settings.Settings("ptt", self.cfg)

    def __init__(self, cfg, on_question, on_status, prompt_fn, focus_check=None, on_state=None):
        self.cfg = cfg
        self.language = language_code(cfg.get("language"))
        self.on_question = on_question          # callback(text)
        self.on_status = on_status              # callback(message dict)
        self.prompt_fn = prompt_fn              # -> vocabulary hint string
        self.focus_check = focus_check          # -> bool, may the key trigger now?
        self.on_state = on_state                # callback("listening" | "transcribing" | "idle"), as it happens
        self.keys = parse_key(self.opt.get("key"))
        self.stt = None
        self.fallback = None                    # the local engine, when an online one is in use
        self.ok = False
        if not IS_WINDOWS:
            return
        self.rec = Recorder()
        self.user32 = ctypes.windll.user32
        threading.Thread(target=self._load_stt, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        self.ok = True

    def _load_local(self, engine):
        """The local engine: whisper when installed (auto / whisper), else Windows speech."""
        if engine in ("auto", "whisper") or engine in ONLINE:
            try:
                return WhisperSTT(self.opt.get("whisper_model"), language=self.language)
            except ModelNotDownloaded:
                self.on_status({"kind": "info", "text": "push-to-talk: the offline speech model is still downloading; "
                                "using Windows speech until it's ready", "speak": False})
            except ImportError:
                if engine == "whisper":
                    self.on_status({"kind": "error", "text": "push-to-talk: faster-whisper is not installed "
                                    "(run setup-voice.cmd); using Windows speech instead", "speak": False})
            except Exception as e:
                self.on_status({"kind": "error", "text": "push-to-talk: whisper failed to load (%s); "
                                "using Windows speech" % e, "speak": False})
        return WindowsSTT(language=self.language)

    def _load_stt(self):
        engine = self.opt.get("engine")
        if engine in ONLINE:
            try:
                online = OnlineSTT(engine, self.cfg.get("online_model") or None, language=self.language)
            except ValueError as e:
                self.on_status({"kind": "error", "text": "push-to-talk: %s; using local speech-to-text" % e,
                                "speak": False})
                online = None
            self.fallback = self._load_local(engine)
            self.stt = online or self.fallback
        else:
            self.stt = self._load_local(engine)
        log("ptt", "hold %s to ask the coach (speech-to-text: %s%s)" % (
            self.opt.get("key"), self.stt.name,
            ", falls back to %s" % self.fallback.name if self.fallback and self.fallback is not self.stt else ""))

    def _state(self, state):
        if self.on_state:
            try:
                self.on_state(state)
            except Exception as e:  # noqa: BLE001 - only the indicator is lost
                log_once("ptt.state", e)

    def _held(self):
        return all(self.user32.GetAsyncKeyState(k) & 0x8000 for k in self.keys)

    def _loop(self):
        down_at = None
        skip = False                            # a press that arrived while transcribing: wait for a fresh one
        while True:
            time.sleep(0.02)
            held = self._held()
            if held and down_at is None:
                if skip or self.transcribing:   # a press during transcription is ignored, not queued
                    skip = True
                    continue
                if self.focus_check and not self.focus_check():
                    continue
                try:
                    self.rec.start()
                except Exception as e:
                    self.on_status({"kind": "error", "text": "push-to-talk: microphone: %s" % e, "speak": False})
                    time.sleep(2)
                    continue
                down_at = time.time()
                self.key_down = True
                self._state("listening")
                if self.opt.get("beep"):
                    threading.Thread(target=beep, args=(880, 60), daemon=True).start()
            elif not held:
                skip = False
                if down_at is None:
                    continue
                dur = time.time() - down_at
                down_at = None
                self.key_down = False
                path = os.path.join(tempfile.gettempdir(), "glide_ptt.wav")
                try:
                    self.rec.stop(path)
                except Exception as e:
                    self._state("idle")
                    self.on_status({"kind": "error", "text": "push-to-talk: %s" % e, "speak": False})
                    continue
                if self.opt.get("beep"):
                    threading.Thread(target=beep, args=(520, 60), daemon=True).start()
                if dur < self.opt.get("min_seconds"):
                    self._state("idle")
                    continue
                self._state("transcribing")
                self.transcribing = True
                threading.Thread(target=self._handle, args=(path,), daemon=True).start()

    def _handle(self, path):
        try:
            self._transcribe(path)
        finally:
            self.transcribing = False
            self._state("idle")

    def _transcribe(self, path):
        while self.stt is None:
            time.sleep(0.1)
        t0 = time.time()
        prompt = self.prompt_fn() if self.prompt_fn else ""
        stt = self.stt
        started = time.monotonic()
        used_fallback = False
        def attribution(status):
            result = {"category": "STT", "operation": "transcription", "role": "spoken question",
                      "elapsed_ms": round((time.monotonic() - started) * 1000), "status": status}
            for key, value in (("provider", getattr(stt, "provider", None)),
                               ("model", getattr(stt, "model_id", None)),
                               ("language", getattr(stt, "language", None))):
                if isinstance(value, str) and len(value) <= 240 and "://" not in value and \
                        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+ -]*", value):
                    result[key] = value
            return result
        try:
            text = stt.transcribe(path, prompt)
        except Exception as e:
            if self.fallback is None or self.fallback is stt:
                self.on_status({"kind": "error", "text": "push-to-talk: transcription failed: %s" % e,
                                "speak": False, "attribution": attribution("error")})
                return
            self.on_status({"kind": "info", "text": "push-to-talk: %s failed (%s); using %s"
                            % (stt.name, str(e)[:160], self.fallback.name), "speak": False,
                            "attribution": attribution("fallback")})
            stt = self.fallback
            used_fallback = True
            try:
                text = stt.transcribe(path, prompt)
            except Exception as e2:
                self.on_status({"kind": "error", "text": "push-to-talk: transcription failed: %s" % e2,
                                "speak": False, "attribution": attribution("error")})
                return
        if not text:
            self.on_status({"kind": "error", "text": "Didn't catch that.", "speak": True,
                            "attribution": attribution("empty")})
            return
        self.on_status({"kind": "question", "text": "🎙 " + text, "speak": False,
                        "model": "%s %.1fs" % (stt.name, time.time() - t0),
                        "attribution": attribution("completed after fallback" if used_fallback else "completed")})
        self.on_question(text)
