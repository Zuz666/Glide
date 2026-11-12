// The walking map: the 3D map of where you can walk, built once from the game files. Its build status arrives on
// the state stream (navbuild); Settings > General shows it with a Rebuild button, as do the activity and Status.
import { taskBar } from "../activity.js";
import { $, esc, icon, toast } from "../dom.js";
import { dateTimeHtml } from "../date-time.js";
import { currentPage } from "../nav.js";
import { Group } from "../settings-schema.js";
import { coachRunning, enginePost, live } from "../state.js";
import { refreshStatus } from "../status.js";

const MAP_NAMES: Record<string, string> = { 0: "Eastern Kingdoms", 1: "Kalimdor" };

/** A continent's name, or "map N" ("Map N" where it starts a sentence). */
export const mapName = (m: number | string, word = "map") => MAP_NAMES[m] || `${word} ${m}`;

function walkMapLine(): string {
  const n = live.nav;
  if (!coachRunning()) return `<span class="dim">Start the coach to see it.</span>`;
  if (!n) return `<span class="dim">Not built in this mode.</span>`;
  if (n.state === "building")
    return `<span class="warn">Building${n.tiles ? `: ${esc(mapName(n.map!))}, ${n.tile} of ${n.tiles} map squares` : "…"}</span> <span class="dim">Routes use the simpler map until it's done.</span>`;
  if (n.state === "checking" || n.state === "waiting") return `<span class="dim">Waiting for the game files to be read…</span>`;
  if (n.state === "failed") return `<span class="bad">The last build failed: the Logs page says why.</span>`;
  if ((n.outdated || []).length)
    return `<span class="warn">${icon("circle-alert")}Made by an older version of the map builder.</span> <span class="dim">Rebuilding gives better routes; it's up to you, and the current map is used until it's done.</span>`;
  return `<span class="ok">${icon("check")}Built</span>${n.finished && n.maps_done ? ` <span class="dim">${n.maps_done} map${n.maps_done > 1 ? "s" : ""} rebuilt ${dateTimeHtml(n.finished * 1000, "dateTime")}</span>` : ""}`;
}

/** The Rebuild button stands out when a better map can be made. */
function walkMapButton(): void {
  const b = document.getElementById("wm-run");
  if (b) b.className = `btn${(live.nav?.outdated || []).length && live.nav?.state !== "building" ? " primary" : ""}`;
}

/** Redraw the panel's status line, when the panel is open. */
export function refreshWalkMapPanel(): void {
  if (document.getElementById("wm-status")) { $("wm-status").innerHTML = walkMapLine(); walkMapButton(); }
}

/** Start a walking map rebuild (Settings button, Status page action). */
export async function rebuildWalkMap(): Promise<void> {
  if (!coachRunning()) return toast("Start the coach first.", true);
  try {
    const r = await (await enginePost("/refresh", { nav: true })).json();
    if (r.refresh?.error) toast(`Can't rebuild: ${r.refresh.error}.`, true);
    else toast(r.refresh?.already ? "The walking map is already being built." : "Rebuilding the walking map in the background.");
    if (r.refresh) live.nav = r.refresh;
  } catch (e) {
    toast(`Couldn't start the rebuild: ${(e as Error).message}`, true);
  }
  refreshWalkMapPanel();
  taskBar();
  if (currentPage() === "status") refreshStatus();
}

export function renderWalkMap(_g: Group, host: HTMLElement): void {
  host.innerHTML = `<div class="field"><div class="lab">Walking map<small>The route on the coach's map and the directions it gives follow a 3D map of where you can really walk, which Glide builds from your game's own files: the terrain and its slopes (WoW stops you climbing anything steeper than 50 degrees, but you can hop small ledges), buildings and walls, caves and tunnels, bridges, trees, rocks and fences, and water. It's built once, after install, in the background at the lowest priority so the game keeps its frame rate. It takes about an hour per continent, starting with the one you're on, and picks up where it left off if you close Glide. Game patches don't rebuild it. When a newer Glide can make a better one, this panel and the Status page say so; rebuilding is up to you.</small></div>
    <div class="ctl"><span id="wm-status">${walkMapLine()}</span><button class="btn" id="wm-run">${icon("refresh-cw")}Rebuild walking map</button></div></div>`;
  $("wm-run").onclick = async () => {
    $<HTMLButtonElement>("wm-run").disabled = true;
    await rebuildWalkMap();
    if (document.getElementById("wm-run")) $<HTMLButtonElement>("wm-run").disabled = false;
  };
  walkMapButton();
}
