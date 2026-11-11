#!/usr/bin/env python3
"""WindowsTts's consumer lifecycle with a silent, controlled child-process boundary.

No SAPI installation, audio device, application settings or network access is used. The worker's
playback history catches late speaking; bounded promise checks catch startup/process-loss hangs.
"""
import os
import subprocess
import sys
import tempfile

import desktop_ts

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
    this.active = null;
    this.catalogue = [{ id: 'Installed voice', name: 'Installed voice' }];
    this.listing = [];
    this.killed = false;
    this.warmups = [];
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
      this.emitMessage({ id: this.active.id, event: 'started', voice: 'Actual installed voice' });
    }
  }
  finish() {
    assert(this.active, 'test worker has no active speech');
    const done = this.active;
    this.active = null;
    this.emitMessage({ id: done.id, event: done.op === 'warmup' ? 'warmed' : 'done', voice: 'Actual installed voice' });
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
  worker.finish();
  await settled(coach);
  assert.deepEqual(coach.value, { voice: 'Actual installed voice' });
  assert(notes.some(line => line.includes('Actual installed voice')));
  const coachStatus = statusEvents.filter(ev => ev.id === 'coach');
  assert.deepEqual(coachStatus.map(ev => [ev.type, ev.elapsedMs ?? null]), [
    ['submitted', null],
    ['started', 35],
    ['done', 155],
  ], 'onStatusChange must emit started synthesis latency distinct from done playback duration');
  offStatus();
  const refreshed = watched(host.voices());
  worker.catalogue = [{ id: 'Newly installed', name: 'Newly installed' }];
  worker.list();
  await settled(refreshed);
  assert.deepEqual(refreshed.value, worker.catalogue, 'voice catalogues must be refreshed');
  assert.equal(workers.length, 1, 'requests share the persistent worker');

  // Targeted cancellation must not cancel the active/queued unrelated request or its list operation.
  const active = watched(host.speak('active', 'active survives', settings));
  const removed = watched(host.speak('removed', 'queued must never be spoken', settings));
  const tail = watched(host.speak('tail', 'tail survives', settings));
  const lookup = watched(host.voices());
  host.stop('removed');
  await failed(removed, /cancel/i);
  worker.finish();
  await settled(active);
  assert.equal(tail.state, 'pending');
  worker.finish();
  await settled(tail);
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

  // Losing a running process rejects all outstanding work; only a later call starts a replacement.
  const recovering = new WindowsTts('silent-worker.ps1');
  const interrupted = watched(recovering.speak('lost', 'do not retry this text', settings));
  const lostList = watched(recovering.voices());
  const old = workers.at(-1);
  old.ready();
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
  replacement.ready();
  assert.deepEqual(replacement.spoken, ['replacement only']);
  replacement.finish();
  await settled(recovered);
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
