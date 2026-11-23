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
export const glide: { invoke(ch: string, ...a: unknown[]): Promise<any> } = (window as any).glide;
export const call = (ch: string, ...a: unknown[]) => glide.invoke(ch, ...a);
export const ipcError = (e: unknown) => String(e);
""",
    "dom.ts": "export const toast = (..._a: unknown[]) => undefined;\n",
    "types.ts": """
export type { LogAttribution } from "./ipc.js";
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
const log = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null };
globalThis.crypto ||= (await import("node:crypto")).webcrypto;
const native = new Map(), lateNativeRejections = [];
let deferNativeCancellation = false;
function nativeSpeak(id, text) {
  return new Promise((resolve, reject) => {
    log.push("start:" + text);
    const timer = setTimeout(() => {
      native.delete(id); log.push("end:" + text); resolve({ voice: "Installed native voice" });
    }, DUR);
    native.set(id, { text, timer, reject });
  });
}
function nativeStop(id) {
  for (const [key, turn] of native) {
    if (id !== undefined && id !== key) continue;
    native.delete(key); clearTimeout(turn.timer); log.push("cancel:" + turn.text);
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
globalThis.glide = { invoke: (ch, ...a) => {
  if (ch === "tts:windowsSpeak") return nativeSpeak(a[0], a[1]);
  if (ch === "tts:windowsStop") return nativeStop(a[0]);
  if (ch !== "tts:synth") return Promise.resolve();
  const text = a[0];
  log.push("synth:" + text);
  const audio = () => ({ data: new TextEncoder().encode(text), mime: "audio/mpeg" });
  if (text === "slow-guide") return new Promise(resolve => { releaseSlowSynthesis = () => resolve(audio()); });
  if (text === "cloud-failure") return Promise.reject(new Error("TTS #77: service unavailable"));
  return sleep(10).then(audio);
} };

const { speech } = await import(process.argv[2]);
const WIN = { provider: "windows", voiceName: "Microsoft Zira" }, ONLINE = { provider: "openai" };
const out = {};
const take = (name) => { out[name] = log.splice(0); };
const mark = (p, what) => p.then(() => log.push(what));

// the coach, Windows voice: in order; an interrupting line cancels; muted says nothing
speech.configure(false, WIN);
speech.say("a"); speech.say("b");
await sleep(DUR * 3); take("coach_win");
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
// Cancelling while synthesis is pending must let another line play before the stale response arrives.
p = mark(speech.speakAndWait("slow-guide", "g"), "resolved:slow-guide");
speech.say("fast-coach1"); speech.say("fast-coach2");
await sleep(2); t = Date.now(); speech.cancel("g"); await p; out.slow_cancel_ms = Date.now() - t;
await sleep(20); releaseSlowSynthesis();
await sleep(DUR * 3); take("late_synthesis");
await speech.speakAndWait("cloud-failure", "g"); take("fallback");
// A rejected audio player must not replace the last voice/text that really played.
out.before_rejected = [speech.lastVoice, speech.lastVoiceAt, speech.lastText];
await speech.speakAndWait("blocked-audio", "g"); take("rejected_online");
out.after_rejected = [speech.lastVoice, speech.lastVoiceAt, speech.lastText];
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
console.log(JSON.stringify(out));
"""

tmp = tempfile.mkdtemp(prefix="glide-narrator-")
try:
    with open(SRC, encoding="utf-8") as fh:
        files = {"narrator.ts": fh.read(), **STUBS}
    for name in ("ipc.ts", "engine.ts"):
        with open(os.path.join(DESK, "src", "shared", name), encoding="utf-8") as fh:
            files[name] = fh.read()
    # narrator.ts beside its stand-ins, compiled once per content (the tests' shared compiles, desktop_ts.py)
    out = desktop_ts.compiled("narrator", files, ["narrator.ts", "--target", "ES2023", "--module", "ES2022",
                                                  "--moduleResolution", "bundler", "--lib", "ES2023,DOM", "--strict",
                                                  "--skipLibCheck"])
    run = os.path.join(tmp, "run.mjs")
    with open(run, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    mod = desktop_ts.url(os.path.join(out, "narrator.js"))
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
print("OK")
