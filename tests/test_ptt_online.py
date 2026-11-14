#!/usr/bin/env python3
"""Online push-to-talk engines (OpenAI-style multipart and Deepgram) against a mock server, and the fallback to the
local engine when the online one fails."""
import http.server
import io
import json
import os
import sys
import struct
import threading
import wave
from email.parser import BytesParser
from email.policy import default
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import ptt  # noqa: E402
from ptt_audio import Recording


class H(http.server.BaseHTTPRequestHandler):
    seen = []
    fail = False
    empty = False
    audio = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        H.seen.append((self.path, self.headers.get("authorization"), self.headers.get("content-type"), body))
        content_type = self.headers.get("content-type", "")
        if content_type.startswith("multipart/"):
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + content_type + "\r\n\r\n").encode() + body)
            upload = next(part for part in message.iter_parts() if part.get_filename())
            assert upload.get_filename() == "question.wav"
            wav_data = upload.get_payload(decode=True)
        else:
            wav_data = body
        with wave.open(io.BytesIO(wav_data), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
            assert audio.getnframes() == len(PCM) // 2
            assert audio.readframes(audio.getnframes()) == PCM
        H.audio.append(wav_data)
        if H.fail:
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        out = ({"results": {"channels": [{"alternatives": [{"transcript": "where is Marshal Dughan"}]}]}}
               if self.path.startswith("/v1/listen") else {"text": "how far to Goldshire"})
        if H.empty:
            out = {}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()  # shutdown() returns at once
ptt.ONLINE["openai"] = ("OpenAI", "http://127.0.0.1:%d/v1/audio/transcriptions" % port, "OPENAI_API_KEY", "gpt-4o-mini-transcribe")
ptt.ONLINE["deepgram"] = ("Deepgram", "http://127.0.0.1:%d/v1/listen" % port, "DEEPGRAM_API_KEY", "nova-3")

PCM = struct.pack("<7h", -32768, -16384, -1, 0, 1, 16384, 32767) * 300
recording = Recording(PCM)

# no key: refuses to start (the companion then uses local speech)
os.environ.pop("OPENAI_API_KEY", None)
try:
    ptt.OnlineSTT("openai")
    raise AssertionError("expected a missing-key error")
except ValueError as e:
    assert "OPENAI_API_KEY" in str(e)

os.environ["OPENAI_API_KEY"] = "sk-test"
s = ptt.OnlineSTT("openai")
with patch("builtins.open", side_effect=AssertionError("audio file access")):
    assert s.transcribe(recording, "World of Warcraft leveling question. Names: Goldshire") == "how far to Goldshire"
path, auth, ctype, body = H.seen[-1]
assert auth == "Bearer sk-test" and ctype.startswith("multipart/form-data; boundary=")
assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in body and b'filename="question.wav"' in body and b"RIFF" in body
assert b"Names: Goldshire" in body

os.environ["DEEPGRAM_API_KEY"] = "dg-test"
d = ptt.OnlineSTT("deepgram")
with patch("builtins.open", side_effect=AssertionError("audio file access")):
    assert d.transcribe(recording, "x. Names: Marshal Dughan, Goldshire") == "where is Marshal Dughan"
path, auth, ctype, body = H.seen[-1]
assert auth == "Token dg-test" and ctype == "audio/wav" and body.startswith(b"RIFF")
assert "model=nova-3" in path and "keyterm=Marshal+Dughan" in path and "keyterm=Goldshire" in path, path

# a failing online engine falls back to the local one for that question
H.fail = True
try:
    s.transcribe(recording)
    raise AssertionError("expected an HTTP error")
except RuntimeError as e:
    assert "429" in str(e)


class Local:
    name = "local"

    def __init__(self):
        self.recordings = []

    def transcribe(self, audio, prompt=""):
        self.recordings.append(audio)
        assert audio is recording
        assert audio.pcm == PCM
        assert audio.wav_bytes()[-len(PCM):] == PCM
        return "what next"


got, status = [], []
p = ptt.PushToTalk.__new__(ptt.PushToTalk)
p.cfg, p.on_question, p.on_status, p.prompt_fn = {}, got.append, status.append, None
p.stt, p.fallback = s, Local()
p._attempt_lock, p._attempt, p._stop = threading.RLock(), object(), threading.Event()
with patch("builtins.open", side_effect=AssertionError("audio file access")):
    p._handle(recording, p._attempt)
assert got == ["what next"], (got, status)
assert any("failed" in m["text"] and "using local" in m["text"] for m in status), status
assert p.fallback.recordings == [recording]
assert all(wav_data == recording.wav_bytes() for wav_data in H.audio)

# A normal empty online result is not a failed Recognition Attempt.
H.fail, H.empty = False, True
got.clear()
p.fallback = Local()
p._attempt = object()
p._handle(recording, p._attempt)
assert not got and not p.fallback.recordings
srv.shutdown()
print("ptt online: ok")
