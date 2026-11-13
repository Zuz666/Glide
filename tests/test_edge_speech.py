#!/usr/bin/env python3
"""Real narrator and Edge adapter at the external IPC, Chromium decoder and audio boundaries.

No application settings, provider network, actual sound or internal module stand-ins. The provider controls
complete candidates/commits, and the player controls when an audible fragment finishes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(HERE, "..", "desktop")
if not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules (cd desktop && npm ci)")
    sys.exit(77)

STUBS = {
    "renderer/bridge.ts": """
export const glide: { invoke(ch: string, ...a: unknown[]): Promise<any>;
  on(ch: string, fn: (p: any) => void): () => void } = (window as any).glide;
export const call = (ch: string, ...a: unknown[]) => glide.invoke(ch, ...a);
export const ipcError = (e: unknown) => String(e);
""",
    "renderer/dom.ts": 'export { esc } from "./real-dom.js";\nexport const toast = (...a: unknown[]) => (globalThis as any).notices.push(a);\n',
}

RUN = r"""
import assert from 'node:assert/strict';
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null };
globalThis.crypto ||= (await import('node:crypto')).webcrypto;
globalThis.notices = [];
const tick = async () => { for (let i = 0; i < 4; ++i) await new Promise(setImmediate); };
const enc = new TextEncoder(), dec = new TextDecoder();
const listeners = new Set(), starts = [], verdicts = [], cancels = [], native = [], players = [], notes = [], playbackStates = [];
const decodeCalls = [], delayedDecode = new Map();
let startHook = () => Promise.resolve(), liveListeners = 0, maxListeners = 0;
const emit = e => { for (const fn of [...listeners]) fn(e); };
globalThis.OfflineAudioContext = class {
  decodeAudioData(data) {
    const text = dec.decode(data); decodeCalls.push(text);
    if (text === 'invalid') return Promise.reject(new Error('invalid MP3'));
    if (text.startsWith('delayed')) return new Promise(resolve => delayedDecode.set(text, resolve));
    return Promise.resolve({ duration: 1, length: 44100, numberOfChannels: 1 });
  }
};
const urls = new Map(); let urlId = 0;
URL.createObjectURL = blob => { const url = 'blob:' + ++urlId; urls.set(url, blob); return url; };
URL.revokeObjectURL = url => urls.delete(url);
let blockPlayback = false;
globalThis.Audio = class {
  constructor(url) { this.blob = urls.get(url); this.paused = false; players.push(this); }
  async play() {
    this.text = await this.blob.text();
    if (blockPlayback) return new Promise(resolve => { this.release = resolve; });
  }
  end() { this.ended = true; this.onpause?.(); this.onended?.(); }
  fail() { this.error = { code: 3 }; this.onerror?.(); }
  pause() { this.paused = true; this.onpause?.(); }
};
globalThis.glide = {
  on(ch, fn) { assert.equal(ch, 'tts:edgeEvent'); listeners.add(fn); maxListeners = Math.max(maxListeners, ++liveListeners);
    return () => { listeners.delete(fn); --liveListeners; }; },
  invoke(ch, ...a) {
    if (ch === 'tts:edgeStart') { starts.push({ id: a[0], text: a[1] }); return startHook(a[0], a[1]); }
    if (ch === 'tts:edgeDecoded') { verdicts.push(a); return Promise.resolve(); }
    if (ch === 'tts:edgeCancel') { cancels.push(a[0]); return Promise.resolve(); }
    if (ch === 'tts:windowsSpeak') { native.push({ id: a[0], text: a[1], settings: a[2] }); return Promise.resolve({ voice: 'Native' }); }
    if (ch === 'app:note') notes.push(a);
    if (ch === 'tts:playbackState') { playbackStates.push(a[0]); return Promise.resolve(); }
    assert.notEqual(ch, 'tts:synth', 'Edge must not use legacy full-text synthesis');
    return Promise.resolve();
  }
};
const { speech } = await import(process.argv[2]);
const settings = { provider: 'edge', edge: { voice: 'en-US-TestNeural' }, voiceName: 'Windows fallback', rate: 1.4, volume: .35 };
speech.configure(false, settings);
const plan = (id, ranges) => emit({ type: 'plan', id, fragments: ranges.map(([start, end], index) => ({ index, start, end })) });
const candidate = (id, index, attempt, start, end, audio) => emit({ type: 'candidate', id, index, attempt, start, end,
  mime: 'audio/mpeg', data: enc.encode(audio), attribution: { provider: 'edge', request_id: id } });
const accepted = (id, index, attempt) => emit({ type: 'accepted', id, index, attempt });
const end = (id, reason) => emit({ type: 'end', id, reason });

// Listener and state exist before start's acknowledgement; both complete copies decode eagerly. A later
// fragment may be accepted first, but only the first ordered accepted fragment can start playback.
let releaseStart;
startHook = id => { plan(id, [[0, 7], [7, 15], [15, 21]]); return new Promise(r => { releaseStart = r; }); };
let done = false;
let waiting = speech.speakAndWait('First. Second. Third.', 'guide').then(() => { done = true; });
let id = starts.at(-1).id;
candidate(id, 1, 'second', 7, 15, 'Second. ');
candidate(id, 0, 'first-a', 0, 7, 'First. ');
candidate(id, 0, 'first-b', 0, 7, 'First. ');
await tick();
assert.deepEqual(decodeCalls, ['Second. ', 'First. ', 'First. ']);
assert.equal(players.length, 0, 'decode verdict is not the worker acceptance commit');
assert.equal(verdicts.length, 3); assert.ok(verdicts.every(v => v[2]));
accepted(id, 1, 'second'); await tick(); assert.equal(players.length, 0);
accepted(id, 0, 'first-a'); await tick();
assert.equal(players.at(-1).text, 'First. '); assert.equal(done, false);
assert.equal(speech.lastText, 'First. '); assert.equal(players.at(-1).playbackRate, 1.4);
assert.equal(players.at(-1).volume, .35); assert.equal(players.at(-1).preservesPitch, true);
assert.equal(speech.lastVoice, 'Edge-TTS en-US-TestNeural (online voice)');
// Terminal failure drops the already-buffered second fragment, finishes only the audible first one,
// then speaks the exact original first-unstarted suffix, once, with captured Windows settings.
end(id, 'failed'); await tick(); assert.equal(native.length, 0); assert.equal(players.length, 1);
players.at(-1).end(); await waiting;
assert.deepEqual(native.map(n => n.text), ['Second. Third.']);
assert.deepEqual(native[0].settings, { voiceName: 'Windows fallback', rate: 1.4, volume: .35 });
assert.equal(native[0].id, id); assert.equal(speech.lastText, 'Second. Third.');
releaseStart(); await tick(); assert.equal(players.length, 1); assert.equal(liveListeners, 0);
const statesFor = reqId => playbackStates.filter(p => p.requestId === reqId);
assert.deepEqual(statesFor(id), [
  { requestId: id, state: 'playing' },
  { requestId: id, state: 'fallback', fallbackVoice: 'Windows fallback' },
  { requestId: id, state: 'ended' },
], 'partial Edge playback then fallback reports playing -> fallback -> ended');

// Failed Chromium decode is acknowledged as rejection, never playback. A replacement accepted complete
// file starts without waiting for later text, and a complete end retains the remaining ready fragments.
startHook = () => Promise.resolve();
waiting = speech.speakAndWait('One. Two.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 5], [5, 9]]); candidate(id, 0, 'bad', 0, 5, 'invalid'); await tick();
assert.deepEqual(verdicts.at(-1), [id, 'bad', false]);
candidate(id, 0, 'good', 0, 5, 'One. '); await tick(); accepted(id, 0, 'good'); await tick();
assert.equal(players.at(-1).text, 'One. ');
candidate(id, 1, 'two', 5, 9, 'Two.'); await tick(); accepted(id, 1, 'two'); end(id, 'complete');
players.at(-1).end(); await tick(); assert.equal(players.at(-1).text, 'Two.');
players.at(-1).end(); await waiting;
assert.equal(native.length, 1); assert.equal(urls.size, 0);
assert.equal(notes.filter(([, attribution]) => attribution.operation === 'audio playback'
  && attribution.request_id === id && attribution.status === 'done').length, 2,
  'Chromium pause-before-ended at natural completion is not a cancellation');
assert.deepEqual(statesFor(id), [
  { requestId: id, state: 'playing' },
  { requestId: id, state: 'ended' },
], 'multi-fragment Edge streaming must not flicker between fragments');

// Cancellation resolves the tagged caller immediately, preserves an unrelated queued line, and ignores
// delayed decoder/start/event callbacks. Queued Edge lines are also prefetched before their turn.
waiting = speech.speakAndWait('Cancelled.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 10]]); candidate(id, 0, 'late', 0, 10, 'delayed-cancelled');
speech.say('Survivor.'); const survivor = starts.at(-1).id;
assert.notEqual(id, survivor);
plan(survivor, [[0, 9]]); candidate(survivor, 0, 'survivor', 0, 9, 'Survivor.'); await tick();
accepted(survivor, 0, 'survivor'); end(survivor, 'complete');
const verdictCount = verdicts.length;
speech.cancel('guide'); await waiting; await tick();
assert.equal(players.at(-1).text, 'Survivor.'); assert.ok(cancels.includes(id));
delayedDecode.get('delayed-cancelled')({ duration: 1, length: 1, numberOfChannels: 1 });
accepted(id, 0, 'late'); end(id, 'failed'); await tick(); assert.equal(verdicts.length, verdictCount);
players.at(-1).end(); await tick(); assert.equal(native.length, 1);

// Deadline before any playback uses the whole original text; event-before-ack failure and a late start
// rejection cannot create a second fallback or interfere with a newer request.
startHook = id => { end(id, 'deadline'); return Promise.reject(new Error('late start failure')); };
await speech.speakAndWait('Whole original 😀.', 'sample');
assert.equal(native.at(-1).text, 'Whole original 😀.'); assert.equal(native.length, 2);
assert.deepEqual(statesFor(starts.at(-1).id), [
  { requestId: starts.at(-1).id, state: 'fallback', fallbackVoice: 'Windows fallback' },
  { requestId: starts.at(-1).id, state: 'ended' },
], 'deadline before audio reports fallback -> ended');
startHook = () => Promise.resolve();

// Source positions are JavaScript UTF-16 offsets, not code-point or encoded-byte counts.
waiting = speech.speakAndWait('😀. Next.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 4], [4, 9]]); candidate(id, 0, 'unicode', 0, 4, '😀. '); await tick();
accepted(id, 0, 'unicode'); await tick(); assert.equal(speech.lastText, '😀. ');
end(id, 'failed'); players.at(-1).end(); await waiting; assert.equal(native.at(-1).text, 'Next.');

// A failure while Audio.play is still pending must not count that file as already audible.
blockPlayback = true;
waiting = speech.speakAndWait('Not started.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 12]]); candidate(id, 0, 'blocked', 0, 12, 'Not started.'); await tick();
accepted(id, 0, 'blocked'); await tick(); const blocked = players.at(-1);
end(id, 'deadline'); await waiting; assert.equal(blocked.paused, true);
assert.equal(native.at(-1).text, 'Not started.'); const lastText = speech.lastText;
blocked.release(); await tick(); assert.equal(speech.lastText, lastText); blockPlayback = false;

// Media failure after audible playback cannot safely repeat partial words or continue buffered speech.
waiting = speech.speakAndWait('Audible. Unsaid.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 9], [9, 16]]); candidate(id, 0, 'audible', 0, 9, 'Audible. '); await tick();
accepted(id, 0, 'audible'); await tick(); const beforeMediaFail = native.length;
players.at(-1).fail(); await waiting; assert.equal(native.length, beforeMediaFail); assert.ok(cancels.includes(id));
assert.deepEqual(statesFor(id), [
  { requestId: id, state: 'playing' },
  { requestId: id, state: 'error' },
], 'audible media failure without fallback reports playing -> error');

// A cancellation from the owner stops even the currently audible fragment without invoking fallback.
waiting = speech.speakAndWait('Owner cancelled.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 16]]); candidate(id, 0, 'owner-cancel', 0, 16, 'Owner cancelled.'); await tick();
accepted(id, 0, 'owner-cancel'); await tick(); const cancelledPlayer = players.at(-1);
end(id, 'cancelled'); await waiting; assert.equal(cancelledPlayer.paused, true);
assert.equal(native.length, beforeMediaFail);

// Urgent speech, push-to-talk and mute keep the same queue/cancellation ownership; no cancellation fallback.
waiting = speech.speakAndWait('Urgent old.', 'guide'); const old = starts.at(-1).id;
speech.say('Urgent new.', true); await waiting; assert.ok(cancels.includes(old));
const urgent = starts.at(-1).id; speech.bargeIn(); await tick(); assert.ok(cancels.includes(urgent));
waiting = speech.speakAndWait('Muted.', 'guide'); const muted = starts.at(-1).id;
speech.configure(true, settings); await waiting; assert.ok(cancels.includes(muted));
assert.equal(native.length, beforeMediaFail); assert.equal(liveListeners, 0); assert.equal(maxListeners, 1);
for (const cancelledId of [old, urgent, muted]) {
  assert(!statesFor(cancelledId).some(s => s.state === 'fallback' || s.state === 'error'),
    'cancellation/barge-in/mute must never report fallback or error');
}

// Candidate identity and the local decoder verdict remain mandatory even with main-validated structure.
speech.configure(false, settings);
waiting = speech.speakAndWait('Decoded only.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 13]]);
const beforeIdentityPlayers = players.length, beforeIdentityDecodes = decodeCalls.length;
candidate(id, 0, 'identity', 0, 13, 'delayed-identity');
candidate(id, 0, 'identity', 0, 13, 'duplicate');
accepted(id, 0, 'identity'); await tick();
assert.equal(players.length, beforeIdentityPlayers, 'acceptance cannot play before the local decode finishes');
assert.equal(decodeCalls.length, beforeIdentityDecodes + 1, 'duplicate attempt cannot replace a decoding candidate');
delayedDecode.get('delayed-identity')({ duration: 1, length: 1, numberOfChannels: 1 }); await tick();
accepted(id, 1, 'identity'); await tick();
assert.equal(players.length, beforeIdentityPlayers, 'acceptance must name the decoded fragment');
accepted(id, 0, 'identity'); end(id, 'complete'); await tick();
assert.equal(players.length, beforeIdentityPlayers + 1);
players.at(-1).end(); await waiting;

// Main completion cannot substitute for renderer-local acceptance; a late decoder cannot revive fallback.
waiting = speech.speakAndWait('Pending.', 'guide'); id = starts.at(-1).id;
plan(id, [[0, 8]]); candidate(id, 0, 'pending', 0, 8, 'delayed-pending');
const beforePendingVerdicts = verdicts.length, beforePendingPlayers = players.length, beforePendingNative = native.length;
end(id, 'complete'); await waiting;
assert.equal(native.length, beforePendingNative + 1);
assert.equal(native.at(-1).text, 'Pending.');
delayedDecode.get('delayed-pending')({ duration: 1, length: 1, numberOfChannels: 1 });
accepted(id, 0, 'pending'); await tick();
assert.equal(verdicts.length, beforePendingVerdicts);
assert.equal(players.length, beforePendingPlayers);
assert.equal(liveListeners, 0);
console.log('OK');
"""

files = dict(STUBS)
with open(os.path.join(DESK, "src", "renderer", "dom.ts"), encoding="utf-8") as fh:
    files["renderer/real-dom.ts"] = fh.read()
for folder, names in (("renderer", ("narrator.ts", "edge-speech.ts", "date-time.ts", "state.ts", "types.ts", "speech-markup.ts")), ("shared", ("ipc.ts", "engine.ts", "date-time.ts", "speech-markup.ts"))):
    for name in names:
        with open(os.path.join(DESK, "src", folder, name), encoding="utf-8") as fh:
            files[f"{folder}/{name}"] = fh.read()
out = desktop_ts.compiled("edge-speech", files, ["renderer/narrator.ts", "--target", "ES2023", "--module", "ES2022",
    "--moduleResolution", "bundler", "--lib", "ES2023,DOM", "--strict", "--skipLibCheck"])
tmp = tempfile.mkdtemp(prefix="glide-edge-speech-")
try:
    run = os.path.join(tmp, "run.mjs")
    with open(run, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    result = subprocess.run([desktop_ts.node, run, desktop_ts.url(os.path.join(out, "renderer", "narrator.js"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr[-4000:]
    assert result.stdout.strip() == "OK", result.stdout
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("OK")
