"""Native SAPI5 speech through one persistent PowerShell worker, shared with the desktop app.

Commands and completion diagnostics use UTF-8 JSON lines. The worker keeps installed COM voice tokens alive,
including third-party engines; errors go to voice.log, and a later say() restarts a dead process.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Callable

import settings
import log as console
from localization import TEXT_RESOURCES
import speech_markup

MAX_SSML_DOCUMENT_LENGTH = 65536  # 4000 escaped scalars plus all 128 permitted span controls.



def _announce(voice, localize=None):
    def say():
        text = TEXT_RESOURCES["voice.ready"]
        voice.say(localize(text) if localize is not None else text)
    if localize is None:
        say()
    else:
        threading.Thread(target=say, daemon=True, name="voice-announcement").start()


class HubVoice:
    """Speech done by the desktop app: each line goes out as a "speak" event ({text, interrupt, style?, tone?}) on the
    dashboard's event stream, and the app speaks it with the voice picked in its settings (voice "output": "app").
    style_fn: () -> how to perform the lines (the coach's persona, persona.py), "" for the voice as it is.
    style folds the line's tone in (the OpenAI voice performs it whole); tone also goes alone, for a voice that acts
    a short cue (ElevenLabs v3's audio tags)."""
    style_fn: Callable[[], str] | None = None

    @property
    def opt(self):
        """The voice settings, read through settings_manifest.json (defaults, ranges)."""
        return settings.Settings("voice", self.cfg)

    def __init__(self, cfg, hub, *, announce=True):
        self.cfg, self.hub = cfg, hub
        self.enabled = self.opt.get("enabled")
        self.muted = False
        self._cancel_generation = 0
        if announce:
            self.announce()

    def announce(self, localize=None):
        if self.enabled and self.opt.get("announce"):
            # Translation runs off the hub's client hook; no network while it holds its client lock.
            self.hub.on_first_client(lambda: _announce(self, localize))

    def set_muted(self, muted):
        """Muted, nothing goes out (the app's narrator also drops what it gets while the app is muted)."""
        self.muted = bool(muted)
        if self.muted:
            self._cancel_generation += 1
        return self.muted

    def hush(self):
        """The player started talking (push-to-talk): the app's narrator stops on the "ptt" event itself."""
        self._cancel_generation += 1
        return None

    def cancellation_token(self):
        """Capture cancellation identity before potentially blocking line preparation."""
        return self._cancel_generation

    def markup_destination(self):
        if not self.enabled:
            return {"provider": "", "identity": "", "features": []}
        destination = self.opt.get("markup_destination")
        return dict(destination) if isinstance(destination, dict) else {"provider": "", "identity": "", "features": []}


    def say(self, text, interrupt=False, tone="", first=True, markup=None, features=None, cancellation_token=None):
        """tone: how to deliver this one line (the coach's TONE: in character), added to the character's voice; it
        goes as its own field on a reply's first sentence only (first=False: a later one: ElevenLabs v3 acts it as a
        tag, once a line), and folded into the style on every sentence (OpenAI performs the whole line with it)."""
        if not self.enabled or not text or self.muted:
            return
        generation = self._cancel_generation if cancellation_token is None else cancellation_token
        if generation != self._cancel_generation:
            return
        style = self.style_fn() if self.style_fn else ""      # the character the coach plays: how to perform it
        tone = " ".join(str(tone or "").split())
        line = speech_markup.canonical(text)
        destination = self.markup_destination()
        if features is not None:
            destination["features"] = [f for f in destination.get("features", []) if f in features]
        prepared = speech_markup.prepare(text, destination, markup, mode=self.opt.get("markup_mode"))
        if tone:
            style = ("%s. This line: %s." % (style.rstrip(" ."), tone)) if style else "Deliver this line %s." % tone
        # a tone always comes with a style (folded in above): {style, tone}, {style} or neither
        if generation != self._cancel_generation or self.muted:
            return
        self.hub.publish("speak", {"text": line,
                                   **({"markup": prepared} if prepared else {}),
                                   "interrupt": bool(interrupt),
                                   **({"style": style, "tone": tone} if tone and first else
                                      {"style": style} if style else {})})


class Voice:
    style_fn: Callable[[], str] | None = None    # unused: Windows speech can't perform a persona
    @property
    def opt(self):
        """The voice settings, read through settings_manifest.json (defaults, ranges)."""
        return settings.Settings("voice", self.cfg)

    def __init__(self, cfg, log_dir=None, *, announce=True, on_spoken=None):
        self.cfg = cfg
        self.proc = None
        self._sequence = 0
        self._pending = {}
        self.on_spoken = on_spoken
        self.muted = False
        self.lock = threading.Lock()          # serialize commands, including blocking pipe writes
        self._state_lock = threading.Lock()   # pending turns/worker identity only; never held across I/O
        self._enqueue_lock = threading.Lock()  # serialize snapshots without blocking cancellation during discovery
        self._cancel_generation = 0
        self.log_path = os.path.join(log_dir or tempfile.gettempdir(), "voice.log")
        self._catalogue = []
        self._catalogue_request = None
        self.enabled = self.opt.get("enabled") and sys.platform == "win32"
        if self.enabled:
            self._start()
            if announce:
                self.announce()

    def announce(self, localize=None):
        if self.enabled and self.opt.get("announce"):
            _announce(self, localize)

    @staticmethod
    def _log_turn(turn, status, detail=""):
        attribution = dict(turn["attribution"], status=status,
                           elapsed_ms=round((time.monotonic() - turn["submitted"]) * 1000))
        text = "SAPI5 %s request=%s" % (status, attribution["request_id"])
        if status == "submitted":
            text += " text=" + json.dumps(turn["text"], ensure_ascii=False)
        if detail:
            text += " reason=" + json.dumps(detail, ensure_ascii=False)
        console.log("voice", text, attribution)

    def _fail_pending(self, detail):
        with self._state_lock:
            pending, self._pending = self._pending, {}
        for turn in pending.values():
            self._log_turn(turn, "error", detail)

    def _start(self):
        self._fail_pending("Speech worker replaced before completion")
        with self._state_lock:
            self.proc = None
            self._catalogue = []
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sapi5.ps1")
            with open(self.log_path, "a", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._state_lock:
                self.proc = proc
            threading.Thread(target=self._read_status, args=(proc,), daemon=True, name="sapi5-status").start()
        except Exception as e:
            console.log("voice", "disabled: %s" % e)
            with self._state_lock:
                self.proc = None

    def _read_status(self, proc):
        """Drain native diagnostics; only matched successful completions update last-spoken state."""
        stdout = proc.stdout
        if stdout is None:
            return
        try:
            with stdout, open(self.log_path, "a", encoding="utf-8", buffering=1) as log:
                for line in stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        log.write(line)
                        continue
                    # Completion XML belongs to the last-spoken observation, not diagnostic logs.
                    if isinstance(event, dict) and "ssml_document" in event:
                        log.write(json.dumps({key: value for key, value in event.items() if key != "ssml_document"},
                                             ensure_ascii=False) + "\n")
                    else:
                        log.write(line)
                    if isinstance(event, dict) and event.get("event") in ("ready", "voices"):
                        with self._state_lock:
                            if self.proc is proc and isinstance(event.get("voices"), list):
                                self._catalogue = event["voices"]
                                request = self._catalogue_request
                                if request is not None and event.get("id") == request[0]:
                                    request[1].set()
                        continue
                    if isinstance(event, dict) and event.get("event") == "error":
                        with self._state_lock:
                            request = self._catalogue_request
                            if self.proc is proc and request is not None and event.get("id") == request[0]:
                                self._catalogue = []
                                request[1].set()
                                continue
                    if not isinstance(event, dict) or event.get("event") not in ("started", "done", "error", "cancelled"):
                        continue
                    ident = event.get("id")
                    if not isinstance(ident, int):
                        continue
                    with self._state_lock:
                        if self.proc is not proc:
                            continue
                        turn = self._pending.get(ident)
                        if turn is None:
                            continue
                        if event["event"] == "started" and event.get("word_boundary") is not True:
                            continue
                        elapsed_ms = round((time.monotonic() - turn["submitted"]) * 1000)
                        if event["event"] == "started":
                            turn["synth_ms"] = elapsed_ms
                        else:
                            self._pending.pop(ident)
                        if isinstance(event.get("voice"), str):
                            turn["attribution"]["voice"] = event["voice"]
                        at = time.time() * 1000
                    self._log_turn(turn, event["event"], str(event.get("error") or ""))
                    if event["event"] == "done" and isinstance(event.get("voice"), str):
                        if self.on_spoken is not None:
                            try:
                                spoken_payload = {"voice": event["voice"], "text": turn["text"], "at": at,
                                                  "ssml": turn["ssml"], "completion_ms": elapsed_ms}
                                document = event.get("ssml_document")
                                if isinstance(document, str) and 0 < len(document) <= MAX_SSML_DOCUMENT_LENGTH:
                                    spoken_payload["ssml_document"] = document
                                if turn.get("synth_ms") is not None:
                                    spoken_payload["synth_ms"] = turn["synth_ms"]
                                # Ages use the worker's clock; anchor them to this request's completion clock.
                                first_age = event.get("first_audio_ago_ms")
                                last_age = event.get("last_audio_ago_ms")
                                if (isinstance(first_age, (int, float)) and isinstance(last_age, (int, float))
                                        and not isinstance(first_age, bool) and not isinstance(last_age, bool)
                                        and 0 <= last_age <= first_age <= elapsed_ms):
                                    spoken_payload["first_audio_ms"] = round(elapsed_ms - first_age)
                                    spoken_payload["last_audio_ms"] = round(elapsed_ms - last_age)
                                self.on_spoken(spoken_payload)
                            except Exception as e:  # the UI must not stop the native reader
                                console.log_once("voice.status", e)
        except Exception as e:
            console.log_once("voice.reader", e)
        finally:
            with self._state_lock:
                pending = self._pending if self.proc is proc else {}
                if self.proc is proc:
                    self._pending = {}
                    self._catalogue = []
                    if self._catalogue_request is not None:
                        self._catalogue_request[1].set()
            for turn in pending.values():
                self._log_turn(turn, "error", "Speech worker closed before completion")

    def _send(self, stdin: IO[str], op: str, **fields):
        """Use the caller's checked pipe under the voice lock so commands cannot interleave."""
        self._sequence += 1
        if op == "speak":
            turn = {"text": fields["text"], "submitted": time.monotonic(), "ssml": fields.get("markup") is not None,
                    "attribution": {"category": "TTS", "operation": "native speech",
                                    "provider": "SAPI5", "request_id": "standalone-%d" % self._sequence,
                                    "voice": fields.get("voice") or "Windows default",
                                    "rate": fields["rate"], "volume": fields["volume"]}}
            # Register before writing: a short turn can finish before write/flush returns.
            with self._state_lock:
                self._pending[self._sequence] = turn
            self._log_turn(turn, "submitted")
        stdin.write(json.dumps({"id": self._sequence, "op": op, **fields}, ensure_ascii=False) + "\n")
        stdin.flush()

    def set_muted(self, muted):
        """Mute or unmute; muting cancels the active native turn and all queued lines."""
        with self.lock:
            self.muted = bool(muted)
            if self.muted:
                self._cancel_generation += 1
            stdin = self.proc.stdin if self.proc is not None else None
            if self.muted and stdin is not None:
                try:
                    self._send(stdin, "cancel")
                except Exception as e:  # noqa: BLE001 - a dead process is restarted by the next say()
                    console.log_once("voice.mute", e)
        return self.muted

    def hush(self):
        """The player started talking: cancel active and queued speech. Never wait long for the lock
        (say() may be restarting the speech process): the push-to-talk thread calls it."""
        self._cancel_generation += 1
        if not self.lock.acquire(timeout=0.05):
            return
        try:
            stdin = self.proc.stdin if self.proc is not None else None
            if stdin is not None:
                try:
                    self._send(stdin, "cancel")
                except Exception as e:  # noqa: BLE001 - a dead process is restarted by the next say()
                    console.log_once("voice.hush", e)
        finally:
            self.lock.release()

    def cancellation_token(self):
        """Capture cancellation identity before potentially blocking line preparation."""
        return self._cancel_generation

    def _markup_destination_locked(self):
        permissions = self.opt.get("markup_voices")
        # Resolve again when permissions could matter. A stale default/name never
        # transfers another token's effects; an unavailable catalogue is neutral.
        if (permissions or self._catalogue) and self.proc is not None and self.proc.stdin is not None:
            ready = threading.Event()
            request = (self._sequence + 1, ready)
            with self._state_lock:
                self._catalogue_request = request
            self._send(self.proc.stdin, "voices")
            # Discovery never synthesizes. Let PTT/mute write while it waits,
            # but retain line ordering with the separate enqueue lock.
            self.lock.release()
            try:
                received = ready.wait(15)
            finally:
                self.lock.acquire()
            if not received:
                return {"provider": "windows", "identity": "", "features": []}
        with self._state_lock:
            catalogue = list(self._catalogue)
        selection = self.opt.get("voice") or ""
        resolved = next((v for v in catalogue if isinstance(v, dict) and
                         (v.get("identity") == selection or v.get("id") == selection
                          if selection else v.get("isDefault") is True)), None)
        identity = resolved.get("identity", "") if resolved else ""
        allowed = permissions.get(identity, []) if isinstance(permissions, dict) and identity else []
        allowed = allowed if isinstance(allowed, list) else []
        return {"provider": "windows", "identity": identity,
                "features": [f for f in ("rate", "pitch", "boundaries", "pauses") if f in allowed]}

    def markup_destination(self):
        if not self.enabled:
            return {"provider": "", "identity": "", "features": []}
        with self._enqueue_lock, self.lock:
            try:
                return self._markup_destination_locked()
            except Exception as e:
                console.log_once("voice.catalogue", e)
                return {"provider": "windows", "identity": "", "features": []}


    def say(self, text, interrupt=False, tone="", first=True, markup=None, features=None, cancellation_token=None):
        if not self.enabled or not text or self.muted:     # (tone, first: Windows speech can't perform a tone)
            return
        line = speech_markup.canonical(text)
        generation = self._cancel_generation if cancellation_token is None else cancellation_token
        with self._enqueue_lock, self.lock:
            if self.muted or generation != self._cancel_generation:
                return
            if self.proc is None or self.proc.poll() is not None:
                console.log("voice", "speech process stopped; restarting (see %s)" % self.log_path)
                self._start()
                if self.proc is None:
                    return
            stdin = self.proc.stdin
            if stdin is None:
                return
            try:
                destination = self._markup_destination_locked()
                if generation != self._cancel_generation or self.muted:
                    return
                if features is not None:
                    destination["features"] = [f for f in destination["features"] if f in features]
                prepared = speech_markup.prepare(text, destination, markup, mode=self.opt.get("markup_mode"))
                if generation != self._cancel_generation or self.muted:
                    return
                self._send(stdin, "speak", text=line, voice=destination["identity"] or self.opt.get("voice") or "",
                           rate=int(self.opt.get("rate")), volume=int(self.opt.get("volume")), interrupt=bool(interrupt),
                           features=list(destination["features"]), **({"markup": prepared} if prepared else {}))
            except Exception as e:
                console.log("voice", "write failed: %s" % e)
                self._fail_pending("Could not send speech to worker")
                with self._state_lock:
                    self.proc = None
