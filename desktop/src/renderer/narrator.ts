// The narrator: speaks the coach's lines ("speak" events on the engine's event stream) with the voice picked in
// Settings > Voice.
//
// Windows voices use the main process's persistent SAPI5 worker. Online providers return audio, prefetched as
// lines are queued and played here in order; a failed online request uses that same native Windows path.
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

interface Line {
  text: string;
  audio: Promise<Blob | null> | null;
  provider: string;
  voiceLabel: string;
  windows: Pick<SpeechSettings, "voiceName" | "rate" | "volume">;
  requestId?: string;
  nativeId?: string;
  player?: HTMLAudioElement;
  tag?: string;
  done?: () => void;
  cancelled?: boolean;
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
  noteVoice(name: string, text: string, at = Date.now()): void {
    if (at < this.lastVoiceAt) return;
    if (name !== this.lastVoice) call("app:note", `[voice] speaking with ${name}`);
    this.lastVoice = name;
    this.lastVoiceAt = at;
    this.lastText = text;
    const el = document.getElementById("sv-last");
    if (el) el.textContent = voiceLastLine();
    const line = document.getElementById("sv-last-text");
    if (line) {
      line.textContent = text;
      line.hidden = false;
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

  private cancelLine(line: Line): void {
    line.cancelled = true;
    if (line.nativeId) void call("tts:windowsStop", line.nativeId);
    line.player?.pause();
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
      text, provider: this.provider, voiceLabel: this.onlineLabel, tag, done, audio: null,
      windows: { voiceName: this.voiceName, rate: this.rate, volume: this.volume },
    };
    if (line.provider !== "windows") {
      const args: [string, string?, string?] = tone ? [text, style, tone] : style ? [text, style] : [text];
      line.audio = glide.invoke("tts:synth", ...args)
        .then((r) => new Blob([r.data as BlobPart], { type: r.mime }))
        .catch((e) => {
          line.requestId = /\bTTS #(\d+):/.exec(ipcError(e))?.[1];
          if (!line.cancelled) this.voiceError(`Voice (${line.provider}) failed, using the Windows voice: ${ipcError(e)}`);
          return null;
        });
    }
    this.queue.push(line);
    void this.next();
  }

  private async windowsSay(line: Line): Promise<void> {
    line.nativeId = crypto.randomUUID();
    try {
      const result = await glide.invoke("tts:windowsSpeak", line.nativeId, line.text, line.windows);
      if (this.current === line) this.noteVoice(`${result.voice} (SAPI5)`, line.text);
    } catch (error) {
      if (this.current === line && !line.cancelled) {
        this.voiceError(`Windows SAPI5 voice failed: ${ipcError(error)}`);
      }
    } finally {
      line.nativeId = undefined;
    }
  }

  private async next(): Promise<void> {
    if (this.current) return;
    const line = this.queue.shift();
    if (!line) return;
    this.current = line;
    const blob = line.audio ? await line.audio : null;
    if (this.current !== line) return;
    if (blob) {
      await this.play(blob, line);
    } else {
      if (line.provider !== "windows") {
        call("app:note", `[voice] TTS #${line.requestId || "unknown"} fallback from=${line.provider} to=sapi5 requested_voice=${JSON.stringify(line.windows.voiceName || "default")}`);
      }
      await this.windowsSay(line);
    }
    if (this.current !== line) return;
    this.finish(line);
    this.current = null;
    void this.next();
  }

  private async play(blob: Blob, line: Line): Promise<void> {
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    line.player = a;
    a.volume = Math.max(0, Math.min(1, line.windows.volume ?? 1));
    a.playbackRate = line.windows.rate ?? 1;
    a.preservesPitch = true;
    call("app:note", `[voice] playback start provider=${line.provider} audio_bytes=${blob.size}`);
    await new Promise<void>((resolve) => {
      let finished = false;
      const finish = (result: string) => {
        if (finished) return;
        finished = true;
        call("app:note", `[voice] playback ${result} provider=${line.provider}`);
        resolve();
      };
      a.onended = () => finish("ended");
      a.onerror = () => finish(`failed media_code=${a.error?.code || 0}`);
      a.onpause = () => finish("paused");
      a.play().then(() => {
        if (this.current === line) this.noteVoice(`${line.provider === "edge" ? line.voiceLabel : line.provider} (online voice)`, line.text);
      }).catch((e: unknown) => finish(`rejected reason=${e instanceof Error
        && ["NotAllowedError", "NotSupportedError", "AbortError"].includes(e.name) ? e.name : "Error"}`));
    });
    URL.revokeObjectURL(url);
    line.player = undefined;
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

/** "Last line spoken with" (Settings > Voice): the voice and the time. */
export function voiceLastLine(): string {
  if (!speech.lastVoice) return "nothing spoken yet this session";
  return `${speech.lastVoice}, ${new Date(speech.lastVoiceAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}
