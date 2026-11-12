// Settings > Voice > Coach voice: the narrator's voice service and its voice, speed and volume, a sample, and the
// last spoken text and voice. Saved to the app settings as each control changes.
import { updateAppSettings } from "../app-settings.js";
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { speech, voiceLastLine } from "../narrator.js";
import { Field, Group } from "../settings-schema.js";
import { filterEdgeVoices, LANGUAGE_OPTIONS, languageCode, speechLanguage as language, voiceSample } from "../speech-language.js";
import { live } from "../state.js";
import { bindField, edits, fieldHtml, getPath, updateDirty } from "./fields.js";
import { usesAppWindowsVoice } from "../../shared/windows-process-voice.mjs";
import { keyRequirementHtml, loadConfiguredKeys } from "./key-links.js";
import { Obj, SpeechSettings, TtsProvider, Voice } from "../types.js";

const PROVIDERS: [string, string, string][] = [   // id, label, key
  ["windows", "Windows voices (SAPI5, offline)", ""],
  ["edge", "Edge-TTS (experimental, online)", ""],
  ["openai", "OpenAI (gpt-4o-mini-tts)", "OPENAI_API_KEY"],
  ["elevenlabs", "ElevenLabs", "ELEVENLABS_API_KEY"],
  ["azure", "Azure Speech (neural voices)", "AZURE_SPEECH_KEY"],
  ["google", "Google Cloud Text-to-Speech", "GOOGLE_TTS_API_KEY"],
];
const edgeVoice = (sp: SpeechSettings) => sp.edge?.voice || "";
// The providers with a voice list: where the pick is stored in their settings.
const VOICE_LIST: Record<string, [string, string]> = { edge: ["edge", "voice"], elevenlabs: ["elevenlabs", "voiceId"], azure: ["azure", "voice"], google: ["google", "voice"] };
let sampleSource: "template" | "last" = "template";
let sampleObserver: MutationObserver | null = null;

function sampleLanguageHelp(): string {
  const saved = language();
  const selected = edits.has("companion.voice.language") ? languageCode(edits.get("companion.voice.language")) : saved;
  const label = (code: string) => `${LANGUAGE_OPTIONS.find(([value]) => value === code)?.[1] || code} (${code})`;
  return selected !== saved
    ? `Selected language: ${label(selected)} (not saved). Save to update the template; currently saved: ${label(saved)}. Replay keeps the original text.`
    : `Saved speech language: ${label(saved)}. Template translation requires the coach; replay keeps the original text.`;
}

function bindSample(host: HTMLElement): void {
  const source = host.querySelector<HTMLSelectElement>("#sv-sample-source")!;
  const button = host.querySelector<HTMLButtonElement>("#sv-test")!;
  const preview = host.querySelector<HTMLElement>("#sv-sample")!;
  const lastText = host.querySelector<HTMLElement>("#sv-last-text")!;
  let template: string | undefined;
  let preparing = false;
  source.value = sampleSource;
  const draw = () => {
    const text = sampleSource === "last" ? speech.lastText : template;
    preview.textContent = text || (sampleSource === "last" ? "No line has been spoken in this session." : "Preparing template…");
    button.disabled = !text?.trim();
    button.querySelector("span")!.textContent = sampleSource === "last" ? "Play last line" : "Play sample";
    lastText.hidden = !speech.lastText || sampleSource === "last";
  };
  const prepare = () => {
    if (sampleSource !== "template" || preparing || template !== undefined) return;
    preparing = true;
    void voiceSample().then((text) => {
      if (!button.isConnected) return;
      template = text;
      preparing = false;
      draw();
    });
  };
  source.onchange = () => {
    sampleSource = source.value === "last" ? "last" : "template";
    prepare();
    draw();
  };
  button.onclick = () => {
    const text = sampleSource === "last" ? speech.lastText : template;
    if (!text?.trim()) return;
    const rate = edits.has("app:speech.rate") ? Number(edits.get("app:speech.rate") ?? 1) : undefined;
    const volume = edits.has("app:speech.volume") ? Number(edits.get("app:speech.volume") ?? 1) : undefined;
    speech.say(text, true, "", "", { rate, volume });
  };
  sampleObserver = new MutationObserver(draw);
  sampleObserver.observe(lastText, { childList: true, characterData: true, subtree: true });
  prepare();
  draw();
}

function processFields(g: Group, sp: SpeechSettings): Field[] {
  return (g.fields || []).map((f) => {
    if (f.key === "app:speech.windowsProcessUseAppVoice")
      return { ...f, def: !usesAppWindowsVoice(sp, getPath(live.config, "companion.voice.voice")) };
    const voice = String((edits.has(f.key) ? edits.get(f.key) : getPath(live.config, f.key)) ?? "");
    return { ...f, options: [["", "Windows default"], ...(voice ? [[voice, voice] as [string, string]] : [])] };
  });
}

function bindProcessVoice(fields: Field[], host: HTMLElement): void {
  fields.forEach(bindField);
  const toggle = host.querySelector<HTMLInputElement>('[data-key="app:speech.windowsProcessUseAppVoice"] input');
  const row = host.querySelector<HTMLElement>('[data-key="companion.voice.voice"]');
  const select = row?.querySelector<HTMLSelectElement>("select");
  if (!toggle || !row || !select) return;
  const sync = () => {
    row.hidden = !toggle.checked;
    select.disabled = !toggle.checked || select.dataset.loading === "true";
    if (!toggle.checked) {
      edits.delete("companion.voice.voice");
      updateDirty();
    }
  };
  toggle.addEventListener("change", sync);
  sync();
}

const field = (label: string, help: string, ctl: string) =>
  `<div class="field"><div class="lab">${label}${help ? `<small>${help}</small>` : ""}</div><div class="ctl">${ctl}</div></div>`;
const keyNote = (env: string) => env ? `<div id="sv-key-note" class="ctl-subnote">${keyRequirementHtml(env, null)}</div>` : "";
const options = (list: [string, string][], cur: string) =>
  list.map(([m, l]) => `<option value="${m}" ${m === cur ? "selected" : ""}>${l}</option>`).join("");
/** A voice picker filled by Load voices; until then it shows the saved voice (or the provider's default). */
const voiceList = (help: string, id: string, shown: string) =>
  field("Voice", help, `<select id="sv-voicelist"><option value="${esc(id)}">${esc(shown)}</option></select><button class="btn" id="sv-load">Load voices</button>`);
const VOICE_SETUP_GUIDE = `<details class="keyhow voice-setup-guide"><summary>How to set up free voices (Windows SAPI5, SileroTTS &amp; Edge-TTS)</summary>
  <p class="keyfree"><b>Offline speech · Windows SAPI5 &amp; SileroTTS</b></p>
  <ol><li>Install a Windows language voice pack or a third-party SAPI5 engine. SileroTTS provides local CIS-language neural voices.</li><li>Select <b>Windows voices (SAPI5, offline)</b> above, click <b>Reload voices</b>, pick your voice and click <b>Play a sample</b>.</li></ol>
  <div class="row"><button type="button" class="btn primary" id="sv-silero">${icon("external-link")}SileroTTS for Windows</button><button type="button" class="btn" id="sv-win-speech">${icon("external-link")}Windows speech settings</button><button type="button" class="btn" id="sv-sapi-panel">${icon("external-link")}SAPI5 voice panel</button></div>
  <p><b>Online speech · Edge-TTS (experimental)</b></p>
  <p>Free online neural and multilingual voices without registration or an API key. Because this unofficial Microsoft service can occasionally fail or rate-limit requests, keep a working Windows SAPI5 voice installed for automatic fallback.</p>
  <ol><li>Select <b>Edge-TTS</b> above and click <b>Load voices</b>. Pick a voice for your language, or enable <b>Multilingual voices only</b>.</li><li>For dependable offline play, select <b>Windows voices (SAPI5, offline)</b> instead.</li></ol>
</details>`;


/** The controls only this provider has. */
function providerFields(prov: string, sp: SpeechSettings): string {
  if (prov === "windows") {
    return field("Voice", "Installed Windows and third-party SAPI5 voices, including SileroTTS. Reload after installing a voice.",
      `<select id="sv-voice"><option value="">Windows default</option>${sp.voiceName ? `<option value="${esc(sp.voiceName)}" selected>${esc(sp.voiceName)}</option>` : ""}</select> <button class="btn" id="sv-windows-load">Reload voices</button>`);
  }
  if (prov === "edge") {
    const voice = edgeVoice(sp);
    return voiceList("Experimental support, still being worked on. Free online, no registration or API key; text is sent to Microsoft. Natural-sounding and multilingual voices are available. The unofficial service may fail; the selected Windows SAPI5 voice is the fallback.", voice, voice || "Choose a voice")
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
  sampleObserver?.disconnect();
  const process = processFields(g, sp);
  const output = edits.get("companion.voice.output") ?? getPath(live.config, "companion.voice.output");
  const showProcess = prov === "windows" || output === "windows";
  const save = (patch: Obj) => updateAppSettings({ speech: patch });
  host.innerHTML = field("Voice service", output === "windows"
    ? "Coach callouts currently use the Windows speech process. This selection is used for this app's voice test; online failures fall back to the app's Windows voice."
    : "Online services sound more natural: Edge-TTS is free, while other services may charge per line. If online synthesis fails, the unspoken text uses the selected Windows voice.",
      `<select id="sv-prov">${PROVIDERS.map(([id, l]) => `<option value="${id}" ${id === prov ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>${keyNote(PROVIDERS.find((p) => p[0] === prov)![2])}${VOICE_SETUP_GUIDE}`)
    + providerFields(prov, sp)
    + (showProcess ? `<section class="voice-process"><h3>Windows speech process</h3><p>Shared Windows voice: ${esc(sp.voiceName || "Windows default")}. Used when Spoken by is Windows speech process; an online Voice service is not used for those callouts.</p>${process.map(fieldHtml).join("")}${prov !== "windows" ? `<button type="button" class="btn" id="sv-process-load">Reload Windows voices</button>` : ""}</section>` : "")
    + field("Test voice", sampleLanguageHelp(), `<div class="voice-preview"><div class="voice-preview-controls"><select id="sv-sample-source" aria-label="Text source for voice test"><option value="template">Template phrase</option><option value="last">Last spoken line</option></select><button type="button" class="btn" id="sv-test" disabled>${icon("volume-2")}<span>Play sample</span></button></div><p id="sv-sample" class="help voice-text" aria-live="polite">Preparing template…</p></div>`)
    + field("Last line spoken with", "The online voice or Windows voice Glide used for its last line. WoW's Text to Speech (Options > Accessibility) and Windows Narrator have their own voices.", `<div class="voice-last" aria-live="polite"><span id="sv-last" class="help">${esc(voiceLastLine())}</span><span id="sv-last-text" class="help voice-text" ${speech.lastText ? "" : "hidden"}>${esc(speech.lastText)}</span></div>`);

  const activeEnv = PROVIDERS.find((p) => p[0] === prov)?.[2] || "";
  if (activeEnv) {
    void loadConfiguredKeys().then((keys) => {
      const el = host.querySelector<HTMLElement>("#sv-key-note");
      if (el && keys && $<HTMLSelectElement>("sv-prov").value === prov) {
        el.innerHTML = keyRequirementHtml(activeEnv, keys);
      }
    });
  }
  host.querySelector<HTMLButtonElement>("#sv-silero")!.onclick = () => call("shell:open", "https://github.com/snakers4/silero-models/releases/tag/v5.6");
  host.querySelector<HTMLButtonElement>("#sv-win-speech")!.onclick = () => call("shell:open", "ms-settings:speech");
  host.querySelector<HTMLButtonElement>("#sv-sapi-panel")!.onclick = () => call("shell:speechPanel");
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
  bindSample(host);
  if (showProcess) bindProcessVoice(process, host);
  if (prov === "windows") $("sv-voice").onchange = async (e) => {
    await save({ voiceName: (e.target as HTMLSelectElement).value });
    if (!host.isConnected) return;
    live.config = (await call("config:get")) || live.config;
    renderVoices(g, host);
  };
  if (prov === "openai") {
    const select = $<HTMLSelectElement>("sv-o-voice");
    call("tts:voices", "openai").then((vs) => {
      if (!select.isConnected) return;
      select.innerHTML = (vs || []).map((v) => `<option ${v.id === (sp.openai?.voice || "coral") ? "selected" : ""}>${esc(v.id)}</option>`).join("");
    });
    const o = () => save({ openai: { model: $<HTMLSelectElement>("sv-o-model").value, voice: $<HTMLSelectElement>("sv-o-voice").value, instructions: $<HTMLInputElement>("sv-o-instr").value } });
    ["sv-o-model", "sv-o-voice", "sv-o-instr"].forEach((id) => $(id).addEventListener("change", o));
  }
  if (VOICE_LIST[prov]) bindVoiceList(prov, sp as Obj, save);
  if (prov === "elevenlabs") $("sv-e-model").onchange = (e) => save({ elevenlabs: { ...(sp.elevenlabs || {}), model: (e.target as HTMLSelectElement).value } });
  if (prov === "azure") $("sv-a-region").onchange = (e) => save({ azure: { ...(sp.azure || {}), region: (e.target as HTMLInputElement).value.trim() } });
  if (prov === "windows" || showProcess) bindWindowsVoices();
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
    const cur = String(getPath(live.appSettings, `speech.${section}.${k}`) ?? "");
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

/** Native enumeration includes third-party SAPI5 engines that the browser's voice list omits. */
function bindWindowsVoices(): void {
  const main = document.getElementById("sv-voice") as HTMLSelectElement | null;
  const process = document.querySelector<HTMLSelectElement>('[data-key="companion.voice.voice"] select');
  const toggle = document.querySelector<HTMLInputElement>('[data-key="app:speech.windowsProcessUseAppVoice"] input');
  const button = (document.getElementById("sv-windows-load") || document.getElementById("sv-process-load")) as HTMLButtonElement;
  const selects = [main, process].filter((select): select is HTMLSelectElement => !!select);
  const load = async () => {
    const selected = selects.map((select) => select.value);
    button.disabled = true;
    for (const select of selects) {
      select.disabled = true;
      select.dataset.loading = "true";
    }
    try {
      const voices = await call("tts:voices", "windows");
      if (!voices || !button.isConnected) return;
      for (const [index, select] of selects.entries()) {
        const value = selected[index];
        select.innerHTML = `<option value="">Windows default</option>`
          + voices.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("")
          + (value && !voices.some((v) => v.id === value) ? `<option value="${esc(value)}">${esc(value)} (not installed)</option>` : "");
        select.value = value;
      }
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        for (const select of selects) {
          delete select.dataset.loading;
          select.disabled = select === process && !toggle?.checked;
        }
      }
    }
  };
  button.onclick = load;
  void load();
}
