// Settings > Voice > Coach voice: the narrator's voice service and its voice, speed and volume, a sample, and the
// voice the last line was spoken with. Saved to the app settings as each control changes.
import { updateAppSettings } from "../app-settings.js";
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { speech, voiceLastLine } from "../narrator.js";
import { Group } from "../settings-schema.js";
import { filterEdgeVoices, speechLanguage as language, voiceSample } from "../speech-language.js";
import { live } from "../state.js";
import { Obj, SpeechSettings, TtsProvider, Voice } from "../types.js";

const PROVIDERS: [string, string, string][] = [   // id, label, key
  ["windows", "Windows voices (offline, free)", ""],
  ["edge", "Edge-TTS (online, free, no key)", ""],
  ["openai", "OpenAI (gpt-4o-mini-tts)", "OPENAI_API_KEY"],
  ["elevenlabs", "ElevenLabs", "ELEVENLABS_API_KEY"],
  ["azure", "Azure Speech (neural voices)", "AZURE_SPEECH_KEY"],
  ["google", "Google Cloud Text-to-Speech", "GOOGLE_TTS_API_KEY"],
];
const edgeVoice = (sp: SpeechSettings) => sp.edge?.voice || "";
// The providers with a voice list: where the pick is stored in their settings.
const VOICE_LIST: Record<string, [string, string]> = { edge: ["edge", "voice"], elevenlabs: ["elevenlabs", "voiceId"], azure: ["azure", "voice"], google: ["google", "voice"] };

const field = (label: string, help: string, ctl: string) =>
  `<div class="field"><div class="lab">${label}${help ? `<small>${help}</small>` : ""}</div><div class="ctl">${ctl}</div></div>`;
const keyNote = (env: string) => env ? `<span class="help">Needs <code>${esc(env)}</code> under AI models and keys.</span>` : "";
const options = (list: [string, string][], cur: string) =>
  list.map(([m, l]) => `<option value="${m}" ${m === cur ? "selected" : ""}>${l}</option>`).join("");
/** A voice picker filled by Load voices; until then it shows the saved voice (or the provider's default). */
const voiceList = (help: string, id: string, shown: string) =>
  field("Voice", help, `<select id="sv-voicelist"><option value="${esc(id)}">${esc(shown)}</option></select><button class="btn" id="sv-load">Load voices</button>`);

/** The controls only this provider has. */
function providerFields(prov: string, sp: SpeechSettings): string {
  if (prov === "windows") {
    const vs = speech.voices();
    return field("Voice", "Windows voices on this PC. More: Windows Settings > Time &amp; language > Speech > Add voices.",
      `<select id="sv-voice"><option value="">Windows default</option>${vs.map((v) => `<option ${v.name === sp.voiceName ? "selected" : ""}>${esc(v.name)}</option>`).join("")}</select>`);
  }
  if (prov === "edge") {
    const voice = edgeVoice(sp);
    return voiceList("Filtered by the saved speech language; the current voice stays visible. Free online, no API key; text is sent to Microsoft. This unofficial service may fail or limit usage; the Windows voice is used if it fails.", voice, voice || "Choose a voice")
      + field("Show all voice languages", "Off: filter by the selected coach language.", `<label class="switch"><input type="checkbox" id="sv-all-languages" aria-label="Show all voice languages"><span class="track"></span><span class="state">Off</span></label>`)
      + field("Multilingual voices only", "Show voices marked Multilingual, regardless of locale. Language support varies by voice.", `<label class="switch"><input type="checkbox" id="sv-multilingual-only" aria-label="Multilingual voices only"><span class="track"></span><span class="state">Off</span></label>`);
  }
  if (prov === "openai") {
    const o = sp.openai || {};
    return field("Model", "", `<select id="sv-o-model">${["gpt-4o-mini-tts", "tts-1", "tts-1-hd"].map((m) => `<option ${m === (o.model || "gpt-4o-mini-tts") ? "selected" : ""}>${m}</option>`).join("")}</select>`)
      + field("Voice", "", `<select id="sv-o-voice"></select>`)
      + field("Style", "gpt-4o-mini-tts only: how to speak.", `<input type="text" class="wide" id="sv-o-instr" value="${esc(o.instructions || "")}" placeholder="Calm, concise game coach. Speak quickly.">`);
  }
  if (prov === "elevenlabs") {
    const e = sp.elevenlabs || {};
    return voiceList("Voices in your ElevenLabs account.", e.voiceId || "", e.voiceId || "default (George)")
      + field("Model", "Flash is fastest; Multilingual sounds best.", `<select id="sv-e-model">${options([["eleven_flash_v2_5", "Flash v2.5 (fast)"], ["eleven_turbo_v2_5", "Turbo v2.5"], ["eleven_multilingual_v2", "Multilingual v2"], ["eleven_v3", "v3 (acts the coach's tone; slower)"]], e.model || "eleven_flash_v2_5")}</select>`);
  }
  if (prov === "azure") {
    const a = sp.azure || {};
    return field("Region", "Your Speech resource's region, e.g. eastus, westeurope.", `<input type="text" id="sv-a-region" value="${esc(a.region || "eastus")}" spellcheck="false">`)
      + voiceList("", a.voice || "", a.voice || "en-US-AndrewMultilingualNeural");
  }
  if (prov === "google") {
    const g = sp.google || {};
    return voiceList("Chirp 3 HD voices sound the most natural.", g.voice || "", g.voice || "en-US-Chirp3-HD-Charon");
  }
  return "";
}

export function renderVoices(g: Group, host: HTMLElement): void {
  const sp: SpeechSettings = live.appSettings.speech || {};
  const prov = sp.provider || "windows";
  const save = (patch: Obj) => updateAppSettings({ speech: patch });
  host.innerHTML = field("Voice service", "Online services sound more natural: Edge-TTS is free, while other services may charge per line. If one fails, that line uses the Windows voice.",
      `<select id="sv-prov">${PROVIDERS.map(([id, l]) => `<option value="${id}" ${id === prov ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>${keyNote(PROVIDERS.find((p) => p[0] === prov)![2])}`)
    + providerFields(prov, sp)
    + field("Speed", "", `<input type="range" id="sv-rate" min="0.5" max="2" step="0.05" value="${sp.rate ?? 1}"><span class="rangeval">${sp.rate ?? 1}</span>`)
    + field("Volume", "", `<input type="range" id="sv-vol" min="0" max="1" step="0.05" value="${sp.volume ?? 1}"><span class="rangeval">${sp.volume ?? 1}</span>`)
    + field("Try it", "Uses the saved speech language; save language changes above first. The coach uses this voice when Spoken by is set to this app.", `<button class="btn" id="sv-test">${icon("volume-2")}Play a sample</button>`)
    + field("Last line spoken with", "The online voice or Windows voice Glide used for its last line. WoW's Text to Speech (Options > Accessibility) and Windows Narrator have their own voices.", `<span id="sv-last" class="help">${esc(voiceLastLine())}</span>`);

  $("sv-prov").onchange = async (e) => {
    const provider = (e.target as HTMLSelectElement).value;
    if (provider === "edge" && !edgeVoice(sp).startsWith(`${language()}-`)) {
      const catalogue = await call("tts:voices", "edge");
      if (!catalogue) return;
      const voice = catalogue.find((v) => v.id.startsWith(`${language()}-`));
      if (!voice) { toast("No Edge-TTS voices for the selected speech language."); return; }
      await save({ provider, edge: { voice: voice.id } });
    } else await save({ provider });
    renderVoices(g, host);
  };
  const range = (id: string, key: string) => $(id).addEventListener("change", () => {
    ($(id).nextElementSibling as HTMLElement).textContent = $<HTMLInputElement>(id).value;
    save({ [key]: Number($<HTMLInputElement>(id).value) });
  });
  range("sv-rate", "rate");
  range("sv-vol", "volume");
  $("sv-test").onclick = async () => speech.say(await voiceSample(), true);
  if (prov === "windows") $("sv-voice").onchange = (e) => save({ voiceName: (e.target as HTMLSelectElement).value });
  if (prov === "openai") {
    call("tts:voices", "openai").then((vs) => {
      $("sv-o-voice").innerHTML = (vs || []).map((v) => `<option ${v.id === (sp.openai?.voice || "coral") ? "selected" : ""}>${esc(v.id)}</option>`).join("");
    });
    const o = () => save({ openai: { model: $<HTMLSelectElement>("sv-o-model").value, voice: $<HTMLSelectElement>("sv-o-voice").value, instructions: $<HTMLInputElement>("sv-o-instr").value } });
    ["sv-o-model", "sv-o-voice", "sv-o-instr"].forEach((id) => $(id).addEventListener("change", o));
  }
  if (VOICE_LIST[prov]) bindVoiceList(prov, sp as Obj, save);
  if (prov === "elevenlabs") $("sv-e-model").onchange = (e) => save({ elevenlabs: { ...(sp.elevenlabs || {}), model: (e.target as HTMLSelectElement).value } });
  if (prov === "azure") $("sv-a-region").onchange = (e) => save({ azure: { ...(sp.azure || {}), region: (e.target as HTMLInputElement).value.trim() } });
  if (prov === "windows" && !speech.voices().length && window.speechSynthesis)
    speechSynthesis.addEventListener("voiceschanged", () => renderVoices(g, host), { once: true });
}

/** The provider's voice list: Load voices fills it, a pick saves it in the provider's own settings. */
function bindVoiceList(prov: string, sp: Obj, save: (patch: Obj) => Promise<void>): void {
  const [section, k] = VOICE_LIST[prov];
  $("sv-voicelist").onchange = (e) => save({ [section]: { ...(sp[section] || {}), [k]: (e.target as HTMLSelectElement).value } });
  const select = $<HTMLSelectElement>("sv-voicelist");
  const allLanguages = document.getElementById("sv-all-languages") as HTMLInputElement | null;
  const multilingualOnly = document.getElementById("sv-multilingual-only") as HTMLInputElement | null;
  let catalogue: Voice[] = [];
  const draw = () => {
    const cur = prov === "edge" ? edgeVoice(live.appSettings.speech || {}) : (sp[section] || {})[k] || "";
    const voices = prov === "edge"
      ? filterEdgeVoices(catalogue, language(), !!allLanguages?.checked, !!multilingualOnly?.checked) : catalogue;
    const saved = cur && !voices.some((v) => v.id === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)} (saved)</option>` : "";
    select.innerHTML = saved + voices.map((v) => `<option value="${esc(v.id)}" ${v.id === cur ? "selected" : ""}>${esc(v.name)}</option>`).join("")
      + (voices.length ? "" : `<option value="" disabled>No matching voices</option>`);
  };
  const load = async (notify: boolean) => {
    const vs = await call("tts:voices", prov as TtsProvider);
    if (!vs || !select.isConnected) return;
    catalogue = vs;
    if (prov === "edge" && !edgeVoice(live.appSettings.speech || {})) {
      const voice = catalogue.find((v) => v.id.startsWith(`${language()}-`));
      if (voice) await save({ edge: { voice: voice.id } });
    }
    draw();
    if (notify) toast(`${catalogue.length} voices loaded`);
  };
  for (const toggle of [allLanguages, multilingualOnly]) if (toggle) toggle.onchange = () => {
    toggle.parentElement!.querySelector(".state")!.textContent = toggle.checked ? "On" : "Off";
    if (allLanguages) allLanguages.disabled = !!multilingualOnly?.checked;
    draw();
  };
  $("sv-load").onclick = () => load(true);
  if (prov === "edge") void load(false);
}
