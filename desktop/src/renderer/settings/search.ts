// Settings search: every word typed must appear in the setting's label, help, key, or its group / section title;
// custom panels match on their title and the words below. Esc clears it.
import { esc } from "../dom.js";
import { Field, Group, SCHEMA, Tab } from "../settings-schema.js";

const SPECIAL_WORDS: Record<string, string> = {
  keys: "api key keys credential credentials token anthropic claude openai gpt gemini google openrouter groq deepgram elevenlabs azure tavily brave paste secret",
  "model-picker": "model models fast deep callouts reviews choose pick claude gpt gemini",
  endpoints: "custom endpoint endpoints ollama lm studio openai compatible local url server model",
  voices: "voice voices speak speech narrator tts text to speech provider windows zira david silero silerotts edge openai elevenlabs azure google rate pitch pauses boundaries ssml markup permissions evidence budget light standard expanded speed volume sample",
  "speech-install": "whisper offline speech recognition push to talk model models download delete repair tiny base small medium distil multilingual",
  "capture-check": "capture calibrate calibration check strip pixels what the coach sees window screenshot",
  "walk-map": "walking map route path directions terrain slope cliffs buildings caves tunnels bridges flight rebuild navigation",
  "data-home": "data folder engine home import runtime python version",
  "overlay-unlock": "overlay move resize unlock position drag",
};

export interface Hit { t: Tab; g: Group; fields: Field[] }

let query = "";
export const searchQuery = () => query;
export const setSearchQuery = (q: string) => { query = q; };

export const queryWords = (): string[] => query.toLowerCase().split(/\s+/).filter(Boolean);
const matchesAll = (hay: string, words: string[]) => { const h = hay.toLowerCase(); return words.every((w) => h.includes(w)); };

/** The groups to show for the search: each with only its matching fields (all of them when the group or section
 * title itself matches), custom panels whole when they match. */
export function searchSchema(): Hit[] {
  const words = queryWords();
  const out: Hit[] = [];
  for (const t of SCHEMA) for (const g of t.groups) {
    const head = `${t.title} ${g.title} ${g.intro || ""}`;
    if (g.special) {
      if (matchesAll(`${head} ${SPECIAL_WORDS[g.special] || ""}`, words)) out.push({ t, g, fields: [] });
      continue;
    }
    const all = g.fields || [];
    const groupHit = matchesAll(`${t.title} ${g.title}`, words);
    const fields = groupHit ? all : all.filter((f) => matchesAll(`${head} ${f.label} ${f.help || ""} ${f.key} ${(f.options || []).map((o) => o[1]).join(" ")}`, words));
    if (fields.length) out.push({ t, g, fields });
  }
  return out;
}

/** Wrap the query's words in <mark> inside the shown labels and group titles (text nodes only). */
export function highlightSearch(root: HTMLElement): void {
  const words = queryWords();
  if (!words.length) return;
  const re = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  root.querySelectorAll<HTMLElement>(".sgroup > h2, .field .lab").forEach((el) => {
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    while (walk.nextNode()) nodes.push(walk.currentNode as Text);
    for (const n of nodes) {
      if (!re.test(n.data)) continue;
      re.lastIndex = 0;
      const span = document.createElement("span");
      span.innerHTML = esc(n.data).replace(re, "<mark>$1</mark>");
      n.replaceWith(...Array.from(span.childNodes));
    }
  });
}
