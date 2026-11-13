// Electron owns one persistent JSONL worker; no speech or provider diagnostics appear in process arguments/logs.
import { execFile, spawn } from "child_process";
import type { ChildProcessWithoutNullStreams } from "child_process";
import path from "path";
import { promisify } from "util";
import { pylibDir, pythonCommand } from "./paths";
import type { EdgeTtsEvent, LogAttribution, Voice } from "../shared/ipc";

const runPython = promisify(execFile);
const DEADLINE_MS = 15000;
const MAX_FRAME_BYTES = 16 * 1024 * 1024;
const MAX_INPUT_QUEUE_BYTES = 512 * 1024;
const MAX_REQUESTS = 32;
const IDENTIFIER = /^[A-Za-z0-9_.:-]{1,128}$/;
const ATTEMPT_ID = /^(?:a\d{1,9}|[AB]-[12])$/;
const VOICE = /^[A-Za-z0-9-]{1,128}$/;
type Fragment = { index: number; start: number; end: number };
type Attempt = { index: number; verdict?: boolean };
const TRANSPORT_FIELDS = ["connections", "renewals", "reconnects", "connect_failures", "retired"] as const;
const TURN_FAILURE_CODES = [
  "zero_audio_reset",
  "partial_audio_reset",
  "first_audio_timeout",
  "stream_stall",
  "empty_audio",
  "protocol_error",
  "decode_rejected",
] as const;
type TurnFailureCode = typeof TURN_FAILURE_CODES[number];
const TURN_FAILURE_SET = new Set<string>(TURN_FAILURE_CODES);
const MAX_SETTLED = 64;
type TransportCounters = Record<typeof TRANSPORT_FIELDS[number], number>;
type Pending = {
  id: string;
  text: string;
  voice: string;
  started: number;
  deadline: number;
  timer: NodeJS.Timeout;
  sent: boolean;
  onEvent: (event: EdgeTtsEvent) => void;
  fragments?: Fragment[];
  attempts: Map<string, Attempt>;
  accepted: Set<number>;
  audioBytes: number;
  diagnostics: number;
  failedTurns: number;
  failureCodes: Map<TurnFailureCode, number>;
  candidateCounts?: number[];
};
type Worker = {
  child: ChildProcessWithoutNullStreams;
  ready: boolean;
  closed: boolean;
  readyTimer: NodeJS.Timeout;
  parts: Buffer[];
  frameBytes: number;
};
type EndReason = Extract<EdgeTtsEvent, { type: "end" }>["reason"];

function pythonContext() {
  const py = pythonCommand();
  const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONNOUSERSITE: "1" };
  if (py.bundled) {
    // Like the engine, don't mix the embedded runtime with a registered host Python or its packages.
    env.PYTHONHOME = path.dirname(py.cmd);
    env.PYTHONPATH = pylibDir();
  } else {
    env.PYTHONPATH = [pylibDir(), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  }
  return { py, env };
}

/** Fetch the service's catalogue instead of shipping a voice-name allowlist. */
export async function edgeVoices(signal: AbortSignal): Promise<Voice[]> {
  const { py, env } = pythonContext();
  try {
    const { stdout } = await runPython(py.cmd, [...py.args, "-c",
      "import asyncio,json,edge_tts; print(json.dumps(asyncio.run(edge_tts.list_voices())))"],
    { env, windowsHide: true, encoding: "utf8", signal, timeout: DEADLINE_MS, killSignal: "SIGKILL", maxBuffer: 2 * 1024 * 1024 });
    const catalogue: { ShortName: string; Gender: string }[] = JSON.parse(stdout);
    return catalogue.map((v) => ({ id: v.ShortName, name: `${v.ShortName} · ${v.Gender}` }))
      .sort((a, b) => a.id.localeCompare(b.id));
  } catch {
    throw new Error("Edge TTS: could not load voices; check the internet connection and edge-tts installation");
  }
}

/** The authoritative deadline includes worker startup, transport, synthesis and Chromium decoding, not playback. */
export class EdgeTts {
  private worker?: Worker;
  private readonly transport: TransportCounters = {
    connections: 0, renewals: 0, reconnects: 0, connect_failures: 0, retired: 0,
  };
  private readySlots = 0;
  private lastWorkerFailure?: string;
  private lastFailureDetail?: string;
  private readonly requestDetails = new Map<string, string>();
  private readonly settled = new Map<string, { voice: string }>();
  private readonly statusListeners = new Set<() => void>();
  private readonly pending = new Map<string, Pending>();
  // IDs cannot be reused within a worker generation: a completed request can still be draining a losing turn.
  private readonly issuedIds = new Set<string>();

  constructor(private readonly scriptPath: () => string,
    private readonly note?: (line: string, attribution?: LogAttribution) => void) {}

  onStatusChange(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  statusSnapshot(): {
    workerState: "stopped" | "starting" | "ready" | "reconnecting" | "error";
    readySlots: number;
    totalSlots: number;
    detail?: string;
  } {
    const readySlots = this.readySlots;
    let workerState: "stopped" | "starting" | "ready" | "reconnecting" | "error";
    if (!this.worker) {
      workerState = this.lastWorkerFailure ? "error" : "stopped";
    } else if (!this.worker.ready) {
      workerState = "starting";
    } else if (readySlots > 0) {
      workerState = "ready";
    } else if (this.transport.connect_failures > 0 || this.transport.retired > 0) {
      workerState = "reconnecting";
    } else {
      workerState = "starting";
    }
    const detail = this.lastWorkerFailure || this.lastFailureDetail;
    return { workerState, readySlots, totalSlots: 6, ...(detail ? { detail } : {}) };
  }

  lastRequestDetail(id: string): string | undefined {
    return this.requestDetails.get(id);
  }

  private notifyStatus(): void {
    for (const fn of this.statusListeners) {
      try { fn(); } catch { /* status listeners must not interrupt synthesis */ }
    }
  }

  configure(enabled: boolean): void {
    if (!enabled) {
      this.lastWorkerFailure = undefined;
      this.close();
      this.notifyStatus();
      return;
    }
    this.ensureWorker();
    this.notifyStatus();
  }

  start(id: string, text: string, voice: string, onEvent: (event: EdgeTtsEvent) => void): void {
    const started = performance.now();
    if (typeof id !== "string" || !IDENTIFIER.test(id)) throw new Error("Edge TTS: invalid request id");
    if (this.pending.has(id) || this.issuedIds.has(id)) throw new Error("Edge TTS: duplicate request id");
    if (typeof text !== "string" || !text.trim() || text.length > 4000) throw new Error("Edge TTS: text must contain 1–4000 characters");
    if (typeof voice !== "string" || !VOICE.test(voice)) throw new Error("Edge TTS: choose a voice in Settings > Voice");
    if (this.pending.size >= MAX_REQUESTS) throw new Error("Edge TTS: too many pending requests");
    const pending: Pending = { id, text, voice, started, deadline: started + DEADLINE_MS,
      timer: setTimeout(() => this.expire(id), DEADLINE_MS), sent: false, onEvent,
      attempts: new Map(), accepted: new Set(), audioBytes: 0, diagnostics: 0,
      failedTurns: 0, failureCodes: new Map() };
    // Register before spawning: child failure and a ready/candidate can precede the IPC acknowledgement.
    this.pending.set(id, pending);
    this.issuedIds.add(id);
    this.log(pending, "started");
    const worker = this.ensureWorker();
    if (!worker) {
      this.finish(pending, "failed", "worker could not start");
    } else if (worker.ready) {
      this.sendStart(worker, pending);
    }
  }

  decoded(id: string, attempt: string, ok: boolean): void {
    const pending = this.pending.get(id);
    if (!pending || typeof ok !== "boolean" || typeof attempt !== "string") return;
    if (this.expired(pending)) return;
    const candidate = pending.attempts.get(attempt);
    if (!candidate || candidate.verdict !== undefined || pending.accepted.has(candidate.index)) return;
    candidate.verdict = ok;
    if (this.worker) this.write(this.worker, { op: "decoded", id, attempt, ok });
  }

  cancel(id: string): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    const worker = this.worker, sent = pending.sent;
    this.finish(pending, "cancelled");
    if (sent && worker) this.write(worker, { op: "cancel", id });
  }

  close(): void {
    const worker = this.worker;
    // Detach first so callbacks cannot accidentally attach a replacement request to the closing worker.
    this.worker = undefined;
    this.issuedIds.clear();
    this.settled.clear();
    this.readySlots = 0;
    if (worker) {
      worker.closed = true;
      clearTimeout(worker.readyTimer);
      worker.parts = [];
      worker.frameBytes = 0;
      try { worker.child.stdin.end(`${JSON.stringify({ op: "shutdown" })}\n`); } catch { /* already closed */ }
      // A busy network coroutine still gets a chance to shut down; a broken worker cannot outlive Electron.
      const kill = setTimeout(() => { try { worker.child.kill(); } catch { /* already exited */ } }, 500);
      kill.unref();
      worker.child.once("exit", () => clearTimeout(kill));
    }
    for (const pending of [...this.pending.values()]) this.finish(pending, "cancelled");
  }

  private ensureWorker(): Worker | undefined {
    if (this.worker) return this.worker;
    for (const key of TRANSPORT_FIELDS) this.transport[key] = 0;
    this.readySlots = 0;
    this.lastWorkerFailure = undefined;
    try {
      const { py, env } = pythonContext();
      const child = spawn(py.cmd, [...py.args, "-u", this.scriptPath()],
        { env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
      const worker: Worker = { child, ready: false, closed: false, parts: [], frameBytes: 0,
        readyTimer: setTimeout(() => this.fail(worker, "worker startup timed out"), DEADLINE_MS) };
      this.worker = worker;
      child.on("error", () => this.fail(worker, "worker process failed"));
      child.on("exit", () => this.fail(worker, "worker exited"));
      child.on("close", () => this.fail(worker, "worker closed"));
      child.stdin.on("error", () => this.fail(worker, "worker input failed"));
      child.stdout.on("error", () => this.fail(worker, "worker output failed"));
      child.stdout.on("end", () => this.fail(worker, "worker output closed"));
      child.stdout.on("data", (chunk: Buffer) => this.consume(worker, chunk));
      child.stderr.on("error", () => this.fail(worker, "worker diagnostic stream failed"));
      // Drain without collecting tracebacks, URLs, query tokens, text, or provider error messages.
      child.stderr.resume();
      return worker;
    } catch {
      this.lastWorkerFailure = "worker could not start";
      for (const pending of [...this.pending.values()]) this.finish(pending, "failed", "worker could not start");
      this.notifyStatus();
      return undefined;
    }
  }

  private sendStart(worker: Worker, pending: Pending): void {
    if (worker.closed || pending.sent || !this.pending.has(pending.id) || this.expired(pending)) return;
    const budget = Math.floor(pending.deadline - performance.now());
    if (budget <= 0) { this.expire(pending.id); return; }
    pending.sent = true;
    this.write(worker, { op: "start", id: pending.id, text: pending.text, voice: pending.voice, budget_ms: budget });
  }

  private write(worker: Worker, command: Record<string, unknown>): void {
    if (worker.closed) return;
    if (worker.child.stdin.writableLength > MAX_INPUT_QUEUE_BYTES) {
      this.fail(worker, "worker input queue exceeded its limit");
      return;
    }
    try {
      worker.child.stdin.write(`${JSON.stringify(command)}\n`, "utf8", (error) => {
        if (error) this.fail(worker, "worker input failed");
      });
    } catch { this.fail(worker, "worker input failed"); }
  }

  private consume(worker: Worker, chunk: Buffer): void {
    if (worker.closed) return;
    let start = 0;
    while (start < chunk.length && !worker.closed) {
      const end = chunk.indexOf(10, start);
      const part = chunk.subarray(start, end < 0 ? chunk.length : end);
      worker.frameBytes += part.length;
      if (worker.frameBytes > MAX_FRAME_BYTES) { this.fail(worker, "worker output exceeded its limit"); return; }
      if (part.length) worker.parts.push(part);
      if (end < 0) return;
      const frame = worker.parts.length === 1 ? worker.parts[0] : Buffer.concat(worker.parts, worker.frameBytes);
      worker.parts = [];
      worker.frameBytes = 0;
      this.receive(worker, frame.toString("utf8"));
      start = end + 1;
    }
  }

  private receive(worker: Worker, line: string): void {
    if (worker.closed) return;
    let event: unknown;
    try { event = JSON.parse(line); } catch { this.fail(worker, "worker sent invalid data"); return; }
    if (!event || typeof event !== "object" || Array.isArray(event) || !("type" in event)) {
      this.fail(worker, "worker sent invalid data");
      return;
    }
    if (event.type === "ready") {
      if (worker.ready) { this.fail(worker, "worker sent invalid data"); return; }
      worker.ready = true;
      clearTimeout(worker.readyTimer);
      for (const pending of this.pending.values()) this.sendStart(worker, pending);
      this.notifyStatus();
      return;
    }
    // Diagnostics are validated against closed allowlists, never forwarded or logged verbatim.
    if (event.type === "diagnostic") {
      const pending = "id" in event && typeof event.id === "string" ? this.pending.get(event.id) : undefined;
      if (pending) pending.diagnostics = Math.min(1000000, pending.diagnostics + 1);
      if ("event" in event && event.event === "turn_failed") {
        this.recordTurnFailure(event, pending);
        return;
      }
      if ("event" in event && event.event === "transport_status") {
        const readySlots: unknown = Reflect.get(event, "ready_slots");
        if (typeof readySlots !== "number" || !Number.isInteger(readySlots) || readySlots < 0 || readySlots > 6
          || Reflect.get(event, "total_slots") !== 6) return;
        const counters: TransportCounters = { ...this.transport };
        for (const key of TRANSPORT_FIELDS) {
          const value: unknown = Reflect.get(event, key);
          const maximum = key === "reconnects" ? 1000000000 : 2000000000;
          if (typeof value !== "number" || !Number.isSafeInteger(value) || value < this.transport[key]
            || value > maximum) return;
          counters[key] = value;
        }
        for (const key of TRANSPORT_FIELDS) this.transport[key] = counters[key];
        this.readySlots = readySlots;
        this.notifyStatus();
      }
      return;
    }
    const pending = "id" in event && typeof event.id === "string" ? this.pending.get(event.id) : undefined;
    if (!pending) return; // Cancelled/completed requests may still have loser-drain output in flight.
    if (!worker.ready || !pending.sent) { this.fail(worker, "worker sent invalid data"); return; }
    if (this.expired(pending)) return;
    const invalid = () => this.fail(worker, "worker sent invalid data");
    if (event.type === "plan") {
      if (pending.fragments || !("fragments" in event) || !Array.isArray(event.fragments)
        || !event.fragments.length || event.fragments.length > pending.text.length) { invalid(); return; }
      const fragments: Fragment[] = [];
      let end = 0;
      const rawFragments: unknown[] = event.fragments;
      for (const [index, fragment] of rawFragments.entries()) {
        if (!fragment || typeof fragment !== "object" || !("index" in fragment) || !("start" in fragment)
          || !("end" in fragment) || fragment.index !== index || fragment.start !== end
          || typeof fragment.end !== "number" || !Number.isInteger(fragment.end)
          || fragment.end <= end || fragment.end > pending.text.length
          || (fragment.end < pending.text.length && /[\uD800-\uDBFF]/.test(pending.text[fragment.end - 1])
            && /[\uDC00-\uDFFF]/.test(pending.text[fragment.end]))) { invalid(); return; }
        fragments.push({ index, start: end, end: fragment.end });
        end = fragment.end;
      }
      if (end !== pending.text.length) { invalid(); return; }
      pending.fragments = fragments;
      pending.candidateCounts = new Array<number>(fragments.length).fill(0);
      this.emit(pending, { type: "plan", id: pending.id, fragments });
    } else if (event.type === "candidate") {
      const fragment = "index" in event && typeof event.index === "number" && Number.isInteger(event.index)
        ? pending.fragments?.[event.index] : undefined;
      if (!fragment) { invalid(); return; }
      if (pending.accepted.has(fragment.index)) return;
      if (!("start" in event) || !("end" in event) || !("attempt" in event) || !("audio" in event)
        || event.start !== fragment.start || event.end !== fragment.end || typeof event.attempt !== "string"
        || !IDENTIFIER.test(event.attempt) || pending.attempts.has(event.attempt)
        || pending.candidateCounts![fragment.index] >= 4 || typeof event.audio !== "string"
        || !event.audio.length || event.audio.length % 4 !== 0
        || !/^[A-Za-z0-9+/]*={0,2}$/.test(event.audio)) { invalid(); return; }
      const data = Buffer.from(event.audio, "base64");
      pending.attempts.set(event.attempt, { index: fragment.index });
      pending.candidateCounts![fragment.index]++;
      pending.audioBytes += data.length;
      this.emit(pending, { type: "candidate", id: pending.id, index: fragment.index, attempt: event.attempt,
        start: fragment.start, end: fragment.end, mime: "audio/mpeg", data });
    } else if (event.type === "accepted") {
      const attemptId = "attempt" in event && typeof event.attempt === "string" ? event.attempt : undefined;
      const attempt = attemptId ? pending.attempts.get(attemptId) : undefined;
      if (!attempt || !attemptId || !("index" in event) || attempt.index !== event.index
        || attempt.verdict !== true || pending.accepted.has(attempt.index)) { invalid(); return; }
      pending.accepted.add(attempt.index);
      this.emit(pending, { type: "accepted", id: pending.id, index: attempt.index, attempt: attemptId });
    } else if (event.type === "end") {
      if (!("reason" in event)) { invalid(); return; }
      if (event.reason === "complete") {
        if (!pending.fragments || pending.accepted.size !== pending.fragments.length) { invalid(); return; }
        this.finish(pending, "complete");
      } else if (event.reason === "failed" || event.reason === "deadline" || event.reason === "cancelled") {
        this.finish(pending, event.reason, event.reason === "failed" ? "synthesis failed" : undefined);
      } else { invalid(); }
    } else { invalid(); }
  }

  private expired(pending: Pending): boolean {
    if (performance.now() < pending.deadline) return false;
    this.expire(pending.id);
    return true;
  }

  private expire(id: string): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    // Timers may fire early; the monotonic clock, not timer scheduling, decides the deadline.
    const remaining = pending.deadline - performance.now();
    if (remaining > 0) {
      clearTimeout(pending.timer);
      pending.timer = setTimeout(() => this.expire(id), Math.ceil(remaining));
      return;
    }
    const worker = this.worker, sent = pending.sent;
    this.finish(pending, "deadline");
    if (sent && worker) this.write(worker, { op: "cancel", id });
  }

  private emit(pending: Pending, event: EdgeTtsEvent): void {
    if (this.pending.get(pending.id) !== pending) return;
    const message = { ...event, attribution: this.attribution(pending, event.type) };
    if (this.expired(pending)) return;
    try { pending.onEvent(message); }
    catch { this.cancel(pending.id); }
  }

  private finish(pending: Pending, reason: EndReason, detail?: string): void {
    if (this.pending.get(pending.id) !== pending) return;
    // A ready/end frame parsed after the authoritative deadline can never turn into a late success.
    if (reason !== "cancelled" && performance.now() >= pending.deadline) { reason = "deadline"; detail = undefined; }
    clearTimeout(pending.timer);
    this.pending.delete(pending.id);
    const codesSummary = this.formatFailureCodes(pending.failureCodes);
    if (codesSummary) {
      this.lastFailureDetail = codesSummary;
      this.rememberDetail(pending.id, codesSummary);
    } else if (reason === "deadline") {
      this.rememberDetail(pending.id, "deadline");
    } else if (reason === "failed" && detail) {
      this.rememberDetail(pending.id, detail);
    }
    if (reason !== "cancelled" && this.worker && !this.worker.closed) {
      this.rememberSettled(pending.id, pending.voice);
    }
    const failedDetail = (!detail || detail === "synthesis failed") && codesSummary
      ? `synthesis failed (${codesSummary})`
      : (detail || "synthesis failed");
    const error = reason === "deadline" ? "Edge TTS: synthesis deadline exceeded"
      : reason === "failed" ? `Edge TTS: ${failedDetail}` : undefined;
    const event: Extract<EdgeTtsEvent, { type: "end" }> = {
      type: "end", id: pending.id, reason, ...(error ? { error } : {}), attribution: this.attribution(pending, reason),
    };
    if (event.reason === "complete" && performance.now() >= pending.deadline) {
      event.reason = "deadline";
      event.error = "Edge TTS: synthesis deadline exceeded";
      event.attribution = this.attribution(pending, "deadline");
    }
    try { pending.onEvent(event); } catch { /* caller is gone */ }
    this.log(pending, event.reason, event.error, codesSummary);
  }

  private attribution(pending: Pending, status: string): LogAttribution {
    return { category: "TTS", operation: "Edge streaming synthesis", provider: "edge", request_id: pending.id,
      voice: pending.voice, status, elapsed_ms: Math.round(performance.now() - pending.started) };
  }

  private formatFailureCodes(codes: Map<TurnFailureCode, number>): string {
    const parts: string[] = [];
    for (const code of TURN_FAILURE_CODES) {
      const count = codes.get(code) || 0;
      if (count > 0) parts.push(`${code}:${count}`);
    }
    return parts.join(",");
  }

  private rememberSettled(id: string, voice: string): void {
    this.settled.delete(id);
    this.settled.set(id, { voice });
    while (this.settled.size > MAX_SETTLED) {
      const oldest = this.settled.keys().next().value;
      if (oldest === undefined) break;
      this.settled.delete(oldest);
    }
  }

  private rememberDetail(id: string, detail: string): void {
    this.requestDetails.delete(id);
    this.requestDetails.set(id, detail);
    while (this.requestDetails.size > MAX_SETTLED) {
      const oldest = this.requestDetails.keys().next().value;
      if (oldest === undefined) break;
      this.requestDetails.delete(oldest);
    }
  }

  private recordTurnFailure(event: object, pending: Pending | undefined): void {
    const id: unknown = Reflect.get(event, "id");
    const index: unknown = Reflect.get(event, "index");
    const attempt: unknown = Reflect.get(event, "attempt");
    const pool: unknown = Reflect.get(event, "pool");
    const code: unknown = Reflect.get(event, "code");
    const audioBytes: unknown = Reflect.get(event, "audio_bytes");
    const draining: unknown = Reflect.get(event, "draining");
    if (typeof id !== "string" || !IDENTIFIER.test(id)
      || typeof index !== "number" || !Number.isSafeInteger(index) || index < 0 || index > 4000
      || typeof attempt !== "string" || !ATTEMPT_ID.test(attempt)
      || (pool !== "A" && pool !== "B")
      || typeof code !== "string" || !TURN_FAILURE_SET.has(code)
      || typeof audioBytes !== "number" || !Number.isSafeInteger(audioBytes) || audioBytes < 0 || audioBytes > MAX_FRAME_BYTES
      || typeof draining !== "boolean") {
      return;
    }
    const safeCode = code as TurnFailureCode;
    if (pending) {
      pending.failedTurns = Math.min(1000, pending.failedTurns + 1);
      pending.failureCodes.set(safeCode, Math.min(1000, (pending.failureCodes.get(safeCode) || 0) + 1));
      return;
    }
    if (!draining) return;
    const settled = this.settled.get(id);
    if (!settled) return;
    this.lastFailureDetail = `${safeCode}:1`;
    try {
      this.note?.(
        `[voice] Edge drain failed request=${JSON.stringify(id)} fragment=${index}`
        + ` attempt=${JSON.stringify(attempt)} pool=${JSON.stringify(pool)}`
        + ` code=${JSON.stringify(safeCode)} audio_bytes=${audioBytes}`,
        {
          category: "TTS",
          operation: "Edge loser drain",
          provider: "edge",
          request_id: id,
          voice: settled.voice,
          status: "drain_failed",
        },
      );
    } catch { /* diagnostics must not interrupt speech */ }
    this.notifyStatus();
  }

  private log(pending: Pending, status: string, error?: string, failureCodes = ""): void {
    const elapsed = Math.round(performance.now() - pending.started);
    const failedInfo = status !== "started" && pending.failedTurns > 0
      ? ` failed_turns=${pending.failedTurns} failure_codes=${JSON.stringify(failureCodes)}`
      : "";
    try {
      this.note?.(`[voice] Edge ${status} request=${JSON.stringify(pending.id)} elapsed_ms=${elapsed} chars=${pending.text.length}`
        + ` candidates=${pending.attempts.size} accepted=${pending.accepted.size} audio_bytes=${pending.audioBytes}`
        + ` connections=${this.transport.connections}`
        + ` renewals=${this.transport.renewals} reconnects=${this.transport.reconnects}`
        + ` connect_failures=${this.transport.connect_failures}`
        + ` retired=${this.transport.retired}`
        + ` diagnostics=${pending.diagnostics}${failedInfo}${error ? ` reason=${JSON.stringify(error)}` : ""}`,
      this.attribution(pending, status));
    } catch { /* diagnostics must not interrupt speech */ }
  }

  private fail(worker: Worker, detail: string): void {
    if (worker.closed) return;
    worker.closed = true;
    if (this.worker === worker) {
      this.worker = undefined;
      this.issuedIds.clear();
      this.settled.clear();
      this.readySlots = 0;
      this.lastWorkerFailure = detail;
    }
    clearTimeout(worker.readyTimer);
    worker.parts = [];
    worker.frameBytes = 0;
    for (const pending of [...this.pending.values()]) this.finish(pending, "failed", detail);
    this.notifyStatus();
    try { worker.child.stdin.destroy(); } catch { /* already closed */ }
    try { worker.child.kill(); } catch { /* already exited */ }
  }
}
