// Offline speech recognition (Whisper) for push-to-talk: its state as the main process reports it (speech:status,
// also shown in the session bar's activity and on the Status page), and Settings > Voice's panel to check, repair,
// download and delete the models.
import { taskBar } from "../activity.js";
import { call, glide } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { Group } from "../settings-schema.js";
import { speechLanguage } from "../speech-language.js";
import { live } from "../state.js";
import { refreshStatus } from "../status.js";
import { SpeechStatus, WhisperModel } from "../types.js";

let whisperModels: WhisperModel[] = [];
let lastSpeechToast = "";
let lastSpeechState = "";

function speechLine(s: Partial<SpeechStatus>): string {
  const st = s.state;
  if (st === "ready") {
    if (speechLanguage() !== "en" && s.modelName?.endsWith(".en"))
      return `<span class="warn">Whisper ${esc(s.modelName)} is English-only. Select a multilingual model, save and restart the coach.</span>`;
    return `<span class="ok">Ready: Whisper ${esc(s.modelName)}${s.version ? ` (faster-whisper ${esc(s.version)})` : ""}</span>`;
  }
  if (st === "off") return `<span class="dim">${esc(s.detail || "not used")}</span>`;
  if (st === "downloading")
    return `<span class="warn">Downloading ${esc(s.fetching || s.modelName)}${s.totalMB ? `: ${s.gotMB || 0} of ${s.totalMB} MB` : ""}</span> <span class="dim">The downloading model is not ready for offline recognition. Configured online recognition can still work; Windows fallback needs the speech language installed.</span>`;
  if (st === "installing" || st === "checking")
    return `<span class="warn">${esc(s.detail || "Checking…")}</span> <span class="dim">Runs by itself; progress is in Logs.</span>`;
  return `<span class="bad">${esc(s.detail || "not available")}</span>`;
}

export function renderSpeechInstall(_g: Group, host: HTMLElement): void {
  const sp = live.speech;
  host.innerHTML = `<div class="field"><div class="lab">Whisper<small>Turns push-to-talk questions into text on your PC, offline. Non-English recognition requires a multilingual model such as small (about 464 MB); .en models recognize English only. Whisper does not support every language offered by speech providers. The selected model downloads when needed and is kept across updates. Offline recognition is unavailable until its model is ready. Online recognition can still work; Windows fallback needs a matching installed recognizer. Save language or model changes, then restart the coach.</small></div>
    <div class="ctl"><span id="sp-msg">${speechLine(sp)}</span><button class="btn" id="sp-install">${sp.state === "error" && sp.package && !sp.model ? "Download now" : "Check and repair"}</button></div></div>
    <div class="field"><div class="lab">Models on this PC<small>Download a model ahead of time to try it later, or delete ones you don't use to free disk space. Pick the one push-to-talk uses in "Whisper model" above.</small></div>
    <div class="ctl wide-ctl" id="sp-models"></div></div>`;
  $("sp-install").onclick = async () => {
    $<HTMLButtonElement>("sp-install").disabled = true;
    await call("speech:install");
    if (document.getElementById("sp-install")) $<HTMLButtonElement>("sp-install").disabled = false;
  };
  renderWhisperModels();
}

/** The models list: `list` when an answer brought it, else asked for again. */
async function renderWhisperModels(list?: WhisperModel[]): Promise<void> {
  if (list) whisperModels = list;
  else whisperModels = (await call("speech:models")) || whisperModels;
  const host = document.getElementById("sp-models");
  if (!host) return;
  const sp = live.speech;
  const busy = !!sp.fetching || sp.state === "installing";
  host.innerHTML = whisperModels.map((m) => {
    const inUse = m.name === sp.modelName;
    const fetching = sp.fetching === m.name;
    const state = fetching ? `<span class="warn">Downloading${sp.totalMB ? ` ${sp.gotMB || 0} of ${sp.totalMB} MB` : "…"}</span>`
      : m.downloaded ? `<span class="ok">${icon("check")}On this PC${inUse ? ", in use" : ""}</span>` : `<span class="dim">Not downloaded</span>`;
    const btn = m.downloaded
      ? (inUse || m.shipped ? "" : `<button class="btn" data-sp-del="${esc(m.name)}" ${busy ? "disabled" : ""}>${icon("trash-2")}Delete</button>`)
      : `<button class="btn" data-sp-get="${esc(m.name)}" ${busy ? "disabled" : ""}>${icon("download")}Download</button>`;
    return `<div class="rem-row"><div class="grow"><b>${esc(m.name)}</b> <span class="dim">${m.mb >= 1024 ? (m.mb / 1024).toFixed(1) + " GB" : m.mb + " MB"}</span><small>${esc(m.note)}</small></div>${state}${btn}</div>`;
  }).join("");
  host.querySelectorAll<HTMLButtonElement>("[data-sp-get]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    renderWhisperModels((await call("speech:download", b.dataset.spGet!)) || undefined);
  }));
  host.querySelectorAll<HTMLButtonElement>("[data-sp-del]").forEach((b) => (b.onclick = async () => {
    const r = await call("speech:delete", b.dataset.spDel!);
    if (r && !r.ok) toast("That model is in use or downloading, so it stays.");
    renderWhisperModels(r?.models);
  }));
}

function onSpeechStatus(s: SpeechStatus): void {
  const prev = live.speech;
  const moved = s.state !== lastSpeechState || s.fetching !== prev.fetching;
  live.speech = s;
  lastSpeechState = s.state;
  const el = document.getElementById("sp-msg");
  if (el) el.innerHTML = speechLine(s);
  if ((s.state === "downloading" || s.state === "installing") && s.detail !== lastSpeechToast) toast((lastSpeechToast = s.detail!));
  else if (s.fetching && s.fetching !== prev.fetching) toast(`Downloading the ${s.fetching} speech model…`);
  if (s.failed && s.failed !== prev.failed) toast(s.failed);
  taskBar();
  if (document.getElementById("sp-models")) renderWhisperModels(moved ? undefined : whisperModels);
  if (moved) refreshStatus();     // progress ticks every second; the Status page only needs the state changes
}

export function wireSpeechStatus(): void {
  glide.on("speech:status", onSpeechStatus);
}
