// The narrator: speaks the coach's lines ("speak" events on the engine's event stream) with the voice picked in
// Settings > Voice.
//
// Windows voices use the main process's persistent SAPI5 worker. Online audio is prefetched as lines are queued;
// Edge streams complete accepted fragments, falling back only from the first unstarted original-text offset.
//
// Coach and tagged Guides lines share one queue. Cancelling a tag releases its waiters immediately, even during
// synthesis or worker startup, without dropping other lines. A late result can never take ownership from a newer
// line. No browser speech catalogue or speechSynthesis queue is involved.
//
// The player can talk over it: holding push-to-talk (the "ptt" event's "listening", coach.ts) calls bargeIn(), which
// stops the line being said and drops the queued ones at once.
import { call, glide, ipcError } from "./bridge.js";
import { toast } from "./dom.js";
import { Obj, SpeechSettings } from "./types.js";
import type { LogAttribution, TtsStatus } from "../shared/ipc.js";
import { edgeSpeech } from "./edge-speech.js";
import type { EdgeStream } from "./edge-speech.js";

interface Line {
  text: string;
  audio: Promise<Blob | null> | null;
  edge?: EdgeStream;
  provider: string;
  voiceLabel: string;
  windows: Pick<SpeechSettings, "voiceName" | "rate" | "volume">;
  requestId: string;
  attribution?: LogAttribution;
  synthMs?: number;
  nativeId?: string;
  player?: HTMLAudioElement;
  tag?: string;
  done?: () => void;
  cancelled?: boolean;
  reportedPlaying?: boolean;
  reportedFallback?: boolean;
}

class Narrator {
  private provider = "windows";
  private voiceName = "";
  private onlineLabel = "";
  private rate = 1;
  private volume = 1;
  private queue: Line[] = [];
  private current: Line | null = null;
  private muted = false;
  private bargedAt = 0;
  lastVoice = "";
  lastVoiceAt = 0;
  lastText = "";
  lastSynthMs: number | null = null;
  private lastRequestId = "";
  private sapiSynthMs = new Map<string, number>();
  private lastError = 0;

  /** Settings apply to newly queued lines. Muting stops the active line and drops the queue. */
  configure(muted: boolean, sp: SpeechSettings): void {
    if (muted && !this.muted) this.stop();
    this.muted = muted;
    this.provider = sp.provider || "windows";
    this.voiceName = sp.voiceName || "";
    this.onlineLabel = speakLabel(sp);
    this.rate = Number(sp.rate ?? 1);
    this.volume = Number(sp.volume ?? 1);
  }

  /** Observe speech without playing it; an older reconnect replay must not replace a newer line. */
  noteVoice(name: string, text: string, at = Date.now(), synthMs: number | null = null, requestId = ""): void {
    if (at < this.lastVoiceAt) return;
    this.lastVoice = name;
    this.lastVoiceAt = at;
    this.lastText = text;
    this.lastSynthMs = synthMs;
    this.lastRequestId = requestId;
    const el = document.getElementById("sv-last");
    if (el) el.textContent = voiceLastLine();
    const line = document.getElementById("sv-last-text");
    if (line) {
      line.textContent = text;
      line.hidden = false;
    }
  }
  onTtsStatus(s: TtsStatus): void {
    if (s.requestId && typeof s.synthMs === "number") {
      if (this.sapiSynthMs.size >= 32) {
        const oldest = this.sapiSynthMs.keys().next().value;
        if (oldest !== undefined) this.sapiSynthMs.delete(oldest);
      }
      this.sapiSynthMs.set(s.requestId, s.synthMs);
      const cur = this.current;
      if (cur && cur.nativeId === s.requestId && (s.phase === "speaking" || s.phase === "fallback") && s.actualVoice) {
        this.noteVoice(`${s.actualVoice} (SAPI5)`, cur.text, Date.now(), s.synthMs, s.requestId);
      }
    }
  }


  private voiceError(message: string): void {
    if (Date.now() - this.lastError <= 60_000) return;
    this.lastError = Date.now();
    toast(message, true, 9000);
  }

  /** A coach line. Only the online providers interpret the optional persona style and tone. */
  say(text: string, interrupt = false, style = "", tone = ""): void {
    if (!text || this.muted) return;
    if (interrupt) this.stop();
    this.enqueue(text, undefined, undefined, style, tone);
  }

  /** Holding push-to-talk interrupts the active line and drops all queued lines. */
  bargeIn(): void {
    this.bargedAt = Date.now();
    this.stop();
  }

  bargedSince(t: number): boolean {
    return this.bargedAt >= t;
  }

  /** Provider failures are reported asynchronously; mute prevents any speech request. */
  canSpeak(): boolean {
    return !this.muted;
  }

  /** Resolve at completion or cancellation, and immediately for an empty or muted line. */
  speakAndWait(text: string, tag: string): Promise<void> {
    if (!text || !this.canSpeak()) return Promise.resolve();
    return new Promise((resolve) => this.enqueue(text, tag, resolve));
  }

  private finish(line: Line): void {
    const done = line.done;
    line.done = undefined;
    done?.();
  }

  private reportPlayback(line: Line, state: "playing" | "fallback" | "ended" | "error"): void {
    const payload = state === "fallback" && line.windows.voiceName
      ? { requestId: line.requestId, state, fallbackVoice: line.windows.voiceName }
      : { requestId: line.requestId, state };
    void call("tts:playbackState", payload);
  }

  private cancelLine(line: Line): void {
    const clearOnline = !line.reportedFallback && (line.reportedPlaying || (line.provider !== "windows" && !line.edge));
    line.cancelled = true;
    line.edge?.cancel();
    if (line.nativeId) void call("tts:windowsStop", line.nativeId);
    line.player?.pause();
    if (clearOnline) this.reportPlayback(line, "ended");
    this.finish(line);
  }

  /** Cancel only this tag; pending synthesis must not block the next surviving line. */
  cancel(tag: string): void {
    this.queue = this.queue.filter((line) => {
      if (line.tag !== tag) return true;
      this.cancelLine(line);
      return false;
    });
    if (this.current?.tag === tag) {
      const line = this.current;
      this.current = null;
      this.cancelLine(line);
      void this.next();
    }
  }

  private enqueue(text: string, tag?: string, done?: () => void, style = "", tone = ""): void {
    const line: Line = {
      text, provider: this.provider, voiceLabel: this.onlineLabel, tag, done, audio: null, requestId: crypto.randomUUID(),
      windows: { voiceName: this.voiceName, rate: this.rate, volume: this.volume },
    };
    if (line.provider === "edge") {
      line.edge = edgeSpeech.start(text, line.requestId);
    } else if (line.provider !== "windows") {
      const synthStarted = performance.now();
      line.audio = glide.invoke("tts:synth", text, style, tone, line.requestId)
        .then((r) => {
          line.attribution = r.attribution;
          line.synthMs = typeof r.attribution?.elapsed_ms === "number"
            ? r.attribution.elapsed_ms
            : Math.max(0, Math.round(performance.now() - synthStarted));
          return new Blob([r.data as BlobPart], { type: r.mime });
        })
        .catch((e) => {
          if (!line.cancelled) this.voiceError(`Voice (${line.provider}) failed, using the Windows voice: ${ipcError(e)}`);
          return null;
        });
    }
    this.queue.push(line);
    void this.next();
  }

  private async windowsSay(line: Line, text = line.text): Promise<boolean> {
    line.nativeId = line.requestId;
    try {
      const result = await glide.invoke("tts:windowsSpeak", line.nativeId, text, line.windows);
      const synthMs = this.sapiSynthMs.get(line.requestId) ?? null;
      this.sapiSynthMs.delete(line.requestId);
      if (this.current === line) this.noteVoice(`${result.voice} (SAPI5)`, text, Date.now(), synthMs, line.requestId);
      return true;
    } catch (error) {
      if (this.current === line && !line.cancelled) {
        this.voiceError(`Windows SAPI5 voice failed: ${ipcError(error)}`);
      }
      return false;
    } finally {
      line.nativeId = undefined;
    }
  }

  private fallback(line: Line): void {
    call("app:note", `[voice] TTS fallback: online synthesis failed; using Windows SAPI5 requested_voice=${JSON.stringify(line.windows.voiceName || "Windows default")}`,
      { category: "TTS", operation: "fallback", provider: "SAPI5", request_id: line.requestId, status: "requested" });
  }

  private async edgeSay(line: Line): Promise<void> {
    const stream = line.edge!;
    let unstarted = 0;
    let playbackFailed = false;
    while (this.current === line && !line.cancelled) {
      const fragment = await stream.next();
      if (this.current !== line || line.cancelled) return;
      if (!fragment) break;
      // Failure discards every unstarted accepted file, including one whose next() just resolved.
      if (stream.terminal && stream.terminal.reason !== "complete") break;
      const result = await this.play(fragment.blob, line, line.text.slice(fragment.start, fragment.end),
        fragment.attribution, () => { unstarted = fragment.end; }, stream, fragment.synthMs);
      if (this.current !== line || line.cancelled) return;
      if (result.failed) {
        stream.cancel();
        if (result.started) {
          this.reportPlayback(line, "error");
          this.voiceError("Edge voice playback failed; stopped to avoid repeating spoken words.");
          return;
        }
        playbackFailed = true;
        break;
      }
    }
    if (this.current !== line || line.cancelled) return;
    const terminal = stream.terminal;
    if ((playbackFailed || terminal?.reason === "failed" || terminal?.reason === "deadline") && unstarted < line.text.length) {
      this.voiceError(`Voice (edge) failed, using the Windows voice${terminal?.error ? `: ${terminal.error}` : ""}`);
      this.fallback(line);
      line.reportedFallback = true;
      this.reportPlayback(line, "fallback");
      const ok = await this.windowsSay(line, line.text.slice(unstarted));
      if (!line.cancelled) this.reportPlayback(line, ok ? "ended" : "error");
    } else if (line.reportedPlaying) {
      if (terminal?.reason === "complete" && this.lastRequestId === line.requestId && stream.synthMs !== undefined) {
        this.lastSynthMs = stream.synthMs;
        const el = document.getElementById("sv-last");
        if (el) el.textContent = voiceLastLine();
      }
      this.reportPlayback(line, "ended");
    }
  }

  private async next(): Promise<void> {
    if (this.current) return;
    const line = this.queue.shift();
    if (!line) return;
    this.current = line;
    const blob = line.audio ? await line.audio : null;
    if (this.current !== line) return;
    if (line.edge) {
      await this.edgeSay(line);
    } else if (blob) {
      const result = await this.play(blob, line);
      if (this.current === line && !line.cancelled) {
        this.reportPlayback(line, result.failed ? "error" : "ended");
      }
    } else if (line.provider !== "windows") {
      this.fallback(line);
      line.reportedFallback = true;
      this.reportPlayback(line, "fallback");
      const ok = await this.windowsSay(line);
      if (!line.cancelled) this.reportPlayback(line, ok ? "ended" : "error");
    } else {
      await this.windowsSay(line);
    }
    if (this.current !== line) return;
    this.finish(line);
    this.current = null;
    void this.next();
  }

  private async play(blob: Blob, line: Line, text = line.text, source = line.attribution,
    onStart?: () => void, stream?: EdgeStream, fragmentSynthMs?: number): Promise<{ started: boolean; failed: boolean }> {
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    line.player = a;
    a.volume = Math.max(0, Math.min(1, line.windows.volume ?? 1));
    a.playbackRate = line.windows.rate ?? 1;
    a.preservesPitch = true;
    const started = performance.now();
    const attribution: LogAttribution = { ...source, category: "TTS", operation: "audio playback",
      request_id: line.requestId, rate: a.playbackRate, volume: a.volume };
    call("app:note", `[voice] playback requested${attribution.provider ? ` provider=${attribution.provider}` : ""} audio_bytes=${blob.size}`,
      { ...attribution, status: "requested", elapsed_ms: 0 });
    let began = false;
    let failed = false;
    await new Promise<void>((resolve) => {
      let finished = false;
      const finish = (result: string, status: string) => {
        if (finished) return;
        finished = true;
        call("app:note", `[voice] playback ${result}${attribution.provider ? ` provider=${attribution.provider}` : ""}`,
          { ...attribution, status, elapsed_ms: Math.round(performance.now() - started) });
        resolve();
      };
      a.onended = () => finish("ended", "done");
      a.onerror = () => { failed = true; finish(`failed media_code=${a.error?.code || 0}`, "error"); };
      a.onpause = () => finish(a.ended ? "ended" : "paused", a.ended ? "done" : "cancelled");
      void stream?.finished.then((terminal) => {
        if (!finished && (terminal.reason === "cancelled" || (!began && terminal.reason !== "complete"))) {
          a.pause();
          finish("stopped before start", "cancelled");
        }
      });
      a.play().then(() => {
        if (finished || this.current !== line || line.cancelled) return;
        began = true;
        if (!line.reportedPlaying) {
          line.reportedPlaying = true;
          this.reportPlayback(line, "playing");
        }
        onStart?.();
        call("app:note", `[voice] playback started${attribution.provider ? ` provider=${attribution.provider}` : ""}`,
          { ...attribution, status: "started", elapsed_ms: Math.round(performance.now() - started) });
        this.noteVoice(`${line.provider === "edge" ? line.voiceLabel : line.provider} (online voice)`, text,
          Date.now(), fragmentSynthMs ?? line.synthMs ?? null, line.requestId);
      }).catch((e: unknown) => {
        if (finished) return;
        failed = true;
        finish(`rejected reason=${e instanceof Error
          && ["NotAllowedError", "NotSupportedError", "AbortError"].includes(e.name) ? e.name : "Error"}`, "error");
      });
    });
    URL.revokeObjectURL(url);
    line.player = undefined;
    return { started: began, failed };
  }

  stop(): void {
    const current = this.current;
    this.current = null;
    if (current) this.cancelLine(current);
    for (const line of this.queue) this.cancelLine(line);
    this.queue = [];
  }
}

export const speech = new Narrator();

/** The narrator's voice for the status bar: the provider and its model or voice, with the same defaults as main/tts.ts. */
export function speakLabel(sp: Obj): string {
  const p = sp.provider || "windows";
  if (p === "edge") return `Edge-TTS ${sp.edge?.voice || "(no voice selected)"}`;
  if (p === "openai") return `OpenAI ${sp.openai?.model || "gpt-4o-mini-tts"}, ${sp.openai?.voice || "coral"}`;
  if (p === "elevenlabs") return `ElevenLabs ${sp.elevenlabs?.model || "eleven_flash_v2_5"}`;
  if (p === "azure") return `Azure ${(sp.azure?.voice || "en-US-AndrewMultilingualNeural").replace(/^[a-z]{2}-[A-Z]{2}-/, "")}`;
  if (p === "google") return `Google ${(sp.google?.voice || "en-US-Chirp3-HD-Charon").replace(/^[a-z]{2}-[A-Z]{2}-/, "")}`;
  const v = String(sp.voiceName || "").replace(/^Microsoft\s+/, "").split(" - ")[0].trim();
  return v ? `SAPI5, ${v}` : "Windows SAPI5";
}

export function fmtSynthDuration(ms: number | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;
}

/** "Last line spoken with" (Settings > Voice): the voice, online synthesis latency, and time. */
export function voiceLastLine(): string {
  if (!speech.lastVoice) return "nothing spoken yet this session";
  const dur = fmtSynthDuration(speech.lastSynthMs);
  const synth = dur ? `, generated in ${dur}` : "";
  return `${speech.lastVoice}${synth}, ${new Date(speech.lastVoiceAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}
