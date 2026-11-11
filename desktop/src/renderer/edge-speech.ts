// Complete Edge files are decoded eagerly, but only the worker's accepted commit makes one playable.
// Chromium validates MP3s without connecting anything to an audio destination; ordinary Audio preserves pitch.
import { glide, ipcError } from "./bridge.js";
import type { EdgeTtsEvent, LogAttribution } from "../shared/ipc.js";

export interface EdgeFragment {
  index: number;
  start: number;
  end: number;
  blob: Blob;
  attribution?: LogAttribution;
}

type Terminal = Extract<EdgeTtsEvent, { type: "end" }>;
type Range = { index: number; start: number; end: number };
type Candidate = EdgeFragment & { decoded: boolean };

export interface EdgeStream {
  next(): Promise<EdgeFragment | null>;
  cancel(): void;
  readonly terminal: Terminal | undefined;
  readonly finished: Promise<Terminal>;
}

class Request implements EdgeStream {
  private plan: Range[] | undefined;
  private candidates = new Map<string, Candidate>();
  private ready = new Map<number, EdgeFragment>();
  private accepted = new Set<number>();
  private index = 0;
  private wake: (() => void) | undefined;
  private finish!: (event: Terminal) => void;
  terminal: Terminal | undefined;
  readonly finished = new Promise<Terminal>((resolve) => { this.finish = resolve; });

  constructor(readonly id: string, private text: string, private release: () => void,
    private decode: (data: Uint8Array) => Promise<void>) {}

  async next(): Promise<EdgeFragment | null> {
    for (;;) {
      const fragment = this.ready.get(this.index);
      if (fragment) {
        this.ready.delete(this.index++);
        return fragment;
      }
      if (this.terminal) return null;
      await new Promise<void>((resolve) => { this.wake = resolve; });
    }
  }

  private notify(): void {
    const wake = this.wake;
    this.wake = undefined;
    wake?.();
  }

  end(event: Terminal): void {
    if (this.terminal) return;
    if (event.reason === "complete" && (!this.plan || this.accepted.size !== this.plan.length)) {
      event = { type: "end", id: this.id, reason: "failed", error: "Incomplete Edge speech" };
    }
    this.terminal = event;
    this.candidates.clear();
    if (event.reason !== "complete") this.ready.clear();
    this.release();
    this.finish(event);
    this.notify();
  }

  cancel(): void {
    if (!this.terminal) this.end({ type: "end", id: this.id, reason: "cancelled" });
    this.ready.clear();
    this.notify();
    void glide.invoke("tts:edgeCancel", this.id).catch(() => {});
  }

  event(event: EdgeTtsEvent): void {
    if (this.terminal) return;
    if (event.type === "end") { this.end(event); return; }
    if (event.type === "plan") {
      if (this.plan) return;
      let offset = 0;
      for (const [index, range] of event.fragments.entries()) {
        if (range.index !== index) {
          this.end({ type: "end", id: this.id, reason: "failed", error: "Invalid Edge speech plan" });
          return;
        }
        if (!Number.isInteger(range.start) || !Number.isInteger(range.end) || range.start !== offset
          || range.end <= range.start || range.end > this.text.length) {
          this.end({ type: "end", id: this.id, reason: "failed", error: "Invalid Edge speech plan" });
          return;
        }
        offset = range.end;
      }
      if (offset !== this.text.length) {
        this.end({ type: "end", id: this.id, reason: "failed", error: "Invalid Edge speech plan" });
        return;
      }
      this.plan = event.fragments;
      return;
    }
    if (this.accepted.has(event.index)) return;
    if (event.type === "accepted") {
      const candidate = this.candidates.get(event.attempt);
      if (!candidate?.decoded || candidate.index !== event.index) return;
      this.accepted.add(event.index);
      this.ready.set(event.index, candidate);
      for (const [attempt, other] of this.candidates) {
        if (other.index === event.index) this.candidates.delete(attempt);
      }
      this.notify();
      return;
    }
    const range = this.plan?.[event.index];
    if (!range || range.start !== event.start || range.end !== event.end || this.candidates.has(event.attempt)) return;
    const candidate: Candidate = { index: event.index, start: event.start, end: event.end,
      blob: new Blob([event.data as BlobPart], { type: event.mime }), attribution: event.attribution, decoded: false };
    this.candidates.set(event.attempt, candidate);
    void this.decode(event.data).then(() => this.verdict(event.attempt, candidate, true),
      () => this.verdict(event.attempt, candidate, false));
  }

  private verdict(attempt: string, candidate: Candidate, ok: boolean): void {
    if (this.terminal || this.candidates.get(attempt) !== candidate) return;
    candidate.decoded = ok;
    if (!ok) this.candidates.delete(attempt);
    void glide.invoke("tts:edgeDecoded", this.id, attempt, ok).catch((error: unknown) => {
      this.end({ type: "end", id: this.id, reason: "failed", error: ipcError(error) });
    });
  }
}

class EdgeSpeech {
  private requests = new Map<string, Request>();
  private unlisten: (() => void) | undefined;
  private decoder: OfflineAudioContext | undefined;

  start(text: string, id: string): EdgeStream {
    if (!this.unlisten) this.unlisten = glide.on("tts:edgeEvent", (event) => this.requests.get(event.id)?.event(event));
    const request = new Request(id, text, () => {
      this.requests.delete(id);
      if (!this.requests.size) { this.unlisten?.(); this.unlisten = undefined; }
    }, (data) => this.decode(data));
    this.requests.set(id, request);
    // Both the subscription and request ownership precede invoke: events can arrive before its acknowledgement.
    void glide.invoke("tts:edgeStart", id, text).catch((error: unknown) => {
      request.end({ type: "end", id, reason: "failed", error: ipcError(error) });
    });
    return request;
  }

  private async decode(data: Uint8Array): Promise<void> {
    this.decoder ??= new OfflineAudioContext(1, 1, 44_100);
    // decodeAudioData detaches its input; retain the complete encoded candidate for normal Audio playback.
    const audio = await this.decoder.decodeAudioData(new Uint8Array(data).buffer);
    if (!(audio.duration > 0) || !Number.isFinite(audio.duration) || audio.length <= 0 || audio.numberOfChannels <= 0) {
      throw new Error("Empty Edge audio");
    }
  }
}

export const edgeSpeech = new EdgeSpeech();
