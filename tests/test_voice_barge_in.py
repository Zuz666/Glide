#!/usr/bin/env python3
"""Talking over the coach: the Coach page (desktop/src/renderer/coach.ts) with the real narrator (narrator.ts), compiled
with the project's TypeScript beside stand-ins for the bridge and the page, and run under Node with a stand-in event
stream, native Windows speech IPC and Audio whose lines end on a virtual clock: no real audio or network calls.

Holding push-to-talk (the engine's "ptt" event, "listening", still held 150 ms later: a brush of the key isn't
talking) stops the line being said and drops the queued ones, with an online voice and a Windows voice alike, like
talking over someone on a call; the coach's next line (its answer) is said as usual, and "transcribing" and "idle"
stop nothing. A Guides page line waiting its turn, or being said, resolves its speakAndWait then (the page doesn't
hang; the Guides walk sees bargedSince and pauses on its step).

Needs node and desktop/node_modules (skips with 77 without them)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
RENDERER = os.path.join(HERE, "..", "desktop", "src", "renderer")
SHARED = os.path.join(HERE, "..", "desktop", "src", "shared")

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
    "dom.ts": """
export const toast = (..._a: unknown[]) => undefined;
const els: Record<string, any> = {};
(globalThis as any).els = els;
export function $<T = any>(id: string): T {
  return (els[id] ??= { id, hidden: true, className: "", textContent: "", title: "", disabled: false, style: {},
                        classList: { add() {}, remove() {} }, removeAttribute() {} }) as T;
}
""",
    "session-bar.ts": "export const sessionBar = (_s: unknown) => undefined;\n",
    "state.ts": "export const live: any = { engineState: \"\", engineToken: \"\", engineUrl: null };\n",
    "status.ts": "export const refreshStatus = () => undefined;\n",
    "types.ts": """
export type { LogAttribution } from "./ipc.js";
export type { LiteInitEvent, PttEvent, SpeakEvent, VoiceSpokenEvent } from "./engine.js";
export type Obj = any;
export interface SpeechSettings { provider?: string; voiceName?: string; rate?: number; volume?: number }
export interface EngineStatus { state: string; detail: string; port: number; url: string | null; token: string }
""",
}

# the stand-ins and the scenarios; every line takes DUR ms to say
RUN = r"""
// virtual time: the timers fire in the order they fall due, each as soon as nothing else is left to run
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

const DUR = 400;
const log = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null };
globalThis.crypto ||= (await import("node:crypto")).webcrypto;
const native = new Map();
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
    turn.reject(new Error("speech cancelled"));
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
    log.push("play:" + this.text);
    setTimeout(() => { if (!this.paused) { log.push("ended:" + this.text); if (this.onended) this.onended(); } }, DUR);
    return Promise.resolve();
  }
  pause() { if (this.paused) return; this.paused = true; log.push("pause:" + this.text); if (this.onpause) this.onpause(); }
};
globalThis.glide = {
  invoke: (ch, ...a) => {
    if (ch === "tts:windowsSpeak") return nativeSpeak(a[0], a[1]);
    if (ch === "tts:windowsStop") return nativeStop(a[0]);
    if (ch !== "tts:synth") return Promise.resolve();
    log.push("synth:" + a[0]);
    return sleep(10).then(() => ({ data: new TextEncoder().encode(a[0]), mime: "audio/mpeg" }));
  },
  on: () => () => {},
};
// the engine's event stream: the page's listeners, fired by hand
const streams = [];
globalThis.EventSource = class {
  constructor(url) { this.url = url; this.l = {}; streams.push(this); }
  addEventListener(name, fn) { this.l[name] = fn; }
  close() {}
};

const { onEngine } = await import(process.argv[2]);
const { speech } = await import(process.argv[3]);
onEngine({ state: "running", detail: "", port: 1, url: "http://engine", token: "t" });
const es = streams[streams.length - 1];
const fire = (name, data) => es.l[name]({ data: JSON.stringify(data) });
const WIN = { provider: "windows", voiceName: "Microsoft Zira" }, ONLINE = { provider: "openai" };
const out = { stream: es.url };
const take = (name) => { out[name] = log.splice(0); };
const mark = (p, what) => p.then(() => log.push(what));
let t, p;

// Online voice: the line being played stops and queued lines are dropped.
speech.configure(false, ONLINE);
fire("speak", { text: "o1", interrupt: false, style: "Gruff. This line: sly.", tone: "sly" });
fire("speak", { text: "o2", interrupt: false });
fire("speak", { text: "o3", interrupt: false, style: "Gruff." });
await sleep(30);
fire("ptt", { state: "listening" });
out.shown = { hidden: els["sb-ptt"].hidden, cls: els["sb-ptt"].className };
await sleep(DUR * 3); take("online");

// the answer that comes after is said; a line said while transcribing, or at idle, plays to its end
fire("ptt", { state: "transcribing" });
fire("speak", { text: "answer", interrupt: false });
await sleep(30);
fire("ptt", { state: "idle" });
await sleep(DUR * 2); take("after");

// a Guides page line waiting behind a coach line resolves at once, unsaid
fire("speak", { text: "o4", interrupt: false });
p = mark(speech.speakAndWait("guide1", "guides"), "resolved:guide1");
await sleep(30); t = Date.now(); fire("ptt", { state: "listening" }); await p; out.wait_ms = Date.now() - t;
await sleep(DUR * 2); take("tagged_online");
// and one being said stops and resolves
p = mark(speech.speakAndWait("guide2", "guides"), "resolved:guide2");
await sleep(30); t = Date.now(); fire("ptt", { state: "listening" }); await p; out.said_ms = Date.now() - t;
await sleep(DUR * 2); take("tagged_playing");

// Windows voice: the line being said and the ones queued behind it are cancelled, the waiting Guides line resolves
speech.configure(false, WIN);
fire("speak", { text: "a", interrupt: false, style: "Gruff. This line: sly.", tone: "sly" });
fire("speak", { text: "b", interrupt: false });
p = mark(speech.speakAndWait("guide3", "guides"), "resolved:guide3");
await sleep(20); t = Date.now(); fire("ptt", { state: "listening" }); await p; out.win_ms = Date.now() - t;
await sleep(DUR * 3); take("win");
fire("speak", { text: "c", interrupt: false });
await sleep(DUR * 2); take("win_after");

// a brush of the key (up before 150 ms) stops nothing; bargedSince says when the player did talk over it
speech.configure(false, ONLINE);
t = Date.now();
fire("speak", { text: "tap", interrupt: false });
await sleep(20); fire("ptt", { state: "listening" }); await sleep(50); fire("ptt", { state: "idle" });
await sleep(DUR * 2); take("tap");
out.barged = { before: speech.bargedSince(0), tap: speech.bargedSince(t) };

// A completed standalone turn is an observation, not another speech request.
const reportedAt = Date.now();
fire("voice_spoken", { voice: "Eugene (Russian)", text: "Путь свободен.", at: reportedAt });
out.standalone = [speech.lastVoice, speech.lastText, speech.lastVoiceAt];
take("standalone_audio");
// Reconnecting must not replace a newer record with an older retained completion.
fire("voice_spoken", { voice: "Old voice", text: "Old text", at: reportedAt - 1 });
out.after_old_status = [speech.lastVoice, speech.lastText, speech.lastVoiceAt];
speech.configure(false, WIN);
p = speech.speakAndWait("in-app sample", "sample");
await sleep(10);
fire("voice_spoken", { voice: "Eugene (Russian)", text: "Следующая цель.", at: Date.now() });
await p;
take("sample_during_status");
out.after_sample = speech.lastText;
console.log(JSON.stringify(out));
"""

tmp = tempfile.mkdtemp(prefix="glide-barge-in-")
try:
    files = dict(STUBS)
    for name, folder in (("coach.ts", RENDERER), ("narrator.ts", RENDERER), ("engine.ts", SHARED), ("ipc.ts", SHARED)):
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            files[name] = fh.read()
    out = desktop_ts.compiled("barge-in", files, ["coach.ts", "--target", "ES2023", "--module", "ES2022",
                                                  "--moduleResolution", "bundler", "--lib", "ES2023,DOM", "--strict",
                                                  "--skipLibCheck"])
    run = os.path.join(tmp, "run.mjs")
    with open(run, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    r = subprocess.run([node, run, desktop_ts.url(os.path.join(out, "coach.js")),
                        desktop_ts.url(os.path.join(out, "narrator.js"))],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, "running the coach page failed: %s" % r.stderr[-2000:]
    got = json.loads(r.stdout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

assert got["stream"] == "http://engine/events?lite=1", got["stream"]

# Online: o1 stops where it is, o2 and o3 never play; the session bar says Listening.
c = got["online"]
assert c[:3] == ["synth:o1", "synth:o2", "synth:o3"], c
assert c[3:] == ["play:o1", "pause:o1"], c
assert got["shown"] == {"hidden": False, "cls": "sb-ptt listening"}, got["shown"]
assert got["after"] == ["synth:answer", "play:answer", "ended:answer"], got["after"]

# the Guides page's lines resolve at once, waiting or being said
c = got["tagged_online"]
assert c[:3] == ["synth:o4", "synth:guide1", "play:o4"] and "pause:o4" in c and "resolved:guide1" in c, c
assert "play:guide1" not in c and 150 <= got["wait_ms"] <= 160, (c, got["wait_ms"])
c = got["tagged_playing"]
assert c == ["synth:guide2", "play:guide2", "pause:guide2", "resolved:guide2"] and 150 <= got["said_ms"] <= 160, (
    c, got["said_ms"])

# Windows: a is interrupted, queued coach and Guides lines never start, and the next answer plays normally.
c = got["win"]
assert c[:2] == ["start:a", "cancel:a"], c
assert "resolved:guide3" in c and not {"end:a", "start:b", "start:guide3"} & set(c), c
assert 150 <= got["win_ms"] <= 160, got["win_ms"]
assert got["win_after"] == ["start:c", "end:c"], got["win_after"]

# a brush of the key: the line plays to its end; the earlier presses count as talking over it, the tap doesn't
c = got["tap"]
assert c == ["synth:tap", "play:tap", "ended:tap"], c
assert got["barged"] == {"before": True, "tap": False}, got["barged"]

assert got["standalone"][:2] == ["Eugene (Russian) (SAPI5)", "Путь свободен."], got["standalone"]
assert got["standalone"][2] > 0 and got["standalone_audio"] == [], got
assert got["after_old_status"] == got["standalone"], got["after_old_status"]
assert got["sample_during_status"] == ["start:in-app sample", "end:in-app sample"], got["sample_during_status"]
assert got["after_sample"] == "in-app sample", got["after_sample"]
print("OK")
