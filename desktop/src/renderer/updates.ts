// Updates in the window: announced once with a toast, then offered by the footer button (Download, then Restart to
// update; the download's progress shows in the footer's activity), with the check's state on the version number's tooltip.
import { taskBar } from "./activity.js";
import { call, glide, quiet } from "./bridge.js";
import { $, toast } from "./dom.js";
import { bindDateTime, clearDateTime } from "./date-time.js";
import { speech } from "./narrator.js";
import { getPath } from "./settings/fields.js";
import { enginePost, JSON_HEADERS, live } from "./state.js";
import { refreshStatus } from "./status.js";
import { Obj, UpdateStatus } from "./types.js";
import { UpdateVoice } from "./update-voice.js";
import { showWhatsNew, wnUpdateLine } from "./whats-new.js";

/** The character's voice direction while the coach plays one (config.json, which the coach writes), else "". */
function personaStyle(cfg: Obj): string {
  if (!String(getPath(cfg, "companion.llm.persona") || "").trim()) return "";
  return String(getPath(cfg, "companion.llm.persona_voice") || "").trim();
}

// the coach says it out loud too, and reminds every 10 minutes while the update waits (update-voice.ts), unless the
// player switched that off (Settings > General > "Say when an update is out": app settings updateVoice)
const voice = new UpdateVoice({
  say: (text, style) => { if (live.appSettings.updateVoice !== false) speech.say(text, false, style); },
  // the coach says it, with what's new, in its own voice (in character when it plays one): the engine's /announce
  announce: async (what, version, title, notes) => {
    if (live.appSettings.updateVoice === false) return true;          // switched off: nothing said, either way
    if (!live.engineUrl) return false;
    try {
      await enginePost("/announce", { what, version, title, notes }, JSON_HEADERS);
      return true;
    } catch {
      return false;
    }
  },
  style: async () => personaStyle((await quiet("config:get")) || live.config),
  setInterval: (fn, ms) => window.setInterval(fn, ms),
  clearInterval: (h) => window.clearInterval(h as number),
});

// the last answer the updater gave, "checking" left out: a re-check while an update waits ("checking", then the same
// answer) is no news, so it toasts nothing
let lastAnswer = "";

export function onUpdate(s: UpdateStatus): void {
  const was = lastAnswer;
  if (s.state !== "checking") lastAnswer = s.state;
  live.update = s;
  voice.onStatus(s);
  if (was !== s.state && s.state === "available") {
    refreshStatus();
    toast(`Glide ${s.version} is out. Download it from the footer when it suits you.`);
  }
  if (was !== s.state && s.state === "ready") {
    refreshStatus();
    toast(`Glide ${s.version} is downloaded. Restart to update when it suits you (footer button).`);
  }
  showFooterButton(s);
  taskBar();
  wnUpdateLine();
  const version = $("app-ver");
  if (s.state === "none" && s.checkedAt != null) {
    bindDateTime(version, s.checkedAt, "longTime", { attribute: "title", prefix: "What's new. Up to date (checked ", suffix: ")" });
  } else {
    clearDateTime(version);
    version.title = s.state === "none" ? "What's new. Up to date"
      : s.state === "disabled" ? (s.error || "automatic updates are off") : s.state === "error" ? `update check failed: ${s.error}. Click to retry`
      : s.state === "checking" ? "checking for updates…" : "click to check for updates";
  }
}

function showFooterButton(s: UpdateStatus): void {
  const ub = $("sb-update");
  ub.hidden = s.state !== "ready" && s.state !== "available";
  ub.dataset.act = s.state === "available" ? "download" : "install";
  if (s.state !== "ready") (ub as HTMLButtonElement).disabled = false;      // only an install in progress keeps it off
  ub.querySelector("use")?.setAttribute("href", s.state === "available" ? "#i-download" : "#i-rotate-cw");
  $("sb-update-l").textContent = s.state === "available" ? `Download Glide ${s.version || ""}` : `Restart to update to ${s.version || ""}`;
  ub.title = s.state === "available" ? `A newer version, Glide ${s.version || ""}, is out (you have ${s.current || "?"}). Click to download it; you choose when to install.`
    : `Glide ${s.version || ""} is downloaded. Click to install it: Glide closes, updates and opens again.`;
}

export function wireUpdates(): void {
  glide.on("update:status", onUpdate);
  glide.on("app:notice", (m) => toast(String(m)));
  $("app-ver").onclick = () => showWhatsNew();
  $("wn-check").onclick = () => call("update:check");
  $("sb-update").onclick = () => {
    const ub = $<HTMLButtonElement>("sb-update");
    if (ub.dataset.act === "download") return call("update:download");
    ub.disabled = true;                          // one click: Glide is on its way to restarting
    $("sb-update-l").textContent = "Restarting…";
    return call("update:install");
  };
}
