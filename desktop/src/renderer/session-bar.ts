// The session bar (footer): time, AI cost, calls, cache hits, the models in use, push-to-talk and the coach's
// mode, from the engine's state stream. The same stream feeds the activity, the walking map panel and reminders.
import { taskBar } from "./activity.js";
import { call, glide } from "./bridge.js";
import { $, cap } from "./dom.js";
import { renderReminders } from "./memory.js";
import { currentPage } from "./nav.js";
import { liveGameChanged } from "./rxp-browser.js";
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

function showModels(m: Partial<Models>): void {
  $("sb-fast").textContent = m.fast_name || m.fast || "–";
  $("sb-deep").textContent = m.deep_name || m.deep || "–";
  $("sb-fast-btn").title = `Fast role: ${m.fast || "not configured"}\nCallouts, quest briefings and translation. Click to open Fast model settings.`;
  $("sb-deep-btn").title = `Deep role: ${m.deep || "not configured"}\nTyped questions and pace reviews. Click to open Deep model settings.`;
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
  if (s.phase === "generating") return "Generating…";
  if (s.phase === "speaking") return "Speaking";
  if (s.phase === "fallback") return s.fallbackVoice ? `Fallback · ${s.fallbackVoice}` : "SAPI5 fallback";
  if (s.phase === "error") return "TTS error";
  return s.totalSlots ? `${s.readySlots ?? 0}/${s.totalSlots} ready` : "Ready";
}

export function showTtsStatus(s: TtsStatus): void {
  const wrap = $("sb-speak-wrap");
  const stateText = ttsPhaseLabel(s);
  $("sb-speak").textContent = s.voiceLabel;
  $("sb-speak-state").textContent = stateText;
  wrap.dataset.phase = s.phase;
  const workerLine = s.workerState
    ? ` (worker: ${s.workerState}${s.totalSlots ? `, ${s.readySlots ?? 0}/${s.totalSlots} slots` : ""})`
    : "";
  wrap.title = `Coach voice: ${s.fullVoice}\nStatus: ${stateText}${workerLine}`
    + `${s.detail ? `\nDiagnostic: ${s.detail}` : ""}\nClick to open Voice settings.`;
}

export function refreshTtsStatus(): void {
  void call("tts:status:get").then((s) => { if (s) showTtsStatus(s); });
}

export function wireSessionBar(): void {
  $("sb-fast-btn").onclick = () => openSettingsTab("models", "fast");
  $("sb-deep-btn").onclick = () => openSettingsTab("models", "deep");
  $("sb-listen-wrap").onclick = () => openSettingsTab("voice", "stt");
  $("sb-speak-wrap").onclick = () => openSettingsTab("voice", "tts");
  $("sb-translation").onclick = () => openSettingsTab("language");
  glide.on("tts:status", showTtsStatus);
  refreshTtsStatus();
  wireSessionBarOrder();
}
