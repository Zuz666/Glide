#!/usr/bin/env python3
"""WindowsTts's consumer lifecycle with a silent, controlled child-process boundary.

No SAPI installation, audio device, application settings or network access is used. The worker's
playback history catches late speaking; bounded promise checks catch startup/process-loss hangs.
"""
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
import xml.etree.ElementTree as ET


def native_file_output():
    """Opt-in real compiler/COM proof; all synthesis targets a fresh scratch directory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-output", required=True)
    parser.add_argument("--voice", default="")
    parser.add_argument("--positive-control", default="")
    args = parser.parse_args()
    powershell = shutil.which("powershell.exe")
    if not powershell or sys.platform != "win32":
        print("SKIP: native file-output proof requires 64-bit Windows PowerShell and installed SAPI voices")
        return 77
    os.makedirs(args.native_output, exist_ok=True)
    directory = tempfile.mkdtemp(prefix="sapi5-", dir=os.path.abspath(args.native_output))
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "companion", "sapi5.ps1")
    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        env[key] = ""
    events, observed = queue.Queue(), []
    sequence = 0
    with open(os.path.join(directory, "worker.log"), "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
             "-OutputDirectory", directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, encoding="utf-8", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def read_events():
            for line in proc.stdout:
                try:
                    events.put(json.loads(line.lstrip("\ufeff")))
                except ValueError:
                    events.put({"event": "invalid", "line": line})
            events.put({"event": "eof"})

        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()

        def command(op, **fields):
            nonlocal sequence
            sequence += 1
            proc.stdin.write(json.dumps({"id": sequence, "op": op, **fields}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return sequence

        def response(ident, expected):
            while True:
                event = events.get(timeout=45)
                observed.append(event)
                assert event["event"] not in ("fatal", "invalid", "eof"), event
                if event.get("id") == ident and event["event"] != "started":
                    assert event["event"] == expected, event
                    return event

        envelope = '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">%s</speak>'
        features = ["rate", "pitch", "boundaries", "pauses"]
        try:
            ready = response(None, "ready")
            voices = ready["voices"]
            assert all(v.get("identity") for v in voices), voices
            assert sum(v.get("isDefault") is True for v in voices) == 1, voices

            def selection(value):
                if not value:
                    return next(v["identity"] for v in voices if v.get("isDefault"))
                return next(v["identity"] for v in voices if value in (v["id"], v["identity"]))

            selected = selection(args.voice)
            valid = envelope % '<p><s>A 😀 &amp; B <prosody rate="slow" pitch="high">repeat</prosody> repeat.</s></p>'
            parsed = response(command("prepare", ssml=valid, features=features), "prepared")["markup"]
            assert parsed["text"] == "A 😀 & B repeat repeat.", parsed
            marked = [s for s in parsed["spans"] if s.get("rate")]
            assert len(marked) == 1 and marked[0]["start"] == 8 and marked[0]["end"] == 14, parsed
            assert sum("boundary" in s for s in parsed["spans"]) == 1, parsed
            assert parsed["spans"][-1]["boundary"] == "p", parsed
            invalid = [
                "<speak>unqualified</speak>", envelope % '<voice name="other">words</voice>',
                envelope % '<audio src="file:///secret.wav"/>', envelope % '<phoneme ph="x">word</phoneme>',
                envelope % '<prosody volume="loud">word</prosody>', envelope % '<prosody rate="x">word</prosody>',
                envelope % '<prosody pitch="x"><prosody pitch="high">word</prosody></prosody>',
                envelope % 'words<break time="1501ms"/>', envelope % '<break time="100ms"/>',
                envelope % ('<s>' * 9 + "words" + '</s>' * 9),
                envelope % ('x' * 4001), envelope % 'words<?control x?>',
                '<!DOCTYPE speak [<!ENTITY x SYSTEM "file:///secret">]>' + envelope % '&x;',
                envelope % '<foo xmlns="https://external.invalid">word</foo>',
                envelope % '<prosody rate="slow">unfinished',
            ]
            for sample in invalid:
                response(command("prepare", ssml=sample, features=features), "error")
            response(command("prepare", ssml=envelope % '<prosody pitch="high">words</prosody>', features=["rate"]), "error")
            assert not any(name.endswith(".wav") for name in os.listdir(directory)), "preparation synthesized audio"

            # Invalid controls with interrupt=true must not cancel an accepted turn or its follower.
            first = command("speak", text="Keep this line.", voice=selected, rate=0, volume=70)
            bad = command("speak", text="Invalid line.", voice=selected, rate=0, volume=70, interrupt=True,
                          markup={"text": "Invalid line.", "spans": [{"start": 0, "end": 7, "pitch": "high"}]},
                          features=[])
            follower = command("speak", text="Keep the follower.", voice=selected, rate=0, volume=70)
            # Commands are processed in order, but started/done for first may precede the validation result.
            completed = set()
            while not {first, bad, follower} <= completed:
                event = events.get(timeout=45)
                observed.append(event)
                assert event["event"] not in ("fatal", "invalid", "eof", "cancelled"), event
                if event.get("id") in (first, bad, follower) and event["event"] != "started":
                    assert event["event"] == ("error" if event["id"] == bad else "done"), event
                    completed.add(event["id"])

            measurements = []
            text = "Keep moving toward the road. Stay on the path."

            def synth(label, data=None, *, voice=selected, rate=0, volume=70, text=text):
                ident = command("speak", text=text, voice=voice, rate=rate, volume=volume,
                                features=features, **({"markup": data} if data else {}))
                done = response(ident, "done")
                if data is None:
                    assert "ssml_document" not in done, done
                else:
                    document = done["ssml_document"]
                    root = ET.fromstring(document)
                    assert root.tag == "{http://www.w3.org/2001/10/synthesis}speak", document
                    assert "".join(root.itertext()) == text[:4000], document
                path = os.path.join(directory, "%d.wav" % ident)
                with wave.open(path, "rb") as wav:
                    assert wav.getnframes() > 0, label
                    measurements.append({"sample": label, "voice": done["voice"], "identity": voice,
                                         "seconds": wav.getnframes() / wav.getframerate(),
                                         "rate": rate, "volume": volume, "wav": path})
                return done

            synth("plain")
            for effect, value in (("rate", "slow"), ("rate", "fast"), ("pitch", "low"), ("pitch", "high")):
                synth(effect + "-" + value, {"text": text, "spans": [{"start": 0, "end": len(text), effect: value}]})
            synth("scoped-rate-pitch", {"text": text, "spans": [{"start": 5, "end": 11, "rate": "slow", "pitch": "high"}]})
            synth("native-pause", {"text": text, "spans": [{"start": 0, "end": 28, "after_ms": 700}]})
            synth("plain-after")
            synth("configured-rate-volume", rate=3, volume=35)
            escaped_text = "A 😀 & <B>."
            escaped = synth("escaped-document", {"text": escaped_text, "spans": [
                {"start": 2, "end": 3, "rate": "slow", "pitch": "high"}]}, text=escaped_text)
            assert "&amp;" in escaped["ssml_document"] and "&lt;B&gt;" in escaped["ssml_document"], escaped
            root = ET.fromstring(escaped["ssml_document"])
            scoped = root.find("{http://www.w3.org/2001/10/synthesis}prosody")
            assert scoped is not None and scoped.text == "😀" and scoped.attrib == {"rate": "slow", "pitch": "high"}
            clipped_text = "A & <B>." + " " * 3992 + "outside"
            clipped = synth("clipped-document", {"text": clipped_text, "spans": [
                {"start": 4000, "end": 4007, "rate": "slow"}]}, text=clipped_text)
            assert "outside" not in clipped["ssml_document"] and "<prosody" not in clipped["ssml_document"], clipped
            if args.positive_control:
                positive = selection(args.positive_control)
                synth("positive-plain", voice=positive)
                synth("positive-pause", {"text": text, "spans": [{"start": 0, "end": 28, "after_ms": 700}]}, voice=positive)
                assert measurements[-1]["seconds"] - measurements[-2]["seconds"] >= 0.4, measurements[-2:]
            with open(os.path.join(directory, "measurements.json"), "w", encoding="utf-8") as output:
                json.dump({"measurements": measurements, "events": observed,
                           "claim": "WAV synthesis and duration only; no listening-quality or capability promotion."},
                          output, ensure_ascii=False, indent=2)
            print("Native file-output artifacts:", directory)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            reader.join(5)
            proc.stdout.close()
    return 0


if "--native-output" in sys.argv:
    sys.exit(native_file_output())

import desktop_ts  # noqa: E402 - the optional native proof does not need the TypeScript test harness

if not desktop_ts.node or not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules")
    sys.exit(77)

RUN = r"""
const assert = require('node:assert/strict');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { PassThrough, Writable } = require('node:stream');
const childProcess = require('node:child_process');
const nativeSetTimeout = global.setTimeout;
const nativeClearTimeout = global.clearTimeout;
const timers = new Map();
let clock = 0;
performance.now = () => clock;
global.setTimeout = (fn, delay) => {
  const timer = { fn, at: clock + delay, unref() { return this; } };
  timers.set(timer, timer);
  return timer;
};
global.clearTimeout = timer => timers.delete(timer);
function advance(ms) {
  clock += ms;
  for (const timer of [...timers.values()]) {
    if (timer.at <= clock && timers.delete(timer)) timer.fn();
  }
}
const turn = () => new Promise(resolve => setImmediate(resolve));
const watched = promise => {
  const result = { state: 'pending' };
  result.promise = promise.then(value => { result.state = 'done'; result.value = value; },
    error => { result.state = 'failed'; result.error = error; });
  return result;
};
async function settled(result) {
  let timer;
  try {
    await Promise.race([result.promise, new Promise((_, reject) => {
      timer = nativeSetTimeout(() => reject(new Error('WindowsTts left a promise unresolved')), 1000);
    })]);
  } finally { nativeClearTimeout(timer); }
  return result;
}
async function failed(result, message) {
  await settled(result);
  assert.equal(result.state, 'failed');
  assert.match(result.error.message, message);
}

class Worker extends EventEmitter {
  constructor() {
    super();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.spoken = [];
    this.queue = [];
    this.wordBoundary = true;
    this.active = null;
    this.catalogue = [{ id: 'Installed voice', name: 'Installed voice', identity: 'token:installed', isDefault: true }];
    this.listing = [];
    this.killed = false;
    this.warmups = [];
    this.preparations = [];
    let input = '';
    this.stdin = new Writable({ write: (chunk, encoding, callback) => {
      input += chunk.toString();
      let newline;
      while ((newline = input.indexOf('\n')) >= 0) {
        const command = JSON.parse(input.slice(0, newline));
        input = input.slice(newline + 1);
        this.accept(command);
      }
      callback();
    } });
  }
  emitMessage(message) { this.stdout.write(JSON.stringify(message) + '\r\n'); }
  ready() { this.emitMessage({ event: 'ready' }); }
  accept(command) {
    if (command.op === 'prepare') { this.preparations.push(command); return; }
    if (command.op === 'voices') { this.listing.push(command); return; }
    if (command.op === 'speak') { this.queue.push(command); this.play(); return; }
    if (command.op === 'warmup') { this.warmups.push(command); this.queue.push(command); this.play(); return; }
    if (command.op === 'cancel') {
      for (const item of [this.active, ...this.queue].filter(Boolean)) {
        if (command.target === undefined || command.target === item.id) {
          if (this.active === item) this.active = null;
          this.queue = this.queue.filter(queued => queued !== item);
          this.emitMessage({ id: item.id, event: 'cancelled' });
        }
      }
      this.emitMessage({ id: command.id, event: 'ok' });
      this.play();
    }
  }
  play() {
    if (this.active || !this.queue.length) return;
    this.active = this.queue.shift();
    if (this.active.op !== 'warmup') {
      this.spoken.push(this.active.text);
      this.emitMessage({ id: this.active.id, event: 'started', voice: 'Actual installed voice', word_boundary: this.wordBoundary });
    }
  }
  finish(fields = {}) {
    assert(this.active, 'test worker has no active speech');
    const done = this.active;
    this.active = null;
    this.emitMessage({ id: done.id, event: done.op === 'warmup' ? 'warmed' : 'done', voice: 'Actual installed voice', ...fields });
    this.play();
  }
  list() {
    for (const request of this.listing.splice(0)) {
      this.emitMessage({ id: request.id, event: 'voices', voices: this.catalogue });
    }
  }
  kill() { this.killed = true; this.active = null; this.queue = []; return true; }
}
const workers = [];
childProcess.spawn = () => { const worker = new Worker(); workers.push(worker); return worker; };
const { WindowsTts } = require(path.join(process.argv[2], 'windows-tts.js'));
const settings = { voiceName: 'Installed voice', rate: 2, volume: 0.45 };

(async () => {
  // Cancelling during initialization must prevent playback, without discarding another turn or a voice lookup.
  const notes = [];
  const host = new WindowsTts('silent-worker.ps1', line => notes.push(line));
  const statusEvents = [];
  const offStatus = host.onStatusChange(ev => statusEvents.push(ev));
  assert.equal(workers.length, 0, 'constructing the host must not start a process');
  const cancelled = watched(host.speak('guide', 'must never be spoken', settings));
  const coach = watched(host.speak('coach', 'coach survives', settings));
  const catalogue = watched(host.voices());
  const worker = workers.at(-1);
  host.stop('guide');
  await failed(cancelled, /cancel/i);
  advance(35);
  worker.ready();
  worker.list();
  await turn();
  assert.deepEqual(worker.spoken, ['coach survives'], 'a cancelled startup request spoke after ready');
  assert.equal(coach.state, 'pending', 'started is not playback completion');
  await settled(catalogue);
  advance(120);
  worker.finish({ first_audio_ago_ms: 100, last_audio_ago_ms: 20 });
  await settled(coach);
  assert.deepEqual(coach.value, { voice: 'Actual installed voice', completionMs: 155,
    firstAudioMs: 55, lastAudioMs: 135 });
  assert(notes.some(line => line.includes('Actual installed voice')));
  const coachStatus = statusEvents.filter(ev => ev.id === 'coach');
  assert.deepEqual(coachStatus.map(ev => [ev.type, ev.elapsedMs ?? null]), [
    ['submitted', null],
    ['started', 35],
    ['done', 155],
  ], 'first-word latency and full native completion time must remain distinct');
  offStatus();
  const refreshed = watched(host.voices());
  worker.catalogue = [{ id: 'Newly installed', name: 'Newly installed', identity: 'token:new', isDefault: true }];
  worker.list();
  await settled(refreshed);
  assert.deepEqual(refreshed.value, worker.catalogue, 'voice catalogues must be refreshed');
  assert.equal(workers.length, 1, 'requests share the persistent worker');

  // Targeted cancellation must not cancel the active/queued unrelated request or its list operation.
  const active = watched(host.speak('active', 'active survives', settings));
  const removed = watched(host.speak('removed', 'queued must never be spoken', settings));
  const tail = watched(host.speak('tail', 'tail survives', settings));
  const lookup = watched(host.voices());
  const removedId = worker.queue.find(item => item.text === 'queued must never be spoken').id;
  host.stop('removed');
  await failed(removed, /cancel/i);
  worker.emitMessage({ id: removedId, event: 'done', voice: 'Cancelled voice',
    first_audio_ago_ms: 0, last_audio_ago_ms: 0 });
  advance(80);
  worker.finish({ first_audio_ago_ms: 65, last_audio_ago_ms: 15 });
  await settled(active);
  assert.deepEqual(active.value, { voice: 'Actual installed voice', completionMs: 80,
    firstAudioMs: 15, lastAudioMs: 65 });
  assert.equal(tail.state, 'pending');
  advance(40);
  worker.finish({ first_audio_ago_ms: 25, last_audio_ago_ms: 5 });
  await settled(tail);
  assert.deepEqual(tail.value, { voice: 'Actual installed voice', completionMs: 120,
    firstAudioMs: 95, lastAudioMs: 115 }, 'queued audio observations belong to their own submission clock');
  worker.list();
  await settled(lookup);
  assert.deepEqual(worker.spoken, ['coach survives', 'active survives', 'tail survives']);

  // Stop-all cancels both pending speaks, not voice enumeration, and later speech still works.
  const one = watched(host.speak('one', 'stop active', settings));
  const two = watched(host.speak('two', 'stop queued', settings));
  const duringStop = watched(host.voices());
  host.stop();
  await failed(one, /cancel/i);
  await failed(two, /cancel/i);
  assert.equal(worker.active, null);
  assert(!worker.spoken.includes('stop queued'));
  worker.list();
  await settled(duringStop);
  const afterStop = watched(host.speak('after-stop', 'later speech', settings));
  worker.finish();
  await settled(afterStop);
  host.dispose();
  assert(worker.killed, 'dispose must terminate its own worker');

  // Some engines complete without reporting a word: retain completion, never fabricate first-word latency.
  const noWordHost = new WindowsTts('silent-worker.ps1');
  const noWordEvents = [];
  noWordHost.onStatusChange(event => noWordEvents.push(event));
  const noWord = watched(noWordHost.speak('no-word', 'Completion without word events', settings));
  const noWordWorker = workers.at(-1);
  noWordWorker.wordBoundary = false;
  noWordWorker.ready();
  advance(75);
  noWordWorker.finish({ first_audio_ago_ms: 75, last_audio_ago_ms: 0 });
  await settled(noWord);
  assert.deepEqual(noWord.value, { voice: 'Actual installed voice', completionMs: 75,
    firstAudioMs: 0, lastAudioMs: 75 }, 'zero endpoints are available observations, not missing data');
  assert.deepEqual(noWordEvents.map(event => event.type), ['submitted', 'done']);
  noWordHost.dispose();

  // Worker ages are retrospective audio-delivery observations, independent of word events.
  const timingHost = new WindowsTts('silent-worker.ps1');
  for (const [id, duration, fields, expected] of [
    ['rounded', 240, { first_audio_ago_ms: 177.6, last_audio_ago_ms: 18.2 },
      { completionMs: 240, firstAudioMs: 62, lastAudioMs: 222 }],
    ['simultaneous', 100, { first_audio_ago_ms: 0, last_audio_ago_ms: 0 },
      { completionMs: 100, firstAudioMs: 100, lastAudioMs: 100 }],
    ['zero-duration', 0, { first_audio_ago_ms: 0, last_audio_ago_ms: 0 },
      { completionMs: 0, firstAudioMs: 0, lastAudioMs: 0 }],
  ]) {
    const speech = watched(timingHost.speak(id, 'timed speech', settings));
    const timingWorker = workers.at(-1);
    timingWorker.ready();
    advance(duration);
    timingWorker.finish(fields);
    await settled(speech);
    assert.deepEqual(speech.value, { voice: 'Actual installed voice', ...expected }, id);
  }
  const timingWorker = workers.at(-1);
  for (const [id, fields] of [
    ['unavailable', {}],
    ['first-only', { first_audio_ago_ms: 80 }],
    ['last-only', { last_audio_ago_ms: 10 }],
    ['null-first', { first_audio_ago_ms: null, last_audio_ago_ms: 10 }],
    ['null-last', { first_audio_ago_ms: 80, last_audio_ago_ms: null }],
    ['string-first', { first_audio_ago_ms: '80', last_audio_ago_ms: 10 }],
    ['string-last', { first_audio_ago_ms: 80, last_audio_ago_ms: '10' }],
    ['boolean-first', { first_audio_ago_ms: true, last_audio_ago_ms: 0 }],
    ['boolean-last', { first_audio_ago_ms: 80, last_audio_ago_ms: false }],
    ['object-first', { first_audio_ago_ms: {}, last_audio_ago_ms: 10 }],
    ['array-last', { first_audio_ago_ms: 80, last_audio_ago_ms: [] }],
    ['negative-first', { first_audio_ago_ms: -1, last_audio_ago_ms: 0 }],
    ['negative-last', { first_audio_ago_ms: 80, last_audio_ago_ms: -1 }],
    ['reversed', { first_audio_ago_ms: 10, last_audio_ago_ms: 80 }],
    ['before-submission', { first_audio_ago_ms: 101, last_audio_ago_ms: 10 }],
    ['both-outside', { first_audio_ago_ms: 120, last_audio_ago_ms: 110 }],
  ]) {
    const speech = watched(timingHost.speak(id, 'successful without audio telemetry', settings));
    advance(100);
    timingWorker.finish(fields);
    await settled(speech);
    assert.deepEqual(speech.value, { voice: 'Actual installed voice', completionMs: 100 },
      `${id}: invalid or unavailable telemetry must omit the whole pair without failing speech`);
  }
  // JSON numeric overflow reaches the consumer as nonfinite numbers (JSON.stringify would erase them).
  for (const ages of [
    '"first_audio_ago_ms":1e400,"last_audio_ago_ms":10',
    '"first_audio_ago_ms":80,"last_audio_ago_ms":1e400',
    '"first_audio_ago_ms":-1e400,"last_audio_ago_ms":0',
  ]) {
    const speech = watched(timingHost.speak('nonfinite', 'successful with invalid numeric ages', settings));
    advance(100);
    timingWorker.stdout.write(`{"event":"done","id":${timingWorker.active.id},"voice":"Actual installed voice",${ages}}\r\n`);
    timingWorker.finish();
    await settled(speech);
    assert.deepEqual(speech.value, { voice: 'Actual installed voice', completionMs: 100 },
      'nonfinite ages must not escape or fail successful speech');
  }
  timingHost.dispose();

  // The enqueue boundary freezes controls, features and the actual token together, even before ready.
  const frozen = new WindowsTts('silent-worker.ps1');
  const selected = { voiceName: 'token:installed', rate: 1, volume: 0.7 };
  const permitted = ['rate', 'pitch'];
  const markup = { text: 'A 😀 & <B>.', spans: [{ start: 2, end: 3, rate: 'slow', pitch: 'high' }] };
  const frozenLine = watched(frozen.speak('frozen', markup.text, selected, markup, permitted));
  const frozenWorker = workers.at(-1);
  selected.voiceName = 'token:changed';
  markup.spans[0].start = 0;
  permitted.splice(0);
  frozenWorker.ready();
  assert.equal(frozenWorker.active.voice, 'token:installed');
  assert.equal(frozenWorker.active.text, 'A 😀 & <B>.', 'literal content stays separate from controls');
  assert.deepEqual(frozenWorker.active.features, ['rate', 'pitch']);
  assert.deepEqual(frozenWorker.active.markup.spans, [{ start: 2, end: 3, rate: 'slow', pitch: 'high' }]);

  // Manual preparation is a silent request, not speech or cancellation. Failures leave active work intact.
  const beforePrepare = frozenWorker.active;
  const rejectedSample = watched(frozen.prepare('invalid manual sample', ['pitch']));
  const rejection = frozenWorker.preparations.at(-1);
  frozenWorker.emitMessage({ id: rejection.id, event: 'error', error: 'Disallowed pitch' });
  await failed(rejectedSample, /Disallowed pitch/);
  assert.equal(frozenWorker.active, beforePrepare);
  assert.deepEqual(frozenWorker.spoken, ['A 😀 & <B>.']);
  const preparedSample = watched(frozen.prepare('validated sample', ['rate']));
  const preparation = frozenWorker.preparations.at(-1);
  frozenWorker.emitMessage({ id: preparation.id, event: 'prepared',
    markup: { text: 'Words & more.', spans: [{ start: 0, end: 5, rate: 'slow' }] } });
  await settled(preparedSample);
  assert.equal(preparedSample.value.text, 'Words & more.');
  assert.equal(frozenWorker.active, beforePrepare);
  const actualDocument = '<speak version="1.0" xml:lang="en-US" xmlns="http://www.w3.org/2001/10/synthesis">A <prosody rate="slow" pitch="high">😀</prosody> &amp; &lt;B&gt;.</speak>';
  advance(90);
  frozenWorker.finish({ ssml_document: actualDocument, first_audio_ago_ms: 70, last_audio_ago_ms: 5 });
  await settled(frozenLine);
  assert.equal(frozenLine.value.ssmlDocument, actualDocument,
    'completion exposes the exact worker document, not a renderer serialization');
  assert.deepEqual([frozenLine.value.firstAudioMs, frozenLine.value.lastAudioMs], [20, 85],
    'audio timing and the exact SSML document must coexist on the same completion');
  const noDocument = watched(frozen.speak('no-document', markup.text, selected, markup, ['rate', 'pitch']));
  frozenWorker.finish();
  await settled(noDocument);
  assert.equal(noDocument.value.ssmlDocument, undefined, 'typed controls cannot invent a completion document');
  for (const invalidDocument of [null, 42, {}, '', 'x'.repeat(65537)]) {
    const invalid = watched(frozen.speak('invalid-document', 'words', settings));
    frozenWorker.finish({ ssml_document: invalidDocument });
    await failed(invalid, /invalid completed SSML document/i);
  }
  const failedMarkup = watched(frozen.speak('failed-document', 'words', settings));
  const failedId = frozenWorker.active.id;
  frozenWorker.emitMessage({ id: failedId, event: 'error', error: 'native failure', ssml_document: actualDocument });
  frozenWorker.finish({ ssml_document: actualDocument });
  await failed(failedMarkup, /native failure/);
  const stoppedMarkup = watched(frozen.speak('stopped-document', 'words', settings));
  const stoppedId = frozenWorker.active.id;
  frozen.stop('stopped-document');
  frozenWorker.emitMessage({ id: stoppedId, event: 'done', voice: 'Stale voice', ssml_document: actualDocument,
    first_audio_ago_ms: 0, last_audio_ago_ms: 0 });
  await failed(stoppedMarkup, /cancel/i);
  const abandonedPreparation = watched(frozen.prepare('never returned', ['rate']));
  advance(60000);
  await failed(abandonedPreparation, /timed out/);
  frozen.dispose();

  // Losing a running process rejects all outstanding work; only a later call starts a replacement.
  const recovering = new WindowsTts('silent-worker.ps1');
  const interrupted = watched(recovering.speak('lost', 'do not retry this text', settings));
  const lostList = watched(recovering.voices());
  const old = workers.at(-1);
  old.ready();
  const lostId = old.active.id;
  old.emit('error', new Error('simulated process loss'));
  const count = workers.length;
  await failed(interrupted, /process|worker|simulated/i);
  await failed(lostList, /process|worker|simulated/i);
  assert.equal(workers.length, count, 'failed speech must not be retried automatically');
  const recovered = watched(recovering.speak('new', 'replacement only', settings));
  const replacement = workers.at(-1);
  assert.notEqual(replacement, old);
  old.emit('exit', 1, null);
  old.emitMessage({ event: 'ready' });
  old.emitMessage({ id: lostId, event: 'done', voice: 'Stale voice', ssml_document: '<speak>stale</speak>',
    first_audio_ago_ms: 0, last_audio_ago_ms: 0 });
  replacement.ready();
  assert.deepEqual(replacement.spoken, ['replacement only']);
  replacement.finish();
  await settled(recovered);
  assert.equal(recovered.value.ssmlDocument, undefined, 'a stale worker cannot supply the document');
  assert.equal(recovered.value.firstAudioMs, undefined, 'a stale worker cannot supply first audio timing');
  assert.equal(recovered.value.lastAudioMs, undefined, 'a stale worker cannot supply last audio timing');
  recovering.dispose();

  // Process exit while still starting must reject both operations, even without an 'error' event.
  const starting = new WindowsTts('silent-worker.ps1');
  const startupSpeech = watched(starting.speak('start', 'cannot start', settings));
  const startupList = watched(starting.voices());
  workers.at(-1).emit('exit', 3, null);
  await failed(startupSpeech, /exit|worker/i);
  await failed(startupList, /exit|worker/i);
  starting.dispose();

  // Startup and enumeration cannot hang; speech itself may outlast either deadline.
  const bounded = new WindowsTts('silent-worker.ps1');
  const neverReady = watched(bounded.voices());
  advance(60000);
  await failed(neverReady, /ready|start|timed out/i);
  const longSpeech = watched(bounded.speak('long', 'long real playback', settings));
  const slowList = watched(bounded.voices());
  const timedWorker = workers.at(-1);
  timedWorker.ready();
  advance(60000);
  await failed(slowList, /voice|timed out/i);
  await turn();
  assert.equal(longSpeech.state, 'pending', 'voice-list deadlines must not end real playback');
  timedWorker.finish();
  await settled(longSpeech);
  const disposedSpeech = watched(bounded.speak('dispose', 'shutdown active', settings));
  const disposedList = watched(bounded.voices());
  bounded.dispose();
  await failed(disposedSpeech, /dispos|shut/i);
  await failed(disposedList, /dispos|shut/i);
  const before = workers.length;
  await assert.rejects(bounded.voices(), /dispos/i);
  await assert.rejects(bounded.speak('forbidden', 'must not start', settings), /dispos/i);
  assert.equal(workers.length, before);

  // Stop-all before readiness also discards every queued turn while keeping lookup alive.
  const early = new WindowsTts('silent-worker.ps1');
  const earlyOne = watched(early.speak('early-one', 'never one', settings));
  const earlyTwo = watched(early.speak('early-two', 'never two', settings));
  const earlyList = watched(early.voices());
  const earlyWorker = workers.at(-1);
  early.stop();
  await failed(earlyOne, /cancel/i);
  await failed(earlyTwo, /cancel/i);
  earlyWorker.ready();
  earlyWorker.list();
  await settled(earlyList);
  assert.deepEqual(earlyWorker.spoken, []);
  early.dispose();

  // Background warmup: disabled configure never spawns; enabled configure warms once without status events,
  // and an unsent warmup is dropped if real speech arrives before worker readiness.
  const warmNotes = [], warmStatuses = [], workerChanges = [];
  const warmer = new WindowsTts('silent-worker.ps1', line => warmNotes.push(line));
  warmer.onStatusChange(ev => warmStatuses.push(ev));
  warmer.onWorkerChange(() => workerChanges.push(warmer.workerState()));
  const countBeforeWarm = workers.length;
  warmer.configure(false, 'Xenia (Russian)');
  assert.equal(workers.length, countBeforeWarm, 'disabled configure must not spawn a worker');
  warmer.configure(true, 'Xenia (Russian)');
  assert.equal(workers.length, countBeforeWarm + 1);
  assert.equal(warmer.workerState(), 'starting');
  // Initial BrowserWindow loadFile fires did-start-navigation -> windowsTts.stop() before PowerShell is ready;
  // speech teardown must preserve the pending background warmup, whereas configure(false) cancels it.
  warmer.stop();
  const warmWorker = workers.at(-1);
  advance(40);
  warmWorker.ready();
  assert.equal(warmer.workerState(), 'ready');
  await turn();
  assert.equal(warmWorker.warmups.length, 1, 'stop() during window navigation must not discard startup warmup');
  assert.equal(warmWorker.warmups[0].voice, 'Xenia (Russian)');
  advance(260);
  warmWorker.finish();
  await turn();
  assert.equal(warmStatuses.length, 0, 'background warmup must not emit playback status transitions');
  assert(warmNotes.some(line => line.includes('SAPI5 warmup ready') && line.includes('elapsed_ms=300')));
  warmer.configure(true, 'Xenia (Russian)');
  await turn();
  assert.equal(warmWorker.warmups.length, 1, 'already-warmed voice must not re-run warmup');
  assert.deepEqual(workerChanges.slice(0, 2), ['starting', 'ready']);
  warmer.dispose();

  const preempted = new WindowsTts('silent-worker.ps1');
  preempted.configure(true, 'Xenia (Russian)');
  const preemptPromise = watched(preempted.speak('first-line', 'real greeting', settings));
  const preemptWorker = workers.at(-1);
  preemptWorker.ready();
  await turn();
  assert.equal(preemptWorker.warmups.length, 0, 'real speech before ready must drop unsent warmup');
  assert.deepEqual(preemptWorker.spoken, ['real greeting']);
  preemptWorker.finish();
  await settled(preemptPromise);
  preempted.dispose();
  console.log('OK: WindowsTts cancellation, completion, restart and deadline transitions');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""

with tempfile.TemporaryDirectory(prefix="glide-windows-tts-") as tmp:
    script = os.path.join(tmp, "check.cjs")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    result = subprocess.run([desktop_ts.node, script, os.path.join(desktop_ts.main(), "main")],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    print(result.stdout + result.stderr)
    assert result.returncode == 0, "WindowsTts lifecycle checks failed"
