// Language-neutral labels and voice samples shared by Settings and first-run setup.
import { toast } from "./dom.js";
import { coachRunning, enginePost, JSON_HEADERS, live } from "./state.js";
import type { Voice } from "./types.js";

export function languageCode(value: unknown): string {
  const tag = typeof value === "string" ? value.trim().toLowerCase() : "";
  return /^[a-z]{2,3}(?:[-_][a-z0-9]{1,8})*$/.test(tag) ? tag.split(/[-_]/)[0] : "en";
}

export const speechLanguage = () => languageCode(live.config.companion?.voice?.language);

/** Multilingual catalogue names describe a voice's capability, not its locale prefix. */
export function filterEdgeVoices(voices: Voice[], language: string, allLanguages: boolean, multilingualOnly: boolean): Voice[] {
  if (multilingualOnly) return voices.filter((v) => /multilingual/i.test(v.id));
  return allLanguages ? voices : voices.filter((v) => v.id.startsWith(`${language}-`));
}

// WoW client languages. Regional variants remain choices in the voice catalogue.
// Names come from platform locale data; the same language set can support future interface localization.
const codes = ["en", "de", "fr", "es", "pt", "it", "ru", "ko", "zh"];
const englishNames = new Intl.DisplayNames(["en"], { type: "language" });
export const LANGUAGE_OPTIONS: [string, string][] = codes.map((code): [string, string] => {
  const name = englishNames.of(code) || code;
  const native = new Intl.DisplayNames([code], { type: "language" }).of(code) || name;
  return [code, name === native ? name : `${name} — ${native}`];
}).sort((a, b) => a[1].localeCompare(b[1], "en"));

const SAMPLE = "Next: turn in Kobold Camp Cleanup to Marshal McBride, 40 yards north.";

/** Samples use the same LLM localization as coach messages; without a coach they stay English. */
export async function voiceSample(): Promise<string> {
  if (!coachRunning()) return SAMPLE;
  try {
    const response = await enginePost("/localize", { text: SAMPLE }, JSON_HEADERS);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json() as { ok?: boolean; text?: string };
    if (!body.ok || typeof body.text !== "string" || !body.text.trim()) throw new Error("Invalid translation response");
    return body.text;
  } catch (error) {
    console.warn("Voice sample localization failed", error);
    toast("Translation unavailable; playing the English sample.");
    return SAMPLE;
  }
}
