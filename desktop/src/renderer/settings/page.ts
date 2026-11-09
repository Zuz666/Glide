// The Settings page: a tab per section of settings-schema.ts (or, while searching, every match), its fields, the
// custom panels some groups ask for (keys, models, voices, ...), and Save / Save and restart / Revert.
import { applyAppSettings } from "../app-settings.js";
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { currentPage, show } from "../nav.js";
import { MANIFEST } from "../settings-manifest.js";
import { Field, Group, SCHEMA } from "../settings-schema.js";
import { languageCode } from "../speech-language.js";
import { enginePost, JSON_HEADERS, live } from "../state.js";
import { Obj } from "../types.js";
import { bindField, edits, fieldHtml, getPath, isApp, merge, patchFor, toStore, updateDirty } from "./fields.js";
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
const allFields = () => SCHEMA.flatMap((t) => t.groups.flatMap((g) => g.fields || []));

/** The custom panels, by the name settings-schema.ts gives them. */
export function registerPanels(p: Record<string, Panel>): void {
  panels = p;
}

/** Go to the Settings page on one section. */
export function openSettingsTab(id: string): void {
  tab = id;
  show("settings");
  renderSettings();
}

export async function renderSettings(): Promise<void> {
  live.config = (await call("config:get")) || {};
  applyAppSettings((await call("app:settings:get")) || {});
  const searching = queryWords().length > 0;
  const hits = searching ? searchSchema() : [];
  const perTab = new Map<string, number>();
  for (const h of hits) perTab.set(h.t.id, (perTab.get(h.t.id) || 0) + (h.fields.length || 1));
  renderTabs(searching, perTab);
  const body = $("set-body");
  const shown: { g: Group; fields: Field[]; crumb?: string }[] = searching
    ? hits.map((h) => ({ g: h.g, fields: h.fields, crumb: h.t.title }))
    : SCHEMA.find((x) => x.id === tab)!.groups.map((g) => ({ g, fields: g.fields || [] }));
  body.innerHTML = searching && !shown.length
    ? `<div class="set-nohit">${icon("search")}<b>No settings match “${esc(searchQuery().trim())}”</b><span>Try another word, or press Esc to see every section.</span></div>`
    : shown.map(({ g, fields, crumb }, i) => `<div class="sgroup"><h2>${crumb ? `<span class="crumb">${esc(crumb)} ›</span> ` : ""}${esc(g.title)}</h2>${g.intro && (!searching || g.special) ? `<p>${esc(g.intro)}</p>` : ""}
    <div class="fields" id="grp_${i}">${fields.map(fieldHtml).join("")}</div></div>`).join("");
  shown.forEach(({ g, fields }, i) => {
    fields.forEach(bindField);
    if (g.special) panels[g.special]?.(g, $(`grp_${i}`));
  });
  body.querySelector<HTMLSelectElement>('[data-key="companion.voice.language"] select')?.addEventListener("change", () => {
    renderSettings(); // keep unsaved edits, but update the compatible Whisper choices
  });
  if (searching) highlightSearch(body);
  body.querySelectorAll<HTMLButtonElement>("[data-reset]").forEach((b) => (b.onclick = async () => {
    const key = b.dataset.reset!;
    if (key === "companion.voice.language") {
      const f = allFields().find((f) => f.key === key)!;
      if (getPath(live.config, key) === f.def) edits.delete(key);
      else edits.set(key, f.def);
      renderSettings();
      return;
    }
    edits.delete(key);
    await call("config:set", patchFor(key, null));
    toast(`${key.split(".").pop()} reset to its default`);
    renderSettings();
  }));
  updateDirty();
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
  const tunable: Obj = {};
  const byKey = new Map(allFields().map((f) => [f.key, f]));
  for (const [key, shown] of edits) {
    const f = byKey.get(key);
    const v = f ? toStore(f, shown) : shown;
    if (isApp(key)) appPatch = merge(appPatch, patchFor(key.slice(4), v));
    else {
      cfgPatch = merge(cfgPatch, patchFor(key, v));
      const leaf = key.slice(LLM.length);
      if (key.startsWith(LLM) && LIVE.has(leaf)) tunable[leaf] = v;
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
  // not saved (config.json unreadable): the error toast says why, and the edits stay for another try
  if (Object.keys(cfgPatch).length) {
    const saved = await call("config:set", cfgPatch);
    if (!saved) return;
    live.config = saved; // includes the backend's language-compatible Whisper model
  }
  if (Object.keys(appPatch).length) applyAppSettings((await call("app:settings:set", appPatch)) || live.appSettings);
  edits.clear();
  if (restart) {
    await call("engine:restart");
    toast("Saved. Restarting the coach…");
  } else {
    if (live.engineUrl && Object.keys(tunable).length) {
      try {
        await enginePost("/settings", tunable, JSON_HEADERS);
      } catch { /* the coach picks them up at its next start */ }
    }
    const needs = allFields().some((f) => f.restart && getPath(cfgPatch, f.key) !== undefined);
    toast(needs ? "Saved. Some changes apply when the coach restarts (↻)." : "Saved.");
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
  $("set-save").onclick = () => saveSettings(false);
  $("set-save-restart").onclick = () => saveSettings(true);
  $("set-revert").onclick = () => { edits.clear(); renderSettings(); };
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
