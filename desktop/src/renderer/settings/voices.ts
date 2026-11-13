// Settings > Voice > Coach voice: the narrator's voice service and its voice, speed and volume, a sample, and the
// last spoken text and voice. Saved to the app settings as each control changes.
import { updateAppSettings } from "../app-settings.js";
import { call, glide, ipcError } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { speech, voiceHistoryHtml } from "../narrator.js";
import { Field, Group } from "../settings-schema.js";
import { filterEdgeVoices, LANGUAGE_OPTIONS, languageCode, speechLanguage as language, voiceSample } from "../speech-language.js";
import { live } from "../state.js";
import { bindField, edits, fieldHtml, getPath, updateDirty } from "./fields.js";
import { usesAppWindowsVoice } from "../../shared/windows-process-voice.mjs";
import { jumpLinkHtml, keyRequirementHtml, loadConfiguredKeys } from "./key-links.js";
import { Obj, SpeechSettings, TtsProvider, Voice } from "../types.js";
import { SPEECH_FEATURES, speechFeatures } from "../speech-markup.js";
import type { SpeechFeature } from "../../shared/speech-markup.js";
import { VOICE_EXAMPLES as EXAMPLES, voiceExample } from "./voice-examples.js";

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
let sampleSource: "template" | "last" | "custom" = "template";
let customSample = "";
let sampleObserver: MutationObserver | null = null;
let sampleMode: "plain" | "ssml" = "plain";
let ssmlSource: "last" | "custom" = "custom";
let ssmlSample = "";
let nativeCatalogue: Voice[] = [];
let permissionIdentity = "";
let permissionDestination = "";
let voiceBindings: AbortController | null = null;
const voicePolicy = (): Obj => getPath(live.config, "companion.voice") || {};
function nativeVoice(selected: string): Voice | undefined {
  return selected ? nativeCatalogue.find((v) => v.identity === selected || v.id === selected) : nativeCatalogue.find((v) => v.isDefault);
}
function permitted(identity: string): SpeechFeature[] {
  return identity ? speechFeatures(voicePolicy().markup_voices?.[identity]) : [];
}

async function changingVoice(host: HTMLElement, save: () => Promise<void>): Promise<void> {
  host.dataset.voiceSaving = String(Number(host.dataset.voiceSaving || 0) + 1);
  host.dispatchEvent(new Event("speech-destination-changed"));
  try {
    await save();
  } finally {
    const pending = Number(host.dataset.voiceSaving || 1) - 1;
    if (pending) host.dataset.voiceSaving = String(pending);
    else delete host.dataset.voiceSaving;
    host.dispatchEvent(new Event("speech-destination-changed"));
  }
}

function markupFields(): string {
  return field("Automatic speech markup", "Permissions are per actual native voice. Online speech remains plain. Manual SSML tests remain available when automatic markup is off.",
    `<select id="sv-markup-mode" aria-label="Automatic speech markup">${options([["ai", "AI with local fallback"], ["local", "Local rules only"], ["off", "Off"]], voicePolicy().markup_mode || "ai")}</select>`)
    + field("Speech markup budget", "Additional output allowance only for eligible native requests; spoken-word limits do not change. The saved choice is retained while inactive.",
      `<select id="sv-markup-profile" aria-label="Speech markup budget">${options([["light", "Light (+64 tokens, 1 hint)"], ["standard", "Standard (+128 tokens, 3 hints)"], ["expanded", "Expanded (+256 tokens, 6 hints)"]], voicePolicy().markup_profile || "standard")}</select><p id="sv-markup-budget-help" class="help"></p>`)
    + field("Native voice effect permissions", "Choose which effects Glide may request for this voice. Enabled does not mean verified. Use Test voice to compare.",
      `<select id="sv-markup-voice" aria-label="Voice effect permissions"></select>
      <p id="sv-markup-evidence" class="help" role="status"></p><div id="sv-markup-features"></div>
      <details id="sv-markup-details" class="keyhow voice-evidence"><summary>Voice details</summary>
        <dl><dt>Effect verification</dt><dd id="sv-markup-verification"></dd>
        <dt>Native voice ID</dt><dd><code id="sv-markup-identity"></code></dd></dl>
        <p>Permissions are saved for this Windows voice ID, not its display name. Selecting a voice never runs a sound probe.</p>
        <button type="button" class="btn" id="sv-markup-load">Refresh voice list</button>
      </details>`);
}

function bindMarkup(host: HTMLElement): void {
  const mode = host.querySelector<HTMLSelectElement>("#sv-markup-mode")!;
  const profile = host.querySelector<HTMLSelectElement>("#sv-markup-profile")!;
  const select = host.querySelector<HTMLSelectElement>("#sv-markup-voice")!;
  const save = (patch: Obj) => changingVoice(host, async () => {
    const saved = await call("config:set", { companion: { voice: patch } });
    if (saved) live.config = saved;
    draw();
    host.dispatchEvent(new Event("speech-markup-changed"));
  });
  const draw = () => {
    if (!host.isConnected) return;
    const policy = voicePolicy(), sp = live.appSettings.speech || {};
    mode.value = policy.markup_mode || "ai";
    profile.value = policy.markup_profile || "standard";
    const active = policy.output === "windows" ? nativeVoice(policy.voice || "") : (sp.provider || "windows") === "windows" ? nativeVoice(sp.voiceName || "") : undefined;
    const eligible = mode.value === "ai" && permitted(active?.identity || "").some((f) => f !== "boundaries");
    profile.disabled = !eligible;
    host.querySelector("#sv-markup-budget-help")!.textContent = eligible ? "Active for eligible AI speech requests." : "Inactive: requires AI mode and an active native voice with rate, pitch or pauses permitted.";
    const fallback = nativeVoice(sp.voiceName || "");
    const destination = `${policy.output || "app"}:${sp.provider || "windows"}:${(active || fallback)?.identity || ""}`;
    if (permissionDestination !== destination) {
      permissionIdentity = (active || fallback)?.identity || "";
      permissionDestination = destination;
    }
    if (!nativeCatalogue.some((v) => v.identity === permissionIdentity)) permissionIdentity = (active || fallback)?.identity || "";
    select.innerHTML = nativeCatalogue.map((v) => `<option value="${esc(v.identity || "")}">${esc(v.name)}${v.isDefault ? " (Windows default)" : ""}${nativeCatalogue.some((other) => other !== v && other.name === v.name) ? ` — ${esc(v.identity || v.id)}` : ""}</option>`).join("");
    select.value = permissionIdentity;
    select.disabled = !nativeCatalogue.length;
    const voice = nativeCatalogue.find((v) => v.identity === permissionIdentity);
    host.querySelector("#sv-markup-evidence")!.textContent = voice
      ? "Effect support unverified"
      : "Native voice unavailable — effect permissions cannot be changed.";
    host.querySelector("#sv-markup-verification")!.textContent = voice
      ? "No reliably version-matched profile is available for this installed voice. This does not mean SSML is unsupported; individual effects may work or be ignored."
      : "Select an available native voice to configure its effect permissions.";
    host.querySelector("#sv-markup-identity")!.textContent = voice?.identity || "Unavailable";
    const features = permitted(permissionIdentity);
    host.querySelector("#sv-markup-features")!.innerHTML = SPEECH_FEATURES.map((f) =>
      `<label><input type="checkbox" data-speech-feature="${f}" ${features.includes(f) ? "checked" : ""} ${voice ? "" : "disabled"}> ${f === "boundaries" ? "Sentence / paragraph boundaries" : f === "pauses" ? "Native pauses (may be ignored)" : f === "rate" ? "Speaking rate" : "Pitch"}</label>`).join("");
    host.querySelectorAll<HTMLInputElement>("[data-speech-feature]").forEach((input) => input.onchange = () => {
      const enabled = [...host.querySelectorAll<HTMLInputElement>("[data-speech-feature]:checked")].map((el) => el.dataset.speechFeature!);
      void save({ markup_voices: { [permissionIdentity]: enabled } });
    });
  };
  mode.onchange = () => { void save({ markup_mode: mode.value }); };
  profile.onchange = () => { void save({ markup_profile: profile.value }); };
  select.onchange = () => { permissionIdentity = select.value; draw(); };
  host.addEventListener("speech-catalogue-changed", draw, { signal: voiceBindings!.signal });
  draw();
}

function sampleLanguageHelp(): string {
  const saved = language();
  const selected = edits.has("companion.voice.language") ? languageCode(edits.get("companion.voice.language")) : saved;
  const label = (code: string) => jumpLinkHtml(`${LANGUAGE_OPTIONS.find(([value]) => value === code)?.[1] || code} (${code})`, "language", "companion.voice.language");
  return selected !== saved
    ? `Selected language: ${label(selected)} (not saved). Save to update the template; currently saved: ${label(saved)}. Last-spoken and custom text are not translated.`
    : `Saved speech language: ${label(saved)}. Template translation requires the coach; last-spoken and custom text are not translated.`;
}

function bindSample(host: HTMLElement): void {
  const source = host.querySelector<HTMLSelectElement>("#sv-sample-source")!;
  const button = host.querySelector<HTMLButtonElement>("#sv-test")!;
  const preview = host.querySelector<HTMLTextAreaElement>("#sv-sample")!;
  const history = host.querySelector<HTMLElement>("#sv-history")!;
  const mode = host.querySelector<HTMLSelectElement>("#sv-sample-mode");
  const examples = host.querySelector<HTMLSelectElement>("#sv-sample-example")!;
  const error = host.querySelector<HTMLElement>("#sv-sample-error")!;
  const help = host.querySelector<HTMLElement>("#sv-sample-help")!;
  let revision = 0;
  let template: string | undefined;
  let preparing = false;
  if (mode) mode.value = sampleMode;
  const draw = () => {
    const ssml = sampleMode === "ssml";
    const selectedSource = ssml ? ssmlSource : sampleSource;
    const text = ssml ? (ssmlSource === "last" ? speech.lastSsmlDocument : ssmlSample)
      : sampleSource === "last" ? speech.lastText : sampleSource === "custom" ? customSample : template || "";
    source.innerHTML = (ssml ? "" : '<option value="template">Template phrase</option>')
      + '<option value="last">Last spoken line</option>'
      + `<option value="custom">${ssml ? "Custom SSML" : "Custom text"}</option>`;
    source.value = selectedSource;
    if (preview.value !== text) preview.value = text;
    preview.placeholder = ssml ? (ssmlSource === "last" ? "The last spoken line has no saved SSML document."
      : "Choose an SSML example or enter a complete <speak> document.")
      : sampleSource === "last" ? "No line has been spoken in this session."
      : sampleSource === "custom" ? "Enter text to test this voice." : "Preparing template…";
    host.querySelector<HTMLElement>("#sv-example-label")!.textContent = ssml ? "SSML example" : "Plain-text example";
    host.querySelector<HTMLElement>("#sv-editor-label")!.textContent = ssml ? "SSML document" : "Text to speak";
    if (mode) mode.value = sampleMode;
    const native = (live.appSettings.speech?.provider || "windows") === "windows";
    const features = permitted(nativeVoice(live.appSettings.speech?.voiceName || "")?.identity || "");
    examples.setAttribute("aria-label", ssml ? "SSML examples" : "Plain-text examples");
    examples.innerHTML = `<option value="">Insert an editable ${ssml ? "SSML" : "plain-text"} example…</option>` + EXAMPLES.map((example) => {
      const missing = ssml ? example.features.filter((f) => !features.includes(f)) : [];
      return `<option value="${example.id}" ${ssml && (!native || missing.length) ? "disabled" : ""}>${esc(ssml ? example.label : example.plainLabel)}${missing.length ? ` — requires ${missing.join(", ")}` : ""}</option>`;
    }).join("");
    help.textContent = !ssml ? "Plain text bypasses automatic speech markup. Built-in examples use the saved speech language and work offline. Selection and editing are silent; only Play speaks. Custom and last-spoken text are not translated."
      : !native ? "SSML testing is available only for the app-selected Windows SAPI5 service. The draft is retained; it will not be sent online."
      : ssmlSource === "last" ? (text ? "Reuses the last spoken line's saved SSML. Editing creates a custom draft. Play validates it against the selected voice and current permissions."
        : "The last spoken line has no saved SSML. It is available only after successful SAPI5 playback with markup.")
      : "Built-in examples use the saved speech language and work offline. Examples require the voice permissions listed above. Effects remain unverified; native pauses may be ignored. Selection and editing are silent. Play validates SSML before interrupting speech.";
    button.disabled = !text.trim() || !!host.dataset.voiceSaving || (sampleMode === "ssml" && !native);
    button.querySelector("span")!.textContent = ssml ? (ssmlSource === "last" ? "Play last SSML" : "Play SSML")
      : sampleSource === "last" ? "Play last line" : "Play sample";
  };
  const prepare = () => {
    if (sampleMode !== "plain" || sampleSource !== "template" || preparing || template !== undefined) return;
    preparing = true;
    void voiceSample().then((text) => {
      if (!button.isConnected) return;
      template = text;
      preparing = false;
      draw();
    });
  };
  source.onchange = () => {
    if (sampleMode === "ssml") ssmlSource = source.value === "last" ? "last" : "custom";
    else sampleSource = source.value === "last" ? "last" : source.value === "custom" ? "custom" : "template";
    revision++;
    error.textContent = "";
    prepare();
    draw();
  };
  preview.oninput = () => {
    if (sampleMode === "ssml") {
      ssmlSample = preview.value;
      ssmlSource = "custom";
    } else {
      customSample = preview.value;
      sampleSource = "custom";
    }
    revision++;
    error.textContent = "";
    draw();
  };
  if (mode) mode.onchange = () => {
    sampleMode = mode.value === "ssml" ? "ssml" : "plain";
    revision++;
    error.textContent = "";
    prepare();
    draw();
  };
  examples.onchange = () => {
    const example = EXAMPLES.find((item) => item.id === examples.value);
    if (!example || (sampleMode === "ssml" && ((live.appSettings.speech?.provider || "windows") !== "windows"
      || example.features.some((f) => !permitted(nativeVoice(live.appSettings.speech?.voiceName || "")?.identity || "").includes(f))))) return;
    const text = voiceExample(example.id, language(), sampleMode);
    if (sampleMode === "ssml") {
      ssmlSample = text;
      ssmlSource = "custom";
    } else {
      customSample = text;
      sampleSource = "custom";
    }
    revision++;
    error.textContent = "";
    draw();
  };
  button.onclick = async () => {
    const text = preview.value;
    if (!text.trim()) return;
    const rate = edits.has("app:speech.rate") ? Number(edits.get("app:speech.rate") ?? 1) : undefined;
    const volume = edits.has("app:speech.volume") ? Number(edits.get("app:speech.volume") ?? 1) : undefined;
    if (sampleMode === "plain") {
      speech.say(text, true, "", "", { rate, volume,
        native: { voiceName: live.appSettings.speech?.voiceName || "", features: [], mode: "off" } });
      return;
    }
    if ((live.appSettings.speech?.provider || "windows") !== "windows") {
      error.textContent = "SSML testing requires the app-selected Windows SAPI5 service.";
      return;
    }
    const version = ++revision, selected = live.appSettings.speech?.voiceName || "";
    const cancellation = speech.cancellationEpoch;
    button.disabled = true;
    error.textContent = "";
    try {
      const voices = await glide.invoke("tts:voices", "windows");
      const voice = selected ? voices.find((v) => v.identity === selected || v.id === selected) : voices.find((v) => v.isDefault);
      if (!voice?.identity) throw new Error("The selected native voice identity could not be resolved.");
      const config = await glide.invoke("config:get");
      if (!button.isConnected || version !== revision || cancellation !== speech.cancellationEpoch) return;
      live.config = config;
      const features = permitted(voice.identity);
      const markup = await glide.invoke("tts:windowsPrepare", text, { voiceName: voice.identity, rate, volume }, features);
      if (!button.isConnected || version !== revision || cancellation !== speech.cancellationEpoch) return;
      if ((live.appSettings.speech?.provider || "windows") !== "windows" || (live.appSettings.speech?.voiceName || "") !== selected)
        throw new Error("The voice destination changed. Press Play to validate the retained draft again.");
      speech.say(markup.text, true, "", "", { rate, volume, markup,
        native: { voiceName: voice.identity, features, markup, mode: "off" } });
    } catch (reason) {
      if (button.isConnected && version === revision && cancellation === speech.cancellationEpoch)
        error.textContent = `SSML not played: ${ipcError(reason)}`;
    } finally {
      if (button.isConnected && version === revision) draw();
    }
  };
  host.addEventListener("speech-markup-changed", () => { revision++; draw(); }, { signal: voiceBindings!.signal });
  host.addEventListener("speech-catalogue-changed", draw, { signal: voiceBindings!.signal });
  host.addEventListener("speech-destination-changed", () => { revision++; draw(); }, { signal: voiceBindings!.signal });
  sampleObserver = new MutationObserver(() => {
    if (sampleMode === "ssml" && ssmlSource === "last" && preview.value !== speech.lastSsmlDocument) revision++;
    draw();
  });
  sampleObserver.observe(history, { childList: true, characterData: true, subtree: true });
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
      const saved = String(getPath(live.config, "companion.voice.voice") ?? "");
      const resolved = nativeVoice(saved)?.identity;
      select.value = resolved && [...select.options].some((option) => option.value === resolved) ? resolved : saved;
      updateDirty();
    }
    const summary = host.querySelector<HTMLElement>("#sv-process-summary");
    if (summary) {
      const voice = toggle.checked ? select.value : live.appSettings.speech?.voiceName;
      summary.textContent = `Selected Windows voice: ${nativeVoice(voice || "")?.name || voice || "Windows default"} (${toggle.checked ? "separate voice" : "follows the app's Windows voice"}). Used only when Spoken by is Windows speech process; online voice services do not apply. Save to apply changes.`;
    }
  };
  toggle.addEventListener("change", sync);
  host.addEventListener("speech-catalogue-changed", sync, { signal: voiceBindings!.signal });
  select.addEventListener("change", sync);
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
  if (prov !== "windows") sampleMode = "plain";
  sampleObserver?.disconnect();
  voiceBindings?.abort();
  voiceBindings = new AbortController();
  const process = processFields(g, sp);
  const output = edits.get("companion.voice.output") ?? getPath(live.config, "companion.voice.output");
  const showProcess = prov === "windows" || output === "windows";
  const save = (patch: Obj) => changingVoice(host, () => updateAppSettings({ speech: patch }));
  host.innerHTML = field("Voice service", output === "windows"
    ? "Coach callouts currently use the Windows speech process. This selection is used for this app's voice test; online failures fall back to the app's Windows voice."
    : "Windows voices work offline. Online services send text to the provider and may require an API key or payment. If online speech fails, unspoken text uses the app's Windows voice.",
      `<select id="sv-prov">${PROVIDERS.map(([id, l]) => `<option value="${id}" ${id === prov ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>${keyNote(PROVIDERS.find((p) => p[0] === prov)![2])}${VOICE_SETUP_GUIDE}`)
    + providerFields(prov, sp)
    + (showProcess ? `<section class="voice-process"><h3>Windows speech process</h3><p id="sv-process-summary" aria-live="polite"></p>${process.map(fieldHtml).join("")}${prov !== "windows" ? `<button type="button" class="btn" id="sv-process-load">Reload Windows voices</button>` : ""}</section>` : "")
    + (prov === "windows" ? markupFields() : "")
    + field("Test voice", sampleLanguageHelp(), `<div class="voice-preview">
      ${prov === "windows" ? `<label class="voice-preview-controls">Format <select id="sv-sample-mode" aria-label="Voice test input mode"><option value="plain">Plain text</option><option value="ssml">SSML</option></select></label>` : ""}
      <label id="sv-source-control" class="voice-preview-control">Text source <select id="sv-sample-source" aria-label="Text source for voice test"></select></label>
      <label class="voice-preview-control"><span id="sv-example-label">Plain-text example</span><select id="sv-sample-example" aria-label="Plain-text examples"></select></label>
      <label class="voice-preview-control"><span id="sv-editor-label">Text to speak</span><textarea id="sv-sample" class="voice-text" aria-label="Voice test text" rows="4" placeholder="Preparing template…"></textarea></label>
      <p id="sv-sample-help" class="help"></p><p id="sv-sample-error" role="alert"></p>
      <div class="voice-preview-controls"><button type="button" class="btn" id="sv-test" disabled>${icon("volume-2")}<span>Play sample</span></button></div>
    </div>`)
    + field("Last 3 spoken lines", "Newest first. Native first/last audio estimates observe delivery to SAPI's playback buffer, not model computation or audible onset. Both appear after successful completion; ≈ includes polling and delivery delay. Speech completion includes playback. If audio timing is unavailable, any first-word report stays separately labelled. Submitted SSML does not prove audible effects.", `<div id="sv-history" class="voice-last" aria-live="polite">${voiceHistoryHtml()}</div>`);

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
  $("sv-prov").onchange = async (e) => changingVoice(host, async () => {
    const provider = (e.target as HTMLSelectElement).value;
    if (provider === "edge" && !edgeVoice(sp).startsWith(`${language()}-`)) {
      const catalogue = await call("tts:voices", "edge");
      if (!catalogue) return;
      const voice = catalogue.find((v) => v.id.startsWith(`${language()}-`));
      if (!voice) { toast("No Edge-TTS voices for the selected speech language."); return; }
      await save({ provider, edge: { voice: voice.id } });
    } else await save({ provider });
    renderVoices(g, host);
  });
  bindSample(host);
  if (prov === "windows") bindMarkup(host);
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
  if (showProcess) bindWindowsVoices();
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
  const button = (document.getElementById("sv-windows-load") || document.getElementById("sv-process-load") || document.getElementById("sv-markup-load")) as HTMLButtonElement;
  const selects = [main, process].filter((select): select is HTMLSelectElement => !!select);
  const draw = (voices: Voice[]) => {
    nativeCatalogue = voices;
    for (const select of selects) {
      // A refresh must not overwrite a choice made while its catalogue request was pending.
      const value = select.value;
      const matched = value ? voices.find((v) => v.identity === value || v.id === value) : undefined;
      const defaultVoice = voices.find((v) => v.isDefault);
      select.innerHTML = `<option value="">Windows default${defaultVoice ? ` — ${esc(defaultVoice.name)}` : ""}</option>`
        + voices.map((v) => `<option value="${esc(v.identity || v.id)}">${esc(v.name)}${voices.some((other) => other !== v && other.name === v.name) ? ` — ${esc(v.identity || v.id)}` : ""}</option>`).join("")
        + (value && !matched ? `<option value="${esc(value)}">${esc(value)} (not installed)</option>` : "");
      select.value = matched?.identity || value;
    }
    button.closest<HTMLElement>('[data-special="voices"]')?.dispatchEvent(new Event("speech-catalogue-changed"));
  };
  if (nativeCatalogue.length) draw(nativeCatalogue);
  const load = async () => {
    button.disabled = true;
    if (!nativeCatalogue.length) for (const select of selects) {
      select.disabled = true;
      select.dataset.loading = "true";
    }
    try {
      const voices = await call("tts:voices", "windows");
      if (!voices || !button.isConnected) return;
      draw(voices);
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
  const markupReload = document.getElementById("sv-markup-load");
  if (markupReload) markupReload.onclick = load;
  void load();
}
