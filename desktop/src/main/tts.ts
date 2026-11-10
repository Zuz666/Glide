// Online speech synthesis: requests audio and returns an MP3 to the renderer for playback.
// Native Windows SAPI5 enumeration and playback live in windows-tts.ts.

import type { SpeechSettings, TtsProvider, Voice } from "../shared/ipc";

type KeyProvider = Exclude<TtsProvider, "windows" | "edge">;
export const TTS_KEYS: Record<KeyProvider, string> = {
  openai: "OPENAI_API_KEY", elevenlabs: "ELEVENLABS_API_KEY", azure: "AZURE_SPEECH_KEY", google: "GOOGLE_TTS_API_KEY",
};

export const OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"];

const TIMEOUT = 15000;

function xml(s: string): string {
  return s.replace(/[<>&'"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", "'": "&apos;", '"': "&quot;" }[c]!));
}

async function ok(r: Response, what: string): Promise<Response> {
  if (!r.ok) throw new Error(`${what} ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r;
}

/** A provider's answer as JSON, or an error with its status and the start of its body. */
async function json<T>(url: string, what: string, init: RequestInit): Promise<T> {
  return (await (await ok(await fetch(url, init), what)).json()) as T;
}

/** A provider's answer as audio bytes. */
async function audio(url: string, what: string, init: RequestInit): Promise<Buffer> {
  return Buffer.from(await (await ok(await fetch(url, init), what)).arrayBuffer());
}

/** The text for ElevenLabs v3, which acts inline audio tags: the line's tone as one ("[dry, amused] Turn in the
 * quest."). The tag is kept short and plain (four words at most; letters, spaces, commas and hyphens; lower case) so
 * a stray instruction can't ride in; no tone, no tag. */
export function v3Text(text: string, tone = ""): string {
  // letters (any script, accents kept), apostrophes and hyphens; commas and anything else split words; up to 6
  // words (the coach is asked for 2-6); a longer tone is no tag rather than half of one; brackets never get through
  const words = String(tone || "").normalize("NFC").toLowerCase().replace(/[’]/g, "'")
    .replace(/[^\p{L}\p{M}'\s-]+/gu, " ").split(/\s+/)
    .map((w) => w.replace(/^['-]+|['-]+$/g, "")).filter((w) => /\p{L}/u.test(w));
  if (!words.length || words.length > 6) return text;
  return `[${words.join(" ")}] ${text}`;
}

type Online = Exclude<TtsProvider, "windows">;
type Synth = (s: SpeechSettings, text: string, key: string, signal: AbortSignal, style: string, tone: string) => Promise<Buffer>;
type VoiceList = (key: string, signal: AbortSignal, region?: string) => Promise<Voice[]>;

const SYNTH: Record<Online, Synth> = {
  async edge(s, text, _key, signal) {
    // The Python paths depend on Electron; other providers and their Node-only tests don't need that runtime.
    const { edgeSynth } = await import("./edge-tts.js");
    return edgeSynth(text, s.edge?.voice || "", signal);
  },
  async openai(s, text, key, signal, style) {
    const o = s.openai || {};
    const model = o.model || "gpt-4o-mini-tts";
    const body: Record<string, unknown> = { model, voice: o.voice || "coral", input: text, response_format: "mp3" };
    // the coach's persona (style) is performed instead of the usual instructions: a calm voice note would fight it
    const instructions = style || o.instructions;
    if (instructions && model.includes("gpt")) body.instructions = instructions;
    return audio("https://api.openai.com/v1/audio/speech", "OpenAI", {
      method: "POST", signal, headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
      body: JSON.stringify(body) });
  },
  async elevenlabs(s, text, key, signal, _style, tone) {
    const e = s.elevenlabs || {};
    const voice = e.voiceId || "JBFqnCBsd6RMkjVDRZzb";     // "George", a default ElevenLabs voice
    const model = e.model || "eleven_flash_v2_5";
    // v3 performs the line's tone as an audio tag; the other models would read the tag out, so they get the words
    const input = /v3/i.test(model) ? v3Text(text, tone) : text;
    return audio(`https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(voice)}?output_format=mp3_44100_128`, "ElevenLabs", {
      method: "POST", signal, headers: { "xi-api-key": key, "content-type": "application/json" },
      body: JSON.stringify({ text: input, model_id: model }) });
  },
  async azure(s, text, key, signal) {
    const a = s.azure || {};
    const region = (a.region || "eastus").trim();
    const voice = a.voice || "en-US-AndrewMultilingualNeural";
    const ssml = `<speak version="1.0" xml:lang="en-US"><voice name="${xml(voice)}">${xml(text)}</voice></speak>`;
    return audio(`https://${encodeURIComponent(region)}.tts.speech.microsoft.com/cognitiveservices/v1`, "Azure", {
      method: "POST", signal, headers: { "Ocp-Apim-Subscription-Key": key, "content-type": "application/ssml+xml",
                                         "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "user-agent": "glide" },
      body: ssml });
  },
  async google(s, text, key, signal) {
    const voice = s.google?.voice || "en-US-Chirp3-HD-Charon";
    const lang = voice.split("-").slice(0, 2).join("-");
    const d = await json<{ audioContent?: string }>(`https://texttospeech.googleapis.com/v1/text:synthesize?key=${encodeURIComponent(key)}`, "Google", {
      method: "POST", signal, headers: { "content-type": "application/json" },
      body: JSON.stringify({ input: { text }, voice: { languageCode: lang, name: voice }, audioConfig: { audioEncoding: "MP3" } }) });
    if (!d.audioContent) throw new Error("Google: no audio in the answer");
    return Buffer.from(d.audioContent, "base64");
  },
};

// OpenAI offers a fixed list; the other providers are asked for theirs.
const VOICE_LISTS: Record<Exclude<KeyProvider, "openai">, VoiceList> = {
  async elevenlabs(key, signal) {
    const d = await json<{ voices?: { voice_id: string; name: string; labels?: Record<string, string> }[] }>(
      "https://api.elevenlabs.io/v1/voices", "ElevenLabs", { signal, headers: { "xi-api-key": key } });
    return (d.voices || []).map((v) => ({ id: v.voice_id, name: [v.name, v.labels?.accent, v.labels?.gender].filter(Boolean).join(" · ") }));
  },
  async azure(key, signal, region) {
    const d = await json<{ ShortName: string; LocalName: string; Locale: string; Gender: string }[]>(
      `https://${encodeURIComponent(region || "eastus")}.tts.speech.microsoft.com/cognitiveservices/voices/list`, "Azure",
      { signal, headers: { "Ocp-Apim-Subscription-Key": key } });
    return d.filter((v) => v.Locale.startsWith("en-")).map((v) => ({ id: v.ShortName, name: `${v.ShortName} · ${v.Gender}` }));
  },
  async google(key, signal) {
    const d = await json<{ voices?: { name: string; ssmlGender: string }[] }>(
      `https://texttospeech.googleapis.com/v1/voices?languageCode=en&key=${encodeURIComponent(key)}`, "Google", { signal });
    return (d.voices || []).map((v) => ({ id: v.name, name: `${v.name} · ${v.ssmlGender.toLowerCase()}` }));
  },
};

/** MP3 audio for `text` from an online provider; `style` = how to perform it (the coach's persona; OpenAI only),
 * `tone` = how to deliver this one line (in character; an audio tag for ElevenLabs v3, the others don't use it). */
export async function synth(s: SpeechSettings, text: string, key: string, style = "", tone = ""): Promise<Buffer> {
  const p = s.provider;
  if (p && Object.hasOwn(TTS_KEYS, p) && !key) {
    throw new Error(`${p}: no ${TTS_KEYS[p as KeyProvider]} (Settings > API keys)`);
  }
  const signal = AbortSignal.timeout(TIMEOUT);
  if (p && Object.hasOwn(SYNTH, p)) return SYNTH[p as Online](s, text, key, signal, style, tone);
  throw new Error(`unknown voice provider ${p}`);
}

/** The voices a provider offers: [{id, name}]. */
export async function voices(provider: TtsProvider, key: string, region?: string): Promise<Voice[]> {
  const signal = AbortSignal.timeout(TIMEOUT);
  if (provider === "openai") return OPENAI_VOICES.map((v) => ({ id: v, name: v }));
  if (provider === "edge") {
    const { edgeVoices } = await import("./edge-tts.js");
    return edgeVoices(signal);
  }
  if (Object.hasOwn(TTS_KEYS, provider) && !key) throw new Error(`no ${TTS_KEYS[provider as KeyProvider]} set`);
  if (Object.hasOwn(VOICE_LISTS, provider)) return VOICE_LISTS[provider as keyof typeof VOICE_LISTS](key, signal, region);
  return [];
}
