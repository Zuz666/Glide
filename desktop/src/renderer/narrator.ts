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
import { esc, toast } from "./dom.js";
import { dateTimeHtml, dateTimeText } from "./date-time.js";
import { Obj, SpeechSettings } from "./types.js";
import type { LogAttribution, TtsStatus } from "../shared/ipc.js";
import { edgeSpeech } from "./edge-speech.js";
import type { EdgeStream } from "./edge-speech.js";
import { live } from "./state.js";
import { prepareSpeechMarkup, speechFeatures } from "./speech-markup.js";
import type { SpeechFeature, SpeechMarkup } from "../shared/speech-markup.js";

interface DeliveryOptions { rate?: number; volume?: number; markup?: SpeechMarkup; native?: NativeLine }
interface NativeLine { voiceName: string; features: SpeechFeature[]; markup?: SpeechMarkup; mode: string }
interface NativeTiming { completionMs?: number; firstAudioMs?: number; lastAudioMs?: number }
interface SpokenLine {
  id: number;
  voice: string;
  text: string;
  at: number;
  synthMs: number | null;
  requestId: string;
  ssml: boolean | null;
  ssmlDocument: string;
  nativeTiming: NativeTiming | null;
}

/** History/replay presentation only; the submitted speech and exact native document stay unchanged. */
function spokenWords(text: string): string {
  const words = text.replace(/<\/?(speak|prosody|p|s|break|emphasis|say-as|phoneme|sub|voice|lang|mark)(?=[\s/>])(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi,
    (_tag, name: string) => /^(p|s|break)$/i.test(name) ? " " : "");
  if (words === text) return text;
  const entities: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
  return words.replace(/&(amp|lt|gt|quot|apos|#\d+|#x[\da-f]+);/gi, (entity, name: string) => {
    if (name[0] !== "#") return entities[name.toLowerCase()];
    const value = name[1].toLowerCase() === "x" ? parseInt(name.slice(2), 16) : Number(name.slice(1));
    return value > 0 && value <= 0x10ffff && !(value >= 0xd800 && value <= 0xdfff) ? String.fromCodePoint(value) : entity;
  }).replace(/[ \t]+/g, " ").trim();
}

interface Line {
  text: string;
  audio: Promise<Blob | null> | null;
  edge?: EdgeStream;
  provider: string;
  voiceLabel: string;
  windows: Pick<SpeechSettings, "voiceName" | "rate" | "volume">;
  native: NativeLine | Promise<NativeLine>;
  requestId: string;
  attribution?: LogAttribution;
  synthMs?: number;
  nativeId?: string;
  nativeText?: string;
  nativeSsml?: boolean;
  player?: HTMLAudioElement;
  tag?: string;
  done?: () => void;
  cancelled?: boolean;
  reportedPlaying?: boolean;
  reportedFallback?: boolean;
  observations?: SpokenLine[];
}

/** Full request identity for the last-spoken record; speakLabel deliberately stays compact for the status bar. */
function onlineVoiceLabel(line: Line, source?: LogAttribution): string {
  const provider = source?.provider || line.provider;
  const name = provider === "openai" ? "OpenAI"
    : provider === "google" ? "Google"
    : provider === "elevenlabs" ? "ElevenLabs"
    : provider === "azure" ? "Azure"
    : provider === "edge" ? "Edge-TTS" : provider;
  const model = source?.reported_model || source?.model || "", voice = source?.voice || "";
  // Successful IPC replies supply the resolved defaults. Never resolve them again from newer settings.
  if (model || voice) return `${name} ${model}${model && voice ? ", " : ""}${voice}`;
  return provider === line.provider ? line.voiceLabel : name;
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
  private cancelEpoch = 0;

  /** Pending preparation belongs to this cancellation lifecycle, not whichever line starts later. */
  get cancellationEpoch(): number { return this.cancelEpoch; }
  private readonly spokenLines: SpokenLine[] = [];
  private spokenSequence = 0;
  get recentLines(): readonly Readonly<SpokenLine>[] { return this.spokenLines; }
  get lastVoice(): string { return this.spokenLines[0]?.voice || ""; }
  get lastVoiceAt(): number { return this.spokenLines[0]?.at || 0; }
  get lastText(): string { return this.spokenLines[0]?.text || ""; }
  get lastSynthMs(): number | null { return this.spokenLines[0]?.synthMs ?? null; }
  get lastNativeTiming(): NativeTiming | null { return this.spokenLines[0]?.nativeTiming ?? null; }
  get lastSsml(): boolean | null { return this.spokenLines[0]?.ssml ?? null; }
  get lastSsmlDocument(): string { return this.spokenLines[0]?.ssmlDocument || ""; }
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
  noteVoice(name: string, text: string, at = Date.now(), synthMs: number | null = null, requestId = "",
    ssml: boolean | null = null, ssmlDocument = "", nativeTiming: NativeTiming | null = null): void {
    text = spokenWords(text);
    const owner = requestId && this.current?.requestId === requestId ? this.current : undefined;
    const index = this.spokenLines.findIndex(line => line.voice === name && (requestId
      ? line.requestId === requestId
      : !line.requestId && line.at === at && line.text === text));
    // The active request owns its observations beyond display eviction, then releases them at completion/cancel.
    const previous = this.spokenLines[index] ?? owner?.observations?.find(line => line.voice === name);
    if ((previous && index < 0) || at < (previous?.at ?? this.lastVoiceAt)) return;
    if (previous) {
      previous.text = text;
      previous.at = at;
      previous.synthMs = synthMs;
      previous.ssml = ssml;
      previous.ssmlDocument = ssml === true ? ssmlDocument : "";
      previous.nativeTiming = nativeTiming;
    } else {
      const line: SpokenLine = { id: ++this.spokenSequence, voice: name, text, at, synthMs,
        requestId, ssml, ssmlDocument: ssml === true ? ssmlDocument : "", nativeTiming };
      if (owner) (owner.observations ??= []).push(line);
      this.spokenLines.unshift(line);
      if (this.spokenLines.length > 3) this.spokenLines.pop();
    }
    this.renderHistory();
  }

  private renderHistory(): void {
    const host = document.getElementById("sv-history");
    if (!host) return;
    const expanded = new Set([...host.querySelectorAll("details[open]")].map(el => (el.parentElement as HTMLElement).dataset.spokenId));
    host.innerHTML = voiceHistoryHtml();
    for (const row of host.querySelectorAll<HTMLElement>(".voice-last-entry")) {
      if (expanded.has(row.dataset.spokenId)) row.querySelector("details")!.open = true;
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
        this.noteVoice(`${s.actualVoice} (SAPI5)`, cur.nativeText ?? cur.text, Date.now(), s.synthMs, s.requestId,
          cur.nativeSsml ?? null, "", {});
      }
    }
  }


  private voiceError(message: string): void {
    if (Date.now() - this.lastError <= 60_000) return;
    this.lastError = Date.now();
    toast(message, true, 9000);
  }

  /** A coach line. Only the online providers interpret the optional persona style and tone. */
  say(text: string, interrupt = false, style = "", tone = "", overrides?: DeliveryOptions): void {
    if (!text || this.muted) return;
    if (interrupt) this.stop();
    this.enqueue(text, undefined, undefined, style, tone, overrides);
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
    line.observations = undefined;
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

  private enqueue(text: string, tag?: string, done?: () => void, style = "", tone = "", overrides?: DeliveryOptions): void {
    if (this.provider === "windows" && text.length > 4000) {
      const original = text;
      text = Array.from(text).slice(0, 4000).join("");
      if (text !== original && overrides?.markup?.text === original) {
        const spans = Array.isArray(overrides.markup.spans)
          ? overrides.markup.spans.filter((span) => span.end <= 4000) : [];
        overrides = { ...overrides, markup: { text, spans } };
      }
    }
    const line: Line = {
      text, provider: this.provider, voiceLabel: this.onlineLabel, tag, done, audio: null, requestId: crypto.randomUUID(),
      windows: { voiceName: this.voiceName, rate: overrides?.rate ?? this.rate, volume: overrides?.volume ?? this.volume },
      native: this.nativeSnapshot(text, overrides),
    };
    if (line.native instanceof Promise) void line.native.then((snapshot) => { line.native = snapshot; });
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

  private nativeSnapshot(text: string, options?: DeliveryOptions): NativeLine | Promise<NativeLine> {
    if (options?.native) return structuredClone(options.native);
    const selected = this.voiceName;
    const config = live.config?.companion?.voice || {};
    const permissions = structuredClone(config.markup_voices || {});
    const mode = String(config.markup_mode || "ai");
    const markup = options?.markup ? structuredClone(options.markup) : undefined;
    return call("tts:voices", "windows").then((voices) => {
      const voice = selected ? voices?.find((v: { identity?: string; id: string }) => v.identity === selected || v.id === selected)
        : voices?.find((v: { isDefault?: boolean }) => v.isDefault);
      const identity = voice?.identity || "";
      const features = identity ? speechFeatures(permissions[identity]) : [];
      return { voiceName: identity || selected, features, mode,
        markup: prepareSpeechMarkup(text, markup, features, mode) };
    });
  }

  private async windowsSay(line: Line, text = line.text): Promise<boolean> {
    if (line.provider !== "windows" && text.length > 4000) text = Array.from(text).slice(0, 4000).join("");
    line.nativeId = line.requestId;
    line.nativeText = text;
    try {
      const native = line.native instanceof Promise ? await line.native : line.native;
      if (this.current !== line || line.cancelled) return false;
      const markup = text === line.text && line.provider === "windows" ? native.markup
        : prepareSpeechMarkup(text, undefined, native.features, native.mode);
      line.nativeSsml = !!markup;
      const result = await glide.invoke("tts:windowsSpeak", line.nativeId, text,
        { ...line.windows, voiceName: native.voiceName }, markup, native.features);
      const synthMs = this.sapiSynthMs.get(line.requestId) ?? null;
      this.sapiSynthMs.delete(line.requestId);
      if (this.current === line) this.noteVoice(`${result.voice} (SAPI5)`, text, Date.now(), synthMs,
        line.requestId, line.nativeSsml, result.ssmlDocument, {
          completionMs: result.completionMs, firstAudioMs: result.firstAudioMs, lastAudioMs: result.lastAudioMs,
        });
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
      const result = await this.play(fragment.blob, line, line.text.slice(0, fragment.end),
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
      const observed = this.spokenLines.find(entry => entry.requestId === line.requestId && !entry.nativeTiming);
      if (terminal?.reason === "complete" && observed && stream.synthMs !== undefined) {
        observed.synthMs = stream.synthMs;
        this.renderHistory();
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
        this.noteVoice(`${onlineVoiceLabel(line, source)} (online voice)`, text,
          Date.now(), fragmentSynthMs ?? line.synthMs ?? null, line.requestId, false);
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
    this.cancelEpoch++;
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

function voiceAttribution(line?: Readonly<SpokenLine>): string {
  if (!line?.voice) return "nothing spoken yet this session";
  const dur = fmtSynthDuration(line.synthMs);
  const native = line.nativeTiming;
  const completion = fmtSynthDuration(native?.completionMs ?? null);
  const first = native?.firstAudioMs, last = native?.lastAudioMs;
  const audio = first !== undefined && last !== undefined && native?.completionMs !== undefined
    && Number.isFinite(first) && Number.isFinite(last) && Number.isFinite(native.completionMs)
    && 0 <= first && first <= last && last <= native.completionMs
    ? `, first audio ≈${fmtSynthDuration(first)}, last audio ≈${fmtSynthDuration(last)}` : "";
  const timing = audio || (dur ? `, ${native ? "first word reported" : "generated"} in ${dur}` : "");
  const unavailable = native && completion && !audio ? ", audio timing unavailable" : "";
  const format = line.ssml === null ? "" : line.ssml ? ", SSML submitted" : ", Plain text";
  return `${line.voice}${timing}${unavailable}${completion ? `, speech completed in ${completion}` : ""}${format}`;
}

/** Plain-text last-spoken metadata for consumers that do not accept markup. */
export function voiceLastLine(): string {
  const time = speech.lastVoice ? dateTimeText(speech.lastVoiceAt, "shortTime") : "";
  return `${voiceAttribution(speech.recentLines[0])}${time ? `, at ${time}` : ""}`;
}

/** Each observation owns its attribution and document; timestamps remain live semantic leaves. */
export function voiceHistoryHtml(): string {
  if (!speech.recentLines.length) return `<span id="sv-last" class="help">${voiceAttribution()}</span>`;
  return speech.recentLines.map((line, index) => {
    const id = (suffix = "") => index === 0 ? ` id="sv-last${suffix}"` : "";
    return `<div class="voice-last-entry" data-spoken-id="${line.id}">
      <span${id()} class="help voice-line-attribution">${esc(voiceAttribution(line))}, at ${dateTimeHtml(line.at, "shortTime")}</span>
      <span${id("-text")} class="help voice-text voice-line-text">${esc(line.text)}</span>
      <details${id("-ssml")} ${line.ssmlDocument ? "" : "hidden"}><summary>SSML sent to SAPI5</summary><pre class="voice-text"><code${id("-ssml-document")}>${esc(line.ssmlDocument)}</code></pre></details>
    </div>`;
  }).join("");
}
