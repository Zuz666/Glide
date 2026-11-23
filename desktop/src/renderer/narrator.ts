// The narrator: speaks the coach's lines ("speak" events on the engine's event stream) with the voice picked in
// Settings > Voice.
//
// Windows voices are spoken here (speechSynthesis); online providers (Edge-TTS, OpenAI, ElevenLabs, Azure, Google) return MP3
// from the main process, which is played in order. Each line's audio is requested as soon as it's queued, so the
// next one is usually ready when the current one ends. A failed request speaks that line with the Windows voice.
//
// Other pages speak tagged lines (speakAndWait: the Guides page's narration) that say when they've finished, and
// cancel(tag) silences only those. A tagged Windows line is handed to speechSynthesis only when nothing the narrator
// gave it is still pending, so cancelling it never drops a coach line: the coach's lines queued behind it are handed
// over again.
//
// The player can talk over it: holding push-to-talk (the "ptt" event's "listening", coach.ts) calls bargeIn(), which
// stops the line being said and drops the queued ones at once.
import { call, glide, ipcError } from "./bridge.js";
import { toast } from "./dom.js";
import { Obj, SpeechSettings } from "./types.js";

interface Line {
  text: string;
  audio: Promise<Blob | null>;
  tag?: string;               // a tagged line (speakAndWait); the coach's lines have none
  done?: () => void;          // resolves its speakAndWait
  cancelled?: boolean;        // cancel(tag) while it was the line being spoken
}
/** An utterance the narrator handed to speechSynthesis and that hasn't ended. */
interface Handed { u: SpeechSynthesisUtterance; tag: string; resolve: () => void }

class Narrator {
  private provider = "windows";
  private voiceName = "";
  private onlineLabel = "";
  private rate = 1;
  private volume = 1;
  private queue: Line[] = [];
  private playing: HTMLAudioElement | null = null;
  private playingLine: Line | null = null;
  private handed: Handed[] = [];              // utterances given to speechSynthesis, in order, not ended yet
  private waiting: { text: string; tag: string; done: () => void }[] = [];   // tagged Windows lines not handed yet
  private pumping = false;
  private muted = false;                      // the narrator is muted (footer button, tray, Ctrl+Alt+M)
  private gen = 0;
  private bargedAt = 0;                       // when the player last talked over it (bargeIn)                            // bumped by stop(): stale plays don't start
  lastVoice = "";                             // the voice the last line asked Windows for (Settings > Voice, Logs)
  lastVoiceAt = 0;
  private lastError = 0;

  /** Take the voice settings (Settings > Voice, the coach page's slider, mute). Muting stops what's being said. */
  configure(muted: boolean, sp: SpeechSettings): void {
    if (muted && !this.muted) this.stop();
    this.muted = muted;
    this.provider = sp.provider || "windows";
    this.onlineLabel = speakLabel(sp);
    this.voiceName = sp.voiceName || "";
    this.rate = Number(sp.rate ?? 1);
    this.volume = Number(sp.volume ?? 1);
  }

  voices(): SpeechSynthesisVoice[] {
    return window.speechSynthesis ? speechSynthesis.getVoices() : [];
  }

  /** The Windows voices, once Chromium has loaded them (empty for a moment after start: speaking then would use
   * the default voice instead of the chosen one). Waits for voiceschanged, three seconds at most. */
  private voicesReady(): Promise<SpeechSynthesisVoice[]> {
    if (!window.speechSynthesis) return Promise.resolve([]);
    const now = speechSynthesis.getVoices();
    if (now.length) return Promise.resolve(now);
    return new Promise((resolve) => {
      const done = () => { speechSynthesis.removeEventListener("voiceschanged", done); clearTimeout(t); resolve(speechSynthesis.getVoices()); };
      const t = setTimeout(done, 3000);
      speechSynthesis.addEventListener("voiceschanged", done);
    });
  }

  /** The chosen voice: by exact name, else by its first part ("Microsoft Zira"), as Windows may relabel it. */
  private pickVoice(list: SpeechSynthesisVoice[]): SpeechSynthesisVoice | undefined {
    if (!this.voiceName) return undefined;
    const exact = list.find((x) => x.name === this.voiceName);
    if (exact) return exact;
    const stem = this.voiceName.split(" - ")[0].trim().toLowerCase();
    return list.find((x) => x.name.toLowerCase().startsWith(stem));
  }

  private async windowsSay(text: string, tag = ""): Promise<void> {
    if (!window.speechSynthesis) return;
    const gen = this.gen;
    const list = await this.voicesReady();
    if (gen !== this.gen) return;                 // stopped or interrupted while the voices loaded
    return new Promise((resolve) => this.handOver(this.utterance(text, list), tag, resolve));
  }

  private utterance(text: string, list: SpeechSynthesisVoice[]): SpeechSynthesisUtterance {
    const u = new SpeechSynthesisUtterance(text);
    const v = this.pickVoice(list);
    if (v) u.voice = v;
    this.noteVoice(v ? v.name : `the Windows default${this.voiceName ? ` (${this.voiceName} not found among ${list.length} voices)` : ""}`);
    u.rate = this.rate;
    u.volume = this.volume;
    return u;
  }

  /** Give speechSynthesis an utterance, kept in `handed` until it ends (or fails), then resolve. */
  private handOver(u: SpeechSynthesisUtterance, tag: string, resolve: () => void): void {
    const h: Handed = { u, tag, resolve };
    u.onend = u.onerror = () => {
      this.handed = this.handed.filter((x) => x !== h);
      resolve();
      this.pump();
    };
    this.handed.push(h);
    speechSynthesis.speak(u);
  }

  /** Hand the next tagged Windows line over once nothing the narrator gave speechSynthesis is pending. */
  private async pump(): Promise<void> {
    if (this.handed.length && window.speechSynthesis && !speechSynthesis.speaking && !speechSynthesis.pending) {
      for (const h of this.handed) h.resolve();   // ended without telling (Chromium can drop onend): not pending
      this.handed = [];
    }
    if (this.pumping || !this.waiting.length || this.handed.length) return;
    this.pumping = true;
    const list = await this.voicesReady();
    this.pumping = false;
    if (this.handed.length) return;               // a coach line went first: its end pumps again
    const w = this.waiting.shift();
    if (w) this.handOver(this.utterance(w.text, list), w.tag, w.done);
  }

  /** Log the voice in use: once per session and whenever it changes, so a wrong voice can be traced. */
  private noteVoice(name: string): void {
    if (name !== this.lastVoice) call("app:note", `[voice] speaking with ${name}`);
    this.lastVoice = name;
    this.lastVoiceAt = Date.now();
    const el = document.getElementById("sv-last");
    if (el) el.textContent = voiceLastLine();
  }

  /** A coach line; `style` = how to perform it (the character the coach plays; the OpenAI voice takes it), `tone` =
   * how to deliver this one line (in character; ElevenLabs v3 acts it as an audio tag). */
  say(text: string, interrupt = false, style = "", tone = ""): void {
    if (!text || this.muted) return;
    if (interrupt) this.stop();
    if (this.provider === "windows") {
      this.windowsSay(text);
      return;
    }
    this.enqueue(text, undefined, undefined, style, tone);
  }

  /** The player started talking (push-to-talk is held): stop at once, like someone talked over on a call. The line
   * being said stops and the queued ones are dropped, the coach's and the tagged ones alike (a page waiting on one
   * goes on, as with stop()); the lines that come after (the coach's answer) are said as usual. */
  bargeIn(): void {
    this.bargedAt = Date.now();
    this.stop();
  }

  /** Whether the player talked over the narrator since `t` (Date.now()): a page waiting on a line then pauses
   * instead of going on to the next (the Guides walk). */
  bargedSince(t: number): boolean {
    return this.bargedAt >= t;
  }

  /** Whether a line would be heard now: not muted, and a voice to speak with. */
  canSpeak(): boolean {
    return !this.muted && (this.provider !== "windows" || !!window.speechSynthesis);
  }

  /** Speak a line for another page and resolve when it has been spoken: at its end (or failure), at once when muted
   * or empty, and when cancel(tag) or stop() drops it. It waits behind the lines already queued. */
  speakAndWait(text: string, tag: string): Promise<void> {
    if (!text || !this.canSpeak()) return Promise.resolve();
    return new Promise((resolve) => {
      if (this.provider === "windows") {
        this.waiting.push({ text, tag, done: resolve });
        this.pump();
      } else {
        this.enqueue(text, tag, resolve);
      }
    });
  }

  /** Drop this tag's queued lines and stop its line being spoken, leaving the coach's lines as they are. */
  cancel(tag: string): void {
    for (const w of this.waiting) if (w.tag === tag) w.done();
    this.waiting = this.waiting.filter((w) => w.tag !== tag);
    this.queue = this.queue.filter((l, i) => {
      if (l.tag !== tag) return true;
      if (i > 0) { l.done?.(); return false; }
      l.cancelled = true;                         // the line being spoken: next() moves on once it stops
      if (this.playingLine === l) this.playing?.pause();
      return true;
    });
    if (!this.handed.some((h) => h.tag === tag) || !window.speechSynthesis) return;
    // speechSynthesis can only cancel everything: cancel, then hand the other lines over again, in order
    const all = this.handed;
    this.handed = [];
    for (const h of all) h.u.onend = h.u.onerror = null;
    speechSynthesis.cancel();
    for (const h of all) {
      if (h.tag === tag) { h.resolve(); continue; }
      const u = new SpeechSynthesisUtterance(h.u.text);
      u.voice = h.u.voice;
      u.rate = h.u.rate;
      u.volume = h.u.volume;
      this.handOver(u, h.tag, h.resolve);
    }
    this.pump();
  }

  private enqueue(text: string, tag?: string, done?: () => void, style = "", tone = ""): void {
    const args: [string, string?, string?] = tone ? [text, style, tone] : style ? [text, style] : [text];
    const audio = glide.invoke("tts:synth", ...args)
      .then((r) => new Blob([r.data as BlobPart], { type: r.mime }))
      .catch((e) => {
        if (Date.now() - this.lastError > 60_000) {             // say why once a minute, not on every line
          this.lastError = Date.now();
          toast(`Voice (${this.provider}) failed, using the Windows voice: ${ipcError(e)}`, true, 9000);
        }
        return null;
      });
    this.queue.push({ text, audio, tag, done });
    if (this.queue.length === 1 && !this.playing) this.next();
  }

  /** Speak the first queued line (its online audio, or the Windows voice when that failed), then the next. */
  private async next(): Promise<void> {
    const line = this.queue[0];
    if (!line) return;
    const gen = this.gen;
    const blob = await line.audio;
    if (gen !== this.gen) return;
    if (line.cancelled) { /* cancelled while its audio was coming */ }
    else if (!blob) await this.windowsSay(line.text, line.tag);
    else { this.playingLine = line; await this.play(blob); this.playingLine = null; }
    if (gen !== this.gen) return;
    line.done?.();
    this.queue.shift();
    this.next();
  }

  private async play(blob: Blob): Promise<void> {
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.volume = Math.max(0, Math.min(1, this.volume));
    a.playbackRate = this.rate;
    a.preservesPitch = true;
    this.noteVoice(`${this.provider === "edge" ? this.onlineLabel : this.provider} (online voice)`);
    this.playing = a;
    await new Promise<void>((resolve) => {
      a.onended = a.onerror = a.onpause = () => resolve();
      a.play().catch(() => resolve());
    });
    URL.revokeObjectURL(url);
    this.playing = null;
  }

  stop(): void {
    this.gen++;
    for (const l of this.queue) l.done?.();       // a page waiting on a line goes on
    for (const w of this.waiting) w.done();
    for (const h of this.handed) h.resolve();
    this.handed = [];
    this.waiting = [];
    this.playingLine = null;
    this.queue = [];
    if (this.playing) {
      this.playing.pause();
      this.playing = null;
    }
    window.speechSynthesis?.cancel();
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
  return v ? `Windows, ${v}` : "Windows voice";
}

/** "Last line spoken with" (Settings > Voice): the voice and the time. */
export function voiceLastLine(): string {
  if (!speech.lastVoice) return "nothing spoken yet this session";
  return `${speech.lastVoice}, ${new Date(speech.lastVoiceAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}
