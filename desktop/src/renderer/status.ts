// The Status page: what's missing or out of date, and the fix for each. The app's own checks (the coach process,
// the addon and WoW folder, speech recognition, updates, the walking map) come first; the engine's /health adds
// the game connection, the route, the databases and the AI. Each thing has one owner: the app checks the install
// (WoW folder, addon installed, RestedXP installed); under the app the engine leaves those out and
// reports only the addon the game is running. The rail's badge counts what needs attention.
import { taskBar } from "./activity.js";
import { call, quiet } from "./bridge.js";
import { ENGINE_LABEL } from "./coach.js";
import { $, esc, icon, toast } from "./dom.js";
import { currentPage, show } from "./nav.js";
import { openSettingsTab } from "./settings/page.js";
import { mapName, rebuildWalkMap } from "./settings/walk-map.js";
import { coachRunning, engineGet, enginePost, live } from "./state.js";
import { AddonStatus, Obj } from "./types.js";

interface Check { id: string; area: string; status: "ok" | "warn" | "error" | "info"; title: string; detail?: string; action?: string | null }
const AREAS: [string, string][] = [["app", "The app"], ["game", "Game connection"], ["addon", "Addon and WoW folder"],
  ["route", "RestedXP route"], ["data", "Quest database and knowledge"], ["game_files", "Game files"], ["ai", "AI coach and voice"]];
const ACTIONS: Record<string, { label: string; icon: string }> = {
  refresh: { label: "Re-read now", icon: "refresh-cw" }, refresh_force: { label: "Rebuild", icon: "refresh-cw" },
  addon_install: { label: "Install addon", icon: "download" }, wow_dir: { label: "Set WoW folder", icon: "folder-open" },
  keys: { label: "Add a key", icon: "key-round" }, engine_start: { label: "Start coach", icon: "play" },
  nav_rebuild: { label: "Rebuild walking map", icon: "refresh-cw" },
  speech_repair: { label: "Repair", icon: "mic" }, speech_download: { label: "Download", icon: "download" }, update_install: { label: "Restart to update", icon: "download" }, update_download: { label: "Download", icon: "download" },
};
const ORDER: Record<string, number> = { error: 0, warn: 1, info: 2, ok: 3 };
let statusBusy = false;

function engineCheck(): Check | null {
  const st = live.engineState;
  if (st === "running") return null;
  return st === "crashed"
    ? { id: "engine", area: "app", status: "error", title: "The coach stopped with an error", detail: "The Logs page shows why. Start tries again.", action: "engine_start" }
    : ["starting", "building", "stopping"].includes(st)
      ? { id: "engine", area: "app", status: "info", title: `The coach is ${ENGINE_LABEL[st].toLowerCase()}`, detail: "Game and knowledge checks appear once it's running." }
      : { id: "engine", area: "app", status: "warn", title: "The coach isn't running", detail: "Start it to read the game and coach you.", action: "engine_start" };
}

function addonChecks(a: AddonStatus): Check[] {
  const c: Check[] = [];
  if (!a.wowDir || !a.wowOk) c.push({ id: "wow_dir", area: "addon", status: "error", title: a.wowDir ? "The WoW folder isn't a game folder" : "WoW folder not set", detail: "Glide installs its addon there and reads your recordings and the game's map data from it.", action: "wow_dir" });
  else if (!a.installed) c.push({ id: "addon_installed", area: "addon", status: "error", title: "Glide addon not installed", detail: `Install ${a.bundled}, then restart WoW and enable it in the AddOns list.`, action: "addon_install" });
  else if (!a.upToDate) c.push({ id: "addon_installed", area: "addon", status: "warn", title: `Addon update available: ${a.installed} to ${a.bundled}`, detail: "Install it, then restart WoW completely.", action: "addon_install" });
  if (a.wowOk && !a.rxp) c.push({ id: "rxp_app", area: "route", status: "warn", title: "RestedXP not installed", detail: "The coach follows RestedXP's guides. Install RestedXP in this WoW folder." });
  return c;
}

function speechCheck(): Check | null {
  const sp = live.speech;
  if (sp.state === "error" && sp.package && !sp.model)
    return { id: "speech", area: "ai", status: "warn", title: `Offline speech model ${sp.modelName} isn't downloaded`, detail: `${sp.detail || ""}. Push-to-talk uses Windows speech meanwhile.`, action: "speech_download" };
  if (sp.state === "error") return { id: "speech", area: "ai", status: "warn", title: "Offline speech recognition needs repair", detail: sp.detail || "", action: "speech_repair" };
  if (sp.state === "downloading") return { id: "speech", area: "ai", status: "info", title: `Downloading the offline speech model ${sp.fetching || sp.modelName}`, detail: `${sp.totalMB ? `${sp.gotMB || 0} of ${sp.totalMB} MB. ` : ""}One time; push-to-talk uses Windows speech until it's done.` };
  if (sp.state === "installing") return { id: "speech", area: "ai", status: "info", title: "Setting up speech recognition", detail: sp.detail || "" };
  if (sp.state === "ready") return { id: "speech", area: "ai", status: "ok", title: `Speech recognition: Whisper ${sp.modelName}`, detail: "Offline, on this PC." };
  return null;
}

function updateCheck(): Check | null {
  const u = live.update;
  if (u.state === "available") return { id: "update", area: "app", status: "info", title: `Glide ${u.version} is available`, detail: `You have ${u.current || "an older version"}. Download it now; you choose when to install.`, action: "update_download" };
  if (u.state === "ready") return { id: "update", area: "app", status: "info", title: `Glide ${u.version} is ready to install`, detail: "Restart to update installs it: the installer shows its progress, then Glide opens again.", action: "update_install" };
  return null;
}

/** The walking map: being built, failed, or made by an older builder (a rebuild is suggested, never forced). */
function walkMapCheck(): Check | null {
  const nv = live.nav;
  if (live.engineState !== "running" || !nv) return null;
  if (nv.state === "building")
    return { id: "walkmap", area: "game_files", status: "info", title: "Building the walking map", detail: `${nv.tiles ? `${mapName(nv.map!, "Map")}: ${nv.tile} of ${nv.tiles} map squares. ` : ""}In the background at the lowest priority; routes use the simpler map until it's done.` };
  if (nv.state === "failed")
    return { id: "walkmap", area: "game_files", status: "warn", title: "The walking map couldn't be built", detail: "Routes use the simpler map. The Logs page says why; rebuilding tries again.", action: "nav_rebuild" };
  if ((nv.outdated || []).length)
    return { id: "walkmap", area: "game_files", status: "warn", title: "Better walking routes are available", detail: `Your walking map (${nv.outdated.map((m: number) => mapName(m)).join(", ")}) was made by an older version of Glide's map builder. Rebuild it when it suits you: it runs in the background at the lowest priority, about an hour per continent, and the current map is used until it's done.`, action: "nav_rebuild" };
  if ((nv.built || []).length)
    return { id: "walkmap", area: "game_files", status: "ok", title: "Walking map", detail: `${nv.built.map((m: number) => mapName(m)).join(", ")}: terrain, buildings, caves, bridges and water.` };
  return null;
}

async function appChecks(): Promise<Check[]> {
  const engine = engineCheck();
  const a = await quiet("addon:status");
  const c: Check[] = [...(engine ? [engine] : []), ...(a ? addonChecks(a) : [])];
  for (const check of [speechCheck(), updateCheck(), walkMapCheck()]) if (check) c.push(check);
  return c;
}

export async function refreshStatus(): Promise<void> {
  if (statusBusy) return;
  statusBusy = true;
  try {
    let health: Obj | null = null;
    if (coachRunning()) {
      try { health = await (await engineGet("/health")).json(); } catch { health = null; }
    }
    const app = await appChecks();
    const ids = new Set(app.map((x) => x.id));
    const all: Check[] = [...app, ...((health?.checks || []) as Check[]).filter((x) => !ids.has(x.id))];
    renderStatus(all, health?.refresh || {});
  } finally {
    statusBusy = false;
  }
}

function renderStatus(all: Check[], refresh: Obj): void {
  const bad = all.filter((x) => x.status === "error").length, warn = all.filter((x) => x.status === "warn").length;
  const badge = $("status-badge");
  badge.hidden = !(bad + warn);
  badge.textContent = String(bad + warn);
  badge.className = `badge${bad ? " error" : ""}`;
  $("status-sum").textContent = bad + warn ? `${bad + warn} thing${bad + warn > 1 ? "s need" : " needs"} attention.` : "Everything Glide needs is in place.";
  // the most urgent thing leads, on the page's one parchment leaf, and isn't repeated in the list under it
  const lead = all.filter((x) => x.status === "error" || x.status === "warn").sort((a, b) => ORDER[a.status] - ORDER[b.status])[0];
  $("checks").innerHTML = AREAS.map(([area, title]) => checkGroupHtml(all.filter((x) => x.area === area && x !== lead), title)).join("");
  $("status-plate").innerHTML = leadHtml(lead);
  document.querySelectorAll<HTMLButtonElement>("#checks button[data-act], #status-plate button[data-act]").forEach((b) => (b.onclick = () => runAction(b.dataset.act!)));
  renderRefresh(refresh);
}

function checkGroupHtml(checks: Check[], title: string): string {
  const list = checks.sort((a, b) => ORDER[a.status] - ORDER[b.status]);
  if (!list.length) return "";
  const mark = (s: string) => `<span class="mark">${s === "ok" ? icon("check") : s === "error" ? icon("x") : ""}</span>`;
  return `<div class="group" aria-label="${esc(title)}"><div class="checks">${list.map((x) => {
    const act = x.action && ACTIONS[x.action];
    return `<div class="check s-${x.status}">${mark(x.status)}<div><b>${esc(x.title)}</b>${x.detail ? `<p>${esc(x.detail)}</p>` : ""}</div>${act ? `<button class="btn${x.status === "ok" ? " ghost" : ""}" data-act="${x.action}">${icon(act.icon)}${esc(act.label)}</button>` : "<span></span>"}</div>`;
  }).join("")}</div></div>`;
}

function leadHtml(lead: Check | undefined): string {
  const la = lead?.action && ACTIONS[lead.action];
  return lead
    ? `<div class="status-plate ${lead.status}"><div><h2>${icon(lead.status === "error" ? "circle-alert" : "alarm-clock", "icon sp-ic")}${esc(lead.title)}</h2>${lead.detail ? `<p>${esc(lead.detail)}</p>` : ""}</div>${la ? `<button class="btn primary big" data-act="${lead.action}">${icon(la.icon)}${esc(la.label)}</button>` : ""}</div>`
    : `<div class="status-plate ok"><div><h2>${icon("check", "icon sp-ic")}Everything Glide needs is in place</h2><p>Game link, addon, RestedXP, knowledge and game data are current.</p></div></div>`;
}

/** The re-read progress: its three stages while it runs, and for a while after it finished. */
function renderRefresh(refresh: Obj): void {
  const card = $("refresh-card"), st = refresh.state, running = st && !["waiting", "done"].includes(st);
  const recent = st === "done" && refresh.finished && Date.now() / 1000 - refresh.finished < 900;
  card.hidden = !(running || recent);
  $<HTMLButtonElement>("reread").disabled = !!running || live.engineState !== "running";
  $<HTMLButtonElement>("reread-force").disabled = !!running || live.engineState !== "running";
  if (running || recent) {
    const stages: [string, string, string][] = [["maps", "Game files", "maps"], ["glide.db", "Quest database", "glide"], ["kb.db", "Knowledge base", "kb"]];
    const idx = stages.findIndex((s) => s[0] === st);
    card.innerHTML = `<h2>${running ? "Re-reading the game data" : `Re-read finished in ${Math.round(refresh.seconds || 0)} s`}</h2><div class="stages">${stages.map(([, label, res], i) => {
      const r = refresh[res];
      const cls = running ? (i < idx ? "done" : i === idx ? "now" : "") : r === "failed" ? "failed" : "done";
      const text = running && i === idx ? "Working…" : r ? String(r).replace(/^./, (c) => c.toUpperCase()) : running && i > idx ? "Waiting" : "Done";
      return `<div class="stage ${cls}"><b>${label}</b><span>${esc(text)}</span></div>`;
    }).join("")}</div>`;
  }
  if (running && currentPage() === "status") window.setTimeout(refreshStatus, 2000);
}

async function postRefresh(force: boolean): Promise<void> {
  if (!live.engineUrl) return toast("Start the coach first.", true);
  try {
    const r = await (await enginePost("/refresh", { force })).json();
    if (r.refresh?.error) { toast(`Can't re-read: ${r.refresh.error}.`, true); return; }
    toast(r.refresh?.already ? "A re-read is already running." : force ? "Rebuilding everything from the game files." : "Re-reading the game data.");
    if (r.refresh) { live.refresh = r.refresh; taskBar(); }
  } catch (e) {
    toast(`Couldn't start the re-read: ${(e as Error).message}`, true);
  }
  window.setTimeout(refreshStatus, 600);
}

/** A check's button (or the lead's): the fix it names. */
async function runAction(act: string): Promise<void> {
  if (act === "refresh") return postRefresh(false);
  if (act === "refresh_force") return postRefresh(true);
  if (act === "nav_rebuild") return rebuildWalkMap();
  if (act === "engine_start") { await call("engine:start"); return; }
  if (act === "wow_dir") return show("addon");
  if (act === "keys") { openSettingsTab("keys"); return; }
  if (act === "speech_repair" || act === "speech_download") { await call("speech:install"); refreshStatus(); return; }
  if (act === "update_install") { await call("update:install"); return; }
  if (act === "update_download") { await call("update:download"); return; }
  if (act === "addon_install") {
    const r = await call("addon:install");
    if (r) toast(`${r.done.join(". ")}. Restart WoW completely to load it.`, false, 8000);
    refreshStatus();
  }
}

export function wireStatus(): void {
  $("reread").onclick = () => postRefresh(false);
  $("reread-force").onclick = () => postRefresh(true);
  window.setInterval(() => { if (currentPage() === "status" || live.engineState === "running") refreshStatus(); }, 20000);
}
