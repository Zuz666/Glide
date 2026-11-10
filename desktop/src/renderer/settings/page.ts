// The Settings page: a tab per section of settings-schema.ts (or, while searching, every match), its fields, the
// custom panels some groups ask for (keys, models, voices, ...), and Save / Save and restart / Revert.
import { applyAppSettings } from "../app-settings.js";
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { currentPage, show, whenShown } from "../nav.js";
import { MANIFEST } from "../settings-manifest.js";
import { Field, Group, SCHEMA } from "../settings-schema.js";
import { languageCode } from "../speech-language.js";
import { enginePost, JSON_HEADERS, live } from "../state.js";
import { Obj } from "../types.js";
import { bindField, edits, fieldHtml, getPath, isApp, merge, patchFor, toStore, updateDirty } from "./fields.js";
import { bindModelReasoning } from "./model-picker.js";
import { highlightSearch, queryWords, searchQuery, searchSchema, setSearchQuery } from "./search.js";

/** A custom panel: draws a group's content into `host` (settings-schema.ts Group.special names it). */
export type Panel = (g: Group, host: HTMLElement) => void | Promise<void>;

const TAB_ICON: Record<string, string> = { general: "settings", models: "key-round", coach: "sparkles", buffs: "swords",
                                          language: "globe", voice: "volume-2", overlay: "maximize", capture: "server" };
const LLM = "companion.llm.";
// live-tunable without a restart (the dashboard's Coach context / Models settings): "live" in the engine's manifest
const LIVE = new Set(Object.entries(MANIFEST).filter(([k, e]) => e.live && k.startsWith("llm.")).map(([k]) => k.slice(4)));

let tab = "general";
let panels: Record<string, Panel> = {};
let renderRevision = 0;
let renderedView = "";
let viewAbort: AbortController | undefined;

/** Keep the same visible field while asynchronous panels above it acquire their height. */
function preserveView(body: HTMLElement, enabled: boolean): () => void {
  viewAbort?.abort();
  const abort = viewAbort = new AbortController();
  let active = true;
  for (const event of ["wheel", "touchmove", "pointerdown", "keydown"])
    body.addEventListener(event, () => { active = false; }, { signal: abort.signal, passive: true });
  const top = body.getBoundingClientRect().top, scroll = enabled ? body.scrollTop : 0;
  const candidates = [...body.querySelectorAll<HTMLElement>("[data-key], [data-pick], h2")];
  const anchor = enabled ? candidates.find((el) => el.getBoundingClientRect().top >= top
    && el.getBoundingClientRect().top < top + body.clientHeight) : undefined;
  const selector = anchor?.dataset.key ? `[data-key="${CSS.escape(anchor.dataset.key)}"]`
    : anchor?.dataset.pick ? `[data-pick="${CSS.escape(anchor.dataset.pick)}"]`
    : anchor ? `[data-group="${CSS.escape(anchor.parentElement!.dataset.group!)}"] > h2` : undefined;
  const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
  return () => {
    if (abort.signal.aborted || !active) return;
    const next = selector ? body.querySelector<HTMLElement>(selector) : null;
    body.scrollTop = next ? body.scrollTop + next.getBoundingClientRect().top - body.getBoundingClientRect().top - offset : scroll;
  };
}
const allFields = () => SCHEMA.flatMap((t) => t.groups.flatMap((g) => g.fields || []));

/** The custom panels, by the name settings-schema.ts gives them. */
export function registerPanels(p: Record<string, Panel>): void {
  panels = p;
}

/** Go to the Settings page on one section. */
export function openSettingsTab(id: string): void {
  tab = id;
  show("settings");
}

export async function renderSettings(): Promise<void> {
  const revision = ++renderRevision;
  const [config, appSettings] = await Promise.all([call("config:get"), call("app:settings:get")]);
  if (revision !== renderRevision || currentPage() !== "settings") return;
  live.config = config || {};
  applyAppSettings(appSettings || {});
  const searching = queryWords().length > 0;
  const hits = searching ? searchSchema() : [];
  const perTab = new Map<string, number>();
  for (const h of hits) perTab.set(h.t.id, (perTab.get(h.t.id) || 0) + (h.fields.length || 1));
  renderTabs(searching, perTab);
  const body = $("set-body");
  const view = `${tab}\n${searchQuery()}`, sameView = renderedView === view;
  const restoreView = preserveView(body, sameView);
  const focused = body.contains(document.activeElement) ? document.activeElement as HTMLInputElement : null;
  const focusSelector = focused?.id ? `#${CSS.escape(focused.id)}`
    : focused?.dataset.pick ? `[data-pick="${CSS.escape(focused.dataset.pick)}"]`
    : focused?.dataset.filter ? `[data-filter="${CSS.escape(focused.dataset.filter)}"]`
    : focused?.dataset.effort ? `[data-effort="${CSS.escape(focused.dataset.effort)}"]` : null;
  const selection = focused instanceof HTMLInputElement && ["text", "search"].includes(focused.type)
    ? [focused.selectionStart, focused.selectionEnd] : null;
  const heights = new Map([...body.querySelectorAll<HTMLElement>("[data-special]")].map((el) => [el.dataset.special!, el.offsetHeight]));
  renderedView = view;
  body.style.overflowAnchor = "none";
  const shown: { g: Group; fields: Field[]; crumb?: string }[] = searching
    ? hits.map((h) => ({ g: h.g, fields: h.fields, crumb: h.t.title }))
    : SCHEMA.find((x) => x.id === tab)!.groups.map((g) => ({ g, fields: g.fields || [] }));
  body.innerHTML = searching && !shown.length
    ? `<div class="set-nohit">${icon("search")}<b>No settings match “${esc(searchQuery().trim())}”</b><span>Try another word, or press Esc to see every section.</span></div>`
    : shown.map(({ g, fields, crumb }, i) => `<div class="sgroup" data-group="${esc(`${crumb || tab}:${g.title}`)}"><h2>${crumb ? `<span class="crumb">${esc(crumb)} ›</span> ` : ""}${esc(g.title)}</h2>${g.intro && (!searching || g.special) ? `<p>${esc(g.intro)}</p>` : ""}
    <div class="fields" id="grp_${i}"${g.special ? ` data-special="${esc(g.special)}"` : ""}>${fields.map(fieldHtml).join("")}</div></div>`).join("");
  const pending: Promise<unknown>[] = [];
  shown.forEach(({ g, fields }, i) => {
    fields.forEach(bindField);
    if (g.special) {
      const host = $(`grp_${i}`);
      if (sameView) host.style.minHeight = `${heights.get(g.special) || 0}px`;
      pending.push(Promise.resolve(panels[g.special]?.(g, host)).finally(() => {
        host.style.minHeight = "";
        if (revision === renderRevision) restoreView();
      }));
    }
  });
  pending.push(Promise.resolve(bindModelReasoning(body)).then(restoreView));
  body.querySelector<HTMLSelectElement>('[data-key="companion.voice.language"] select')?.addEventListener("change", () => {
    renderSettings(); // keep unsaved edits, but update the compatible Whisper choices
  });
  if (searching) highlightSearch(body);
  body.querySelectorAll<HTMLButtonElement>("[data-reset]").forEach((b) => (b.onclick = async () => {
    const key = b.dataset.reset!;
    if (key === "companion.voice.language" || key.endsWith("_reasoning_effort")
        || key === LLM + "fast_model" || key === LLM + "deep_model") {
      if (getPath(live.config, key) === undefined) edits.delete(key);
      else edits.set(key, null);
      if (key === LLM + "fast_model" || key === LLM + "deep_model") {
        const effort = key.replace(/_model$/, "_reasoning_effort");
        if (getPath(live.config, effort) === undefined) edits.delete(effort);
        else edits.set(effort, null);
      }
      void renderSettings();
      return;
    }
    edits.delete(key);
    await call("config:set", patchFor(key, null));
    toast(`${key.split(".").pop()} reset to its default`);
    renderSettings();
  }));
  updateDirty();
  restoreView();
  const restoreFocus = () => {
    if (!sameView || !focusSelector || document.activeElement !== document.body) return;
    const next = body.querySelector<HTMLInputElement>(focusSelector);
    next?.focus({ preventScroll: true });
    if (selection && next) next.setSelectionRange(selection[0], selection[1]);
  };
  restoreFocus();
  await Promise.allSettled(pending);
  if (revision === renderRevision) {
    restoreView();
    restoreFocus();
    viewAbort?.abort();
    body.style.overflowAnchor = "";
  }
}

/** The section tabs; while searching, each shows how many matches it has (and dims without any). */
function renderTabs(searching: boolean, perTab: Map<string, number>): void {
  $("set-tabs").classList.toggle("searching", searching);
  $("set-tabs").innerHTML = SCHEMA.map((t) => `<button data-tab="${t.id}" class="${!searching && t.id === tab ? "on" : ""}${searching && !perTab.get(t.id) ? " nohit" : ""}">${icon(TAB_ICON[t.id] || "settings")}${esc(t.title)}${searching && perTab.get(t.id) ? `<span class="hits">${perTab.get(t.id)}</span>` : ""}</button>`).join("");
  $("set-tabs").querySelectorAll<HTMLButtonElement>("button").forEach((b) => (b.onclick = () => {
    tab = b.dataset.tab!;
    setQuery("");                       // a section click leaves the search for that section
  }));
}

/** Save the edits: config.json and the app settings each get one patch; the coach takes the live-tunable ones at
 * once, or restarts. */
async function saveSettings(restart: boolean): Promise<void> {
  let cfgPatch: Obj = {}, appPatch: Obj = {};
  const submitted = new Map(edits);
  const tunable: Obj = {};
  const byKey = new Map(allFields().map((f) => [f.key, f]));
  for (const [key, shown] of submitted) {
    const f = byKey.get(key);
    const v = f ? toStore(f, shown) : shown;
    if (isApp(key)) appPatch = merge(appPatch, patchFor(key.slice(4), v));
    else {
      cfgPatch = merge(cfgPatch, patchFor(key, v));
      const leaf = key.slice(LLM.length);
      if (key.startsWith(LLM) && LIVE.has(leaf)) tunable[leaf] = v === null ? f!.def : v;
    }
  }
  if (getPath(cfgPatch, "companion.voice.language") !== undefined && live.appSettings.speech?.provider === "edge") {
    const language = languageCode(getPath(cfgPatch, "companion.voice.language"));
    const voice = getPath(appPatch, "speech.edge.voice") ?? live.appSettings.speech.edge?.voice;
    if (typeof voice !== "string" || !voice.startsWith(`${language}-`)) {
      const catalogue = await call("tts:voices", "edge");
      if (!catalogue) return;
      const next = catalogue.find((v) => v.id.startsWith(`${language}-`));
      if (!next) { toast("No Edge-TTS voices for the selected speech language."); return; }
      appPatch = merge(appPatch, { speech: { edge: { voice: next.id } } });
    }
  }
  // The running engine validates, persists and applies live fields in one request.
  // Never discard drafts or claim success after an HTTP error or failed local write.
  if (Object.keys(cfgPatch).length) {
    if (!restart && live.engineUrl && live.engineState === "running" && Object.keys(tunable).length) {
      try {
        const response = await enginePost("/settings", { save: cfgPatch, ...tunable }, JSON_HEADERS);
        const result = await response.json();
        if (!response.ok || !result.ok || !result.applied?.saved) throw new Error(result.error || "Settings were not applied");
        live.config = (await call("config:get")) || live.config;
      } catch (e) {
        toast(`Couldn't save and apply settings: ${(e as Error).message}. Your edits are kept.`, true);
        return;
      }
    } else {
      const saved = await call("config:set", cfgPatch);
      if (!saved) return;
      live.config = saved;
    }
  }
  if (Object.keys(appPatch).length) {
    const saved = await call("app:settings:set", appPatch);
    if (!saved) return;
    applyAppSettings(saved);
  }
  for (const [key, value] of submitted) if (edits.get(key) === value) edits.delete(key);
  if (restart) {
    await call("engine:restart");
    toast("Saved. Restarting the coach…");
  } else {
    const needs = [...submitted.keys()].some((key) => byKey.get(key)?.restart);
    toast(needs ? "Saved. Changes marked ↻ apply when the coach restarts." :
      live.engineState === "running" ? "Saved. Applies to the next call." : "Saved. Applies when the coach starts.");
  }
  renderSettings();
}

// the search box: filters as you type; Esc clears it; Ctrl+F jumps to it on the Settings page
function setQuery(q: string): void {
  setSearchQuery(q);
  const box = $<HTMLInputElement>("set-search");
  if (box.value !== q) box.value = q;
  renderSettings();
}

export function wireSettings(): void {
  whenShown("settings", () => { void renderSettings(); });
  $("set-save").onclick = () => saveSettings(false);
  $("set-save-restart").onclick = () => saveSettings(true);
  $("set-revert").onclick = () => { edits.clear(); renderSettings(); };
  document.addEventListener("model-settings-saved", () => {
    if (currentPage() === "settings") renderSettings();
  });
  // onEngine owns live connection state; this listener runs after that writer.
  let connection = `${live.engineState}|${live.engineUrl}|${live.engineToken}`;
  document.addEventListener("engine-connection-changed", () => {
    const next = `${live.engineState}|${live.engineUrl}|${live.engineToken}`;
    if (next === connection) return;
    connection = next;
    if (currentPage() === "settings") void renderSettings();
  });
  let searchTimer: number | undefined;
  $("set-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = (e.target as HTMLInputElement).value;
    searchTimer = window.setTimeout(() => { setSearchQuery(q); renderSettings(); }, 120);
  });
  $("set-search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); setQuery(""); }
    if (e.key === "Enter") { const first = $("set-body").querySelector<HTMLElement>("input, select, textarea, button"); first?.focus(); }
  });
  document.addEventListener("keydown", (e) => {
    if (currentPage() !== "settings") return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); $<HTMLInputElement>("set-search").focus(); $<HTMLInputElement>("set-search").select(); }
    else if (e.key === "Escape" && searchQuery() && document.activeElement !== $("set-search")) setQuery("");
  });
}
