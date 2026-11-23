// Interactive RestedXP Guide & Map Browser: browse imported guides, filter by race/class/mode as RestedXP does
// in-game, scrub through a visual step timeline, and walk the route over the zone map with clickable waypoints,
// directional arrows, objectives, and guide commentary.
// Which steps show and their numbers come from the engine (rxp-model.ts reads its masks); every step is known by its
// number in the guide (st.idx), never by its place in the filtered list, so a filter change keeps what was picked.
import type { RxpFactsPrep, RxpFactsTarget, RxpGuideAction, RxpGuideDetail, RxpGuideFacts, RxpGuideStep, RxpGuideSummary, RxpGuideWaypoint,
  RxpImport, RxpRouteLeg, RxpRoutes } from "../shared/ipc.js";
import { call, quiet } from "./bridge.js";
import { $, esc, icon } from "./dom.js";
import { currentPage, show, whenLeft } from "./nav.js";
import { speech } from "./narrator.js";
import { NarrationStop, stepLine, stepVia, stepWho, transitionLine } from "./rxp-narration.js";
import { coachRunning, live } from "./state.js";
import { comboOf, liveClassPick, liveGuide, liveRacePick, liveStepPos, racePick, Visibility } from "./rxp-model.js";
import { bearingDeg, between, yards, yardsText } from "./rxp-geometry.js";
import { dangerRuns, dingText, hazardLine, hazardWhere, nextDing, type PlanSteps, roughDur, span, spanText } from "./rxp-plan.js";
import { PREP_GLYPH, prepLabel, prepNeed, prepShown, prepTitle } from "./rxp-prep.js";
import { actionIcon, type GameIcon, lootIcon, prepIcon, rankIcon, stepIcon, targetIcon } from "./rxp-icons.js";
import { chainCrossings, chainOf, chainStops, continuesAfter, questsOf, type QuestRole, skipLine } from "./rxp-quests.js";
import { aheadOpacity, durText, hull, layoutTicks, legInfo, legPieces, pinStop, type Piece, planLegs, type PlanLeg, type Pt,
  type Stop, stopsOf } from "./rxp-route.js";

const MAP_W = 1002;
const MAP_H = 668;
const mapX = (v: number) => +(v * 10.02).toFixed(1);
const mapY = (v: number) => +(v * 6.68).toFixed(1);

// RestedXP's in-game guideTextColors (Themes.lua)
const RXP_COLORS: Record<string, string> = {
  RXP_FRIENDLY_: "#00ff25",
  RXP_ENEMY_: "#ff5722",
  RXP_LOOT_: "#00bcd4",
  RXP_WARN_: "#fcdc00",
  RXP_PICK_: "#db2eef",
  RXP_BUY_: "#2ecc40",
};

interface CatalogEntry extends RxpGuideSummary {
  packId: string;
}

interface VisibleStep {
  step: RxpGuideStep;
  src: number;              // the step's number in the guide (st.idx): what the page keys everything by
  rxpNum: number | null;    // RestedXP's step number for the picked character (null: another character's step)
  dimmed: boolean;          // shown only because "Show all <<" is on
  kind: "accept" | "turnin" | "combat" | "travel" | "hearth" | "death" | "step";
  x: number | null;
  y: number | null;
  ui_map: number | null;
  zone: string | null;
}

const cache = new Map<string, RxpGuideDetail>();
// each opened guide's real step count (its #include steps expanded): the list counts the guide's own step lines
const stepCounts = new Map<string, number>();
let catalog: CatalogEntry[] = [];
let currentGuide: RxpGuideDetail | null = null;
let vis: Visibility | null = null;          // the current guide's masks
let visibleSteps: VisibleStep[] = [];
let posBySrc = new Map<number, number>();   // st.idx -> position in visibleSteps
let activeSrc: number | null = null;        // the current step (st.idx)
let activeIdx = 0;          // its position in visibleSteps (derived from activeSrc)
let activeWpIdx = 0;        // the active sub-waypoint: its position in the step's own waypoints
let activeMapId: number | null = null;
/** The element picked on the map (shown in the inspector): a step's pin, one of its waypoints (sub = its position in
 * the step's waypoints), a leg (key, src = the step it arrives at), a grind loop or an objective (sub = its index). */
let selected: { kind: "step" | "wp" | "leg" | "loop" | "obj" | "hz"; src: number; sub?: number; key?: string } | null = null;
let playTimer: number | null = null;
let playMs = 1500;
const checkedObjs = new Set<string>();      // `${guide id}:${index}:${st.idx}:${action line}`
let defaultsApplied = false;                // the toggles start from the coach's settings once, then are the player's
let liveApplied = false;                    // the pickers took the live character's values (once a session)
let pickersTouched = false;                 // the player changed the race, class or a toggle
let liveSrc: number | null = null;          // the live character's step in the open guide (st.idx), null when not open
// the route: the legs between the listed steps' stops (rxp-route.ts planLegs), and the engine's worked-out legs
let planned: PlanLeg[] = [];
let legsByTo = new Map<number, PlanLeg[]>();     // st.idx -> the legs arriving at that step (the way in, then its own)
let stopsBySrc = new Map<number, Stop[]>();
const routeLegs = new Map<string, RxpRouteLeg>();    // the current guide's legs from addon:rxpRoutes, by leg key
let routesVersion = 0;
let visKey = "";                               // the steps listed (st.idx and number): what the layers were built for
const listKeyOf = () => visKey;                         // bumps when routeLegs changes: the route layer is rebuilt
let facts: RxpGuideFacts | null = null;        // what the knowledge base knows of the current guide (map targets)
let factsVersion = 0;
const factsCache = new Map<string, RxpGuideFacts | null>();
// the quest chain picked (a quest badge's button): its steps lit on the map, the timeline and in the list
let factsWant = "";                           // the facts last asked for (guide, class, race): an older answer is dropped
let scrubFrom: number | null = null;          // the list position the scrub started from (time and XP shown from there)
let scrubTimer: number | null = null;
let scrubHeld = false;                        // the slider's thumb is held
let focusQuest: number | null = null;
let chainSrcs = new Set<number>();            // the chain's steps (st.idx)

// Filter state (matches RestedXP's in-game player & profile checks)
const filter = {
  group: "",
  search: "",
  stepSearch: "",
  race: "",                 // the race picker's value: "Human", ..., or "Skyborne@Alliance" / "Skyborne@Horde"
  cls: "",
  ssf: true,                // the coach's defaults until the guide's own say otherwise (RxpGuideDetail.defaults)
  groupQuests: true,
  hardcore: false,
  showAllCond: false,
  showDone: false,          // the pins of the steps before the current one ("Done steps")
  follow: false,            // keep the current step in the middle of the map ("Follow step")
  narrate: false,           // Walk speaks each step and the way to the next, and waits for the voice ("Narrate")
};

// ---- the map's zoom and pan: the layer holding the image and the drawing, scaled and moved together -------------
const MAX_ZOOM = 8;
const view = { z: 1, fx: 0, fy: 0, map: null as number | null };   // fx, fy: the layer's offset as a share of the stage

function stageSize(): { w: number; h: number } {
  const r = $("rxpb-map-stage").getBoundingClientRect();
  return { w: r.width || 1, h: r.height || 1 };
}

/** Put the layer where `view` says, kept over the stage (no empty edge), and keep pins at their size on screen. */
function applyView(): void {
  view.z = Math.min(MAX_ZOOM, Math.max(1, view.z));
  view.fx = Math.min(0, Math.max(1 - view.z, view.fx));
  view.fy = Math.min(0, Math.max(1 - view.z, view.fy));
  const { w, h } = stageSize();
  $("rxpb-map-layer").style.transform = `translate(${(view.fx * w).toFixed(1)}px, ${(view.fy * h).toFixed(1)}px) scale(${view.z})`;
  $("rxpb-map-svg").style.setProperty("--inv", String(1 / view.z));
}

/** Zoom by `factor` keeping the stage point (sx, sy) (pixels; default the middle) where it is. */
function zoomAt(factor: number, sx?: number, sy?: number): void {
  const { w, h } = stageSize();
  const px = sx ?? w / 2, py = sy ?? h / 2;
  const z0 = view.z, z1 = Math.min(MAX_ZOOM, Math.max(1, z0 * factor));
  view.fx = (px - (px - view.fx * w) * (z1 / z0)) / w;
  view.fy = (py - (py - view.fy * h) * (z1 / z0)) / h;
  view.z = z1;
  applyView();
}

/** The whole zone again. */
function resetView(): void {
  view.z = 1;
  view.fx = view.fy = 0;
  applyView();
}

/** Put map point (x, y) (percent) in the middle, zooming in first when the whole zone is shown. */
function centerOn(x: number, y: number): void {
  if (view.z < 2) view.z = 2.5;
  view.fx = 0.5 - (x / 100) * view.z;
  view.fy = 0.5 - (y / 100) * view.z;
  applyView();
}

/** With "Follow step": centre the map on where the current step sends the player (its target waypoint, else its pin). */
function followStep(): void {
  if (!filter.follow || activeMapId == null) return;
  const tgt = targetStop(visibleSteps[activeIdx]);
  const at = tgt ? pointOn(activeMapId, tgt.um, tgt.x, tgt.y) : null;
  if (at) centerOn(at.x, at.y);
}

/** The characters the race and class pickers allow (positions in the guide's players). */
function pickedPlayers(): number[] {
  return vis ? vis.pick(filter.race, filter.cls) : [];
}

/** Whether an action applies to the picked characters (its own `<<` condition, as the engine read it). */
function actionApplies(a: RxpGuideAction): boolean {
  return !vis || vis.actionShown(a.vis, pickedPlayers());
}

/** The step's waypoints the picked characters get, each with its position in the step's own list (its letter). */
function stepWaypoints(st: RxpGuideStep): { w: RxpGuideWaypoint; wi: number }[] {
  return (st.waypoints || []).map((w, wi) => ({ w, wi }))
    .filter(({ w }) => filter.showAllCond || w.act == null || !st.actions[w.act] || actionApplies(st.actions[w.act]));
}

const letter = (wi: number) => String.fromCharCode(65 + (wi % 26));
const stepNum = (vs: VisibleStep) => (vs.rxpNum != null ? String(vs.rxpNum) : "–");

/** Convert WoW / RestedXP markup (`|cRXP_WARN_...|r`, `|cffRRGGBB...|r`, `|T...|t`, `\n`) into safe HTML. */
function formatRxpText(raw: string | null | undefined): string {
  if (!raw) return "";
  const s = String(raw).replace(/\\n/g, "\n").replace(/\|T[^|]*\|t/g, "").replace(/\|A[^|]*\|a/g, "")
    .replace(/\|H[^|]*\|h(.*?)\|h/g, "$1").replace(/npc:(.*?):\d+/g, "$1").replace(/::\d+/g, "");
  const out: string[] = [];
  let openSpans = 0;
  const re = /\|c(RXP_[A-Z]+_|[0-9a-fA-F]{8}|n[A-Za-z0-9_]+:)|\|r/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(esc(s.slice(last, m.index)));
    if (m[0] === "|r") {
      if (openSpans > 0) { out.push("</span>"); openSpans--; }
    } else {
      const code = m[1];
      let color = RXP_COLORS[code] || "#f2c24f";
      if (/^[0-9a-fA-F]{8}$/.test(code)) color = "#" + code.slice(2);
      out.push(`<span style="color:${color};font-weight:550">`);
      openSpans++;
    }
    last = re.lastIndex;
  }
  if (last < s.length) out.push(esc(s.slice(last)));
  while (openSpans > 0) { out.push("</span>"); openSpans--; }
  return out.join("").replace(/\n/g, "<br>");
}

function classifyStep(st: RxpGuideStep): VisibleStep["kind"] {
  const verbs = new Set((st.actions || []).map((a) => a.verb));
  if (verbs.has("deathskip")) return "death";
  if (verbs.has("hs") || verbs.has("home")) return "hearth";
  if (verbs.has("fly") || verbs.has("fp")) return "travel";
  if (verbs.has("turnin")) return "turnin";
  if (verbs.has("accept")) return "accept";
  if (verbs.has("complete") || verbs.has("kill") || verbs.has("collect") || verbs.has("mob")) return "combat";
  return "step";
}

/** A quest's name: the guide's (glide.db), else the knowledge base's. */
const questName = (a: RxpGuideAction) => a.quest_name || (a.quest_id != null ? facts?.quests?.[String(a.quest_id)]?.name : null) || null;
/** An item's name from the facts' prep list (the knowledge base, else the guide's own words). */
const itemName = (id: number | null) => (id != null ? facts?.prep?.find((p) => p.item_id === id && p.name)?.name : null) || null;
const QUEST_BADGE = new Set(["accept", "turnin", "complete", "collect"]);

interface Badge { cls: string; sym: string; label: string; quest?: number; gi?: GameIcon | null }

/** A glyph that gives way to the game's own icon once its image has loaded (glide://icon, rxp-icons.ts); an image
 * that fails to load leaves the glyph (watchGameIcons). */
function giHtml(sym: string, gi: GameIcon | null | undefined): string {
  if (!gi) return `<b>${esc(sym)}</b>`;
  return `<b class="rxpb-gw"><span>${esc(sym)}</span><img class="rxpb-gi" src="${esc(gi.url)}" alt="${esc(gi.what)}" draggable="false"></b>`;
}

/** The same in the map's SVG: the icon over its glyph or shape, shown once loaded; size in map units. */
function giSvg(gi: GameIcon | null | undefined, size: number): string {
  return gi ? `<image class="rxpb-gi" href="${esc(gi.url)}" x="${-size / 2}" y="${-size / 2}" width="${size}" height="${size}"/>` : "";
}

/** An image of a game icon loaded: its glyph steps aside; failed (no client, not decoded): the image goes. */
function watchGameIcons(root: HTMLElement): void {
  root.addEventListener("load", (e) => {
    const t = e.target as Element;
    if (t.classList?.contains("rxpb-gi")) t.parentElement?.classList.add("gi-on");
  }, true);
  root.addEventListener("error", (e) => {
    const t = e.target as Element;
    if (t.classList?.contains("rxpb-gi")) { t.parentElement?.classList.remove("gi-on"); t.remove(); }
  }, true);
}

/** An action's badge as HTML: a quest's badge is a button that lights the quest's chain. */
function badgeHtml(b: Badge, mini = false): string {
  const cls = `rxpb-badge${mini ? " mini" : ""} ${b.cls}`;
  if (b.quest == null) return `<span class="${cls}">${giHtml(b.sym, b.gi)} ${esc(b.label)}</span>`;
  const on = b.quest === focusQuest;
  return `<button type="button" tabindex="-1" class="${cls} rxpb-qb${on ? " on" : ""}" data-quest="${b.quest}" aria-pressed="${on}"
    title="${on ? "Stop showing this quest's chain (Esc)" : "Show this quest's chain on the map, the timeline and the list"}">${giHtml(b.sym, b.gi)} ${esc(b.label)}</button>`;
}

/** An action's badge; with its step, the game's icon for it (the step's targets name the creatures). */
function actionBadge(a: RxpGuideAction, st?: RxpGuideStep): Badge | null {
  const b = actionBadgeOf(a);
  if (b && a.quest_id && QUEST_BADGE.has(a.verb)) b.quest = a.quest_id;
  if (b && st) b.gi = actionIcon(a, facts?.icons, facts?.targets?.[String(st.idx)]);
  return b;
}

/** A step's badges, with their icons. */
const stepBadges = (st: RxpGuideStep) => (st.actions || []).map((a) => actionBadge(a, st)).filter(Boolean) as Badge[];

function actionBadgeOf(a: RxpGuideAction): Badge | null {
  const qn = questName(a);
  switch (a.verb) {
    case "accept":
      return { cls: "ab-accept", sym: "!", label: qn ? `Accept: ${qn}` : `Accept quest #${a.quest_id || "?"}` };
    case "turnin":
      return { cls: "ab-turnin", sym: "?", label: qn ? `Turn in: ${qn}` : `Turn in quest #${a.quest_id || "?"}` };
    case "complete":
      return { cls: "ab-combat", sym: "◎", label: (qn ? `${qn}` : `Quest #${a.quest_id || "?"}`) + (a.qty ? ` (×${a.qty})` : "") };
    case "collect":
      return { cls: "ab-loot", sym: "◆", label: `Collect ${a.qty && a.qty > 1 ? `${a.qty}× ` : ""}${a.entity_name || itemName(a.entity_id) || `item #${a.entity_id || "?"}`}` };
    case "kill":
    case "mob":
      return { cls: "ab-combat", sym: "⚔", label: `Target: ${(a.units && a.units.join(", ")) || a.entity_name || "mob"}` };
    case "target":
    case "unitscan":
      return { cls: "ab-friendly", sym: "⌖", label: `${a.verb === "unitscan" ? "Scan" : "NPC"}: ${(a.units && a.units.join(", ")) || a.entity_name || ""}` };
    case "hs":
      return { cls: "ab-hearth", sym: "⌂", label: `Hearthstone${a.location ? ` → ${a.location}` : ""}` };
    case "home":
      return { cls: "ab-hearth", sym: "⌂", label: `Set Hearthstone${a.location ? `: ${a.location}` : ""}` };
    case "fly":
      return { cls: "ab-travel", sym: "✦", label: `Fly to ${a.location || "destination"}` };
    case "fp":
      return { cls: "ab-travel", sym: "✦", label: `Get flight path${a.location ? `: ${a.location}` : ""}` };
    case "train":
      return { cls: "ab-train", sym: "📖", label: a.entity_name ? `Train ${a.entity_name}` : `Train spell #${a.entity_id || "?"}` };
    case "vendor":
    case "buy":
      return { cls: "ab-buy", sym: "◈", label: a.verb === "buy" ? `Buy ${a.qty || 1}× ${a.entity_name || `item #${a.entity_id || "?"}`}` : "Vendor / resupply" };
    case "deathskip":
      return { cls: "ab-death", sym: "☠", label: "Deathskip to Spirit Healer" };
    case "xp":
      return { cls: "ab-xp", sym: "★", label: `Grind to level ${a.level || "?"}${a.xp ? ` (+${a.xp} XP)` : ""}` };
    default:
      return null;
  }
}

function stepHeadline(st: RxpGuideStep): string {
  for (const a of st.actions || []) {
    if (a.text) return a.text.split("\n")[0];
  }
  for (const a of st.actions || []) {
    const b = actionBadge(a);
    if (b) return b.label;
  }
  if (st.zone && st.x != null && st.y != null) return `Go to ${st.zone} (${st.x.toFixed(1)}, ${st.y.toFixed(1)})`;
  return `Step ${st.idx}`;
}

function rebuildVisibleSteps(): void {
  if (!currentGuide) { visibleSteps = []; posBySrc = new Map(); return; }
  const g = currentGuide;
  const out: VisibleStep[] = [];
  // which steps the picked characters get under these settings, numbered as RestedXP numbers them (the engine's masks)
  const listed = vis
    ? vis.list(g.steps.map((st) => st.vis), comboOf(filter.ssf, filter.groupQuests, filter.hardcore), pickedPlayers(), filter.showAllCond)
    : g.steps.map((_st, pos) => ({ pos, num: pos + 1, dimmed: false }));
  // Carry forward the last known zone/waypoint when a step has no .goto of its own, so the timeline marker stays placed
  let lastMap: number | null = g.zones?.[0]?.ui_map ?? null;
  let lastZone: string | null = g.zones?.[0]?.name ?? null;
  let lastX: number | null = null;
  let lastY: number | null = null;
  for (const { pos, num, dimmed } of listed) {
    const st = g.steps[pos];
    if (st.ui_map != null) { lastMap = st.ui_map; lastZone = st.zone || lastZone; }
    if (st.x != null && st.y != null) { lastX = st.x; lastY = st.y; }
    out.push({
      step: st,
      src: st.idx,
      rxpNum: num,
      dimmed,
      kind: classifyStep(st),
      x: st.x ?? lastX,
      y: st.y ?? lastY,
      ui_map: st.ui_map ?? lastMap,
      zone: st.zone ?? lastZone,
    });
  }
  visibleSteps = out;
  posBySrc = new Map(out.map((vs, i) => [vs.src, i]));
  planned = planLegs(out.map((vs) => vs.step));
  legsByTo = new Map();
  for (const l of planned) {
    if (!legsByTo.has(l.to)) legsByTo.set(l.to, []);
    legsByTo.get(l.to)!.push(l);
  }
  stopsBySrc = new Map(out.map((vs) => [vs.src, stopsOf(vs.step)]));
  visKey = out.map((v) => `${v.src}${v.rxpNum ?? "-"}`).join(",");
  // the same step as before the change, else the next one still listed (else the last)
  activeIdx = 0;
  if (activeSrc != null && out.length) {
    const at = posBySrc.get(activeSrc);
    activeIdx = at ?? (() => { const i = out.findIndex((vs) => vs.src > activeSrc!); return i >= 0 ? i : out.length - 1; })();
    if (at === undefined) activeWpIdx = pinStop(out[activeIdx].step)?.wi ?? 0;
  }
  activeSrc = out[activeIdx]?.src ?? null;
  if (selected && !posBySrc.has(selected.src)) selected = null;
  const cur = visibleSteps[activeIdx];
  if (cur && cur.ui_map != null) activeMapId = cur.ui_map;
  else if (!activeMapId && g.zones.length) activeMapId = g.zones[0].ui_map;
}

function mapImageUrl(uiMap: number | null): string | null {
  if (!uiMap) return null;
  return currentGuide?.mapImages?.[uiMap] || (live.engineUrl ? `${live.engineUrl}/map/${uiMap}.png` : null);
}

const mapsGeo = () => currentGuide?.maps;
const mapName = (um: number | null | undefined) => (um == null ? "" : currentGuide?.zones.find((z) => z.ui_map === um)?.name
  || currentGuide?.maps?.[String(um)]?.name || `Map ${um}`);

/** Whether map `um` is a continent the guide's zones lie on (the continent view): zones there have a rectangle. */
function isParentMap(um: number | null): boolean {
  const maps = mapsGeo();
  return um != null && !!maps && Object.values(maps).some((g) => g?.parent === um && g.rect);
}

/** The continent view the guide gets: the parent map shared by at least two of its zones (each with its rectangle
 * there), or by one zone when a leg flies or crosses into another zone; null for a one-zone guide or without a
 * knowledge base. */
function continentTab(): number | null {
  const g = currentGuide, maps = mapsGeo();
  if (!g || !maps) return null;
  const count = new Map<number, number>();
  for (const z of g.zones) {
    const geo = maps[String(z.ui_map)];
    if (geo?.parent && geo.rect) count.set(geo.parent, (count.get(geo.parent) || 0) + 1);
  }
  let best: number | null = null;
  for (const [p, n] of count) if (n >= 2 && (best == null || n > (count.get(best) || 0))) best = p;
  return best;
}

/** A point of map `from` on map `um` (percent): the same map, or a zone's point on its continent view. */
function pointOn(um: number, from: number, x: number, y: number): { x: number; y: number } | null {
  if (from === um) return { x, y };
  return isParentMap(um) ? between(mapsGeo(), from, um, x, y) : null;
}

const SVGP = (pts: Pt[]) => pts.map(([x, y]) => `${mapX(x)},${mapY(y)}`).join(" ");
const MODE_GLYPH: Record<string, string> = { hearth: "⌂", death: "☠", boat: "⚓" };

/** One piece of a leg as SVG: walking legs as the coach screen draws them (a dark case, the line, gold dashes
 * flowing toward the step when it's the leg being walked), flights dotted cyan with a landing circle, hearths,
 * deathskips and boats as a faint arc with their glyph, and anything not worked out yet as a faint straight line. */
function pieceSvg(p: Piece, active: boolean): string {
  const out: string[] = [];
  const st = active ? "a" : "r";
  for (const run of p.runs) {
    const pts = SVGP(run);
    if (p.mode === "walk") {
      out.push(`<polyline class="rxpb-${st}-case" points="${pts}"/><polyline class="rxpb-${st}-walk" points="${pts}"/>`);
      if (active) out.push(`<polyline class="rxpb-leg-flow" points="${pts}"/>`);
    } else if (p.mode === "fly") {
      out.push(`<polyline class="rxpb-${st}-case fly" points="${pts}"/><polyline class="rxpb-${st}-fly" points="${pts}"/>`);
    } else if (p.mode === "straight") {
      out.push(`<polyline class="rxpb-${st}-approx" points="${pts}"/>`);
    } else {
      out.push(`<polyline class="rxpb-${st}-jump j-${p.mode}" points="${pts}"/>`);
    }
  }
  const last = p.runs[p.runs.length - 1], end = last[last.length - 1];
  if (p.mode === "fly" && !p.exit) {
    out.push(`<g transform="translate(${mapX(end[0])},${mapY(end[1])})"><g class="rxpb-body"><circle class="rxpb-land${active ? " on" : ""}" r="${active ? 6.5 : 4.5}"/></g></g>`);
  }
  if (MODE_GLYPH[p.mode]) {
    const all = p.runs.flat(), mid = all[Math.floor(all.length / 2)];
    out.push(`<g transform="translate(${mapX(mid[0])},${mapY(mid[1])})"><g class="rxpb-body"><circle class="rxpb-jump-dot j-${p.mode}" r="${active ? 10 : 8}"/>`
      + `<text class="rxpb-jump-glyph j-${p.mode}" dy="0.35em">${MODE_GLYPH[p.mode]}</text></g></g>`);
  }
  return out.join("");
}

/** A marker where a leg crosses the map's edge: "from <zone>" where it comes in, "to <zone>" where it leaves;
 * clicking it opens that zone's map. */
function edgeSvg(at: Pt, um: number, dir: "from" | "to", active: boolean): string {
  const [x, y] = at;
  // the label sits inside the map, away from the edge it marks
  const anchor = x > 92 ? "end" : x < 8 ? "start" : "middle";
  const dx = anchor === "end" ? -12 : anchor === "start" ? 12 : 0, dy = y > 92 ? -14 : y < 8 ? 18 : anchor === "middle" ? -14 : 4;
  const label = `${dir === "from" ? "from" : "to"} ${mapName(um)}`;
  return `<g class="rxpb-edge${active ? " on" : ""}" data-kind="edge" data-map="${um}" data-dir="${dir}" tabindex="0" role="button"
      aria-label="${esc(`${dir === "from" ? "The way comes from" : "The way goes on to"} ${mapName(um)}: open its map`)}" transform="translate(${mapX(x)},${mapY(y)})">
    <g class="rxpb-body"><circle r="${active ? 6 : 4.5}"/><text x="${dx}" y="${dy}" text-anchor="${anchor}">${esc(label)} ›</text></g></g>`;
}

const ROLE_NAME: Record<string, string> = { kill: "Kill", loot: "Loot", use: "Use", talk: "Talk to", area: "Go to" };
const RANK_NAME: Record<number, string> = { 1: "Elite", 2: "Rare elite", 3: "Boss", 4: "Rare" };

/** The shape of one objective point: a kill target a dot (gold-rimmed when elite), an object a diamond, a quest
 * area a ring. With the game's icon for the target (its beast family or creature type, the item an object gives),
 * the icon in a frame of the role's colour takes the shape's place once it has loaded. */
function objMarker(t: RxpFactsTarget, gi: GameIcon | null): string {
  if (!gi) return objShape(t);
  return `<g class="rxpb-sh">${objShape(t)}</g><rect class="rxpb-gf" x="-7.5" y="-7.5" width="15" height="15" rx="2.5"/>${giSvg(gi, 13)}`;
}

function objShape(t: RxpFactsTarget): string {
  if (t.kind === "object") return `<rect x="-3.6" y="-3.6" width="7.2" height="7.2" transform="rotate(45)"/>`;
  if (t.kind === "poi") return `<circle class="ring" r="5"/>`;
  return `<circle r="${(t.rank || 0) >= 1 ? 4.6 : 3.8}"/>`;
}

let layersKey = "";           // what the persistent layers were built for: rebuilt only when it changes

/** Build the map's persistent layers for this map: the route (every leg), grind loops and objectives (per step,
 * shown for the current one), the pins, and the zone outlines on a continent view. Selecting a step later only
 * moves classes (updateClasses) and redraws the active layer (drawActive). */
function buildLayers(mapId: number | null, imgUrl: string | null): void {
  const svgEl = $("rxpb-map-svg");
  const focusedSrc = (document.activeElement as Element | null)?.closest?.<SVGGElement>(".rxpb-pin")?.dataset.src;
  const maps = mapsGeo();
  const parentView = isParentMap(mapId);
  const grid: string[] = [], zones: string[] = [], route: string[] = [], loops: string[] = [], objs: string[] = [], pins: string[] = [];
  if (!imgUrl) {
    for (let g = 10; g < 100; g += 10) {
      grid.push(`<line x1="${mapX(g)}" y1="0" x2="${mapX(g)}" y2="${MAP_H}" class="rxpb-grid"/>`);
      grid.push(`<line x1="0" y1="${mapY(g)}" x2="${MAP_W}" y2="${mapY(g)}" class="rxpb-grid"/>`);
    }
  }
  if (mapId != null && parentView && currentGuide) {
    // the guide's zones on the continent: an outline and the name, a click opens the zone
    for (const z of currentGuide.zones) {
      const r = maps?.[String(z.ui_map)]?.rect;
      if (!r || maps?.[String(z.ui_map)]?.parent !== mapId) continue;
      const [x0, y0, x1, y1] = r;
      zones.push(`<g class="rxpb-zone" data-kind="zone" data-map="${z.ui_map}" tabindex="0" role="button" aria-label="${esc(`${z.name}: open its map`)}">
        <rect x="${mapX(Math.min(x0, x1))}" y="${mapY(Math.min(y0, y1))}" width="${Math.abs(mapX(x1) - mapX(x0)).toFixed(1)}" height="${Math.abs(mapY(y1) - mapY(y0)).toFixed(1)}"/>
        <g transform="translate(${mapX((x0 + x1) / 2)},${mapY(Math.min(y0, y1))})"><g class="rxpb-body"><text y="14" text-anchor="middle">${esc(z.name)}</text></g></g></g>`);
    }
  }
  if (mapId != null) {
    for (const leg of planned) {
      const eng = routeLegs.get(leg.key);
      const pieces = legPieces(leg, eng, mapId, maps);
      if (!pieces.length) continue;
      const hit = pieces.flatMap((p) => p.runs).map((run) => `<polyline class="rxpb-hit" points="${SVGP(run)}"/>`).join("");
      let edges = "";
      if (!parentView) {
        const first = pieces[0], last = pieces[pieces.length - 1];
        if (first.enter && leg.pa.um !== mapId) edges += edgeSvg(first.enter, leg.pa.um, "from", false);
        if (last.exit && leg.pb.um !== mapId) edges += edgeSvg(last.exit, leg.pb.um, "to", false);
      }
      route.push(`<g class="rxpb-rleg${eng ? "" : " pending"}" data-kind="leg" data-leg="${leg.key}" data-pos="${leg.pos}">${pieces.map((p) => pieceSvg(p, false)).join("")}${hit}${edges}</g>`);
    }
    for (const vs of visibleSteps) {
      if (!parentView) {
        const ls = (vs.step.loops || []).map((lp, li) => ({ lp, li })).filter(({ lp }) => (!lp.ui_map || lp.ui_map === mapId) && lp.points.length > 1);
        if (ls.length) {
          loops.push(`<g class="rxpb-sloops" data-src="${vs.src}">${ls.map(({ lp, li }) => `<${lp.kind === "loop" ? "polygon" : "polyline"} class="rxpb-loop" data-kind="loop" data-src="${vs.src}" data-sub="${li}" points="${SVGP(lp.points as Pt[])}"/>`).join("")}</g>`);
        }
        const ts = (facts?.targets?.[String(vs.src)] || []).map((t, ti) => ({ t, ti })).filter(({ t }) => t.ui_map === mapId && t.points?.length);
        if (ts.length) {
          const parts: string[] = [];
          for (const { t, ti } of ts) {
            for (const grp of t.groups || []) {
              if (grp.length >= 3) parts.push(`<polygon class="rxpb-ohull r-${t.role}" data-kind="obj" data-src="${vs.src}" data-sub="${ti}" points="${SVGP(hull(grp as Pt[]))}"/>`);
            }
          }
          for (const { t, ti } of ts) {
            const shape = objShape(t);
            // the game's icon once per cluster of spawns (its first point), so a camp reads as one boar, not twenty
            const gi = targetIcon(t, facts?.icons);
            const lead = new Set((t.groups?.length ? t.groups : [t.points]).map((grp) => grp[0] && `${grp[0][0]},${grp[0][1]}`));
            parts.push(`<g class="rxpb-obj r-${t.role} k-${t.kind}${(t.rank || 0) >= 1 ? " elite" : ""}" data-kind="obj" data-src="${vs.src}" data-sub="${ti}">`
              + t.points.map(([x, y]) => `<g transform="translate(${mapX(x)},${mapY(y)})"><g class="rxpb-body">${gi && lead.has(`${x},${y}`) ? objMarker(t, gi) : shape}</g></g>`).join("") + `</g>`);
          }
          objs.push(`<g class="rxpb-sobjs" data-src="${vs.src}">${parts.join("")}</g>`);
        }
      }
      const stop = pinStop(vs.step);
      const at = stop ? pointOn(mapId, stop.um, stop.x, stop.y) : null;
      if (!at || at.x < -2 || at.x > 102 || at.y < -2 || at.y > 102) continue;
      const sym = vs.kind === "accept" ? "!" : vs.kind === "turnin" ? "?" : vs.kind === "combat" ? "⚔"
        : vs.kind === "travel" ? "✦" : vs.kind === "hearth" ? "⌂" : vs.kind === "death" ? "☠" : stepNum(vs);
      pins.push(`<g class="rxpb-pin k-${vs.kind}${parentView ? " small" : ""}" data-kind="step" data-src="${vs.src}" data-pos="${posBySrc.get(vs.src)}"
        transform="translate(${mapX(at.x)},${mapY(at.y)})" tabindex="0" role="button" aria-label="Step ${stepNum(vs)}: ${esc(stepHeadline(vs.step))}">
        <g class="rxpb-body"><circle class="rxpb-pin-pulse" r="22"/><circle class="rxpb-pin-dot" r="10.5"/><text dy="0.35em">${esc(sym)}</text>${giSvg(parentView ? null : stepIcon(vs.kind, vs.step.actions || [], facts?.targets?.[String(vs.src)], facts?.icons), 15)}</g></g>`);
    }
  }
  const zoneSteps = visibleSteps.some((vs) => vs.ui_map === mapId && vs.step.x != null);
  let note = "";
  if (!zoneSteps && !parentView && currentGuide) {
    const why = currentGuide.world_only && !currentGuide.placed
      ? "Glide can't place this guide on the map yet: it needs the zone data in its knowledge base, which builds while the coach runs."
      : "No waypoints in this zone for the steps shown.";
    note = `<text class="rxpb-map-note" x="${MAP_W / 2}" y="${MAP_H / 2}" text-anchor="middle">${esc(why)}</text>`;
  }
  svgEl.innerHTML = `<defs></defs>${grid.join("")}<g id="rxpb-l-zones">${zones.join("")}</g><g id="rxpb-l-route">${route.join("")}</g>`
    + `<g id="rxpb-l-loops">${loops.join("")}</g><g id="rxpb-l-objectives">${objs.join("")}</g><g id="rxpb-l-leg"></g>`
    + `<g id="rxpb-l-chain"></g><g id="rxpb-l-pins">${pins.join("")}</g><g id="rxpb-l-active"></g><g id="rxpb-l-hz"></g><g id="rxpb-l-hl"></g>${note}`;
  if (focusedSrc) svgEl.querySelector<SVGGElement>(`.rxpb-pin[data-src="${focusedSrc}"]`)?.focus();
}

/** The current step and what's selected, as classes on the persistent layers: pins done / current / next, the legs
 * walked and still to walk, the current step's loops and objectives shown, the selected element marked. */
function updateClasses(): void {
  const svgEl = $("rxpb-map-svg");
  svgEl.classList.toggle("show-done", filter.showDone);
  const selSrc = selected?.src ?? null;
  svgEl.querySelectorAll<SVGGElement>(".rxpb-pin").forEach((g) => {
    const pos = Number(g.dataset.pos), src = Number(g.dataset.src);
    g.classList.toggle("is-cur", pos === activeIdx);
    g.classList.toggle("is-done", pos < activeIdx);
    g.classList.toggle("is-next", pos > activeIdx);
    g.classList.toggle("is-pinned", selected?.kind === "step" && src === selSrc && pos !== activeIdx);
    g.style.setProperty("--ahead", String(aheadOpacity(pos - activeIdx)));
    g.classList.toggle("in-chain", chainSrcs.has(src));
  });
  svgEl.classList.toggle("chain-on", focusQuest != null);
  svgEl.querySelectorAll<SVGGElement>(".rxpb-rleg").forEach((g) => {
    const pos = Number(g.dataset.pos);
    g.classList.toggle("is-done", pos < activeIdx);
    g.classList.toggle("is-cur", pos === activeIdx);
    g.style.setProperty("--ahead", String(aheadOpacity(pos - activeIdx)));
    g.classList.toggle("is-sel", selected?.kind === "leg" && g.dataset.leg === selected.key);
  });
  svgEl.querySelectorAll<SVGGElement>(".rxpb-sloops, .rxpb-sobjs").forEach((g) => {
    const src = Number(g.dataset.src);
    g.classList.toggle("on", src === activeSrc || (src === selSrc && selected?.kind !== "leg"));
  });
  svgEl.querySelectorAll<SVGElement>(".rxpb-loop, .rxpb-obj, .rxpb-ohull").forEach((el) => {
    el.classList.toggle("is-sel", !!selected && selected.kind === el.dataset.kind && Number(el.dataset.src) === selSrc
      && Number(el.dataset.sub) === selected.sub);
  });
  restack(document.getElementById("rxpb-l-route"));
  restack(document.getElementById("rxpb-l-pins"));
}

/** Draw a layer's steps nearest the current one on top (steps done at the bottom, then the furthest ahead up to the
 * current step), so a faded step far ahead never covers the step about to be walked, nor takes its pointer. */
function restack(layer: Element | null): void {
  if (!layer) return;
  const key = (el: Element) => {
    const pos = Number((el as SVGElement).dataset.pos);
    return Number.isNaN(pos) ? -2e9 : pos < activeIdx ? -1e9 + pos : activeIdx - pos;
  };
  const items = [...layer.children];
  const order = items.map((el, i) => ({ el, i, k: key(el) })).sort((a, b) => a.k - b.k || a.i - b.i);
  if (order.every((o, i) => o.el === items[i])) return;
  const focused = document.activeElement;
  for (const { el } of order) layer.appendChild(el);
  if (focused instanceof SVGElement && layer.contains(focused)) focused.focus();
}

/** Bring one step back to full strength on the map while it's looked at (its pin and the leg into it, which fade the
 * further ahead they are: aheadOpacity), or none: `pos` is its place in the visible steps. */
let litPos: number | null = null;
function lightStep(pos: number | null): void {
  if (pos === litPos) return;
  const svgEl = document.getElementById("rxpb-map-svg");
  svgEl?.querySelectorAll(".rxpb-pin.lit, .rxpb-rleg.lit").forEach((el) => el.classList.remove("lit"));
  litPos = pos;
  if (pos == null || !svgEl) return;
  svgEl.querySelectorAll(`.rxpb-pin[data-pos="${pos}"], .rxpb-rleg[data-pos="${pos}"]`).forEach((el) => el.classList.add("lit"));
}

/** The place in the visible steps of the step a map element belongs to (a leg: the step it leads to). */
function posOfEl(el: Element): number | null {
  const d = (el as HTMLElement | SVGElement).dataset;
  if (d.pos != null) return Number(d.pos);
  return d.src != null ? posBySrc.get(Number(d.src)) ?? null : null;
}

/** The step's waypoints that are stops (not path points), each with its letter: the map and the step's waypoint
 * buttons letter the same places. */
function stopLetters(st: RxpGuideStep): Map<number, string> {
  const out = new Map<number, string>();
  let n = 0;
  for (const { w, wi } of stepWaypoints(st)) if (w.mode !== "path") out.set(wi, letter(n++));
  return out;
}

/** The current step's target: the waypoint picked, else where its pin is. */
function targetStop(vs: VisibleStep | undefined): Stop | null {
  if (!vs) return null;
  return stopsBySrc.get(vs.src)?.find((s) => s.wi === activeWpIdx) || pinStop(vs.step);
}

/** The legs walked to reach the current step's target: the way in from the step before, then the step's own legs
 * up to the target. */
function activeLegs(): PlanLeg[] {
  const vs = visibleSteps[activeIdx];
  if (!vs) return [];
  const stops = stopsBySrc.get(vs.src) || [];
  const tgt = targetStop(vs);
  const upTo = Math.max(0, stops.findIndex((s) => s.wi === tgt?.wi));
  const order = new Map(stops.map((s, i) => [s.wi, i]));
  return (legsByTo.get(vs.src) || []).filter((l) => (order.get(l.b) ?? 0) <= upTo);
}

/** Redraw the active layer: the legs to the current target in the coach screen's style, an arrowhead into the
 * target, the current step's path points (small dots) and its lettered stops. */
function drawActive(mapId: number | null): void {
  const legEl = document.getElementById("rxpb-l-leg"), actEl = document.getElementById("rxpb-l-active");
  if (!legEl || !actEl) return;
  const vs = visibleSteps[activeIdx];
  const lines: string[] = [], marks: string[] = [];
  if (vs && mapId != null) {
    const parentView = isParentMap(mapId);
    const legs = activeLegs();
    let first: Pt | null = null, lastRun: Pt[] | null = null;
    for (const leg of legs) {
      const pieces = legPieces(leg, routeLegs.get(leg.key), mapId, mapsGeo());
      lines.push(`<g class="rxpb-aleg" data-kind="leg" data-leg="${leg.key}">${pieces.map((p) => pieceSvg(p, true)).join("")}`
        + pieces.flatMap((p) => p.runs).map((run) => `<polyline class="rxpb-hit" points="${SVGP(run)}"/>`).join("") + `</g>`);
      if (!parentView && pieces.length) {
        if (pieces[0].enter && leg.pa.um !== mapId) marks.push(edgeSvg(pieces[0].enter, leg.pa.um, "from", true));
        const lp = pieces[pieces.length - 1];
        if (lp.exit && leg.pb.um !== mapId) marks.push(edgeSvg(lp.exit, leg.pb.um, "to", true));
      }
      for (const p of pieces) {
        if (!first && !p.enter && leg === legs[0]) first = p.runs[0][0];
        if (p.mode !== "fly") lastRun = p.runs[p.runs.length - 1];
      }
    }
    if (first) marks.push(`<g transform="translate(${mapX(first[0])},${mapY(first[1])})"><g class="rxpb-body"><circle class="rxpb-leg-start" r="4.5"/></g></g>`);
    if (lastRun && lastRun.length > 1) {
      // an arrowhead short of the target pin, along the last stretch
      const [x2, y2] = lastRun[lastRun.length - 1].map((v, i) => (i ? mapY(v) : mapX(v)));
      let k = lastRun.length - 2;
      while (k > 0 && Math.hypot(mapX(lastRun[k][0]) - x2, mapY(lastRun[k][1]) - y2) < 24) k--;
      const x1 = mapX(lastRun[k][0]), y1 = mapY(lastRun[k][1]);
      const len = Math.hypot(x2 - x1, y2 - y1);
      if (len > 4) {
        const back = Math.min(len / 2, 22);
        const deg = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
        marks.push(`<g transform="translate(${(x2 - ((x2 - x1) / len) * back).toFixed(1)},${(y2 - ((y2 - y1) / len) * back).toFixed(1)}) rotate(${deg.toFixed(1)})">`
          + `<g class="rxpb-body"><path class="rxpb-arrowhead" d="M 7 0 L -5 -5.5 L -2 0 L -5 5.5 Z"/></g></g>`);
      }
    }
    // the current step's own places: path points as small dots, stops lettered when there are several
    const letters = stopLetters(vs.step);
    const stops = (stopsBySrc.get(vs.src) || []).filter((s) => s.wi >= 0);
    for (const s of stops) {
      const at = pointOn(mapId, s.um, s.x, s.y);
      if (!at) continue;
      const tr = `transform="translate(${mapX(at.x)},${mapY(at.y)})"`;
      if (s.path) {
        marks.push(`<g class="rxpb-pathdot" data-kind="wp" data-src="${vs.src}" data-sub="${s.wi}" ${tr}><g class="rxpb-body"><circle r="3.4"/></g></g>`);
      } else if (letters.size > 1 && letters.has(s.wi)) {
        const on = s.wi === activeWpIdx;
        marks.push(`<g class="rxpb-subwp${on ? " on" : ""}" data-kind="wp" data-src="${vs.src}" data-sub="${s.wi}" ${tr} tabindex="0" role="button"
          aria-label="Waypoint ${letters.get(s.wi)}"><g class="rxpb-body"><circle r="${on ? 11 : 8}"/><text dy="0.35em">${letters.get(s.wi)}</text></g></g>`);
      }
    }
  }
  legEl.innerHTML = lines.join("");
  actEl.innerHTML = marks.join("");
}

/** Where a listed step is on map `mapId` (its pin, else the place carried from the steps before it), or null. */
function stepPoint(src: number, mapId: number): { x: number; y: number } | null {
  const at = posBySrc.get(src);
  if (at === undefined) return null;
  const vs = visibleSteps[at];
  const stop = pinStop(vs.step);
  if (stop) return pointOn(mapId, stop.um, stop.x, stop.y);
  return vs.ui_map != null && vs.x != null && vs.y != null ? pointOn(mapId, vs.ui_map, vs.x, vs.y) : null;
}

/** The map a listed step happens on (its pin's, else the one carried from the steps before it). */
function stepMap(src: number): number | null {
  const at = posBySrc.get(src);
  if (at === undefined) return null;
  return pinStop(visibleSteps[at].step)?.um ?? visibleSteps[at].ui_map;
}

/** The picked quest chain on the map: a gold connector through its steps in step order, and where it goes on in
 * (or comes from) another zone, a "continues in <zone>" marker that opens that zone's map. */
function drawChain(mapId: number | null): void {
  const el = document.getElementById("rxpb-l-chain");
  if (!el) return;
  if (focusQuest == null || mapId == null) { el.innerHTML = ""; return; }
  const srcs = [...chainSrcs].filter((src) => posBySrc.has(src)).sort((a, b) => a - b);
  const parts: string[] = [];
  let run: Pt[] = [];
  const flush = () => { if (run.length > 1) parts.push(`<polyline class="rxpb-chain-case" points="${SVGP(run)}"/><polyline class="rxpb-chain-line" points="${SVGP(run)}"/>`); run = []; };
  for (const src of srcs) {
    const at = stepPoint(src, mapId);
    if (!at) { flush(); continue; }
    run.push([at.x, at.y]);
  }
  flush();
  if (!isParentMap(mapId)) {
    for (const c of chainCrossings(srcs.map((src) => ({ src, um: stepMap(src) })), mapId)) {
      const at = stepPoint(c.at, mapId);
      if (!at) continue;
      const label = c.dir === "to" ? `continues in ${mapName(c.um)}` : `from ${mapName(c.um)}`;
      const anchor = at.x > 75 ? "end" : "start", dx = anchor === "end" ? -18 : 18;
      parts.push(`<g class="rxpb-chain-edge" data-kind="edge" data-chain="1" data-map="${c.um}" data-dir="${c.dir}" tabindex="0" role="button"
        aria-label="${esc(`The quest chain ${c.dir === "to" ? "continues in" : "comes from"} ${mapName(c.um)}: open its map`)}" transform="translate(${mapX(at.x)},${mapY(at.y)})">
        <g class="rxpb-body"><text x="${dx}" y="${c.dir === "to" ? 22 : -16}" text-anchor="${anchor}">${esc(label)} ›</text></g></g>`);
    }
  }
  el.innerHTML = parts.join("");
}

/** The middle of the longest drawn stretch of the legs into the current step on map `mapId`, or null. */
function legMiddle(mapId: number): Pt | null {
  let best: Pt[] | null = null, bestLen = 0;
  for (const leg of activeLegs()) {
    if (leg.from === leg.to) continue;
    for (const p of legPieces(leg, routeLegs.get(leg.key), mapId, mapsGeo())) {
      for (const run of p.runs) {
        let len = 0;
        for (let i = 1; i < run.length; i++) len += Math.hypot(run[i][0] - run[i - 1][0], run[i][1] - run[i - 1][1]);
        if (len > bestLen) { bestLen = len; best = run; }
      }
    }
  }
  if (!best || bestLen < 2) return null;
  let half = bestLen / 2;
  for (let i = 1; i < best.length; i++) {
    const seg = Math.hypot(best[i][0] - best[i - 1][0], best[i][1] - best[i - 1][1]);
    if (seg >= half) { const t = seg ? half / seg : 0; return [best[i - 1][0] + (best[i][0] - best[i - 1][0]) * t, best[i - 1][1] + (best[i][1] - best[i - 1][1]) * t]; }
    half -= seg;
  }
  return best[best.length - 1];
}

/** The current step's hazards on the map: a warning sign on the way in for what's met on the road (the coach's risks
 * near the way), beside the pin for the step's own elite target or a dangerous quest. A card says it in a line. */
function drawHazards(mapId: number | null): void {
  const el = document.getElementById("rxpb-l-hz");
  if (!el) return;
  const vs = visibleSteps[activeIdx];
  const hz = vs && mapId != null ? facts?.hazards?.[String(vs.src)] || [] : [];
  if (!hz.length || mapId == null) { el.innerHTML = ""; return; }
  const road = legMiddle(mapId), at = stepPoint(vs.src, mapId);
  const pin: Pt | null = at ? [at.x, at.y] : null;
  const out: string[] = [];
  let onRoad = 0, atPin = 0;
  hz.slice(0, 4).forEach((h, i) => {
    const way = h.why === "risk" || h.why === "near";
    const base: Pt | null = way && road ? road : pin;
    if (!base) return;
    const k = way && road ? onRoad++ : atPin++;
    // side by side, and off the pin itself
    const dx = way && road ? k * 26 : 24 + k * 26, dy = way && road ? 0 : -24;
    out.push(`<g class="rxpb-hz${h.elite ? " elite" : ""}" data-kind="hz" data-src="${vs.src}" data-sub="${i}" tabindex="0" role="button"
      aria-label="${esc(`Danger: ${hazardLine(h)}`)}" transform="translate(${mapX(base[0])},${mapY(base[1])})">
      <g class="rxpb-body"><g transform="translate(${dx},${dy})"><path d="M0 -11 L10.5 8 L-10.5 8 Z"/><text dy="5.5">!</text></g></g></g>`);
  });
  el.innerHTML = out.join("");
}

/** Light quest `qid`'s chain (or the chain shown, off again): its steps on the map, the timeline and in the list,
 * the rest faded, and the chain bar saying what a skip breaks. */
function focusChain(qid: number | null, toggle = true): void {
  focusQuest = toggle && qid != null && qid === focusQuest ? null : qid;
  const quests = questsOf(facts, currentGuide?.steps || []);
  chainSrcs = focusQuest == null ? new Set() : new Set(chainStops(quests, chainOf(quests, focusQuest)).map((c) => c.src));
  renderChain();
}

/** Redraw what the chain changes: the bar, the list's classes and badges, the timeline, the map. */
function renderChain(): void {
  renderChainBar();
  const listEl = $("rxpb-step-list");
  listEl.classList.toggle("chain-on", focusQuest != null);
  listEl.querySelectorAll<HTMLLIElement>("li[data-src]").forEach((li) => setItemState(li, posBySrc.get(Number(li.dataset.src))!));
  listEl.querySelectorAll<HTMLButtonElement>(".rxpb-qb").forEach((b) => {
    const on = Number(b.dataset.quest) === focusQuest;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  renderTimeline();
  if (currentGuide) renderMap();
}

const ROLE_GLYPH: Record<QuestRole, string> = { accept: "!", objective: "◎", turnin: "?" };
const ROLE_WORD: Record<QuestRole, string> = { accept: "Accept", objective: "Do", turnin: "Turn in" };

/** The chain bar over the step list while a chain is lit: the chain's quests in order, each with its steps (a click
 * goes there), and what skipping the picked quest loses (the coach's rule; said only with the knowledge base). */
function renderChainBar(): void {
  const bar = $("rxpb-chainbar");
  if (focusQuest == null || !currentGuide) { bar.hidden = true; bar.innerHTML = ""; return; }
  const quests = questsOf(facts, currentGuide.steps);
  const chain = chainOf(quests, focusQuest);
  const nameOf = (q: number) => quests[String(q)]?.name || currentGuide!.steps.flatMap((st) => st.actions || [])
    .find((a) => a.quest_id === q && a.quest_name)?.quest_name || `Quest #${q}`;
  const num = (src: number) => { const at = posBySrc.get(src); return at !== undefined ? visibleSteps[at].rxpNum : null; };
  // a long chain shows the quests around the picked one (fewer in a narrow guide window)
  const at = chain.indexOf(focusQuest);
  const span = ($("rxpb-chainbar").parentElement?.clientWidth || 900) > 760 ? 5 : 3;
  const lo = Math.max(0, Math.min(at - (span >> 1), chain.length - span)), hi = Math.min(chain.length, lo + span);
  const stops = chainStops(quests, chain);
  const pieces = chain.slice(lo, hi).map((q) => {
    const chips = stops.filter((c) => c.quest === q).map((c) => {
      const n = num(c.src);
      const label = n != null ? `${ROLE_WORD[c.role]}: step ${n}` : `${ROLE_WORD[c.role]}: another character's step`;
      return `<button type="button" class="rxpb-cb-step r-${c.role}${n == null ? " other" : ""}${c.src === activeSrc ? " on" : ""}" data-src="${c.src}"
        ${n == null ? "disabled" : ""} title="${esc(label)}" aria-label="${esc(label)}"><b>${ROLE_GLYPH[c.role]}</b>${n ?? "–"}</button>`;
    }).join("");
    return `<span class="rxpb-cb-q${q === focusQuest ? " on" : ""}"><span class="rxpb-cb-name">${esc(nameOf(q))}</span>${chips}</span>`;
  });
  if (lo > 0) pieces.unshift(`<span class="rxpb-cb-more">${lo} before</span>`);
  if (hi < chain.length) pieces.push(`<span class="rxpb-cb-more">${chain.length - hi} more</span>`);
  if (continuesAfter(quests, chain)) pieces.push(`<span class="rxpb-cb-more">goes on after this guide</span>`);
  const skip = skipLine(quests, focusQuest, (src) => { const n = num(src); return n != null ? String(n) : null; });
  const head = chain.length > 1 ? `Quest ${at + 1} of ${chain.length} in a chain` : "Quest";
  bar.hidden = false;
  bar.innerHTML = `<div class="rxpb-cb-row"><span class="rxpb-cb-head">⛓ ${esc(head)}</span>
      <button type="button" class="rxpb-ins-close" id="rxpb-cb-close" title="Stop showing the chain (Esc)" aria-label="Stop showing the chain">×</button></div>
    <div class="rxpb-cb-path">${pieces.join(` <span class="rxpb-cb-arrow" aria-hidden="true">→</span> `)}</div>
    ${skip ? `<div class="rxpb-cb-skip" title="The coach's own rule: what can't be picked up once this quest is skipped, in this guide and the next">${esc(skip)}</div>` : ""}`;
}

const BUY_VERBS = new Set(["collect", "buy", "buyAll", "buyUntilBroke"]);

/** Whether a prep item is the picked characters': its step is listed for them (not dimmed) and the action getting
 * it applies to them (the engine's masks, the Character picks). */
function prepIsShown(p: RxpFactsPrep): boolean {
  const at = posBySrc.get(p.idx);
  if (at === undefined || visibleSteps[at].dimmed) return false;
  const bank = p.kind === "bank_in" || p.kind === "bank_out";
  const a = (visibleSteps[at].step.actions || []).find((x) => bank ? x.verb === (p.kind === "bank_in" ? "bankdeposit" : "bankwithdraw")
    : BUY_VERBS.has(x.verb) && x.entity_id === p.item_id);
  return !a || actionApplies(a);
}

let prepKey = "";

/** "Before you start": the guide's purchases and bank trips for the picked character still ahead of the current step,
 * in one slim row at the top of the guide window, each with its count and where it's needed; a click goes to the
 * step that gets it. A row too long for the window ends in "+N", which opens the rest. */
function renderPrep(): void {
  const box = $("rxpb-prep");
  const all = currentGuide && facts?.prep?.length ? prepShown(facts.prep, prepIsShown) : [];
  const items = all.filter((p) => activeSrc == null || p.idx >= activeSrc);
  const key = `${currentGuide?.id}:${currentGuide?.index}|${factsVersion}|${visKey}|${items.map((p) => p.idx).join(",")}`;
  if (key !== prepKey) {
    prepKey = key;
    if (!items.length) { box.hidden = true; box.innerHTML = ""; return; }
    const num = (src: number) => { const at = posBySrc.get(src); return at !== undefined && visibleSteps[at].rxpNum != null ? String(visibleSteps[at].rxpNum) : null; };
    const qname = (q: number) => facts?.quests?.[String(q)]?.name || null;
    box.hidden = false;
    const head = items.length === all.length ? "Before you start" : "Still to get";
    box.innerHTML = `<span class="rxpb-prep-head" title="What this guide has you buy or take from the bank, for this character, from here on">${head}</span>`
      + `<div class="rxpb-prep-items">${items.map((p) => {
        const need = prepNeed(p, num);
        return `<button type="button" class="rxpb-pc k-${p.kind}" data-src="${p.idx}" title="${esc(prepTitle(p, num, qname))}">`
          + `${giHtml(PREP_GLYPH[p.kind], prepIcon(p, facts?.icons))} ${esc(prepLabel(p))}${need ? ` <em>${esc(need)}</em>` : ""}</button>`;
      }).join("")}</div><button type="button" class="rxpb-prep-more" id="rxpb-prep-more" hidden></button>`;
    fitPrep();
  }
  box.querySelectorAll<HTMLButtonElement>(".rxpb-pc").forEach((b) => b.classList.toggle("on", Number(b.dataset.src) === activeSrc));
}

/** The prep row in one line: the items that don't fit are counted on a "+N" button that opens the whole list. */
function fitPrep(): void {
  const box = $("rxpb-prep");
  const row = box.querySelector<HTMLElement>(".rxpb-prep-items"), more = document.getElementById("rxpb-prep-more");
  if (!row || !more || box.hidden) return;
  if (box.classList.contains("open")) {
    row.querySelectorAll<HTMLElement>(".rxpb-pc").forEach((c) => { c.hidden = false; });
    more.hidden = false; more.textContent = "Less"; more.title = "Back to one row"; return;
  }
  const chips = [...row.children] as HTMLElement[];
  chips.forEach((c) => { c.hidden = false; });
  more.hidden = false;                       // measured with the "+N" in the row, so the last chip shown isn't cut
  more.textContent = `+${chips.length}`;
  const right = row.getBoundingClientRect().right;
  const cut = chips.filter((c) => c.getBoundingClientRect().right > right + 1);
  cut.forEach((c) => { c.hidden = true; });
  const hidden = cut.length;
  if (!hidden) { more.hidden = true; return; }
  more.textContent = `+${hidden}`;
  more.title = `${hidden} more: show them all`;
}

/** The yards and time of the legs to the current target, when the engine has worked them all out. */
function activeLegTotals(): { yd: number | null; s: number | null } {
  const legs = activeLegs();
  if (!legs.length) return { yd: null, s: null };
  let yd = 0, s = 0;
  for (const l of legs) {
    const e = routeLegs.get(l.key);
    if (!e || e.yd == null) return { yd: null, s: null };
    yd += e.yd;
    s += e.s || 0;
  }
  return { yd, s: s || null };
}

/** Render the zone map: the zone tabs, the image, the persistent layers (when the guide, map, steps, routes or
 * objectives changed), the classes for the current step, the active leg, the arrow and the inspector. */
function renderMap(): void {
  const wrap = $("rxpb-map-stage");
  if (!wrap || !currentGuide) return;
  const cur = visibleSteps[activeIdx];
  const mapId = activeMapId ?? cur?.ui_map ?? currentGuide.zones[0]?.ui_map ?? null;
  const zoneName = mapName(mapId) || cur?.zone || "Zone Map";

  // Zone tabs (when a guide crosses multiple zones), and the continent when it spans zones on one
  const zoneCounts = new Map<number, number>();
  for (const vs of visibleSteps) {
    if (vs.step.ui_map != null) zoneCounts.set(vs.step.ui_map, (zoneCounts.get(vs.step.ui_map) || 0) + 1);
  }
  const cont = continentTab();
  const tabs = currentGuide.zones.map((z) => ({ um: z.ui_map, name: z.name, cnt: zoneCounts.get(z.ui_map) || 0, cont: false }));
  if (cont != null && !tabs.some((t) => t.um === cont)) tabs.push({ um: cont, name: mapName(cont), cnt: visibleSteps.length, cont: true });
  const tabsHtml = tabs.map((t) => `<button type="button" role="tab" aria-selected="${t.um === mapId}" tabindex="${t.um === mapId ? 0 : -1}"
    class="rxpb-ztab${t.um === mapId ? " on" : ""}${t.cont ? " cont" : ""}" data-map="${t.um}"${t.cont ? ` title="The whole route across ${esc(t.name)}"` : ""}>${t.cont ? "◎ " : ""}${esc(t.name)} <span>${t.cnt}</span></button>`).join("");
  if ($("rxpb-zones").innerHTML !== tabsHtml) $("rxpb-zones").innerHTML = tabsHtml;

  if (view.map !== mapId) {
    view.map = mapId;
    resetView();
  }

  const imgUrl = mapImageUrl(mapId);
  const imgEl = $<HTMLImageElement>("rxpb-map-img");
  if (imgUrl) {
    if (imgEl.getAttribute("src") !== imgUrl) imgEl.src = imgUrl;
    imgEl.style.display = "block";
    wrap.classList.remove("no-img");
  } else {
    imgEl.style.display = "none";
    wrap.classList.add("no-img");
  }

  const key = `${currentGuide.id}:${currentGuide.index}|${mapId}|${imgUrl ? 1 : 0}|${listKeyOf()}|${routesVersion}|${factsVersion}|${filter.showAllCond}`;
  if (key !== layersKey) {
    layersKey = key;
    buildLayers(mapId, imgUrl);
  }
  updateClasses();
  drawActive(mapId);
  drawChain(mapId);
  drawHazards(mapId);

  // Update the RestedXP Waypoint Arrow HUD widget: where the current target is, how far and how long the way there
  const arrowHud = $("rxpb-arrow-hud");
  const tgt = targetStop(cur);
  if (tgt) {
    const legs = activeLegs();
    const from = legs[0]?.pa;
    let deg = 0;
    let distText = `${tgt.x.toFixed(1)}, ${tgt.y.toFixed(1)}`;
    const totals = activeLegTotals();
    if (from && from.um === tgt.um && (from.x !== tgt.x || from.y !== tgt.y)) {
      const dx = tgt.x - from.x, dy = tgt.y - from.y;
      deg = bearingDeg(mapsGeo(), tgt.um, dx, dy);
    }
    const ydv = totals.yd ?? (from && from.um === tgt.um ? yards(mapsGeo(), tgt.um, tgt.x - from.x, tgt.y - from.y) : null);
    const yd = ydv != null && ydv >= 1 ? yardsText(ydv) : "";
    if (yd) distText += ` · ${yd}`;
    if (totals.s) distText += ` · ${durText(totals.s)}`;
    arrowHud.hidden = false;
    arrowHud.innerHTML = `<div class="rxpb-arrow-wheel"><svg viewBox="0 0 36 36" style="transform:rotate(${deg.toFixed(1)}deg)">
        <path d="M18 3 L30 30 L18 23 L6 30 Z" fill="#00ff25" stroke="#081a0b" stroke-width="2"/>
      </svg></div>
      <div class="rxpb-arrow-txt">
        <b>${esc(tgt.um !== mapId ? mapName(tgt.um) : zoneName)}</b>
        <span>${esc(distText)}</span>
      </div>`;
  } else {
    arrowHud.hidden = true;
  }

  renderInspector();
}

/** What a map element's card says (hover card and inspector alike): a head line and the body. */
function elementCard(kind: string, src: number, sub: number | null, legK: string | null): { head: string; body: string } | null {
  const at = posBySrc.get(src);
  const vs = at !== undefined ? visibleSteps[at] : undefined;
  if (kind === "step" && vs) {
    const st = vs.step;
    const text = (st.actions || []).find((a) => a.raw_text)?.raw_text;
    const badges = stepBadges(st);
    const where = st.x != null && st.y != null ? `${st.zone || vs.zone || ""} ${st.x.toFixed(1)}, ${st.y.toFixed(1)}` : (st.zone || "");
    return {
      head: `<b>Step ${stepNum(vs)}</b><span>${esc(where)}</span>${vs.dimmed ? `<span>(another character's)</span>` : ""}`,
      body: `<div class="rxpb-hc-text">${text ? formatRxpText(text) : esc(stepHeadline(st))}</div>`
        + (badges.length ? `<div class="rxpb-badges">${badges.slice(0, 6).map((b) => `<span class="rxpb-badge mini ${b.cls}">${giHtml(b.sym, b.gi)} ${esc(b.label)}</span>`).join("")}</div>` : ""),
    };
  }
  if (kind === "wp" && vs && sub != null) {
    const w = vs.step.waypoints?.[sub];
    if (!w) return null;
    const path = w.mode === "path";
    const l = stopLetters(vs.step).get(sub);
    return {
      head: `<b>${path ? "On the way" : `Waypoint ${l || ""}`}</b><span>Step ${stepNum(vs)} · ${esc(w.zone || vs.zone || "")} ${w.x.toFixed(1)}, ${w.y.toFixed(1)}</span>`,
      body: `<div class="rxpb-hc-text">${path ? "RestedXP's arrow walks you through here on the way to the step." : w.text ? formatRxpText(w.text) : "Click to aim the arrow here."}</div>`,
    };
  }
  if (kind === "leg" && legK) {
    const leg = planned.find((l) => l.key === legK);
    if (!leg) return null;
    const info = legInfo(leg, routeLegs.get(leg.key), mapsGeo(), mapName);
    const a = posBySrc.get(leg.from), b = posBySrc.get(leg.to);
    const fromTxt = a !== undefined ? `Step ${stepNum(visibleSteps[a])}` : "", toTxt = b !== undefined ? `step ${stepNum(visibleSteps[b])}` : "";
    return {
      head: `<b>${esc(info.head)}</b><span>${leg.from === leg.to ? `Inside step ${esc(toTxt.replace("step ", ""))}` : `${esc(fromTxt)} → ${esc(toTxt)}`}</span>`,
      body: info.lines.map((l) => `<div class="rxpb-hc-line">${esc(l)}</div>`).join("") + (info.approx && routeLegs.has(leg.key) ? `<div class="rxpb-hc-line dim">Part of it is a straight guess.</div>` : ""),
    };
  }
  if (kind === "loop" && vs && sub != null) {
    const lp = vs.step.loops?.[sub];
    if (!lp) return null;
    let per = 0;
    const pts = lp.points as Pt[];
    for (let i = 1; i < pts.length + (lp.kind === "loop" ? 1 : 0); i++) {
      const p = pts[i - 1], q = pts[i % pts.length];
      const yd = yards(mapsGeo(), lp.ui_map || vs.ui_map, q[0] - p[0], q[1] - p[1]);
      if (yd == null) { per = NaN; break; }
      per += yd;
    }
    const kills = (vs.step.actions || []).filter((x) => x.verb === "mob" || x.verb === "kill" || x.verb === "complete").map((x) => actionBadge(x)?.label).filter(Boolean);
    return {
      head: `<b>${lp.kind === "loop" ? "Grind loop" : "Grind line"}</b><span>Step ${stepNum(vs)}${isFinite(per) && per ? ` · ${yardsText(per)} ${lp.kind === "loop" ? "around" : "long"}` : ""}</span>`,
      body: `<div class="rxpb-hc-text">${lp.kind === "loop" ? "Circle this loop" : "Walk up and down this line"}${lp.range ? `, staying within ${lp.range} yd of it,` : ""} until the step is done.</div>`
        + (kills.length ? `<div class="rxpb-hc-line">${esc(kills.slice(0, 3).join(" · "))}</div>` : ""),
    };
  }
  if (kind === "hz" && vs && sub != null) {
    const h = facts?.hazards?.[String(src)]?.[sub];
    if (!h) return null;
    return {
      head: `<b class="warn">⚠ ${esc(hazardLine(h))}</b>`,
      body: `<div class="rxpb-hc-line dim">${esc(hazardWhere(h))} · step ${esc(stepNum(vs))}</div>`,
    };
  }
  if (kind === "obj" && vs && sub != null) {
    const t = facts?.targets?.[String(src)]?.[sub];
    if (!t) return null;
    const quest = (vs.step.actions || []).find((x) => x.quest_id != null && x.quest_id === t.quest);
    const item = t.role === "loot" ? (vs.step.actions || []).find((x) => x.verb === "collect" && (!t.quest || x.quest_id === t.quest))?.entity_name
      || itemName(t.item ?? null) : null;
    const bits = [t.level ? `level ${t.level}` : "", t.rank ? RANK_NAME[t.rank] || "" : ""].filter(Boolean);
    // the game's icons: the creature (its family or type), the elite or rare mark, the item to loot
    const gi = targetIcon(t, facts?.icons), rk = rankIcon(t.rank, facts?.icons), loot = lootIcon(t, facts?.icons);
    const lootHtml = loot && (item || t.kind !== "object") ? giHtml("◆", loot) + " " : "";
    return {
      head: `${gi ? giHtml("", gi) : ""}<b>${esc(`${ROLE_NAME[t.role] || ""} ${t.name || (t.kind === "poi" ? "the quest area" : `#${t.id}`)}`.trim())}</b>${bits.length ? `<span class="${(t.rank || 0) >= 1 ? "warn" : ""}">${rk ? giHtml("", rk) : ""}${esc(bits.join(" · "))}</span>` : ""}`,
      body: (item ? `<div class="rxpb-hc-line">${lootHtml}For ${esc(item)}</div>` : "")
        + (quest ? `<div class="rxpb-hc-line">${esc(questName(quest) || `Quest #${quest.quest_id}`)}</div>` : "")
        + `<div class="rxpb-hc-line dim">${t.points.length > 1 ? `${t.points.length} places near the step` : "Near the step"}</div>`,
    };
  }
  return null;
}

/** The inspector: the selected map element of any kind, as a compact card that never scrolls. */
function renderInspector(): void {
  const box = $("rxpb-inspector");
  const c = selected ? elementCard(selected.kind, selected.src, selected.sub ?? null, selected.key ?? null) : null;
  if (!c) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.innerHTML = `<div class="rxpb-ins-head">${c.head}<button type="button" class="rxpb-ins-close" id="rxpb-ins-close" title="Close details (Esc)" aria-label="Close details">×</button></div>${c.body}`;
  const closeBtn = document.getElementById("rxpb-ins-close");
  if (closeBtn) closeBtn.onclick = clearSelection;
}

/** Close the inspector (Escape, its ×). */
function clearSelection(): void {
  selected = null;
  hideHoverCard();
  updateClasses();
  renderInspector();
}


const KIND_GLYPH: Record<string, string> = { accept: "!", turnin: "?", combat: "⚔", travel: "✦", hearth: "⌂", death: "☠" };
const KIND_RANK: Record<string, number> = { travel: 6, hearth: 6, death: 6, turnin: 4, accept: 3, combat: 1 };
const KIND_NAME: Record<string, string> = { accept: "Accept", turnin: "Turn in", combat: "Objective", travel: "Flight",
  hearth: "Hearth", death: "Deathskip" };

/** The timeline: the slider, the position, and above the slider a glyph tick for each accept, turn-in, objective,
 * flight, hearth and deathskip (the pins' own glyphs and colours), plus a dashed line where the zone changes. */
function renderTimeline(): void {
  const n = visibleSteps.length;
  const slider = $<HTMLInputElement>("rxpb-scrub");
  slider.max = String(Math.max(0, n - 1));
  slider.value = String(activeIdx);
  slider.disabled = n <= 1;

  const cur = visibleSteps[activeIdx];
  const numbered = visibleSteps.reduce((k, vs) => k + (vs.rxpNum != null ? 1 : 0), 0);
  $("rxpb-tl-pos").textContent = !n ? "No steps" : cur?.rxpNum != null ? `${cur.rxpNum} / ${numbered}` : "Other's";
  $("rxpb-tl-pos").setAttribute("aria-label", cur?.rxpNum != null ? `Step ${cur.rxpNum} of ${numbered}` : "Another character's step");
  $("rxpb-tl-pos").title = cur ? `${cur.zone || ""}${cur.step.x != null ? ` (${cur.step.x.toFixed(1)}, ${cur.step.y!.toFixed(1)})` : ""}` : "";
  $<HTMLButtonElement>("rxpb-prev").disabled = activeIdx <= 0;
  $<HTMLButtonElement>("rxpb-next").disabled = activeIdx >= n - 1;
  $<HTMLButtonElement>("rxpb-play").innerHTML = walking ? "⏸ Pause" : `${icon("play")}Walk`;

  const ticks: string[] = [];
  if (n > 1) {
    // in a crowded hub the glyphs would pile up: two rows, the travel and quest moments first, the rest left out
    // (the slider still reaches every step)
    const width = $("rxpb-tl-ticks").clientWidth || 600;
    const marked = visibleSteps.map((vs, i) => ({ vs, i })).filter(({ vs, i }) => vs.kind !== "step" || i === activeIdx);
    const rows = layoutTicks(marked.map(({ vs, i }) => ({ x: (i / (n - 1)) * width, rank: i === activeIdx ? 9 : KIND_RANK[vs.kind] || 0 })), 13);
    let prevZone = visibleSteps[0]?.zone;
    visibleSteps.forEach((vs, i) => {
      if (vs.zone && vs.zone !== prevZone) {
        prevZone = vs.zone;
        ticks.push(`<i class="tl-zone" style="left:${((i / (n - 1)) * 100).toFixed(2)}%"></i>`);
      }
    });
    marked.forEach(({ vs, i }, k) => {
      if (rows[k] < 0 || vs.kind === "step") return;
      const pct = ((i / (n - 1)) * 100).toFixed(2);
      const label = `Step ${stepNum(vs)}, ${KIND_NAME[vs.kind]}: ${stepHeadline(vs.step)}`;
      ticks.push(`<button type="button" tabindex="-1" class="tl-tick tl-${vs.kind}${rows[k] ? " row2" : ""}${i === activeIdx ? " on" : ""}${chainSrcs.has(vs.src) ? " in-chain" : ""}" style="left:${pct}%" data-src="${vs.src}" title="${esc(label)}" aria-label="${esc(label)}">${KIND_GLYPH[vs.kind]}</button>`);
    });
    if (focusQuest != null) {
      for (const src of chainSrcs) {
        const at = posBySrc.get(src);
        if (at === undefined) continue;
        const label = `Quest chain: step ${stepNum(visibleSteps[at])}`;
        ticks.push(`<button type="button" tabindex="-1" class="tl-tick tl-chain" style="left:${((at / (n - 1)) * 100).toFixed(2)}%" data-src="${src}" title="${esc(label)}" aria-label="${esc(label)}"></button>`);
      }
    }
    const li = liveSrc != null ? posBySrc.get(liveSrc) : undefined;
    if (li !== undefined) {
      const label = `${liveWho()} is here: step ${stepNum(visibleSteps[li])}`;
      ticks.push(`<button type="button" tabindex="-1" class="tl-tick tl-live" style="left:${((li / (n - 1)) * 100).toFixed(2)}%" data-src="${liveSrc}" title="${esc(label)}" aria-label="${esc(label)}"></button>`);
    }
  }
  if (n > 1 && facts) {
    // the dangerous stretches: a red band over the slider (a click goes to its first step)
    const order = visibleSteps.map((v) => v.src);
    for (const r of dangerRuns(order, facts.hazards || {}, planSteps())) {
      const a = (r.from / (n - 1)) * 100, b = (r.to / (n - 1)) * 100;
      const steps = r.from === r.to ? `Step ${stepNum(visibleSteps[r.from])}` : `Steps ${stepNum(visibleSteps[r.from])}–${stepNum(visibleSteps[r.to])}`;
      const label = `${steps}: ${r.hz.length ? r.hz.slice(0, 2).map(hazardLine).join("; ") : "a risky stretch at the coach's reckoning"}`;
      ticks.push(`<button type="button" tabindex="-1" class="tl-danger" style="left:${a.toFixed(2)}%;width:max(4px, ${(b - a).toFixed(2)}%)" data-src="${visibleSteps[r.from].src}" title="${esc(label)}" aria-label="${esc(label)}"></button>`);
    }
  }
  $("rxpb-tl-ticks").innerHTML = ticks.join("");
  $("rxpb-tl-ticks").classList.toggle("chain-on", focusQuest != null);
  renderPace();
}

const CLASS_WORD = (c: string) => c[0] + c.slice(1).toLowerCase();

/** The pace beside the timeline: "45 min to 23" from the current step (the coach's plan for the picked class), and
 * while scrubbing, a bubble over the slider with the time and XP from where the scrub started. */
function renderPace(): void {
  const eta = $("rxpb-tl-eta"), bubble = $("rxpb-tl-bubble");
  const plan = planSteps();
  const order = visibleSteps.map((v) => v.src);
  const d = plan ? nextDing(plan, order, activeIdx) : null;
  eta.hidden = !d;
  if (d) {
    eta.textContent = dingText(d);
    eta.title = `At the coach's pace for a ${CLASS_WORD(filter.cls)}: level ${d.level} at step ${stepNum(visibleSteps[d.pos])}`
      + (facts?.plan?.approx ? " (roughly: the knowledge base has another version of this guide)" : "");
  }
  const sp = plan && scrubFrom != null && scrubFrom !== activeIdx ? span(plan, order, scrubFrom, activeIdx) : null;
  const txt = spanText(sp, scrubFrom != null && activeIdx < scrubFrom);
  bubble.hidden = !txt;
  if (txt) {
    bubble.textContent = txt;
    const n = visibleSteps.length;
    bubble.style.left = `${n > 1 ? (activeIdx / (n - 1)) * 100 : 0}%`;
  }
}

/** The slider moved: the scrub's start is remembered until the slider rests (time and XP are said from there). */
function noteScrub(before: number): void {
  if (scrubFrom == null) scrubFrom = before;
  if (scrubTimer) window.clearTimeout(scrubTimer);
  scrubTimer = window.setTimeout(endScrub, 1600);
}

function endScrub(): void {
  if (scrubHeld) return;
  scrubTimer = null;
  scrubFrom = null;
  renderPace();
}

/** The guide window's header: the guide's name and RestedXP's #next guide. */
function renderHeader(): void {
  if (!currentGuide) return;
  $("rxpb-win-title").textContent = currentGuide.displayname || currentGuide.name;
  $("rxpb-win-sub").textContent = [currentGuide.group, currentGuide.subgroup].filter(Boolean).join(" · ");
  const nextBtn = $<HTMLButtonElement>("rxpb-next-guide");
  const who = pickedPlayers();
  const nextEntry = (currentGuide.next_list || []).find((n) => n.name && (!vis || vis.actionShown(n.vis, who)));
  nextBtn.hidden = true;
  if (!nextEntry || !nextEntry.name) return;
  // the group RestedXP names (#next Group\Name), else the group being browsed: a Dungeon guide's next is the
  // Dungeon guide of that name, not the Speedrun one; only then any group
  const target = nextEntry.name.trim().toLowerCase();
  const cg = currentGuide;
  const group = nextEntry.group || catalog.find((c) => c.packId === cg.id && c.index === cg.index
    && (!filter.group || c.group === filter.group))?.group || cg.group;
  const named = (c: CatalogEntry) => c.name.toLowerCase() === target;
  const endsAs = (c: CatalogEntry) => c.name.toLowerCase().endsWith(target);
  const match = catalog.find((c) => c.group === group && named(c)) || catalog.find((c) => c.group === group && endsAs(c))
    || catalog.find(named) || catalog.find(endsAs);
  if (match) {
    nextBtn.hidden = false;
    nextBtn.textContent = `Next: ${match.name} →`;
    nextBtn.title = `Open ${match.name}`;
    nextBtn.onclick = () => openGuide(match.packId, match.index);
  }
}

/** The current step as it opens in the list (RestedXP's CurrentStepFrame): its objectives to tick, waypoints and notes. */
function activeStepHtml(vs: VisibleStep): string {
  const g = currentGuide!;
  const st = vs.step;
  const flagPills: string[] = [];
  if (st.flags?.sticky) flagPills.push(`<span class="rxpb-flag">Sticky</span>`);
  if (st.flags?.optional) flagPills.push(`<span class="rxpb-flag">Optional</span>`);
  if (st.flags?.completewith) flagPills.push(`<span class="rxpb-flag">Complete with next</span>`);
  if (st.flags?.included_from) flagPills.push(`<span class="rxpb-flag" title="${esc(String(st.flags.included_from))}">From ${esc(String(st.flags.included_from).split("||").pop() || "")}</span>`);
  if (st.cond) flagPills.push(`<span class="rxpb-cond-pill">&lt;&lt; ${esc(st.cond)}</span>`);
  const rows: string[] = [];
  for (const a of (st.actions || []).filter((x) => filter.showAllCond || actionApplies(x))) {
    const b = actionBadge(a, st);
    const hasText = !!(a.raw_text || a.text);
    if (!b && !hasText && a.verb !== "goto" && a.verb !== "link") continue;
    if (a.verb === "goto" && !hasText) continue;    // pure coordinate lines are the waypoint pills below
    const objKey = `${g.id}:${g.index}:${st.idx}:${a.line ?? a.verb}`;
    const done = checkedObjs.has(objKey);
    const isCheckable = a.verb !== "text" || a.kind === "+";
    const condTag = a.cond ? ` <span class="rxpb-cond-pill">&lt;&lt; ${esc(a.cond)}</span>` : "";
    const commentTag = a.comment ? `<div class="rxpb-line-comment">-- ${esc(a.comment)}</div>` : "";
    const urlLink = a.url ? ` <a href="${esc(a.url)}" class="rxpb-ext" data-url="${esc(a.url)}">[Video / Link ↗]</a>` : "";
    rows.push(`<div class="rxpb-obj-row${done ? " done" : ""}" data-obj="${esc(objKey)}">
      ${isCheckable ? `<input type="checkbox" class="rxpb-chk" tabindex="-1" ${done ? "checked" : ""} aria-label="Objective done">` : `<span class="rxpb-bullet">•</span>`}
      <div class="rxpb-obj-body">
        ${b ? badgeHtml(b) : ""}
        ${hasText ? `<div class="rxpb-obj-text">${formatRxpText(a.raw_text || a.text)}${urlLink}${condTag}</div>` : `${urlLink}${condTag}`}
        ${commentTag}
      </div>
    </div>`);
  }
  // the places the step sends the player (RestedXP's path points are only on the way there), lettered as on the map
  const letters = stopLetters(st);
  const wpPills = stepWaypoints(st).filter(({ wi }) => letters.has(wi)).map(({ w, wi }) => `<button type="button" tabindex="-1" class="rxpb-wp-pill${wi === activeWpIdx ? " on" : ""}" data-wp="${wi}">
    📍 ${esc(w.zone || st.zone || "Zone")} <b>${w.x.toFixed(1)}, ${w.y.toFixed(1)}</b>${letters.size > 1 ? ` <em>(${letters.get(wi)})</em>` : ""}
  </button>`).join("");
  return `<div class="rxpb-active-step">
    <div class="rxpb-cs-head">
      <span class="rxpb-cs-num">${vs.rxpNum != null ? `Step ${vs.rxpNum}` : "Another character's step"}</span>
      <div class="rxpb-cs-flags">${flagPills.join("")}</div>
    </div>
    <div class="rxpb-cs-rows">${rows.length ? rows.join("") : `<div class="rxpb-obj-text dim">Travel / transition step</div>`}</div>
    ${wpPills ? `<div class="rxpb-wp-bar">${wpPills}</div>` : ""}
    ${hazardRows(vs.src)}
    ${st.comments ? `<div class="rxpb-comment">💬 ${esc(st.comments)}</div>` : ""}
  </div>`;
}

/** The opened step's hazards, a line each (the two that matter most): what the player meets here. */
function hazardRows(src: number): string {
  const hz = facts?.hazards?.[String(src)] || [];
  return hz.slice(0, 2).map((h) => `<div class="rxpb-hz-row${h.elite ? " elite" : ""}" title="${esc(hazardWhere(h))}"><b>⚠</b> ${esc(hazardLine(h))}</div>`).join("");
}

/** One step in the list: collapsed (number, instruction, badges), or opened when it's the current step. */
function stepItemHtml(vs: VisibleStep, i: number): string {
  const st = vs.step;
  const on = i === activeIdx;
  const badges = stepBadges(st);
  const summaryText = st.text || badges.map((b) => b.label).join(" · ") || stepHeadline(st);
  const coord = st.x != null && st.y != null ? `${st.zone || ""} ${st.x.toFixed(1)}, ${st.y.toFixed(1)}` : (st.zone || "");
  return `<div class="rxpb-sitem-top">
        <span class="rxpb-snum">${stepNum(vs)}</span>
        <span class="rxpb-stitle">${formatRxpText((st.actions || []).find((a) => a.raw_text)?.raw_text || summaryText)}</span>
        ${coord ? `<span class="rxpb-scoord">${esc(coord)}</span>` : ""}
      </div>
      ${badges.length ? `<div class="rxpb-sbadges">${badges.slice(0, 4).map((b) => badgeHtml(b, true)).join("")}</div>` : ""}
      ${on ? activeStepHtml(vs) : ""}`;
}

function setItemState(li: HTMLElement, i: number): void {
  const vs = visibleSteps[i];
  const on = i === activeIdx;
  li.className = `rxpb-sitem k-${vs.kind}${on ? " on" : ""}${i < activeIdx ? " done" : ""}${vs.dimmed ? " dimmed" : ""}${vs.src === liveSrc ? " live" : ""}${chainSrcs.has(vs.src) ? " in-chain" : ""}`;
  li.setAttribute("aria-selected", on ? "true" : "false");
  if (vs.src === liveSrc) li.setAttribute("aria-current", "location"); else li.removeAttribute("aria-current");
  li.tabIndex = on ? 0 : -1;
}

let listKey = "";             // what the list was built for (guide, steps listed, search): rebuilt only when it changes
let listShownSrc: number | null = null;     // the step opened in the list

/** The step list (a listbox, the guide window's one scroller). Built once per guide, filter or search; moving the
 * current step only closes the old item and opens the new one. */
function renderStepList(scrollActive = false): void {
  const listEl = $("rxpb-step-list");
  if (!listEl || !currentGuide) return;
  const q = filter.stepSearch.trim().toLowerCase();
  const key = `${currentGuide.id}:${currentGuide.index}|${visibleSteps.map((v) => `${v.src}${v.rxpNum ?? "-"}`).join(",")}|${q}|${factsVersion}`;
  const hadFocus = listEl.contains(document.activeElement);
  if (key !== listKey) {
    listKey = key;
    const items: string[] = [];
    visibleSteps.forEach((vs, i) => {
      if (q) {
        const st = vs.step;
        const badges = (st.actions || []).map((a) => actionBadge(a)).filter(Boolean) as { label: string }[];
        const hay = `${stepNum(vs)} ${st.text || ""} ${stepHeadline(st)} ${st.zone || ""} ${st.comments || ""} ${badges.map((b) => b.label).join(" ")}`.toLowerCase();
        if (!hay.includes(q) && i !== activeIdx) return;
      }
      items.push(`<li role="option" id="rxpb-s-${vs.src}" data-src="${vs.src}">${stepItemHtml(vs, i)}</li>`);
    });
    listEl.innerHTML = items.length ? items.join("") : `<li class="rxpb-empty">${q ? `No steps match "${esc(filter.stepSearch)}".` : "No steps for this character."}</li>`;
    listEl.querySelectorAll<HTMLLIElement>("li[data-src]").forEach((li) => setItemState(li, posBySrc.get(Number(li.dataset.src))!));
  } else if (listShownSrc !== activeSrc) {
    for (const src of [listShownSrc, activeSrc]) {
      const li = src != null ? document.getElementById(`rxpb-s-${src}`) : null;
      const at = src != null ? posBySrc.get(src) : undefined;
      if (li && at !== undefined) li.innerHTML = stepItemHtml(visibleSteps[at], at);
    }
    // done/current classes move with the current step
    listEl.querySelectorAll<HTMLLIElement>("li[data-src]").forEach((li) => setItemState(li, posBySrc.get(Number(li.dataset.src))!));
  }
  listShownSrc = activeSrc;
  const activeLi = activeSrc != null ? document.getElementById(`rxpb-s-${activeSrc}`) : null;
  if (activeLi && hadFocus && document.activeElement !== activeLi && !activeLi.contains(document.activeElement)) activeLi.focus({ preventScroll: true });
  if (scrollActive && activeLi) {
    // scroll the list itself (scrollIntoView would also scroll the page around it); a tall current step shows its top
    const top = activeLi.offsetTop - listEl.offsetTop, bottom = top + activeLi.offsetHeight;
    if (top < listEl.scrollTop || activeLi.offsetHeight > listEl.clientHeight) listEl.scrollTo({ top: Math.max(0, top - 8), behavior: "smooth" });
    else if (bottom > listEl.scrollTop + listEl.clientHeight) listEl.scrollTo({ top: bottom - listEl.clientHeight + 8, behavior: "smooth" });
  }
}

/** Re-open the current step in the list (its ticked objectives or waypoint changed). */
function renderActiveStep(): void {
  const li = activeSrc != null ? document.getElementById(`rxpb-s-${activeSrc}`) : null;
  if (li) li.innerHTML = stepItemHtml(visibleSteps[activeIdx], activeIdx);
}

/** Make the step at this place in the list the current one. */
function selectStep(idx: number, scrollList = true): void {
  if (!visibleSteps.length) return;
  const before = activeSrc;
  activeIdx = Math.max(0, Math.min(visibleSteps.length - 1, idx));
  activeSrc = visibleSteps[activeIdx].src;
  const vs = visibleSteps[activeIdx];
  activeWpIdx = pinStop(vs.step)?.wi ?? 0;
  if (vs && vs.ui_map != null) activeMapId = vs.ui_map;
  // a step reached by a flight shows on the continent, where the whole flight is drawn
  const cont = continentTab();
  const inLeg = (legsByTo.get(vs.src) || []).find((l) => l.from !== l.to);
  if (cont != null && inLeg && inLeg.kind === "fly" && mapsGeo()?.[String(inLeg.pa.um)]?.parent === cont
      && mapsGeo()?.[String(inLeg.pb.um)]?.parent === cont) activeMapId = cont;
  renderTimeline();
  if (focusQuest != null) renderChainBar();
  renderPrep();
  renderStepList(scrollList);
  renderMap();
  followStep();
  scheduleRoutes();
  // the player picked another step while the walk goes on (a pin, a zone tab): it goes on from there, said afresh
  if (walking && !stepByWalk && activeSrc !== before) walkOn();
}

// ---- the walking routes: asked of the engine nearest the current step first, again while legs are pending ------
let routesTimer: number | null = null;
let routesBusy = false;
let routesAgain = false;
let routesIdle = 0;                            // answers in a row that brought no new leg (stop asking after a few)

/** Ask for the routes soon (debounced: scrubbing asks once, for where it stops). */
function scheduleRoutes(delay = 300): void {
  if (routesTimer) window.clearTimeout(routesTimer);
  routesTimer = window.setTimeout(() => { routesTimer = null; requestRoutes(); }, delay);
}

/** One request in flight per guide: the visible steps, the current one first; legs the budget didn't reach come
 * back pending and are asked for again (each call mostly reads the engine's cache). */
async function requestRoutes(): Promise<void> {
  const g = currentGuide;
  if (!g || !visibleSteps.length) return;
  if (routesBusy) { routesAgain = true; return; }
  routesBusy = true;
  const seq = openSeq;
  const faction = racePick(filter.race).faction || g.faction || null;
  let r: RxpRoutes | undefined;
  try {
    r = await quiet("addon:rxpRoutes", g.id, g.index, { steps: visibleSteps.map((v) => v.src), focus: activeSrc ?? undefined,
      budget_ms: 2500, faction, level: g.level_min });
  } finally {
    routesBusy = false;
  }
  if (seq !== openSeq || currentGuide !== g) { if (routesAgain) { routesAgain = false; requestRoutes(); } return; }
  let added = 0;
  for (const leg of r?.legs || []) {
    const k = `${leg.from}:${leg.a}>${leg.to}:${leg.b}`;
    const old = routeLegs.get(k);
    if (!old || JSON.stringify(old) !== JSON.stringify(leg)) { routeLegs.set(k, leg); added++; }
  }
  routesIdle = added ? 0 : routesIdle + 1;
  if (added) {
    routesVersion++;
    if (currentPage() === "guides") renderMap();
  }
  const more = !!r && !r.error && !r.unavailable && (r.pending || []).length > 0 && routesIdle < 3;
  if (routesAgain || (more && currentPage() === "guides")) {
    routesAgain = false;
    requestRoutes();
  }
}

/** What the knowledge base knows of the guide (its map targets): asked once per guide, after the guide is drawn. */
/** The class and race the facts' plan is asked for: the Character picks (no plan without a class). */
function factsPick(): { cls?: string; race?: string } {
  const race = racePick(filter.race).race;
  return { cls: filter.cls || undefined, race: filter.cls && race ? race : undefined };
}

/** What the knowledge base knows of the guide (map targets, quests, prep, and for the picked class the coach's
 * plan and hazards): asked once per guide and pick, after the guide is drawn; the last ask wins. */
async function requestFacts(g: RxpGuideDetail): Promise<void> {
  const want = factsPick();
  const key = `${g.id}:${g.index}:${want.cls || ""}:${want.race || ""}`;
  factsWant = key;
  let f = factsCache.get(key);
  if (f === undefined) {
    f = ((await quiet("addon:rxpFacts", g.id, g.index, want.cls, want.race)) as RxpGuideFacts | null | undefined) ?? null;
    factsCache.set(key, f);
  }
  if (currentGuide !== g || factsWant !== key) return;
  facts = f;
  factsVersion++;
  renderStepList(false);             // the badges take the knowledge base's quest names
  if (focusQuest != null) focusChain(focusQuest, false); else renderChainBar();
  renderPrep();
  renderCharacter();
  renderTimeline();
  renderMap();
}

/** The coach's plan for the picked class (step_brief figures per st.idx), when the facts hold it for that class. */
function planSteps(): PlanSteps | null {
  return facts?.plan && filter.cls && facts.plan.class === filter.cls ? facts.plan.steps : null;
}

/** Make the step with this guide step number (st.idx) the current one, when it is listed. */
function selectSrc(src: number, scrollList = true): void {
  const at = posBySrc.get(src);
  if (at !== undefined) selectStep(at, scrollList);
}

// ---- walking the route: on a timer, or with Narrate on, each step said aloud and the walk waiting for the voice ----
const NARRATION_TAG = "guides";
const NARRATION_BEAT_MS = 700;      // the pause after a step's lines before the next step
let walking = false;
let walkRun = 0;                    // bumped whenever the walk stops or starts over: an older loop sees it and ends
let stepByWalk = false;             // the walk itself is moving to the next step (not the player's pick)
const saidVia = new Set<number>();  // steps whose own line was said in this walk (their .fly / .hs isn't said twice)

/** Narrate is on and the voice would be heard: muted, the walk keeps its timer and never waits on silence. */
const narrating = () => filter.narrate && speech.canSpeak();
const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));

function stopPlay(): void {
  walking = false;
  walkRun++;
  if (playTimer !== null) {
    window.clearInterval(playTimer);
    playTimer = null;
  }
  speech.cancel(NARRATION_TAG);
  const btn = document.getElementById("rxpb-play");
  if (btn) btn.innerHTML = `${icon("play")}Walk route`;
}

function togglePlay(): void {
  if (walking) {
    stopPlay();
    return;
  }
  if (activeIdx >= visibleSteps.length - 1) activeIdx = 0;
  selectStep(activeIdx, true);
  walking = true;
  saidVia.clear();
  walkOn();
  renderTimeline();
}

/** The walk goes on from the current step: said aloud with Narrate on and the voice heard, else on the timer. */
function walkOn(): void {
  const run = ++walkRun;
  if (playTimer !== null) { window.clearInterval(playTimer); playTimer = null; }
  speech.cancel(NARRATION_TAG);
  if (narrating()) { narrateWalk(run); return; }
  playTimer = window.setInterval(() => {
    if (currentPage() !== "guides") { stopPlay(); return; }
    if (narrating()) { walkOn(); return; }          // the voice came back or Narrate was ticked: speak from here
    if (activeIdx < visibleSteps.length - 1) {
      walkStep();
    } else {
      stopPlay();
    }
  }, playMs);
}

function walkStep(): void {
  stepByWalk = true;
  try { selectStep(activeIdx + 1, true); } finally { stepByWalk = false; }
}

/** Say the current step, then the way to the next when there is one, wait for both and a beat, move on; again. */
async function narrateWalk(run: number): Promise<void> {
  while (run === walkRun) {
    if (currentPage() !== "guides") { stopPlay(); return; }
    if (!narrating()) { walkOn(); return; }
    const at = activeIdx;
    const vs = visibleSteps[at];
    const from = Date.now();
    if (vs) {
      const line = stepLine(vs.step, narratedActions(vs.step), null);
      if (line) saidVia.add(vs.src);
      await sayLine(line);
    }
    if (run !== walkRun) return;
    // the player talked over it (push-to-talk): pause on this step, so playing again says it again
    if (speech.bargedSince(from)) { stopPlay(); return; }
    await sayLine(transitionSpeech(at));            // worked out after the step's line: its route has had time
    if (run !== walkRun) return;
    if (speech.bargedSince(from)) { stopPlay(); return; }
    await sleep(NARRATION_BEAT_MS);
    if (run !== walkRun) return;
    if (!narrating()) { walkOn(); return; }         // muted mid-step: the timer takes over from this step
    if (activeIdx >= visibleSteps.length - 1) { stopPlay(); return; }
    walkStep();
  }
}

/** Speak a line and wait for its end; a voice that never says it has ended holds the walk a while, not for ever. */
async function sayLine(text: string): Promise<void> {
  if (!text) return;
  const cap = 6000 + text.split(/\s+/).length * 1000;
  await Promise.race([speech.speakAndWait(text, NARRATION_TAG), sleep(cap)]);
}

/** The step's actions the picked character gets (all of them with "Show all <<" on), as the step list shows them. */
function narratedActions(st: RxpGuideStep): RxpGuideAction[] {
  return (st.actions || []).filter((a) => filter.showAllCond || actionApplies(a));
}

/** Where a step is: its first or last waypoint the picked character gets, else its own position; null without one. */
function stopOf(st: RxpGuideStep, last: boolean): NarrationStop | null {
  const wps = stepWaypoints(st);
  const w = wps.length ? wps[last ? wps.length - 1 : 0].w : null;
  const x = w ? w.x : st.x, y = w ? w.y : st.y;
  if (x == null || y == null) return null;
  return { ui_map: (w && w.ui_map) || st.ui_map, x, y, zone: (w && w.zone) || st.zone, who: stepWho(st, narratedActions(st)) };
}

/** The way from the current step to the next, "" when it isn't worth saying (rxp-narration.ts decides). It starts
 * at the last step with a position (a hearth or a note between has none) and ends at the next step's first stop. */
function transitionSpeech(at: number): string {
  const nxt = visibleSteps[at + 1];
  const to = nxt ? stopOf(nxt.step, false) : null;
  if (!to || !currentGuide) return "";
  let fromAt = at, from: NarrationStop | null = null;
  for (; fromAt >= 0 && at - fromAt < 30; fromAt--) {
    from = stopOf(visibleSteps[fromAt].step, true);
    if (from) break;
  }
  if (!from) return "";
  let via: ReturnType<typeof stepVia> = null, viaSaid = false;
  for (let i = fromAt; i <= at; i++) {
    const v = stepVia(visibleSteps[i].step, narratedActions(visibleSteps[i].step));
    if (v) { via = v; viaSaid = saidVia.has(visibleSteps[i].src); }
  }
  const fromSrc = visibleSteps[fromAt].src;
  // the legs the map asks the engine for (requestRoutes): what has come back by now, never waited for
  const legs = [...routeLegs.values()].filter((l) => l.from === fromSrc && l.to === nxt.src);
  return transitionLine({ from, to, legs, via, viaSaid, maps: currentGuide.maps });
}

let openSeq = 0;            // the latest openGuide call: an older one that answers later is dropped
let wantGuide: { packId: string; index: number } | null = null;   // browseGuide's pick, opened once the list loads

export async function openGuide(packId: string, index: number): Promise<void> {
  stopPlay();
  selected = null;
  focusQuest = null;
  chainSrcs = new Set();
  renderChainBar();
  $("rxpb-step-list").classList.remove("chain-on");
  facts = null;
  renderPrep();
  const seq = ++openSeq;
  const key = `${packId}:${index}`;
  let g = cache.get(key) || null;
  if (!g) {
    $("rxpb-win-title").textContent = "Loading guide…";
    g = (await call("addon:rxpImports", packId, index)) as RxpGuideDetail | null;
    if (g) cache.set(key, g);
  }
  if (seq !== openSeq) return;
  if (!g) {
    $("rxpb-win-title").textContent = "Couldn't open that guide";
    $("rxpb-win-sub").textContent = "Try another, or restart the coach and try again";
    return;
  }
  currentGuide = g;
  if (stepCounts.get(key) !== g.steps.length) {
    stepCounts.set(key, g.steps.length);
    for (const c of catalog) if (c.packId === packId && c.index === index) c.steps = g.steps.length;
  }
  routeLegs.clear();
  routesVersion++;
  routesIdle = 0;
  facts = null;
  factsVersion++;
  vis = g.players && g.vis_masks ? new Visibility(g.players, g.vis_masks) : null;
  if (!defaultsApplied && g.defaults) {
    // the toggles start as the coach plays (config.json "rxp", else solo self-found and group quests on)
    defaultsApplied = true;
    filter.ssf = !!g.defaults.ssf;
    filter.groupQuests = !!g.defaults.group_quests;
    filter.hardcore = !!g.defaults.hardcore;
    $<HTMLInputElement>("rxpb-ssf").checked = filter.ssf;
    $<HTMLInputElement>("rxpb-grp").checked = filter.groupQuests;
    $<HTMLInputElement>("rxpb-hc").checked = filter.hardcore;
  }
  activeSrc = null;
  activeIdx = 0;
  activeWpIdx = 0;
  activeMapId = g.zones?.[0]?.ui_map ?? null;

  // Sync the group & guide dropdowns: the group picker moves only when the guide isn't in the group shown (a guide
  // RestedXP files under both factions, such as Zephras Isle, stays in the one the player picked it from)
  const grpSel = $<HTMLSelectElement>("rxpb-group");
  const inGroup = (grp: string) => catalog.some((c) => c.packId === packId && c.index === index && c.group === grp);
  if (filter.group && !inGroup(filter.group)) {
    const entry = catalog.find((c) => c.packId === packId && c.index === index);
    filter.group = entry ? entry.group : "";
    grpSel.value = filter.group;
  }
  populateGuideSelect();
  $<HTMLSelectElement>("rxpb-guide").value = key;

  rebuildVisibleSteps();
  activeWpIdx = visibleSteps.length ? pinStop(visibleSteps[0].step)?.wi ?? 0 : 0;
  liveSrc = liveStepSrc();
  renderLive();
  renderHeader();
  renderCharacter();
  renderTimeline();
  renderStepList(false);
  renderMap();
  scheduleRoutes(0);
  requestFacts(g);
}

function populateGuideSelect(): void {
  const grpSel = $<HTMLSelectElement>("rxpb-group");
  const guideSel = $<HTMLSelectElement>("rxpb-guide");
  const q = filter.search.trim().toLowerCase();
  const filtered = catalog.filter((c) => {
    if (filter.group && c.group !== filter.group) return false;
    if (q && !`${c.name} ${c.subgroup || ""} ${c.group}`.toLowerCase().includes(q)) return false;
    return true;
  });

  // Group by subgroup for familiar RestedXP menu hierarchy
  const bySub = new Map<string, CatalogEntry[]>();
  for (const c of filtered) {
    const sub = c.subgroup || c.group || "Guides";
    if (!bySub.has(sub)) bySub.set(sub, []);
    bySub.get(sub)!.push(c);
  }
  const html: string[] = [];
  for (const [sub, items] of bySub) {
    html.push(`<optgroup label="${esc(sub)}">`);
    for (const item of items) {
      const val = `${item.packId}:${item.index}`;
      html.push(`<option value="${esc(val)}">${esc(item.name)} (${item.steps} steps)</option>`);
    }
    html.push(`</optgroup>`);
  }
  guideSel.innerHTML = html.join("") || `<option value="">No matching guides</option>`;
  if (currentGuide) {
    const curKey = `${currentGuide.id}:${currentGuide.index}`;
    if (Array.from(guideSel.options).some((o) => o.value === curKey)) guideSel.value = curKey;
  }
  grpSel.value = filter.group;
}

export async function updateRxpBrowser(imports: RxpImport[]): Promise<void> {
  const sec = $("rxpb-block");
  if (!sec) return;
  const entries: CatalogEntry[] = [];
  for (const imp of imports) {
    const items = imp.items || imp.guides.map((name, index) => ({
      index, group: "RestedXP", subgroup: null, name, next: null, faction: null, defaultFor: null, version: null, steps: 0,
    }));
    for (const it of items) entries.push({ ...it, packId: imp.id, steps: stepCounts.get(`${imp.id}:${it.index}`) ?? it.steps });
  }
  // each group in level order, so RestedXP's free 1-20 guides come before a bought string's 20-60 ones
  const groupOrder = new Map<string, number>();
  entries.forEach((e) => { if (!groupOrder.has(e.group)) groupOrder.set(e.group, groupOrder.size); });
  const startLevel = (name: string) => { const m = /^\s*0?(\d+)\s*-/.exec(name); return m ? Number(m[1]) : 999; };
  catalog = entries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => groupOrder.get(a.e.group)! - groupOrder.get(b.e.group)! || startLevel(a.e.name) - startLevel(b.e.name) || a.i - b.i)
    .map((x) => x.e);
  $("rxpb-empty").hidden = catalog.length > 0;
  if (!catalog.length) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;
  const groups = Array.from(new Set(catalog.map((c) => c.group)));
  const grpSel = $<HTMLSelectElement>("rxpb-group");
  grpSel.innerHTML = `<option value="">All guide groups (${catalog.length} guides)</option>`
    + groups.map((g) => `<option value="${esc(g)}">${esc(g)} (${catalog.filter((c) => c.group === g).length})</option>`).join("");
  if (filter.group && !groups.includes(filter.group)) filter.group = "";
  populateGuideSelect();

  const want = wantGuide;
  wantGuide = null;
  if (want && catalog.some((c) => c.packId === want.packId && c.index === want.index)) {
    await openGuide(want.packId, want.index);
  } else if (!liveApplied && !pickersTouched && liveEntry()) {
    // the first show with the coach running: the character's guide, picks and step
    await jumpToLive();
  } else if (!currentGuide || !catalog.some((c) => c.packId === currentGuide!.id && c.index === currentGuide!.index)) {
    // Prefer a starter 1-6 / 1-10 / 1-11 guide by default if present, else the first guide
    const starter = catalog.find((c) => /^0?1-\d+/.test(c.name)) || catalog[0];
    if (starter) await openGuide(starter.packId, starter.index);
  }
}

/** The Guides page was shown: the guides added in the app and RestedXP's free ones, read fresh. */
export async function refreshGuides(): Promise<void> {
  await updateRxpBrowser(((await quiet("addon:rxpImports")) as RxpImport[]) || []);
  liveKey = "";               // the step may have moved while the page was away
  liveGameChanged();
}

// ---- the live character: their guide and step, from the coach's light stream (live.game) ------------------------

/** The character in game, while the coach runs and they are on a RestedXP guide step. */
function liveGame() {
  const g = live.game;
  return coachRunning() && g && g.guide && g.step != null && g.step > 0 ? g : null;
}

const liveWho = () => live.game?.name || "Your character";

/** The page's guide the character plays, or null (not on a guide, or a guide the page doesn't have). */
function liveEntry(): CatalogEntry | null {
  const g = liveGame();
  return g ? liveGuide(catalog, g.guide, g.guide_group, g.faction) : null;
}

const isOpen = (e: CatalogEntry | null) => !!e && !!currentGuide && currentGuide.id === e.packId && currentGuide.index === e.index;

/** The character's RestedXP settings: the addon's solo self-found and group quests, else the coach's (config.json),
 * and hardcore from config.json (the strip doesn't carry it). */
function liveSettings(): { ssf: boolean; groupQuests: boolean; hardcore: boolean } {
  const rs = live.game?.rxp_settings, d = currentGuide?.defaults;
  return { ssf: rs?.ssf ?? d?.ssf ?? filter.ssf, groupQuests: rs?.group_quests ?? d?.group_quests ?? filter.groupQuests,
    hardcore: d?.hardcore ?? filter.hardcore };
}

/** The live step in the open guide (st.idx), numbered for the character and their settings by the engine's masks
 * (RestedXP's own count), whatever the pickers show; null when the character plays another guide. */
function liveStepSrc(): number | null {
  const g = liveGame();
  if (!g || !currentGuide || !isOpen(liveEntry())) return null;
  const steps = currentGuide.steps;
  if (!vis) return steps[Math.min(g.step!, steps.length) - 1]?.idx ?? null;
  const s = liveSettings();
  const player = vis.players.indexOf(`${liveRacePick(g.race, g.faction)}/${liveClassPick(g.class)}`);
  const pos = liveStepPos(vis, steps.map((st) => st.vis), player, comboOf(s.ssf, s.groupQuests, s.hardcore), g.step!);
  return pos == null ? null : steps[pos].idx;
}

/** The "Your step N" button in the timeline bar: hidden without the coach or a guide step; it opens the character's
 * guide when another is open. */
function renderLive(): void {
  const btn = document.getElementById("rxpb-live") as HTMLButtonElement | null;
  if (!btn) return;
  const g = liveGame();
  btn.hidden = !g;
  if (!g) return;
  const e = liveEntry();
  btn.disabled = !e;
  btn.innerHTML = e && !isOpen(e) ? `<i class="lv-mark"></i>Open <span class="lv-word">your guide</span>`
    : `<i class="lv-mark"></i><span class="lv-word">Your step </span>${g.step}`;
  btn.setAttribute("aria-label", e && !isOpen(e) ? "Open your guide" : `Your step ${g.step}`);
  btn.title = !e ? `${g.guide} isn't among your guides here`
    : isOpen(e) ? `Jump to where ${liveWho()} is in RestedXP (L)`
      : `Open ${e.name} at ${liveWho()}'s step ${g.step} (L)`;
}

/** The pickers take the character's race, class and RestedXP settings. */
function applyLiveFilters(): void {
  const g = live.game;
  if (!g) return;
  liveApplied = true;
  defaultsApplied = true;
  const s = liveSettings();
  filter.race = liveRacePick(g.race, g.faction);
  filter.cls = liveClassPick(g.class);
  filter.ssf = s.ssf;
  filter.groupQuests = s.groupQuests;
  filter.hardcore = s.hardcore;
  $<HTMLSelectElement>("rxpb-race").value = filter.race;
  $<HTMLSelectElement>("rxpb-class").value = filter.cls;
  $<HTMLInputElement>("rxpb-ssf").checked = filter.ssf;
  $<HTMLInputElement>("rxpb-grp").checked = filter.groupQuests;
  $<HTMLInputElement>("rxpb-hc").checked = filter.hardcore;
}

/** Jump to the character's step: open their guide first when another is open; the first time (or when the pickers
 * hide that step), the pickers take the character's values. */
async function jumpToLive(): Promise<void> {
  const e = liveEntry();
  if (!e || !liveGame()) return;
  stopPlay();
  if (!isOpen(e)) await openGuide(e.packId, e.index);
  if (!isOpen(e)) return;                     // another guide was opened meanwhile
  liveSrc = liveStepSrc();
  if (!liveApplied || (liveSrc != null && !posBySrc.has(liveSrc))) {
    applyLiveFilters();
    refilter();
  }
  renderLive();
  if (liveSrc != null && posBySrc.has(liveSrc)) {
    selectSrc(liveSrc, true);
    const tgt = targetStop(visibleSteps[activeIdx]);
    if (tgt && view.z > 1) centerOn(tgt.x, tgt.y);
  }
}

let liveKey = "";

/** The light stream brought a state (once a second) or the coach stopped: move the marker and the button when the
 * character, guide or step changed. With the coach started after the page opened, the untouched pickers take the
 * character's values once. */
export function liveGameChanged(): void {
  const g = liveGame();
  const key = g ? JSON.stringify([g.name, g.class, g.race, g.faction, g.guide, g.guide_group, g.step, g.rxp_settings]) : "";
  if (key === liveKey) return;
  liveKey = key;
  if (g && !liveApplied && !pickersTouched && currentGuide) {
    applyLiveFilters();
    refilter();
  }
  const was = liveSrc;
  liveSrc = liveStepSrc();
  renderLive();
  if (was === liveSrc) return;
  for (const src of [was, liveSrc]) {
    const li = src != null ? document.getElementById(`rxpb-s-${src}`) : null;
    const at = src != null ? posBySrc.get(src) : undefined;
    if (li && at !== undefined) setItemState(li, at);
  }
  renderTimeline();
}

/** Open a guide on the Guides page (the Addon page's "Browse guides"). */
export function browseGuide(packId: string, index: number): void {
  wantGuide = { packId, index };
  show("guides");               // the page loads its list (refreshGuides), then opens this guide
}

/** The card a map element shows while hovered or focused (pins, waypoints, legs, loops, objectives), beside it or
 * beside the pointer for a long element (a leg, a loop, an objective cluster); kept inside the map. */
function showHoverCard(el: SVGElement, pt?: { x: number; y: number }): void {
  const card = $("rxpb-hovercard");
  const kind = el.dataset.kind || "";
  const c = kind === "edge" || kind === "zone"
    ? { head: `<b>${esc(mapName(Number(el.dataset.map)))}</b>`, body: `<div class="rxpb-hc-line">${kind === "edge" ? (el.dataset.chain ? (el.dataset.dir === "from" ? "The quest chain comes from there." : "The quest chain goes on there.")
      : el.dataset.dir === "from" ? "The way comes in from here." : "The way goes on there.") + " " : ""}Click to open its map.</div>` }
    : elementCard(kind, Number(el.dataset.src ?? (kind === "leg" ? planned.find((l) => l.key === el.dataset.leg)?.to : NaN)),
      el.dataset.sub != null ? Number(el.dataset.sub) : null, el.dataset.leg ?? null);
  if (!c) { card.hidden = true; return; }
  card.innerHTML = `<div class="rxpb-hc-head">${c.head}</div>${c.body}`;
  card.hidden = false;
  const stage = $("rxpb-map-stage").getBoundingClientRect();
  const box = pt && (kind === "leg" || kind === "loop" || kind === "obj")
    ? { left: pt.x - 4, right: pt.x + 4, top: pt.y - 4 } : el.getBoundingClientRect();
  const w = card.offsetWidth, h = card.offsetHeight;
  let left = box.right - stage.left + 10, top = box.top - stage.top - 6;
  if (left + w > stage.width - 8) left = box.left - stage.left - w - 10;
  top = Math.max(8, Math.min(stage.height - h - 8, top));
  card.style.left = `${Math.max(8, left)}px`;
  card.style.top = `${top}px`;
}

function hideHoverCard(): void {
  $("rxpb-hovercard").hidden = true;
}

/** Open another map's tab (an edge marker, a zone outline on the continent). */
function openMap(um: number): void {
  activeMapId = um;
  hideHoverCard();
  renderMap();
}

/** Click (or Enter / Space) on a map element: a pin makes its step current; a waypoint aims the arrow; a leg, a loop
 * or an objective opens in the inspector; an edge marker or a zone outline opens that map. */
function pickElement(el: SVGElement): void {
  const kind = el.dataset.kind, src = Number(el.dataset.src);
  if (kind === "step") {
    selected = { kind: "step", src };
    stopPlay();
    selectSrc(src, true);
  } else if (kind === "wp") {
    if (src !== activeSrc) selectSrc(src, true);
    activeWpIdx = Number(el.dataset.sub);
    selected = { kind: "wp", src, sub: activeWpIdx };
    renderMap();
    renderActiveStep();
    followStep();
  } else if (kind === "leg") {
    const leg = planned.find((l) => l.key === el.dataset.leg);
    if (leg) { selected = { kind: "leg", src: leg.to, key: leg.key }; updateClasses(); renderInspector(); }
  } else if (kind === "loop" || kind === "obj" || kind === "hz") {
    selected = { kind, src, sub: Number(el.dataset.sub) };
    updateClasses();
    renderInspector();
  } else if (kind === "edge" || kind === "zone") {
    openMap(Number(el.dataset.map));
  }
}

let wheelTimer: number | null = null;

/** Zoom with the wheel (Ctrl+wheel stays the app's own zoom), pan by dragging, the whole zone on double-click; the
 * view buttons and toggles; hover, focus and click on every map element through one set of listeners. */
function wireMapView(): void {
  const stage = $("rxpb-map-stage");
  const onMapItself = (e: Event) => !(e.target as HTMLElement).closest(".rxpb-map-tools, .rxpb-inspector, .rxpb-arrow-hud");
  stage.addEventListener("wheel", (e) => {
    if (e.ctrlKey || !onMapItself(e)) return;
    e.preventDefault();
    // the layer is a raster only while it moves; at rest the browser draws it sharp at the new zoom
    stage.classList.add("moving");
    if (wheelTimer) window.clearTimeout(wheelTimer);
    wheelTimer = window.setTimeout(() => { stage.classList.remove("moving"); wheelTimer = null; }, 220);
    const r = stage.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.25 : 1 / 1.25, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  let drag: { x: number; y: number; fx: number; fy: number; moved: boolean } | null = null;
  let justDragged = false;
  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !onMapItself(e)) return;
    drag = { x: e.clientX, y: e.clientY, fx: view.fx, fy: view.fy, moved: false };
  });
  window.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) { drag.moved = true; stage.classList.add("dragging", "moving"); hideHoverCard(); }
    const { w, h } = stageSize();
    view.fx = drag.fx + dx / w;
    view.fy = drag.fy + dy / h;
    applyView();
  });
  window.addEventListener("pointerup", () => {
    if (drag?.moved) justDragged = true;       // the click that ends a drag doesn't pick anything
    drag = null;
    stage.classList.remove("dragging", "moving");
  });
  stage.addEventListener("click", (e) => { if (justDragged) { e.stopPropagation(); justDragged = false; } }, true);
  stage.addEventListener("dblclick", (e) => { if (onMapItself(e)) resetView(); });
  $("rxpb-zoom-in").onclick = () => zoomAt(1.5);
  $("rxpb-zoom-out").onclick = () => zoomAt(1 / 1.5);
  $("rxpb-zoom-reset").onclick = resetView;
  $<HTMLInputElement>("rxpb-follow").onchange = (e) => {
    filter.follow = (e.target as HTMLInputElement).checked;
    followStep();
  };
  $<HTMLInputElement>("rxpb-narrate").onchange = (e) => {
    filter.narrate = (e.target as HTMLInputElement).checked;
    if (walking) walkOn();                // speak from the current step, or back to the timer
  };
  $<HTMLInputElement>("rxpb-showdone").onchange = (e) => {
    filter.showDone = (e.target as HTMLInputElement).checked;
    updateClasses();
  };
  new ResizeObserver(() => applyView()).observe(stage);

  // every map element: a card on hover or keyboard focus, a click (or Enter / Space) picks it
  const svgEl = $("rxpb-map-svg");
  const elOf = (t: EventTarget | null) => (t as Element | null)?.closest?.<SVGElement>("[data-kind]") || null;
  // (a faded step ahead comes back to full strength while it's looked at: lightStep)
  svgEl.addEventListener("pointerover", (e) => {
    const el = elOf(e.target);
    if (el && !drag) { lightStep(posOfEl(el)); showHoverCard(el, { x: e.clientX, y: e.clientY }); }
  });
  svgEl.addEventListener("pointerout", (e) => {
    const el = elOf(e.target);
    if (el && elOf((e as PointerEvent).relatedTarget) !== el) { hideHoverCard(); lightStep(null); }   // left it (not just moved within it)
  });
  svgEl.addEventListener("focusin", (e) => { const el = elOf(e.target); if (el) { lightStep(posOfEl(el)); showHoverCard(el); } });
  svgEl.addEventListener("focusout", () => { hideHoverCard(); lightStep(null); });
  svgEl.addEventListener("click", (e) => {
    const el = elOf(e.target);
    if (!el) return;
    e.stopPropagation();
    pickElement(el);
  });
  svgEl.addEventListener("keydown", (e) => {
    const el = elOf(e.target);
    if (el && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      e.stopPropagation();
      pickElement(el);
    }
  });
  stage.addEventListener("wheel", hideHoverCard, { passive: true });
}


/** On the Guides page, away from a text box: left/right step, Space walks the route or stops, +/- zoom, 0 whole zone,
 * L the live character's step. */
function wireKeys(): void {
  document.addEventListener("keydown", (e) => {
    if (currentPage() !== "guides" || e.ctrlKey || e.altKey || e.metaKey || !visibleSteps.length) return;
    if (e.key === "Escape") {
      // the card, else the inspector (a popover closes itself first)
      const card = $("rxpb-hovercard");
      if (!card.hidden && !selected) { hideHoverCard(); e.preventDefault(); return; }
      if (selected) { clearSelection(); e.preventDefault(); return; }
      if (focusQuest != null) { focusChain(null); e.preventDefault(); }
      return;
    }
    // a text box, a select, a checkbox or the timeline slider handles its own keys (the slider steps on arrows);
    // Space on a focused button presses it
    const el = e.target as HTMLElement;
    if (el.closest("input, select, textarea, [contenteditable]")) return;
    if (e.key === " " && el.closest("button, a, label")) return;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      stopPlay();
      selectStep(activeIdx + (e.key === "ArrowRight" ? 1 : -1), true);
    } else if (e.key === " ") {
      togglePlay();
    } else if (e.key === "+" || e.key === "=") {
      zoomAt(1.5);
    } else if (e.key === "-") {
      zoomAt(1 / 1.5);
    } else if (e.key === "0") {
      resetView();
    } else if (e.key === "l" || e.key === "L") {
      if (!liveEntry()) return;
      jumpToLive();
    } else {
      return;
    }
    e.preventDefault();
  });
}

/** The step list: click a step to make it current; in the current step, tick objectives, pick a waypoint, open a
 * link; Up/Down/Home/End/PageUp/PageDown move through the steps (a listbox with a roving tabindex). */
function wireStepList(): void {
  const listEl = $("rxpb-step-list");
  // pointing at a step in the list brings it back to full strength on the map too
  listEl.addEventListener("pointerover", (e) => {
    const li = (e.target as HTMLElement).closest<HTMLLIElement>("li[data-src]");
    lightStep(li ? posBySrc.get(Number(li.dataset.src)) ?? null : null);
  });
  listEl.addEventListener("pointerleave", () => lightStep(null));
  listEl.addEventListener("click", (e) => {
    const el = e.target as HTMLElement;
    const li = el.closest<HTMLLIElement>("li[data-src]");
    if (!li) return;
    const qb = el.closest<HTMLButtonElement>(".rxpb-qb");
    if (qb) { focusChain(Number(qb.dataset.quest)); return; }
    const link = el.closest<HTMLAnchorElement>(".rxpb-ext");
    if (link) { e.preventDefault(); if (link.dataset.url) call("shell:open", link.dataset.url); return; }
    const pill = el.closest<HTMLButtonElement>(".rxpb-wp-pill");
    if (pill) {
      activeWpIdx = Number(pill.dataset.wp);
      const wp = visibleSteps[activeIdx]?.step.waypoints?.[activeWpIdx];
      if (wp?.ui_map) activeMapId = wp.ui_map;
      renderActiveStep();
      renderMap();
      followStep();
      return;
    }
    const row = el.closest<HTMLElement>(".rxpb-obj-row");
    if (row && row.querySelector(".rxpb-chk") && Number(li.dataset.src) === activeSrc) {
      if (el.tagName === "INPUT") e.preventDefault();      // the row toggles it (a click on the box would toggle twice)
      const key = row.dataset.obj!;
      if (checkedObjs.has(key)) checkedObjs.delete(key);
      else checkedObjs.add(key);
      renderActiveStep();
      return;
    }
    if (Number(li.dataset.src) !== activeSrc) { stopPlay(); selectSrc(Number(li.dataset.src), false); }
  });
  listEl.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.altKey || e.metaKey || (e.target as HTMLElement).closest("input, a")) return;
    const page = Math.max(1, Math.floor(listEl.clientHeight / 56));
    const to: Record<string, number> = { ArrowDown: activeIdx + 1, ArrowUp: activeIdx - 1, Home: 0, End: visibleSteps.length - 1,
      PageDown: activeIdx + page, PageUp: activeIdx - page };
    if (!(e.key in to)) return;
    e.preventDefault();
    e.stopPropagation();
    stopPlay();
    selectStep(to[e.key], true);
    const li = activeSrc != null ? document.getElementById(`rxpb-s-${activeSrc}`) : null;
    li?.focus({ preventScroll: true });
  });
}

const RACE_NAMES: Record<string, string> = { NightElf: "Night Elf", "Skyborne@Alliance": "Skyborne (Alliance)",
  "Skyborne@Horde": "Skyborne (Horde)" };

/** The Character chip: who the steps are for and the RestedXP settings, in a few words. */
function renderCharacter(): void {
  const race = filter.race ? RACE_NAMES[filter.race] || filter.race : "";
  const cls = filter.cls ? filter.cls[0] + filter.cls.slice(1).toLowerCase() : "";
  const who = race && cls ? `${race} ${cls}` : race ? `${race}, any class` : cls ? `Any ${cls}` : "Everyone";
  const opts = [filter.ssf ? "SSF" : "", filter.groupQuests ? "group" : "", filter.hardcore ? "HC" : ""].filter(Boolean);
  $("rxpb-char-label").textContent = opts.length ? `${who} · ${opts.join(", ")}` : who;
  const one = vis ? vis.pick(filter.race, filter.cls).length === 1 : false;
  $("rxpb-char-note").textContent = (!vis ? "" : one ? "Step numbers are RestedXP's own for this character."
    : "Showing the steps any of these characters gets. Pick a race and a class for RestedXP's own step numbers.")
    + (!filter.cls && facts?.briefs?.length ? " Pick a class to see how long to each level at the coach's pace." : "");
}

/** The Character chip opens its popover; Escape or a click elsewhere closes it. */
function wireCharacter(): void {
  const btn = $<HTMLButtonElement>("rxpb-char-btn"), pop = $("rxpb-char-pop");
  const open = (on: boolean) => {
    pop.hidden = !on;
    btn.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) pop.querySelector<HTMLElement>("select")?.focus();
  };
  btn.onclick = () => open(pop.hasAttribute("hidden"));
  document.addEventListener("pointerdown", (e) => {
    if (!pop.hidden && !(e.target as Element).closest(".rxpb-char")) open(false);
  });
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); open(false); btn.focus(); }
  });
  renderCharacter();
}

/** The race, class or a toggle changed: list the steps again, keeping the current one. */
function refilter(): void {
  if (currentGuide) {
    const p = factsPick();
    if (factsWant !== `${currentGuide.id}:${currentGuide.index}:${p.cls || ""}:${p.race || ""}`) requestFacts(currentGuide);
  }
  rebuildVisibleSteps();
  renderChainBar();
  renderPrep();
  renderHeader();
  renderCharacter();
  renderTimeline();
  renderStepList(true);
  renderMap();
  routesIdle = 0;
  scheduleRoutes();
}

export function wireRxpBrowser(): void {
  watchGameIcons(document.body);       // the page, its cards and its popovers
  wireMapView();
  wireKeys();
  $("rxpb-to-addon").onclick = () => show("addon");
  $("rxpb-group").onchange = (e) => {
    filter.group = (e.target as HTMLSelectElement).value;
    populateGuideSelect();
    const first = $<HTMLSelectElement>("rxpb-guide").value;
    if (first && first.includes(":")) {
      const [pid, idx] = first.split(":");
      openGuide(pid, Number(idx));
    }
  };
  $("rxpb-guide").onchange = (e) => {
    const v = (e.target as HTMLSelectElement).value;
    if (v && v.includes(":")) {
      const [pid, idx] = v.split(":");
      openGuide(pid, Number(idx));
    }
  };
  $("rxpb-search").oninput = (e) => {
    filter.search = (e.target as HTMLInputElement).value;
    populateGuideSelect();
  };
  $("rxpb-step-search").oninput = (e) => {
    filter.stepSearch = (e.target as HTMLInputElement).value;
    renderStepList(false);
  };
  $("rxpb-race").onchange = (e) => { pickersTouched = true; filter.race = (e.target as HTMLSelectElement).value; refilter(); };
  $("rxpb-class").onchange = (e) => { pickersTouched = true; filter.cls = (e.target as HTMLSelectElement).value; refilter(); };
  $("rxpb-ssf").onchange = (e) => { pickersTouched = defaultsApplied = true; filter.ssf = (e.target as HTMLInputElement).checked; refilter(); };
  $("rxpb-grp").onchange = (e) => { pickersTouched = defaultsApplied = true; filter.groupQuests = (e.target as HTMLInputElement).checked; refilter(); };
  $("rxpb-hc").onchange = (e) => { pickersTouched = defaultsApplied = true; filter.hardcore = (e.target as HTMLInputElement).checked; refilter(); };
  $("rxpb-allcond").onchange = (e) => { filter.showAllCond = (e.target as HTMLInputElement).checked; refilter(); };

  $("rxpb-scrub").oninput = (e) => {
    stopPlay();
    noteScrub(activeIdx);
    selectStep(Number((e.target as HTMLInputElement).value), true);
  };
  $("rxpb-scrub").addEventListener("pointerdown", () => { scrubHeld = true; });
  window.addEventListener("pointerup", () => {
    if (!scrubHeld) return;
    scrubHeld = false;
    if (scrubTimer) window.clearTimeout(scrubTimer);
    scrubTimer = window.setTimeout(endScrub, 1600);
  });
  $("rxpb-prev").onclick = () => { stopPlay(); selectStep(activeIdx - 1, true); };
  $("rxpb-next").onclick = () => { stopPlay(); selectStep(activeIdx + 1, true); };
  $("rxpb-play").onclick = togglePlay;
  $("rxpb-live").onclick = () => { jumpToLive(); };
  // zone tabs (and the continent): open that map; a zone the current step isn't in starts at its first step
  const zonesEl = $("rxpb-zones");
  zonesEl.addEventListener("click", (e) => {
    const b = (e.target as Element).closest<HTMLButtonElement>("button[data-map]");
    if (!b) return;
    const um = Number(b.dataset.map);
    if (!b.classList.contains("cont") && visibleSteps[activeIdx]?.ui_map !== um) {
      const first = visibleSteps.findIndex((vs) => vs.ui_map === um);
      activeMapId = um;
      if (first >= 0) { stopPlay(); selectStep(first, true); activeMapId = um; renderMap(); return; }
    }
    openMap(um);
  });
  zonesEl.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const tabs = Array.from(zonesEl.querySelectorAll<HTMLButtonElement>("button[data-map]"));
    const at = tabs.findIndex((t) => t === document.activeElement);
    if (at < 0) return;
    e.preventDefault();
    e.stopPropagation();
    const next = tabs[(at + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    next.click();
    zonesEl.querySelector<HTMLButtonElement>(`button[data-map="${next.dataset.map}"]`)?.focus();
  });
  $("rxpb-tl-ticks").addEventListener("click", (e) => {
    const t = (e.target as Element).closest<HTMLElement>(".tl-tick, .tl-danger");
    if (t) { stopPlay(); selectSrc(Number(t.dataset.src), true); }
  });
  wireStepList();
  wireCharacter();
  $("rxpb-prep").addEventListener("click", (e) => {
    const el = e.target as Element;
    if (el.closest("#rxpb-prep-more")) { $("rxpb-prep").classList.toggle("open"); fitPrep(); return; }
    const b = el.closest<HTMLButtonElement>(".rxpb-pc");
    if (b) { stopPlay(); selectSrc(Number(b.dataset.src), true); }
  });
  new ResizeObserver(() => fitPrep()).observe($("rxpb-prep"));
  $("rxpb-chainbar").addEventListener("click", (e) => {
    const el = e.target as Element;
    if (el.closest("#rxpb-cb-close")) { focusChain(null); return; }
    const b = el.closest<HTMLButtonElement>(".rxpb-cb-step");
    if (b && !b.disabled) { stopPlay(); selectSrc(Number(b.dataset.src), true); }
  });
  $("rxpb-speed").onchange = (e) => {
    playMs = Number((e.target as HTMLSelectElement).value) || 1600;
    if (playTimer !== null) { stopPlay(); togglePlay(); }
  };
  whenLeft("guides", stopPlay);           // leaving the page stops the walk and its voice at once
}
