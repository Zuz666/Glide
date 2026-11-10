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
        if announce:
            self.announce()

    def announce(self, localize=None):
        if self.enabled and self.opt.get("announce"):
            # Translation runs off the hub's client hook; no network while it holds its client lock.
            self.hub.on_first_client(lambda: _announce(self, localize))

    def set_muted(self, muted):
        """Muted, nothing goes out (the app's narrator also drops what it gets while the app is muted)."""
        self.muted = bool(muted)
        return self.muted

    def hush(self):
        """The player started talking (push-to-talk): the app's narrator stops on the "ptt" event itself."""
        return None

    def say(self, text, interrupt=False, tone="", first=True):
        """tone: how to deliver this one line (the coach's TONE: in character), added to the character's voice; it
        goes as its own field on a reply's first sentence only (first=False: a later one: ElevenLabs v3 acts it as a
        tag, once a line), and folded into the style on every sentence (OpenAI performs the whole line with it)."""
        if not self.enabled or not text or self.muted:
            return
        style = self.style_fn() if self.style_fn else ""      # the character the coach plays: how to perform it
        tone = " ".join(str(tone or "").split())
        if tone:
            style = ("%s. This line: %s." % (style.rstrip(" ."), tone)) if style else "Deliver this line %s." % tone
        # a tone always comes with a style (folded in above): {style, tone}, {style} or neither
        self.hub.publish("speak", {"text": text.replace("\n", " ").replace("\r", " ").strip(),
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
        self.log_path = os.path.join(log_dir or tempfile.gettempdir(), "voice.log")
        self.enabled = self.opt.get("enabled") and sys.platform == "win32"
        if self.enabled:
            self._start()
            if announce:
                self.announce()

    def announce(self, localize=None):
        if self.enabled and self.opt.get("announce"):
            _announce(self, localize)

    def _start(self):
        with self._state_lock:
            self._pending.clear()
            self.proc = None
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
        """Drain native diagnostics and report only matched, successful completions from the current worker."""
        stdout = proc.stdout
        if stdout is None:
            return
        try:
            with stdout, open(self.log_path, "a", encoding="utf-8", buffering=1) as log:
                for line in stdout:
                    log.write(line)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or event.get("event") not in ("done", "error", "cancelled"):
                        continue
                    ident = event.get("id")
                    if not isinstance(ident, int):
                        continue
                    with self._state_lock:
                        if self.proc is not proc:
                            continue
                        text = self._pending.pop(ident, None)
                        at = time.time() * 1000
                    if event["event"] == "done" and text is not None and isinstance(event.get("voice"), str):
                        if self.on_spoken is not None:
                            try:
                                self.on_spoken({"voice": event["voice"], "text": text, "at": at})
                            except Exception as e:  # the UI must not stop the native reader
                                console.log_once("voice.status", e)
        except Exception as e:
            console.log_once("voice.reader", e)
        finally:
            with self._state_lock:
                if self.proc is proc:
                    self._pending.clear()

    def _send(self, stdin: IO[str], op: str, **fields):
        """Use the caller's checked pipe under the voice lock so commands cannot interleave."""
        self._sequence += 1
        if op == "speak":
            # Register before writing: a short turn can finish before write/flush returns.
            with self._state_lock:
                self._pending[self._sequence] = fields["text"]
        stdin.write(json.dumps({"id": self._sequence, "op": op, **fields}, ensure_ascii=False) + "\n")
        stdin.flush()

    def set_muted(self, muted):
        """Mute or unmute; muting cancels the active native turn and all queued lines."""
        with self.lock:
            self.muted = bool(muted)
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

    def say(self, text, interrupt=False, tone="", first=True):
        if not self.enabled or not text or self.muted:     # (tone, first: Windows speech can't perform a tone)
            return
        line = text.replace("\n", " ").replace("\r", " ").strip()
        with self.lock:
            if self.muted:                  # muted while this line waited for the lock
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
                self._send(stdin, "speak", text=line, voice=self.opt.get("voice") or "",
                           rate=int(self.opt.get("rate")), volume=int(self.opt.get("volume")), interrupt=bool(interrupt))
            except Exception as e:
                console.log("voice", "write failed: %s" % e)
                with self._state_lock:
                    self._pending.clear()
                    self.proc = None
