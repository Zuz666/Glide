// One place to explain the saved language's translation, recognition and speech choices.
import { call } from "../bridge.js";
import { esc, icon } from "../dom.js";
import { MANIFEST } from "../settings-manifest.js";
import { Group } from "../settings-schema.js";
import { speechLanguage } from "../speech-language.js";
import { live } from "../state.js";
import { openSettingsTab } from "./page.js";

const PROVIDERS: Record<string, [string, string]> = {
  edge: ["Edge-TTS", "Multilingual · free · online · no API key. The voice picker filters the live catalogue by language. This unofficial Microsoft service may be unavailable or limit requests."],
  windows: ["Windows speech", "Multilingual · free · offline. Only installed Windows voices are available; install a voice for the selected language in Windows Settings."],
  openai: ["OpenAI TTS", "Multilingual, but its voices are optimized for English. Requires an OpenAI API key and API billing; a ChatGPT subscription does not include API usage."],
  elevenlabs: ["ElevenLabs", "Multilingual support depends on the selected model and voice. Choose a native-language voice and a multilingual model; requires an ElevenLabs API key."],
  azure: ["Azure Speech", "Glide's current voice picker lists English voices only. Azure itself supports other languages. Requires an Azure Speech key and region."],
  google: ["Google Cloud Text-to-Speech", "Glide's current voice picker lists English voices only. Google Cloud itself supports other languages. Requires a Google Cloud TTS API key."],
};
const ONLINE_STT: Record<string, [string, string]> = {
  openai: ["OpenAI transcription", "gpt-4o-mini-transcribe"],
  groq: ["Groq transcription", "whisper-large-v3-turbo"],
  deepgram: ["Deepgram transcription", "nova-3"],
};
const row = (label: string, value: string, help: string, tab: string, link: string, labelExtra = "") =>
  `<div class="field"><div class="lab">${esc(label)}${labelExtra}<small>${esc(help)}</small></div><div class="ctl language-tool"><span>${esc(value)}</span><button class="btn" data-language-go="${tab}">${icon("external-link")}${esc(link)}</button></div></div>`;

export function renderLanguage(_g: Group, host: HTMLElement): void {
  const c = live.config.companion || {}, llm = c.llm || {}, ptt = c.ptt || {}, voice = c.voice || {};
  const sp = live.appSettings.speech || {};
  const fast = String(llm.fast_model || MANIFEST["llm.fast_model"].default);
  const enabled = llm.enabled ?? MANIFEST["llm.enabled"].default;
  const endpoint = fast.startsWith("ep:") ? llm.endpoints?.find((e: { id: string }) => e.id === fast.split(":")[1]) : undefined;
  const api = endpoint?.label || (fast.startsWith("ep:") ? fast.split(":")[1] : fast.startsWith("gemini:") ? "Gemini API" : fast.includes("/") ? "OpenRouter" : "Anthropic API");
  const ownWindows = voice.output === "windows";
  const provider = ownWindows ? "windows" : sp.provider || "windows";
  const name = PROVIDERS[provider]?.[0] || provider;
  const selectedVoice = ownWindows ? voice.voice || "Windows default" : provider === "edge" ? sp.edge?.voice || "no voice selected"
    : provider === "windows" ? sp.voiceName || "Windows default" : provider === "openai" ? `${sp.openai?.model || "gpt-4o-mini-tts"} / ${sp.openai?.voice || "coral"}`
    : provider === "elevenlabs" ? `${sp.elevenlabs?.model || "eleven_flash_v2_5"} / ${sp.elevenlabs?.voiceId || "no voice selected"}`
    : provider === "azure" ? sp.azure?.voice || "provider default" : sp.google?.voice || "provider default";
  const engine = ptt.engine || "auto";
  const online = ONLINE_STT[engine];
  const model = ptt.whisper_model || (speechLanguage() === "en" ? "small.en" : "small");
  const recognition = ptt.enabled === false ? "Push-to-talk disabled" : online ? `${online[0]} / ${ptt.online_model || online[1]}`
    : engine === "windows" ? "Windows speech recognition" : `Whisper / ${model}${engine === "auto" ? " (Windows fallback)" : ""}`;
  host.innerHTML = `<div class="field"><div class="lab">Interface language <span class="dim">WIP</span><small>Interface localization is work in progress. Only English is available; this does not change the coach's language.</small></div><div class="ctl"><select id="interface-language" aria-label="Interface language (WIP)" disabled><option value="en" selected>English</option></select></div></div>`
    + `<div class="field"><div class="lab">Translate news and community notes <span class="dim">WIP</span><small>Translation of news, tips, data and mechanics from the community feed is not implemented yet. These entries stay in their original language, independently of the coach language.</small></div><div class="ctl"><label class="switch"><input type="checkbox" id="translate-community" aria-label="Translate news and community notes (WIP)" disabled><span class="track"></span><span class="state">Off</span></label></div></div>`
    + row("Translation and coach text", enabled ? `${api} / ${fast}` : "AI disabled — English text",
    "Uses the configured fast model for translation; generated answers use their existing fast/deep model. Only an available, enabled LLM produces localized text. Without one, messages stay English. API translation may cost money.", "models", "AI models & keys")
    + row("Speech recognition", recognition,
      `Selected language: ${speechLanguage()}. Non-English Whisper needs a multilingual model, such as small, not a .en model. Online recognition falls back to ${model}; language support depends on the recognition provider. Windows requires a matching installed recognizer.`, "voice", "Recognition settings")
    + row("Speech output", `${voice.enabled === false ? "Disabled · " : ""}${name} / ${selectedVoice}`,
      "Speech synthesis reads the coach's text; it does not translate it.", "voice", "Voice settings",
      `<button class="language-help-trigger" type="button" popovertarget="language-provider-help" aria-label="Language support" title="Language support"><span aria-hidden="true">i</span></button>`)
    + `<p class="help">Recommended: enable an available AI model for translated messages, choose Edge-TTS for free multilingual online speech, and select a voice matching your language. For offline speech, install the matching Windows voice. Save language changes and restart the coach.</p>`
    + `<div id="language-provider-help" class="language-help-popup" popover role="dialog" aria-labelledby="language-provider-title">
      <header><h3 id="language-provider-title">Speech languages and setup</h3><button class="btn" popovertarget="language-provider-help" popovertargetaction="hide" aria-label="Close language support">${icon("x")}</button></header>
      <dl>${Object.entries(PROVIDERS).map(([key, [label, help]]) => `<dt>${esc(label)}</dt><dd>${esc(help)}${key === "windows" ? `<p><button class="btn" id="language-windows-speech" type="button">${icon("external-link")}Open Windows speech settings</button></p>` : ""}</dd>`).join("")}</dl></div>`;
  host.querySelectorAll<HTMLButtonElement>("[data-language-go]").forEach((button) => {
    button.onclick = () => openSettingsTab(button.dataset.languageGo!);
  });
  host.querySelector<HTMLButtonElement>("#language-windows-speech")!.onclick = () => call("shell:open", "ms-settings:speech");
}
