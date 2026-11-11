// Persistent native SAPI5 playback. Speech stays on stdin, never in process arguments; app logs record each turn.
import { spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import { createInterface } from "readline";
import type { Interface } from "readline";
import type { LogAttribution, SpeechSettings, Voice } from "../shared/ipc";

const READY_TIMEOUT_MS = 15000;
const VOICES_TIMEOUT_MS = 15000;

type SpeechOptions = Pick<SpeechSettings, "voiceName" | "rate" | "volume">;
type Command = { id: number; op: "voices" }
  | { id: number; op: "warmup"; voice: string }
  | { id: number; op: "speak"; text: string; voice: string; rate: number; volume: number };
type Pending = {
  command: Command;
  sent: boolean;
  timer?: NodeJS.Timeout;
  reject: (error: Error) => void;
} & ({ kind: "voices"; resolve: (voices: Voice[]) => void }
  | { kind: "warmup"; submittedAt: number; voice: string; resolve: () => void }
  | { kind: "speak"; callerId: string; submittedAt: number; actualVoice?: string;
    resolve: (result: { voice: string }) => void });

interface Worker {
  child: ChildProcessWithoutNullStreams;
  lines: Interface;
  ready: boolean;
  closed: boolean;
  readyTimer?: NodeJS.Timeout;
  pending: Map<number, Pending>;
  warmedVoices: Set<string>;
}
/** Owns one lazy worker; a failed utterance is never replayed after a process restart. */
export class WindowsTts {
  private worker?: Worker;
  private nextId = 1;
  private disposed = false;
  private readonly statusListeners = new Set<(event: {
    type: "submitted" | "started" | "done" | "cancelled" | "error";
    id: string;
    voice?: string;
    elapsedMs?: number;
  }) => void>();
  private readonly workerListeners = new Set<() => void>();

  constructor(private readonly scriptPath: string,
    private readonly note?: (line: string, attribution?: LogAttribution) => void) {}

  onStatusChange(listener: (event: {
    type: "submitted" | "started" | "done" | "cancelled" | "error";
    id: string;
    voice?: string;
    elapsedMs?: number;
  }) => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  onWorkerChange(listener: () => void): () => void {
    this.workerListeners.add(listener);
    return () => this.workerListeners.delete(listener);
  }

  configure(enabled: boolean, voiceName = ""): void {
    if (this.disposed) return;
    const voice = String(voiceName || "");
    if (!enabled) {
      this.cancelWarmups();
      return;
    }
    let worker: Worker;
    try { worker = this.start(); } catch { return; }
    if (worker.warmedVoices.has(voice)) return;
    for (const active of worker.pending.values()) {
      if (active.kind === "speak") return;
      if (active.kind === "warmup" && active.voice === voice) return;
    }
    this.cancelWarmups();
    const command: Command = { id: this.nextId++, op: "warmup", voice };
    this.enqueue(worker, {
      kind: "warmup", command, submittedAt: performance.now(), voice, sent: false,
      resolve: () => undefined, reject: () => undefined,
    });
  }

  workerState(): "stopped" | "starting" | "ready" {
    if (!this.worker || this.worker.closed) return "stopped";
    return this.worker.ready ? "ready" : "starting";
  }
  voices(): Promise<Voice[]> {
    return new Promise((resolve, reject) => {
      const worker = this.start();
      const command: Command = { id: this.nextId++, op: "voices" };
      this.enqueue(worker, { kind: "voices", command, sent: false, resolve, reject });
    });
  }

  speak(id: string, text: string, settings: SpeechOptions): Promise<{ voice: string }> {
    return new Promise((resolve, reject) => {
      const submittedAt = performance.now();
      const multiplier = typeof settings.rate === "number" && Number.isFinite(settings.rate) && settings.rate > 0
        ? settings.rate : 1;
      const volume = typeof settings.volume === "number" && Number.isFinite(settings.volume) ? settings.volume : 1;
      const command: Command = { id: this.nextId++, op: "speak", text, voice: settings.voiceName || "",
        rate: Math.max(-10, Math.min(10, Math.round(10 * Math.log10(multiplier)))),
        volume: Math.max(0, Math.min(100, Math.round(volume * 100))) };
      const pending: Pending = { kind: "speak", callerId: id, command, submittedAt, sent: false, resolve, reject };
      this.noteSpeech(pending, "submitted");
      let worker: Worker;
      try { worker = this.start(); } catch (error) {
        this.noteSpeech(pending, "error", error instanceof Error ? error.message : "native worker failed");
        reject(error);
        return;
      }
      for (const [wid, wp] of worker.pending) {
        if (wp.kind === "warmup" && !wp.sent) this.remove(worker, wid);
      }
      for (const active of worker.pending.values()) {
        if (active.kind === "speak" && active.callerId === id) {
          const error = new Error("Windows TTS: speech request ID is already pending");
          this.noteSpeech(pending, "error", error.message);
          reject(error);
          return;
        }
      }
      this.enqueue(worker, pending);
    });
  }

  stop(id?: string): void {
    const worker = this.worker;
    if (!worker || worker.closed) return;
    const sent: number[] = [];
    for (const [requestId, pending] of worker.pending) {
      if (pending.kind !== "speak" || (id !== undefined && pending.callerId !== id)) continue;
      if (pending.sent) sent.push(requestId);
      // Remove before ready can flush the queue, and before synchronous stream failure callbacks can run.
      this.remove(worker, requestId);
      this.noteSpeech(pending, "cancelled");
      pending.reject(new Error("Windows TTS: speech cancelled"));
    }
    if (!sent.length || !worker.ready) return;
    if (id === undefined) {
      this.write(worker, { id: this.nextId++, op: "cancel" });
    } else {
      for (const target of sent) this.write(worker, { id: this.nextId++, op: "cancel", target });
    }
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.worker) this.fail(this.worker, new Error("Windows TTS: host disposed"));
  }

  private notifyWorker(): void {
    for (const fn of this.workerListeners) {
      try { fn(); } catch { /* status listeners must not interrupt speech */ }
    }
  }

  private cancelWarmups(): void {
    const worker = this.worker;
    if (!worker || worker.closed) return;
    const sent: number[] = [];
    for (const [requestId, pending] of worker.pending) {
      if (pending.kind !== "warmup") continue;
      if (pending.sent) sent.push(requestId);
      this.remove(worker, requestId);
    }
    if (sent.length && worker.ready) {
      for (const target of sent) this.write(worker, { id: this.nextId++, op: "cancel", target });
    }
  }

  private start(): Worker {
    if (this.disposed) throw new Error("Windows TTS: host disposed");
    if (this.worker) return this.worker;
    const child = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
      "-File", this.scriptPath], { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    const worker: Worker = { child, lines, ready: false, closed: false, pending: new Map(), warmedVoices: new Set() };
    this.worker = worker;
    this.notifyWorker();
    worker.readyTimer = setTimeout(() => {
      this.fail(worker, new Error("Windows TTS: worker startup timed out waiting for ready"));
    }, READY_TIMEOUT_MS);
    child.on("error", (error) => this.fail(worker, new Error(`Windows TTS: worker process error (${error.message})`)));
    child.on("exit", (code, signal) => {
      this.fail(worker, new Error(`Windows TTS: worker exited (${signal || code || "unknown"})`));
    });
    child.on("close", () => this.fail(worker, new Error("Windows TTS: worker process closed")));
    child.stdin.on("error", () => this.fail(worker, new Error("Windows TTS: worker input failed")));
    child.stdout.on("error", () => this.fail(worker, new Error("Windows TTS: worker output failed")));
    child.stderr.on("error", () => this.fail(worker, new Error("Windows TTS: worker diagnostic stream failed")));
    // Drain diagnostics without retaining native tracebacks, which can contain spoken text.
    child.stderr.resume();
    lines.on("line", (line) => this.receive(worker, line));
    lines.on("close", () => this.fail(worker, new Error("Windows TTS: worker output closed")));
    lines.on("error", () => this.fail(worker, new Error("Windows TTS: worker output failed")));
    return worker;
  }

  private enqueue(worker: Worker, pending: Pending): void {
    worker.pending.set(pending.command.id, pending);
    if (worker.ready) this.send(worker, pending);
  }

  private send(worker: Worker, pending: Pending): void {
    if (worker.closed || !worker.pending.has(pending.command.id)) return;
    pending.sent = true;
    if (pending.kind === "voices") {
      pending.timer = setTimeout(() => {
        if (worker.pending.get(pending.command.id) !== pending) return;
        this.remove(worker, pending.command.id);
        pending.reject(new Error("Windows TTS: loading voices timed out"));
      }, VOICES_TIMEOUT_MS);
    }
    this.write(worker, pending.command);
  }

  private write(worker: Worker, command: Command | { id: number; op: "cancel"; target?: number }): void {
    if (worker.closed) return;
    try {
      worker.child.stdin.write(`${JSON.stringify(command)}\n`, "utf8", (error) => {
        if (error) this.fail(worker, new Error("Windows TTS: could not send request to worker"));
      });
    } catch {
      this.fail(worker, new Error("Windows TTS: could not send request to worker"));
    }
  }

  private receive(worker: Worker, line: string): void {
    if (worker.closed || this.worker !== worker || !line.trim()) return;
    let message: { event?: string; id?: number; voice?: string; voices?: Voice[]; error?: string };
    try {
      message = JSON.parse(line.replace(/^\uFEFF/, ""));
      if (!message || typeof message !== "object") throw new Error("Invalid response");
    } catch {
      this.fail(worker, new Error("Windows TTS: invalid worker response"));
      return;
    }
    if (message.event === "fatal") {
      this.fail(worker, new Error(`Windows TTS: ${typeof message.error === "string" ? message.error : "native worker failed"}`));
      return;
    }
    if (message.event === "ready") {
      if (worker.ready) return;
      worker.ready = true;
      clearTimeout(worker.readyTimer);
      worker.readyTimer = undefined;
      this.notifyWorker();
      for (const pending of worker.pending.values()) this.send(worker, pending);
      return;
    }
    if (typeof message.id !== "number") return;
    const pending = worker.pending.get(message.id);
    if (!pending || !pending.sent) return;
    if (message.event === "started" && pending.kind === "speak") {
      if (typeof message.voice === "string") pending.actualVoice = message.voice;
      this.noteSpeech(pending, "started");
      return;
    }
    if (message.event === "warmed" && pending.kind === "warmup") {
      this.remove(worker, message.id);
      worker.warmedVoices.add(pending.voice);
      if (typeof message.voice === "string" && message.voice) worker.warmedVoices.add(message.voice);
      const elapsed = Math.round(performance.now() - pending.submittedAt);
      const actual = typeof message.voice === "string" && message.voice ? message.voice : (pending.voice || "Windows default");
      try {
        this.note?.(`[voice] SAPI5 warmup ready voice=${JSON.stringify(actual)} elapsed_ms=${elapsed}`, {
          category: "TTS", operation: "native warmup", provider: "SAPI5", status: "ready",
          voice: actual, elapsed_ms: elapsed,
        });
      } catch { /* logging only */ }
      pending.resolve();
      return;
    }
    if (message.event === "error" || message.event === "cancelled") {
      this.remove(worker, message.id);
      if (pending.kind === "warmup") {
        if (message.event === "error") {
          const elapsed = Math.round(performance.now() - pending.submittedAt);
          try {
            this.note?.(`[voice] SAPI5 warmup failed requested_voice=${JSON.stringify(pending.voice || "Windows default")} elapsed_ms=${elapsed}`, {
              category: "TTS", operation: "native warmup", provider: "SAPI5", status: "error",
              ...(pending.voice ? { voice: pending.voice } : {}), elapsed_ms: elapsed,
            });
          } catch { /* logging only */ }
        }
        pending.resolve();
        return;
      }
      const error = message.event === "cancelled" ? new Error("Windows TTS: speech cancelled")
        : new Error(`Windows TTS: ${typeof message.error === "string" ? message.error : "native speech failed"}`);
      this.noteSpeech(pending, message.event === "cancelled" ? "cancelled" : "error", error.message);
      pending.reject(error);
      return;
    }
    if (message.event === "voices" && pending.kind === "voices") {
      this.remove(worker, message.id);
      if (!Array.isArray(message.voices) || !message.voices.every(voice => voice
        && typeof voice.id === "string" && typeof voice.name === "string")) {
        pending.reject(new Error("Windows TTS: invalid voice catalogue"));
      } else {
        pending.resolve(message.voices);
      }
    } else if (message.event === "done" && pending.kind === "speak") {
      this.remove(worker, message.id);
      if (typeof message.voice !== "string") {
        const error = new Error("Windows TTS: missing completed voice name");
        this.noteSpeech(pending, "error", error.message);
        pending.reject(error);
      } else {
        pending.actualVoice = message.voice;
        if (pending.command.op === "speak") worker.warmedVoices.add(pending.command.voice);
        worker.warmedVoices.add(message.voice);
        this.noteSpeech(pending, "done");
        pending.resolve({ voice: message.voice });
      }
    }
  }

  private noteSpeech(pending: Pending, status: string, error?: string): void {
    if (pending.kind !== "speak" || pending.command.op !== "speak") return;
    const command = pending.command;
    const elapsed = Math.round(performance.now() - pending.submittedAt);
    const voice = pending.actualVoice || command.voice;
    const attribution: LogAttribution = { category: "TTS", operation: "native speech", provider: "SAPI5",
      request_id: pending.callerId, status, rate: command.rate, volume: command.volume,
      ...(voice ? { voice } : {}), ...(status === "submitted" ? {} : { elapsed_ms: elapsed }) };
    const detail = status === "submitted"
      ? ` requested_voice=${JSON.stringify(command.voice || "Windows default")} rate=${command.rate} volume=${command.volume} text=${JSON.stringify(command.text)}`
      : `${pending.actualVoice ? ` voice=${JSON.stringify(pending.actualVoice)}` : ""} elapsed_ms=${elapsed}${error ? ` reason=${JSON.stringify(error)}` : ""}`;
    // Diagnostics must not interrupt playback or leave promises unresolved.
    try { this.note?.(`[voice] SAPI5 ${status} request=${JSON.stringify(pending.callerId)}${detail}`, attribution); }
    catch { /* logging only */ }
    if (status === "submitted" || status === "started" || status === "done" || status === "cancelled" || status === "error") {
      for (const fn of this.statusListeners) {
        try {
          fn({ type: status, id: pending.callerId, ...(voice ? { voice } : {}),
               ...(status !== "submitted" ? { elapsedMs: elapsed } : {}) });
        } catch { /* status only */ }
      }
    }
  }

  private remove(worker: Worker, id: number): void {
    const pending = worker.pending.get(id);
    if (pending?.timer) clearTimeout(pending.timer);
    worker.pending.delete(id);
  }

  private fail(worker: Worker, error: Error): void {
    if (worker.closed) return;
    worker.closed = true;
    if (this.worker === worker) {
      this.worker = undefined;
      this.notifyWorker();
    }
    for (const [id, pending] of worker.pending) {
      this.remove(worker, id);
      this.noteSpeech(pending, "error", error.message);
      pending.reject(error);
    }
    worker.lines.close();
    try { worker.child.stdin.end(); } catch { /* already closed */ }
    try { worker.child.kill(); } catch { /* already exited */ }
  }
}
