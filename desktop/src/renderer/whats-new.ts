// What's new: desktop/changelog.json, opened from the version number and once after an update.
import { updateAppSettings } from "./app-settings.js";
import { call } from "./bridge.js";
import { $, esc } from "./dom.js";
import { dateTimeHtml } from "./date-time.js";
import { show } from "./nav.js";
import { live } from "./state.js";
import { Obj } from "./types.js";

const vnum = (v: string) => String(v || "0").split(".").map((x) => parseInt(x, 10) || 0);
const vnewer = (a: string, b: string) => { const x = vnum(a), y = vnum(b); for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); return false; };
let appVersion = "";
let notesBefore = "";            // the notes the user had seen before this session's What's new

/** At start: remember which notes were seen; show them once when Glide was updated since (not on a first run). */
export async function catchUpNotes(version: string): Promise<void> {
  appVersion = version;
  notesBefore = String(live.appSettings.notesSeen || "");
  if (!notesBefore && appVersion) {             // first run: nothing to catch up on
    await updateAppSettings({ notesSeen: appVersion });
  } else if (appVersion && vnewer(appVersion, notesBefore)) {
    setTimeout(() => showWhatsNew(), 600);     // updated since the notes were last seen: show them once
  }
}

export async function showWhatsNew(): Promise<void> {
  show("whatsnew");
  const cl = (await call("app:changelog")) || { versions: [] };
  const list: Obj[] = (cl.versions || []).slice(0, 12);
  $("wn-lede").textContent = `You have Glide ${appVersion || "?"}. The last ${list.length} versions, newest first.`;
  $("wn-list").innerHTML = list.length ? list.map((v) => {
    const mine = v.version === appVersion, fresh = notesBefore && vnewer(v.version, notesBefore) && !vnewer(v.version, appVersion);
    return `<article class="wn-card${mine ? " mine" : ""}"><header><b class="wn-ver">${esc(v.version)}</b>${mine ? `<span class="wn-tag mine">You have this</span>` : ""}${fresh ? `<span class="wn-tag new">New</span>` : ""}<span class="wn-date">${v.date ? dateTimeHtml(v.date, "date") : ""}</span></header>
      <h2>${esc(v.title || "")}</h2><ul>${(v.items || []).map((i: string) => `<li>${esc(i)}</li>`).join("")}</ul></article>`;
  }).join("") : `<p class="dim">No release notes in this build.</p>`;
  wnUpdateLine();
  if (appVersion && live.appSettings.notesSeen !== appVersion) await updateAppSettings({ notesSeen: appVersion });
}

/** The update line on the What's new page. */
export function wnUpdateLine(): void {
  const s = live.update, el = document.getElementById("wn-upd");
  if (!el) return;
  el.textContent = s.state === "available" ? `Glide ${s.version} is out: Download it from the footer.`
    : s.state === "downloading" ? `Downloading ${s.version}…` : s.state === "ready" ? `${s.version} is ready: Restart to update.`
    : s.state === "checking" ? "Checking…" : s.state === "none" ? "Up to date." : s.state === "error" ? "The update check failed (see Logs)." : "";
}
