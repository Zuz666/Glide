#!/usr/bin/env python3
"""Real JSON-line SAPI worker with test-side, silent COM creation replacements.

The worker and its polling/lifecycle code run unchanged. No installed voice, audio
endpoint, application settings or network is used; only Windows PowerShell is needed.
"""
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time


POWERSHELL = shutil.which("powershell.exe")
WORKER = Path(__file__).resolve().parents[1] / "companion" / "sapi5.ps1"

# Replace the OS/COM creation boundary before loading the real entry point. The
# deterministic cursor advances are independent of elapsed wall-clock time.
FIXTURE = r"""
param([string]$Worker, [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
public sealed class TimingToken {
    public string Id { get; private set; }
    public TimingToken(string id) { Id = id; }
    public string GetDescription() { return Id; }
    public string GetAttribute(string name) { return "409"; }
}
public sealed class TimingTokens {
    static readonly string[] Names = { "normal", "unavailable", "late-failure", "no-data", "flat", "reset", "hold", "speak-failure", "synthesis-failure" };
    public int Count { get { return Names.Length; } }
    public TimingToken Item(int index) { return new TimingToken(Names[index]); }
}
public sealed class TimingCategory {
    public void SetId(string id, bool create) { }
    public TimingTokens EnumerateTokens(string required, string optional) { return new TimingTokens(); }
}
public sealed class TimingAudioStatus {
    public object CurrentSeekPosition { get; set; }
}
public sealed class TimingOutput {
    readonly TimingVoice owner;
    public TimingOutput(TimingVoice voice) { owner = voice; }
    public TimingAudioStatus Status {
        get {
            if (owner.Mode == "unavailable" || (owner.Mode == "late-failure" && owner.Poll >= 2))
                throw new InvalidOperationException("Controlled cursor unavailability");
            if (owner.Mode == "no-data") return new TimingAudioStatus();
            decimal position = owner.Mode == "hold" && owner.Running ? 164 : 100;
            if (owner.Mode == "reset" && owner.Poll >= 2) position = 32;
            else if (owner.Mode != "flat" && owner.Mode != "hold") {
                if (owner.Poll >= 4) position = 260;
                else if (owner.Poll >= 2) position = 228;
                else if (owner.Poll >= 1) position = 164;
            }
            return new TimingAudioStatus { CurrentSeekPosition = position };
        }
    }
}
public sealed class TimingFile {
    public void Open(string path, int mode, bool events) { }
    public void Close() { }
}
public sealed class TimingVoiceStatus {
    public int LastHResult { get; set; }
    public int InputWordLength { get { return 1; } }
}
public sealed class TimingVoice {
    TimingToken token = new TimingToken("normal");
    readonly TimingOutput output;
    object assignedOutput;
    public TimingVoice() { output = new TimingOutput(this); }
    public string Mode { get { return token.Id; } }
    public int Poll { get; private set; }
    public bool Running { get; private set; }
    public TimingToken Voice {
        get { return token; }
        set { token = value; Poll = 0; Running = false; }
    }
    public int Rate { get; set; }
    public int Volume { get; set; }
    public object AudioOutputStream {
        get { return assignedOutput ?? output; }
        set { assignedOutput = value; }
    }
    public TimingTokens GetVoices(string required, string optional) { return new TimingTokens(); }
    public int Speak(string text, int flags) {
        if (flags == 19) { Poll = 0; Running = false; return 0; }
        if (Mode == "speak-failure") throw new InvalidOperationException("Controlled Speak failure");
        Poll = 0;
        Running = true;
        return 1;
    }
    public bool WaitUntilDone(int timeout) { Poll++; return Mode != "hold" && Poll >= 6; }
    public TimingVoiceStatus Status {
        get { return new TimingVoiceStatus { LastHResult = Mode == "synthesis-failure" && Poll >= 6 ? unchecked((int)0x80004005) : 0 }; }
    }
}
'@
function New-Object {
    param([string]$ComObject)
    switch ($ComObject) {
        'SAPI.SpVoice' { return [TimingVoice]::new() }
        'SAPI.SpObjectTokenCategory' { return [TimingCategory]::new() }
        'SAPI.SpFileStream' { return [TimingFile]::new() }
        default { throw "Unexpected COM class: $ComObject" }
    }
}
& $Worker -OutputDirectory $OutputDirectory
exit $LASTEXITCODE
"""


class Worker:
    def __init__(self, fixture, output_directory=""):
        self.events = queue.Queue()
        self.observed = []
        self.sequence = 0
        arguments = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(fixture), "-Worker", str(WORKER)]
        if output_directory:
            arguments.extend(["-OutputDirectory", output_directory])
        self.process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.receive(None, "ready")

    def _read(self):
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line.lstrip("\ufeff")))
            except ValueError:
                self.events.put({"event": "invalid", "line": line})
        self.events.put({"event": "eof"})

    def command(self, op, **fields):
        self.sequence += 1
        self.process.stdin.write(json.dumps({"id": self.sequence, "op": op, **fields}) + "\n")
        self.process.stdin.flush()
        return self.sequence

    def speak(self, voice="normal", **fields):
        return self.command("speak", voice=voice, text="Controlled speech.", rate=0, volume=70, **fields)

    def receive(self, ident, expected):
        while True:
            event = self.events.get(timeout=15)
            self.observed.append(event)
            assert event["event"] not in ("fatal", "invalid", "eof"), event
            if event.get("id") == ident:
                if event["event"] == "started" and expected != "started":
                    assert event["word_boundary"] is True, event
                    absent(event)
                    continue
                assert event["event"] == expected, event
                return event

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError("Worker did not exit after EOF") from error
        finally:
            self.reader.join(5)
            self.process.stdout.close()
        error = self.process.stderr.read()
        self.process.stderr.close()
        assert code == 0, error


def absent(event):
    assert "first_audio_ago_ms" not in event and "last_audio_ago_ms" not in event, event


def ordinary(worker):
    before = time.monotonic()
    event = worker.receive(worker.speak(), "done")
    elapsed_ms = (time.monotonic() - before) * 1000
    first, last = event["first_audio_ago_ms"], event["last_audio_ago_ms"]
    assert type(first) in (int, float) and type(last) in (int, float), event
    assert 0 <= last < first <= elapsed_ms + 1, event
    return event


def main():
    if sys.platform != "win32" or not POWERSHELL:
        print("SKIP: controlled SAPI worker needs Windows PowerShell (no installed voice required)")
        return 77
    with tempfile.TemporaryDirectory(prefix="sapi-timing-") as directory:
        fixture = Path(directory) / "controlled-worker.ps1"
        fixture.write_text(FIXTURE, encoding="utf-8")
        worker = Worker(fixture)
        try:
            ordinary(worker)
            # Unavailable baselines, loss after an advance, missing data, a stopped
            # cursor and a backward reset all preserve successful speech without ages.
            for mode in ("unavailable", "late-failure", "no-data", "flat", "reset"):
                absent(worker.receive(worker.speak(mode), "done"))
                ordinary(worker)  # Invalid observation state must not cross turns.
            for mode in ("speak-failure", "synthesis-failure"):
                absent(worker.receive(worker.speak(mode), "error"))
                ordinary(worker)
            absent(worker.receive(worker.command("warmup", voice="normal"), "warmed"))
            held = worker.speak("hold")
            absent(worker.receive(held, "started"))
            cancelled = worker.command("cancel", target=held)
            absent(worker.receive(held, "cancelled"))
            worker.receive(cancelled, "ok")
            ordinary(worker)
        finally:
            worker.close()
        file_worker = Worker(fixture, directory)
        try:
            absent(file_worker.receive(file_worker.speak(), "done"))
        finally:
            file_worker.close()
    print("Controlled SAPI worker timing lifecycle passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
