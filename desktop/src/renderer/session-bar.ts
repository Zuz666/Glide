// The session bar (footer): time, AI cost, calls, cache hits, the models in use, push-to-talk and the coach's
// mode, from the engine's state stream. The same stream feeds the activity, the walking map panel and reminders.
import { taskBar } from "./activity.js";
import { call, glide } from "./bridge.js";
import { $, cap } from "./dom.js";
import { renderReminders } from "./memory.js";
import { speech } from "./narrator.js";
import { currentPage } from "./nav.js";
import { liveGameChanged } from "./rxp-browser.js";
import { TTS_PROVIDER_KEY } from "./settings/key-links.js";
import { openSettingsTab } from "./settings/page.js";
import { refreshWalkMapPanel } from "./settings/walk-map.js";
import { wireSessionBarOrder } from "./session-bar-order.js";
import { live } from "./state.js";
import { CoachStats, LiteState, Models, TtsStatus } from "./types.js";

const fmtDur = (m: number) => (m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`);

/** A state from the light stream (/events?lite=1), or null when the coach stopped. */
export function sessionBar(d: LiteState | null): void {
  if (!d) {
    for (const id of ["sb-time", "sb-cost", "sb-calls", "sb-cache", "sb-fast", "sb-deep", "sb-listen", "sb-mode"]) $(id).textContent = "–";
    $("sb-fast-eff").hidden = true;
    $("sb-deep-eff").hidden = true;
    $("sb-fast-ic").innerHTML = '<use href="#i-mic"/>';
    $("sb-deep-ic").innerHTML = '<use href="#i-keyboard"/>';
    $("sb-mode").className = "";
    live.refresh = null;
    $("sb-translation").hidden = true;
    live.game = null;
    if (currentPage() === "guides") liveGameChanged();
    taskBar();
    return;
  }
  live.refresh = d.refresh || null;
  live.nav = d.navbuild || null;
  refreshWalkMapPanel();
  const prevRem = JSON.stringify(live.reminders);
  live.reminders = d.reminders || [];
  if (currentPage() === "memory" && JSON.stringify(live.reminders) !== prevRem) renderReminders();
  live.game = d.class ? { name: d.name ?? null, class: d.class, race: d.race ?? null, faction: d.faction ?? null,
    guide: d.guide ?? null, guide_group: d.guide_group ?? null, step: d.step ?? null, rxp_settings: d.rxp_settings ?? null } : null;
  if (currentPage() === "guides") liveGameChanged();
  showUsage(d.session_minutes, d.coach || {});
  showModels(d.models || {});
  showTranslation(d.models || {});
  showMode(String((d.coach || {}).mode || ""));
}

function showUsage(minutes: number | undefined, c: Partial<CoachStats>): void {
  $("sb-time").textContent = fmtDur(Math.round(minutes || 0));
  const cost = Number(c.cost_usd || 0);
  $("sb-cost").textContent = `$${cost < 10 ? cost.toFixed(3) : cost.toFixed(2)}`;
  $("sb-calls").textContent = String(c.calls ?? 0);
  const tot = (c.cache_read_tokens || 0) + (c.uncached_in_tokens || 0) + (c.cache_write_tokens || 0);
  $("sb-cache").textContent = tot ? `${Math.round((100 * (c.cache_read_tokens || 0)) / tot)}%` : "–";
}

function shortEffort(effort: string): string {
  if (effort === "minimal") return "min";
  if (effort === "medium") return "med";
  return effort;
}

function setEffortBadge(id: string, hasModel: boolean, effort: string): void {
  const el = $(id);
  el.hidden = !hasModel;
  el.textContent = shortEffort(effort);
  el.dataset.auto = effort === "auto" ? "1" : "0";
}

function fmtRoleModel(name: string | null | undefined, id: string | null | undefined): string {
  if (name && id && name !== id) return `${name} (${id})`;
  return name || id || "not configured";
}

function automaticThinkingHint(m: Partial<Models>, role: "fast" | "deep", effort: string): string {
  const state = m.tune_thinking === true ? "On" : m.tune_thinking === false ? "Off" : "unavailable";
  const label = `Automatic thinking tuning: ${state}`;
  if (effort !== "auto") return `${label} — not applied (explicit ${effort} effort).`;
  if (m.tune_thinking === false) return `${label} — provider default (no automatic override).`;
  const automatic = m.automatic_reasoning?.[role];
  if (m.tune_thinking !== true || !automatic) return `${label} — automatic level unavailable.`;
  const controls = [
    automatic.effort ? `${automatic.effort} effort requested` : "",
    automatic.thinking ? `thinking: ${automatic.thinking}` : "",
  ].filter(Boolean).join("; ");
  return controls ? `${label} — ${controls}.\nAutomatic options may fall back if rejected.`
    : `${label} — provider default (no automatic override).`;
}

function showModels(m: Partial<Models>): void {
  const fastLabel = m.fast_name || m.fast || "–";
  const deepLabel = m.deep_name || m.deep || "–";
  const fastEff = (m.fast_reasoning || "auto").trim().toLowerCase() || "auto";
  const deepEff = (m.deep_reasoning || "auto").trim().toLowerCase() || "auto";
  const pttDeep = m.voice_question === "deep";
  $("sb-fast").textContent = fastLabel;
  $("sb-deep").textContent = deepLabel;
  $("sb-fast-ic").innerHTML = `<use href="#i-${pttDeep ? "zap" : "mic"}"/>`;
  $("sb-deep-ic").innerHTML = `<use href="#i-${pttDeep ? "mic" : "keyboard"}"/>`;
  setEffortBadge("sb-fast-eff", Boolean(m.fast || m.fast_name), fastEff);
  setEffortBadge("sb-deep-eff", Boolean(m.deep || m.deep_name), deepEff);
  const fastUses = pttDeep
    ? `Live callouts, quest briefings and translation (spoken questions routed to Deep).\nDeep model: ${fmtRoleModel(m.deep_name, m.deep)}; reasoning effort: ${deepEff}.`
    : "Live callouts, spoken (push-to-talk) questions, quest briefings and translation.";
  const deepUses = pttDeep
    ? "Typed & spoken (push-to-talk) questions, pace reviews and memory reflection."
    : "Typed questions, pace reviews and memory reflection.";
  $("sb-fast-btn").title = `Fast role: ${fmtRoleModel(m.fast_name, m.fast)}\nReasoning effort: ${fastEff}\n${automaticThinkingHint(m, "fast", fastEff)}\n${fastUses}\nClick to open Fast model settings.`;
  $("sb-deep-btn").title = `Deep role: ${fmtRoleModel(m.deep_name, m.deep)}\nReasoning effort: ${deepEff}\n${automaticThinkingHint(m, "deep", deepEff)}\n${deepUses}\nClick to open Deep model settings.`;
  const listen = String(m.listen || ""), fb = m.listen_fallback ? String(m.listen_fallback) : "";
  $("sb-listen").textContent = listen === "off" ? "Push-to-talk off" : listen === "loading" ? "Loading…" : listen ? cap(listen) : "–";
  $("sb-listen-wrap").classList.toggle("off", listen === "off");
  $("sb-listen-wrap").title = `Speech-to-text for push-to-talk: ${$("sb-listen").textContent}${fb ? `, falls back to ${cap(fb)}` : ""}\nClick to open Push-to-talk settings.`;
}

function showTranslation(m: Partial<Models>): void {
  const button = $("sb-translation"), translation = m.translation;
  button.hidden = !translation || translation.language === "en";
  if (button.hidden || !translation) return;
  const language = translation.language.toUpperCase();
  const label = `${translation.available ? "AI translation" : "Translation off"} · ${language}`;
  $("sb-translation-label").textContent = translation.available ? `AI → ${language}` : `AI off · ${language}`;
  button.classList.toggle("off", !translation.available);
  button.setAttribute("aria-label", label);
  button.title = `Coach message translation → ${language}\nModel: ${m.fast_name || m.fast || "not configured"} (${m.fast || "none"})\n`
    + (translation.available ? "Uses the AI model; translation calls are included in AI cost. Individual failures leave the original English text."
      : "The translation model is unavailable or disabled; deterministic messages stay in English.")
    + "\nClick to open Language settings. News and community notes are not translated.";
}

/** AI live, paused (and why) or offline with rules only. */
function showMode(mode: string): void {
  $("sb-mode").textContent = mode.startsWith("offline:") ? `Paused: ${mode.slice(8).trim()}` : mode === "offline" ? "Offline, rules only" : mode ? "AI live" : "–";
  const modeCls = mode.startsWith("offline:") ? "paused" : mode === "offline" ? "offline" : "";
  $("sb-mode-wrap").className = `sb-item sb-mode ${modeCls}`;
  $("sb-mode-wrap").title = `Coach: ${$("sb-mode").textContent}`;
  $("sb-mode-ic").innerHTML = `<use href="#i-${modeCls ? "wifi-off" : "bot"}"/>`;
  $("sb-models").classList.toggle("idle", mode === "offline" || mode.startsWith("offline:"));
}

function ttsPhaseLabel(s: TtsStatus): string {
  if (s.phase === "muted") return "Muted";
  if (s.phase === "no-key") return "No API key";
  if (s.phase === "connecting") return s.totalSlots ? `Connecting (${s.readySlots ?? 0}/${s.totalSlots})` : "Connecting…";
  if (s.phase === "generating") return "Synthesizing…";
  if (s.phase === "speaking") return "Speaking";
  if (s.phase === "fallback") return s.fallbackVoice ? `Fallback · ${s.fallbackVoice}` : "SAPI5 fallback";
  if (s.phase === "error") return "TTS error";
  return s.totalSlots ? `${s.readySlots ?? 0}/${s.totalSlots} ready` : "Ready";
}

export function showTtsStatus(s: TtsStatus): void {
  const wrap = $("sb-speak-wrap");
  speech.onTtsStatus(s);
  const stateText = ttsPhaseLabel(s);
  $("sb-speak").textContent = s.voiceLabel;
  $("sb-speak-state").textContent = stateText;
  wrap.dataset.phase = s.phase;
  wrap.dataset.provider = s.provider;
  const missingKey = s.phase === "no-key" ? TTS_PROVIDER_KEY[s.provider] : "";
  const workerLine = s.workerState
    ? ` (worker: ${s.workerState}${s.totalSlots ? `, ${s.readySlots ?? 0}/${s.totalSlots} slots` : ""})`
    : "";
  wrap.title = `Coach voice: ${s.fullVoice}\nStatus: ${stateText}${workerLine}`
    + `${s.detail ? `\nDiagnostic: ${s.detail}` : ""}\n${missingKey ? `Click to set ${missingKey} in Credentials.` : "Click to open Voice settings."}`;
}

export function refreshTtsStatus(): void {
  void call("tts:status:get").then((s) => { if (s) showTtsStatus(s); });
}

export function wireSessionBar(): void {
  $("sb-fast-btn").onclick = () => openSettingsTab("models", "fast");
  $("sb-deep-btn").onclick = () => openSettingsTab("models", "deep");
  $("sb-listen-wrap").onclick = () => openSettingsTab("voice", "stt");
  $("sb-speak-wrap").onclick = () => {
    const wrap = $("sb-speak-wrap");
    const env = wrap.dataset.phase === "no-key" ? TTS_PROVIDER_KEY[wrap.dataset.provider || ""] : "";
    if (env) openSettingsTab("keys", `key:${env}`);
    else openSettingsTab("voice", "tts");
  };
  $("sb-translation").onclick = () => openSettingsTab("language");
  glide.on("tts:status", showTtsStatus);
  refreshTtsStatus();
  wireSessionBarOrder();
}
