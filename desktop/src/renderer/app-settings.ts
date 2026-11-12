// The app's own settings in the window: taken whenever the main process sends them, shown in the footer (mute
// button, the narrator's voice), handed to the narrator, and kept in step with the coach page's voice controls.
import { call, glide } from "./bridge.js";
import { $ } from "./dom.js";
import { speech } from "./narrator.js";
import { refreshTtsStatus } from "./session-bar.js";
import { orderSessionBar } from "./session-bar-order.js";
import { live } from "./state.js";
import { AppSettings, Obj } from "./types.js";
export function applyAppSettings(s: AppSettings): void {
  live.appSettings = s;
  setTimeout(postVoice, 0);
  const muted = !!s.muted;
  speech.configure(muted, s.speech || {});
  showMute(muted);
  showVolume(Number(s.speech?.volume ?? 1), muted);
  refreshTtsStatus();
  orderSessionBar(s.sessionBarOrder);
}

/** Save a patch to the app settings and take what the main process answers (unchanged when the call fails). */
export async function updateAppSettings(patch: Obj): Promise<void> {
  applyAppSettings((await call("app:settings:set", patch)) || live.appSettings);
}

function showMute(muted: boolean): void {
  const mb = $("sb-mute");
  mb.classList.toggle("on", muted);
  mb.setAttribute("aria-pressed", String(muted));
  mb.title = muted ? "The narrator is muted: callouts still show. Click to unmute (Ctrl+Alt+M)" : "Mute the narrator (Ctrl+Alt+M)";
  mb.setAttribute("aria-label", muted ? "Unmute the narrator" : "Mute the narrator");
  mb.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${muted ? "volume-x" : "volume-2"}"/></svg>${muted ? '<span class="sb-l">muted</span>' : ""}`;
}

// ---- the narrator's volume: the footer's speaker mutes on a click; pointing at it raises a vertical volume bar
// above it (the wheel over the speaker steps it too). The bar lives on the page, not in the footer, which clips.

const pct = (v: number) => `${Math.round(v * 100)}%`;

function showVolume(v: number, muted: boolean): void {
  const range = $<HTMLInputElement>("sb-vol");
  if (document.activeElement !== range) range.value = String(v);
  $("sb-vol-v").textContent = muted ? "Muted" : pct(Number(range.value));
  $("sb-vol-pop").classList.toggle("muted", muted);
  range.style.setProperty("--fill", pct(Number(range.value)));
  const settingsVol = document.getElementById("f_app_speech_volume") as HTMLInputElement | null;
  const fieldEl = settingsVol?.closest(".field");
  if (settingsVol && document.activeElement !== settingsVol && !fieldEl?.classList.contains("changed")) {
    settingsVol.value = String(v);
    if (settingsVol.nextElementSibling) settingsVol.nextElementSibling.textContent = String(v);
  }
}

/** A volume from the bar or the wheel: saved, and it unmutes (turning the narrator up means you want to hear it). */
async function setVolume(v: number): Promise<void> {
  v = Math.round(Math.max(0, Math.min(1, v)) * 20) / 20;
  if (live.appSettings.muted && v > 0) await call("app:mute", false);
  await updateAppSettings({ speech: { volume: v } });
}

function wireVolume(): void {
  const wrap = $("sb-vol-wrap"), pop = $("sb-vol-pop"), range = $<HTMLInputElement>("sb-vol");
  document.body.appendChild(pop);
  let closing: ReturnType<typeof setTimeout> | undefined;
  const open = () => {
    clearTimeout(closing);
    const r = wrap.getBoundingClientRect();
    pop.style.left = `${r.left + r.width / 2}px`;
    pop.style.bottom = `${window.innerHeight - r.top + 8}px`;
    pop.classList.add("on");
  };
  const close = () => {
    clearTimeout(closing);
    closing = setTimeout(() => { if (document.activeElement !== range) pop.classList.remove("on"); }, 350);
  };
  for (const el of [wrap, pop]) {
    el.addEventListener("pointerenter", open);
    el.addEventListener("pointerleave", close);
  }
  $("sb-mute").addEventListener("focus", open);
  range.addEventListener("blur", close);
  range.addEventListener("input", () => {
    $("sb-vol-v").textContent = pct(Number(range.value));
    range.style.setProperty("--fill", pct(Number(range.value)));
  });
  range.addEventListener("change", () => setVolume(Number(range.value)));
  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    open();
    setVolume(Number(live.appSettings.speech?.volume ?? 1) + (e.deltaY < 0 ? 0.05 : -0.05));
  }, { passive: false });
}

/** Tell the coach page the narrator's volume and mute state (its level-up fanfare follows them). */
function postVoice(): void {
  const f = document.getElementById("dash") as HTMLIFrameElement | null;
  if (!f?.contentWindow || !live.engineUrl) return;
  f.contentWindow.postMessage({ glide: "voice", volume: Number(live.appSettings.speech?.volume ?? 1), muted: !!live.appSettings.muted },
                              new URL(live.engineUrl).origin);          // only the coach page, never a page it was swapped for
}

export function wireAppSettings(): void {
  $("sb-mute").onclick = () => call("app:mute", !live.appSettings.muted);
  wireVolume();
  // the coach page asks for the voice state when it loads
  window.addEventListener("message", (e) => {
    const f = document.getElementById("dash") as HTMLIFrameElement | null;
    const d = e.data as Obj;
    if (!f || e.source !== f.contentWindow || !d || d.glide !== "voice") return;
    postVoice();
  });
  glide.on("app:settings", (s) => {
    applyAppSettings(s);
  });
}
