#!/usr/bin/env python3
"""The narrator (desktop/src/renderer/narrator.ts), compiled with the project's TypeScript beside stand-ins for the
bridge and the page, and run under Node with native speech IPC and Audio stand-ins whose lines end on a virtual clock.

Coach and Guides lines play in order. Interrupt, mute and tag cancellation release waiting callers immediately;
late synthesis or cancellation replies cannot disturb a newer line. Online synthesis is prefetched, and failed
online requests use native Windows speech. No real audio, application settings or network access is used.

Needs node and desktop/node_modules (skips with 77 without them)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(HERE, "..", "desktop")
SRC = os.path.join(DESK, "src", "renderer", "narrator.ts")

node = desktop_ts.node
if not node or not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules (cd desktop && npm ci)")
    sys.exit(77)

STUBS = {
    "bridge.ts": """
export const glide: { invoke(ch: string, ...a: unknown[]): Promise<any>;
  on(ch: string, fn: (p: any) => void): () => void } = (window as any).glide;
export const call = (ch: string, ...a: unknown[]) => glide.invoke(ch, ...a);
export const ipcError = (e: unknown) => String(e);
""",
    "dom.ts": "export const toast = (..._a: unknown[]) => undefined;\n",
    "types.ts": """
export type { LogAttribution } from "../shared/ipc.js";
export type Obj = any;
export interface SpeechSettings { provider?: string; voiceName?: string; rate?: number; volume?: number }
""",
}

# the stand-ins and the scenarios; every line takes DUR ms to say
RUN = r"""
// virtual time: the timers fire in the order they fall due, each as soon as nothing else is left to run, so the
// lines' DUR and the waits take no real time and the measured times are exact (a loaded machine can't stretch them).
// The clock starts at the real time (the narrator compares Date.now() with 0 for its once-a-minute notes).
let now = Date.now(), seq = 0, pumping = false;
const timers = [];
const pump = () => {
  pumping = false;
  if (!timers.length) return;
  timers.sort((a, b) => a.at - b.at || a.seq - b.seq);
  const t = timers.shift();
  now = Math.max(now, t.at);
  t.fn(...t.args);
  schedule();
};
const realImmediate = setImmediate;
const schedule = () => { if (!pumping && timers.length) { pumping = true; realImmediate(pump); } };
globalThis.setTimeout = (fn, ms = 0, ...args) => {
  const t = { at: now + Math.max(0, Number(ms) || 0), seq: seq++, fn, args };
  timers.push(t); schedule(); return t;
};
globalThis.clearTimeout = (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); };
Date.now = () => now;

const DUR = 80;
const log = [], playback = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null };
globalThis.crypto ||= (await import("node:crypto")).webcrypto;
const native = new Map(), lateNativeRejections = [];
let deferNativeCancellation = false;
let onNativeStarted;
function nativeSpeak(id, text) {
  if (text === "native-failure") return Promise.reject(new Error("Native speech failed before start"));
  return new Promise((resolve, reject) => {
    log.push("start:" + text);
    const startTimer = setTimeout(() => {
      onNativeStarted?.(id);
    }, 12);
    const timer = setTimeout(() => {
      native.delete(id); log.push("end:" + text); resolve({ voice: "Installed native voice" });
    }, DUR);
    native.set(id, { text, startTimer, timer, reject });
  });
}
function nativeStop(id) {
  for (const [key, turn] of native) {
    if (id !== undefined && id !== key) continue;
    native.delete(key); clearTimeout(turn.startTimer); clearTimeout(turn.timer); log.push("cancel:" + turn.text);
    const reject = () => turn.reject(new Error("speech cancelled"));
    if (deferNativeCancellation) lateNativeRejections.push(reject); else reject();
  }
  return Promise.resolve();
}
const dec = new TextDecoder();
globalThis.Blob = class { constructor(parts) { this.text = dec.decode(parts[0]); } };
const urls = new Map();
let nurl = 0;
URL.createObjectURL = (b) => { const u = "blob:" + (++nurl); urls.set(u, b.text); return u; };
URL.revokeObjectURL = () => {};
globalThis.Audio = class {
  constructor(url) { this.text = urls.get(url); this.paused = false; }
  play() {
    if (this.text === "blocked-audio") return Promise.reject(new Error("Playback blocked"));
    log.push("play:" + this.text);
    setTimeout(() => { if (!this.paused) { log.push("ended:" + this.text); if (this.onended) this.onended(); } }, DUR);
    return Promise.resolve();
  }
  pause() { if (this.paused) return; this.paused = true; log.push("pause:" + this.text); if (this.onpause) this.onpause(); }
};
let releaseSlowSynthesis;
const synthesisAttributions = new Map(), synthesisRequests = new Map(), deferredSyntheses = new Map();
const edgeAttributions = new Map();
const edgeListeners = new Set();
const edgeEvent = (event) => { for (const fn of [...edgeListeners]) fn(event); };
const edgeCandidates = new Map();
globalThis.OfflineAudioContext = class {
  decodeAudioData() { return Promise.resolve({ duration: 1, length: 44100, numberOfChannels: 1 }); }
};
globalThis.glide = { invoke: (ch, ...a) => {
  if (ch === "tts:edgeStart") {
    const [id, text] = a, attribution = { category: "TTS", provider: "edge", request_id: a[0],
      voice: "en-US-AriaNeural", ...edgeAttributions.get(a[1]) };
    log.push("edge:" + text);
    edgeCandidates.set(id, text);
    return sleep(10).then(() => {
      edgeEvent({ type: "plan", id, fragments: [{ index: 0, start: 0, end: text.length }] });
      edgeEvent({ type: "candidate", id, index: 0, attempt: id, start: 0, end: text.length,
        mime: "audio/mpeg", data: new TextEncoder().encode(text), attribution });
    });
  }
  if (ch === "tts:edgeDecoded") {
    const [id, attempt, ok] = a;
    if (ok && edgeCandidates.has(id)) {
      edgeEvent({ type: "accepted", id, index: 0, attempt });
      edgeEvent({ type: "end", id, reason: "complete" });
    }
    return Promise.resolve();
  }
  if (ch === "tts:edgeCancel") { edgeCandidates.delete(a[0]); return Promise.resolve(); }
  if (ch === "tts:windowsSpeak") {
    nativeOpts.push({ text: a[1], rate: a[2]?.rate, volume: a[2]?.volume });
    nativeVoices.set(a[1], a[2]?.voiceName);
    return nativeSpeak(a[0], a[1]);
  }
  if (ch === "tts:windowsStop") return nativeStop(a[0]);
  if (ch === "tts:playbackState") { playback.push(a[0]); return Promise.resolve(); }
  if (ch !== "tts:synth") return Promise.resolve();
  const text = a[0], requestId = a[3];
  synthesisRequests.set(text, requestId);
  log.push("synth:" + text);
  const attribution = { category: "TTS", elapsed_ms: text === "o2" ? 2424 : 10,
    ...(synthesisAttributions.get(text) ?? { provider: "openai", model: "gpt-4o-mini-tts", voice: "coral" }),
    request_id: requestId };
  const audio = () => ({ data: new TextEncoder().encode(text), mime: "audio/mpeg", attribution });
  if (text === "identity-pending") return new Promise(resolve => { deferredSyntheses.set(text, () => resolve(audio())); });
  if (text === "slow-guide") return new Promise(resolve => { releaseSlowSynthesis = () => resolve(audio()); });
  if (text === "identity-fallback") return sleep(10).then(() => { throw new Error("Online synthesis failed"); });
  if (text === "cloud-failure") return Promise.reject(new Error("TTS #77: service unavailable"));
  return sleep(10).then(audio);
}, on: (_ch, fn) => { edgeListeners.add(fn); return () => edgeListeners.delete(fn); } };

const { speech, voiceLastLine } = await import(process.argv[2]);
onNativeStarted = (id) => speech.onTtsStatus({
  provider: "windows", voiceLabel: "SAPI5 · Installed", fullVoice: "Windows SAPI5 (Installed native voice)",
  phase: "speaking", requestId: id, actualVoice: "Installed native voice", synthMs: 12, updatedAt: Date.now(),
});
const WIN = { provider: "windows", voiceName: "Microsoft Zira" }, ONLINE = { provider: "openai" };
const out = {};
const nativeOpts = [];
const nativeVoices = new Map();
const take = (name) => { out[name] = log.splice(0); out[name + "_pb"] = playback.splice(0); };
const mark = (p, what) => p.then(() => log.push(what));
const lastState = () => ({ voice: speech.lastVoice, at: speech.lastVoiceAt, text: speech.lastText, ms: speech.lastSynthMs });

// the coach, Windows voice: in order; an interrupting line cancels; muted says nothing
speech.configure(false, WIN);
speech.say("a"); speech.say("b");
await sleep(DUR * 3); take("coach_win");
out.win_last_line = voiceLastLine();
speech.say("a"); await sleep(10); speech.say("c", true);
await sleep(DUR * 2); take("coach_interrupt");
speech.configure(true, WIN);
speech.say("x");
let t = Date.now(); await speech.speakAndWait("muted line", "g"); out.muted_ms = Date.now() - t;
out.muted_can = speech.canSpeak();
await sleep(20); take("coach_muted");
speech.configure(false, WIN);
out.can = speech.canSpeak();
t = Date.now(); await speech.speakAndWait("", "g"); out.empty_ms = Date.now() - t;

// a tagged line waits for the coach's line, then resolves at its own end
speech.say("coach1");
t = Date.now();
await mark(speech.speakAndWait("guide1", "g"), "resolved:guide1"); out.tagged_ms = Date.now() - t;
take("tagged_win");

// cancel(tag) while it speaks, with a coach line queued behind it: the coach line is still said
let p = mark(speech.speakAndWait("guide2", "g"), "resolved:guide2");
const q2 = mark(speech.speakAndWait("guide3", "g"), "resolved:guide3");
await sleep(10); speech.say("coach2");
await sleep(20); t = Date.now(); speech.cancel("g"); await p; await q2; out.cancel_ms = Date.now() - t;
await sleep(DUR * 2); take("cancel_win");
// another tag's cancel leaves the coach alone
speech.say("coach3"); await sleep(10); speech.cancel("g");
await sleep(DUR * 2); take("cancel_other_win");

// A delayed cancellation response must not advance or clear the newer native turn.
deferNativeCancellation = true;
p = mark(speech.speakAndWait("late-guide", "g"), "resolved:late-guide");
speech.say("new-coach1"); speech.say("new-coach2");
await sleep(5); speech.cancel("g"); await p; await sleep(5);
lateNativeRejections.splice(0).forEach(reject => reject());
deferNativeCancellation = false;
await sleep(DUR * 3); take("late_native_cancel");

// the coach, online voice: every line fetched at once, played in order
speech.configure(false, ONLINE);
speech.say("o1"); speech.say("o2");
await sleep(DUR * 3); take("coach_online");
out.online_last_line = voiceLastLine();
out.online_state = lastState();
// Cancelling while synthesis is pending must let another line play before the stale response arrives.
p = mark(speech.speakAndWait("slow-guide", "g"), "resolved:slow-guide");
speech.say("fast-coach1"); speech.say("fast-coach2");
await sleep(2); t = Date.now(); speech.cancel("g"); await p; out.slow_cancel_ms = Date.now() - t;
await sleep(20);
out.before_stale_synthesis = lastState();
releaseSlowSynthesis();
await sleep(1);
out.after_stale_synthesis = lastState();
await sleep(DUR * 3); take("late_synthesis");
await speech.speakAndWait("cloud-failure", "g"); take("fallback");
out.fallback_state = lastState();
// A rejected audio player must not replace the last voice/text that really played.
out.before_rejected = lastState();
await speech.speakAndWait("blocked-audio", "g"); take("rejected_online");
out.after_rejected = lastState();
// tagged online lines: cancel stops the one playing, drops the queued one, the coach's next line plays
speech.say("o3");
p = mark(speech.speakAndWait("og1", "g"), "resolved:og1");
const q = mark(speech.speakAndWait("og2", "g"), "resolved:og2");
speech.say("o4");
await sleep(DUR + DUR / 2); speech.cancel("g"); await p; await q;
await sleep(DUR * 2); take("cancel_online");
// stop() (the coach's interrupt, mute) lets a waiting page go on
p = mark(speech.speakAndWait("og5", "g"), "resolved:og5");
await sleep(30); speech.stop(); await p; take("stop_online");
speech.configure(false, WIN);
p = mark(speech.speakAndWait("guide6", "g"), "resolved:guide6");
await sleep(20); speech.configure(true, WIN); await p; await sleep(10); take("mute_win");
// Edge uses the same queue/tag ownership, while each queued request starts its streaming prefetch immediately.
speech.configure(false, { provider: "edge" });
speech.say("e1"); speech.say("e2");
await sleep(DUR * 3); take("edge_order");
out.edge_last_line = voiceLastLine();
p = mark(speech.speakAndWait("edge-guide", "g"), "resolved:edge-guide");
speech.say("edge-coach");
await sleep(20); speech.cancel("g"); await p;
await sleep(DUR * 3); take("edge_cancel");
// Per-utterance preview overrides must not leak into subsequent coach callouts.
speech.configure(false, { provider: "windows", voiceName: "Microsoft Zira", rate: 1.15, volume: 0.75 });
nativeOpts.length = 0;
speech.say("sample-draft", true, "", "", { rate: 1.85, volume: 0 });
await sleep(DUR + 15);
speech.say("normal-callout");
await sleep(DUR + 15);
out.preview_opts = nativeOpts.slice();

// Full identities come from each actual request, not compact status labels or current selection.
out.identities = [];
for (const [provider, model, voice] of [
  ["openai", "gpt-4o-mini-tts", "coral"],
  ["openai", "tts-1-hd", "nova"],
  ["google", undefined, "en-US-Chirp3-HD-Charon"],
  ["google", undefined, "ru-RU-Chirp3-HD-Achernar"],
  ["elevenlabs", "eleven_flash_v2_5", "JBFqnCBsd6RMkjVDRZzb"],
  ["elevenlabs", "eleven_v3", "custom-eleven-voice"],
  ["azure", undefined, "en-US-AndrewMultilingualNeural"],
  ["azure", undefined, "ru-RU-SvetlanaNeural"],
]) {
  const text = `identity-${provider}-${out.identities.length}`;
  synthesisAttributions.set(text, { provider, model, voice, elapsed_ms: 1234 });
  speech.configure(false, { provider, openai: { model: "not-used", voice: "not-used" },
    google: { voice: "not-used" }, elevenlabs: { model: "not-used", voiceId: "not-used" },
    azure: { voice: "not-used" } });
  await speech.speakAndWait(text, "identity");
  const reports = playback.splice(0);
  out.identities.push({ provider, model, voice, actual: lastState(), label: voiceLastLine(),
    requestId: synthesisRequests.get(text), reports });
  log.length = 0;
}

// A settings change while synthesis is outstanding cannot relabel the eventual audio.
synthesisAttributions.set("identity-pending", { provider: "openai", model: "tts-1", voice: "onyx", elapsed_ms: 3210 });
speech.configure(false, { provider: "openai", openai: { model: "tts-1", voice: "onyx" } });
out.before_pending = lastState();
p = speech.speakAndWait("identity-pending", "identity");
await sleep(2);
speech.configure(false, { provider: "google", google: { voice: "ru-RU-Chirp3-HD-Fenrir" } });
out.while_pending = lastState();
deferredSyntheses.get("identity-pending")();
await p;
out.pending_state = lastState();
out.pending_last_line = voiceLastLine();
take("identity_pending");

// Prefetch can finish behind another line; only playback may publish its captured identity.
synthesisAttributions.set("identity-blocker", { provider: "openai", model: "tts-1-hd", voice: "alloy", elapsed_ms: 4321 });
synthesisAttributions.set("identity-queued", { provider: "google", voice: "ru-RU-Chirp3-HD-Zephyr", elapsed_ms: 654 });
speech.configure(false, { provider: "openai" });
speech.say("identity-blocker");
speech.configure(false, { provider: "google" });
p = speech.speakAndWait("identity-queued", "identity");
speech.configure(false, { provider: "elevenlabs", elevenlabs: { voiceId: "new-selection" } });
await sleep(20);
out.while_queued = lastState();
await p;
out.queued_state = lastState();
out.queued_last_line = voiceLastLine();
take("identity_queued");

// Edge fragment metadata likewise wins over the full queued settings snapshot.
edgeAttributions.set("identity-edge", { provider: "edge", voice: "ru-RU-DmitryNeural" });
speech.configure(false, { provider: "edge", edge: { voice: "en-US-AriaNeural" } });
p = speech.speakAndWait("identity-edge", "identity");
speech.configure(false, { provider: "edge", edge: { voice: "en-US-JennyNeural" } });
await p;
out.edge_identity = lastState();
out.edge_identity_label = voiceLastLine();
take("identity_edge");

// A late native status from an unrelated/cancelled request must not replace online attribution.
out.before_stale_native = lastState();
speech.onTtsStatus({ provider: "windows", voiceLabel: "stale", fullVoice: "stale", phase: "speaking",
  requestId: "cancelled-native-request", actualVoice: "Stale Windows voice", synthMs: 9999, updatedAt: Date.now() });
out.after_stale_native = lastState();

// Native fallback uses the per-line Windows choice, but reports the worker's actual selected voice.
speech.configure(false, { provider: "openai", voiceName: "Fallback requested before settings changed" });
p = speech.speakAndWait("identity-fallback", "identity");
await sleep(2);
speech.configure(false, { provider: "windows", voiceName: "New Windows selection" });
await p;
out.fallback_snapshot = { ...lastState(), requested: nativeVoices.get("identity-fallback") };
take("identity_fallback");
out.before_native_failure = lastState();
await speech.speakAndWait("native-failure", "identity");
out.after_native_failure = lastState();
console.log(JSON.stringify(out));
"""

tmp = tempfile.mkdtemp(prefix="glide-narrator-")
try:
    with open(SRC, encoding="utf-8") as fh:
        files = {"renderer/narrator.ts": fh.read(), **{f"renderer/{name}": data for name, data in STUBS.items()}}
    with open(os.path.join(DESK, "src", "renderer", "edge-speech.ts"), encoding="utf-8") as fh:
        files["renderer/edge-speech.ts"] = fh.read()
    for name in ("ipc.ts", "engine.ts"):
        with open(os.path.join(DESK, "src", "shared", name), encoding="utf-8") as fh:
            files[f"shared/{name}"] = fh.read()
    # narrator.ts beside its stand-ins, compiled once per content (the tests' shared compiles, desktop_ts.py)
    out = desktop_ts.compiled("narrator", files, ["renderer/narrator.ts", "--target", "ES2023", "--module", "ES2022",
                                                  "--moduleResolution", "bundler", "--lib", "ES2023,DOM", "--strict",
                                                  "--skipLibCheck"])
    run = os.path.join(tmp, "run.mjs")
    with open(run, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    mod = desktop_ts.url(os.path.join(out, "renderer", "narrator.js"))
    r = subprocess.run([node, run, mod], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, "running the narrator failed: %s" % r.stderr[-2000:]
    got = json.loads(r.stdout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# the coach's lines, as before
assert got["coach_win"] == ["start:a", "end:a", "start:b", "end:b"], got["coach_win"]
assert got["coach_interrupt"] == ["start:a", "cancel:a", "start:c", "end:c"], got["coach_interrupt"]
assert got["coach_muted"] == [] and got["muted_ms"] < 20 and got["muted_can"] is False and got["can"] is True, got
assert got["empty_ms"] < 20, got
assert got["coach_online"] == ["synth:o1", "synth:o2", "play:o1", "ended:o1", "play:o2", "ended:o2"], got["coach_online"]

# a tagged line waits its turn and resolves at its end
assert got["tagged_win"] == ["start:coach1", "end:coach1", "start:guide1", "end:guide1", "resolved:guide1"], got["tagged_win"]
assert got["tagged_ms"] >= 2 * 80 - 5, got["tagged_ms"]

# cancel: at once, the tagged lines dropped, the coach line said in full
c = got["cancel_win"]
assert c[:3] == ["start:guide2", "cancel:guide2", "start:coach2"], c
assert "end:guide2" not in c and "start:guide3" not in c and "end:coach2" in c, c
assert c.index("resolved:guide2") < c.index("end:coach2") and "resolved:guide3" in c, c
assert got["cancel_ms"] < 20, got["cancel_ms"]
assert got["cancel_other_win"] == ["start:coach3", "end:coach3"], got["cancel_other_win"]

c = got["cancel_online"]
c = [x for x in c if not x.startswith("synth:")]
assert c[:4] == ["play:o3", "ended:o3", "play:og1", "pause:og1"] and c[-2:] == ["play:o4", "ended:o4"], c
assert sorted(c[4:-2]) == ["resolved:og1", "resolved:og2"] and "play:og2" not in c, c
assert "resolved:og5" in got["stop_online"], got["stop_online"]
assert "resolved:guide6" in got["mute_win"] and "end:guide6" not in got["mute_win"], got["mute_win"]
c = got["late_native_cancel"]
assert [x for x in c if x.startswith(("start:", "end:"))] == [
    "start:late-guide", "start:new-coach1", "end:new-coach1", "start:new-coach2", "end:new-coach2"], c
c = got["late_synthesis"]
assert [x for x in c if x.startswith(("play:", "ended:"))] == [
    "play:fast-coach1", "ended:fast-coach1", "play:fast-coach2", "ended:fast-coach2"], c
assert got["slow_cancel_ms"] < 20 and "resolved:slow-guide" in c, c
assert got["fallback"] == ["synth:cloud-failure", "start:cloud-failure", "end:cloud-failure"], got["fallback"]
assert got["after_rejected"] == got["before_rejected"], got
assert got["edge_order"] == ["edge:e1", "edge:e2", "play:e1", "ended:e1", "play:e2", "ended:e2"], got["edge_order"]
c = got["edge_cancel"]
assert c[:4] == ["edge:edge-guide", "edge:edge-coach", "play:edge-guide", "pause:edge-guide"], c
assert c[-2:] == ["play:edge-coach", "ended:edge-coach"] and "resolved:edge-guide" in c, c
assert [p["state"] for p in got["coach_online_pb"]] == ["playing", "ended", "playing", "ended"], got["coach_online_pb"]
assert [p["state"] for p in got["fallback_pb"]] == ["fallback", "ended"], got["fallback_pb"]
assert [p["state"] for p in got["rejected_online_pb"]] == ["error"], got["rejected_online_pb"]
assert not any(p["state"] in ("fallback", "error") for p in got["late_synthesis_pb"]), got["late_synthesis_pb"]
assert "Installed native voice (SAPI5), generated in 12ms," in got["win_last_line"], got["win_last_line"]
assert "OpenAI gpt-4o-mini-tts, coral (online voice), generated in 2.42s," in got["online_last_line"], got["online_last_line"]
assert "generated in " in got["edge_last_line"] and "ms," in got["edge_last_line"], got["edge_last_line"]
assert got["preview_opts"] == [
    {"text": "sample-draft", "rate": 1.85, "volume": 0},
    {"text": "normal-callout", "rate": 1.15, "volume": 0.75},
], got["preview_opts"]
assert got["online_state"]["ms"] == 2424, got["online_state"]
assert got["before_stale_synthesis"] == got["after_stale_synthesis"], got
assert got["fallback_state"]["voice"] == "Installed native voice (SAPI5)", got["fallback_state"]
assert got["fallback_state"]["text"] == "cloud-failure" and got["fallback_state"]["ms"] == 12, got["fallback_state"]
provider_labels = {"openai": "OpenAI", "google": "Google", "elevenlabs": "ElevenLabs", "azure": "Azure"}
for case in got["identities"]:
    identity = ", ".join(filter(None, (case.get("model"), case["voice"])))
    expected = f'{provider_labels[case["provider"]]} {identity} (online voice)'
    assert case["actual"]["voice"] == expected, case
    assert case["actual"]["ms"] == 1234 and case["actual"]["at"] > 0, case
    assert f"{expected}, generated in 1.23s," in case["label"], case
    assert [r["state"] for r in case["reports"]] == ["playing", "ended"], case
    assert all(r["requestId"] == case["requestId"] for r in case["reports"]), case
assert got["before_pending"] == got["while_pending"], got
assert got["pending_state"]["voice"] == "OpenAI tts-1, onyx (online voice)", got["pending_state"]
assert got["pending_state"]["ms"] == 3210, got["pending_state"]
assert "generated in 3.21s," in got["pending_last_line"], got["pending_last_line"]
assert got["while_queued"]["voice"] == "OpenAI tts-1-hd, alloy (online voice)", got["while_queued"]
assert got["while_queued"]["text"] == "identity-blocker" and got["while_queued"]["ms"] == 4321, got["while_queued"]
assert got["queued_state"]["voice"] == "Google ru-RU-Chirp3-HD-Zephyr (online voice)", got["queued_state"]
assert got["queued_state"]["text"] == "identity-queued" and got["queued_state"]["ms"] == 654, got["queued_state"]
assert got["queued_state"]["at"] > got["while_queued"]["at"], got
assert "generated in 654ms," in got["queued_last_line"], got["queued_last_line"]
assert got["edge_identity"]["voice"] == "Edge-TTS ru-RU-DmitryNeural (online voice)", got["edge_identity"]
assert got["edge_identity"]["text"] == "identity-edge" and got["edge_identity"]["ms"] >= 0, got["edge_identity"]
assert got["before_stale_native"] == got["after_stale_native"], got
assert got["fallback_snapshot"]["voice"] == "Installed native voice (SAPI5)", got["fallback_snapshot"]
assert got["fallback_snapshot"]["text"] == "identity-fallback" and got["fallback_snapshot"]["ms"] == 12, got["fallback_snapshot"]
assert got["fallback_snapshot"]["requested"] == "Fallback requested before settings changed", got["fallback_snapshot"]
assert got["before_native_failure"] == got["after_native_failure"], got
print("OK")
