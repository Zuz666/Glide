"""Text-to-speech through Windows' built-in speech engine (one persistent PowerShell process).

The script is run with -File so its arguments reach $args (with -Command they don't, which left the
volume at 0). Errors go to data/live/voice.log; the process is restarted if it dies.
"""
import os
import subprocess
import sys
import tempfile
import threading
from typing import Callable

import settings
import log as console
from localization import TEXT_RESOURCES

PS = r"""
$ErrorActionPreference = 'Continue'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToDefaultAudioDevice()
$s.Rate = [int]$args[0]
$s.Volume = [int]$args[1]
if ($args.Count -gt 2 -and $args[2]) {
  try { $s.SelectVoice($args[2]) } catch { [Console]::Error.WriteLine("voice not found: " + $args[2]) }
}
[Console]::Error.WriteLine("voice ready: " + $s.Voice.Name + " rate " + $s.Rate + " volume " + $s.Volume)
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($line -eq $null) { break }
  try {
    if ($line.StartsWith('!')) { $s.SpeakAsyncCancelAll(); $line = $line.Substring(1) }
    if ($line.Length -gt 0) { [void]$s.SpeakAsync($line) }
  } catch { [Console]::Error.WriteLine($_.Exception.Message) }
}
"""


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

    def __init__(self, cfg, log_dir=None, *, announce=True):
        self.cfg = cfg
        self.proc = None
        self.muted = False
        self.lock = threading.Lock()
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
        try:
            script = os.path.join(tempfile.gettempdir(), "glide_voice.ps1")
            with open(script, "w", encoding="utf-8-sig") as fh:
                fh.write(PS)
            log = open(self.log_path, "a", encoding="utf-8")
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 str(int(self.opt.get("rate"))), str(int(self.opt.get("volume"))),
                 self.opt.get("voice") or ""],
                stdin=subprocess.PIPE, stdout=log, stderr=log, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            console.log("voice", "disabled: %s" % e)
            self.proc = None

    def set_muted(self, muted):
        """Mute or unmute; muting cuts off the line being spoken (the speech process's "!" = interrupt)."""
        with self.lock:
            self.muted = bool(muted)
            stdin = self.proc.stdin if self.proc is not None else None
            if self.muted and stdin is not None:
                try:
                    stdin.write("! \n")
                    stdin.flush()
                except Exception as e:  # noqa: BLE001 - a dead process is restarted by the next say()
                    console.log_once("voice.mute", e)
        return self.muted

    def hush(self):
        """The player started talking (push-to-talk): cut off the line being said and what's queued ("!"). Never
        waits long for the lock (say() may be restarting the speech process): the push-to-talk thread calls it."""
        if not self.lock.acquire(timeout=0.05):
            return
        try:
            stdin = self.proc.stdin if self.proc is not None else None
            if stdin is not None:
                try:
                    stdin.write("! \n")
                    stdin.flush()
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
                stdin.write(("!" if interrupt else "") + line + "\n")
                stdin.flush()
            except Exception as e:
                console.log("voice", "write failed: %s" % e)
                self.proc = None
