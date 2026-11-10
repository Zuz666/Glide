// Settings > Choose models applies catalogue picks live; manual ids and effort share Save/Revert.
import { call } from "../bridge.js";
import { esc, icon, toast } from "../dom.js";
import { MANIFEST } from "../settings-manifest.js";
import { Group } from "../settings-schema.js";
import { engineGet, enginePost, live } from "../state.js";
import { Obj } from "../types.js";
import { edits, getPath } from "./fields.js";
import { AUTO_THINKING_HELP } from "./thinking-help.js";

const MORDER: Record<string, number> = { anthropic: 0, gemini: 1, endpoint: 2, openrouter: 3 };
const MGROUP: Record<string, string> = { anthropic: "Anthropic (direct)", gemini: "Google Gemini (direct)", endpoint: "Custom endpoints", openrouter: "OpenRouter" };
const ROLES: [string, string, string, string][] = [
  ["fast", "fast_model", "Fast role", "Callouts, quest briefings and translation. Spoken questions use the role selected below."],
  ["deep", "deep_model", "Deep role", "Typed questions and pace reviews. Spoken questions use the role selected below."]];
const EFFORT_HELP = "Choices use advertised capabilities where available, or known model/provider profiles otherwise. Auto is safest when support is unverified.";
const per = (v: number | null | undefined) => (v == null ? "?" : v < 0.01 && v > 0 ? v.toFixed(4) : (+v).toFixed(v < 1 ? 3 : 2));
const pickerRenders = new WeakMap<HTMLElement, AbortController>();
const reasoningBindings = new WeakMap<HTMLElement, AbortController>();
// Only filter text survives page refreshes; catalogue data and requests belong to one connected host.
let filterSession: { url: string | null; token: string; filters: Record<string, string> } | null = null;

const fullKey = (key: string) => `companion.llm.${key}`;
const draft = (key: string): string => String((edits.has(fullKey(key)) ? edits.get(fullKey(key)) : getPath(live.config, fullKey(key))) ?? MANIFEST[`llm.${key}`].default);


/** Live picks supersede only the same role's drafts; unrelated pending edits remain. */
function modelSettingsSaved(applied: Obj): void {
  for (const path of ["fast", "deep"]) {
    if (`${path}_model` in applied || `${path}_reasoning_effort` in applied) {
      edits.delete(fullKey(`${path}_model`));
      edits.delete(fullKey(`${path}_reasoning_effort`));
    }
  }
  document.dispatchEvent(new Event("model-settings-saved"));
}

/** Dollars an hour from catalogue prices and the role's token profile (null: price unknown). */
function mcost(models: Obj, path: string, model: Obj): number | null {
  const p = model.price, pr = (models.profile || {})[path];
  if (!p || p.in == null || !pr) return null;
  const rd = p.cache_read != null ? p.cache_read : p.in;
  return (pr.cache_read * rd + pr.uncached * p.in + pr.output * (p.out || 0)) / 1e6;
}

function mlabel(m: Obj): string {
  const p = m.price;
  return `${m.name}${m.provider !== "anthropic" ? `  [${m.id}]` : ""}, ${p ? `$${per(p.in)} in / $${per(p.out)} out per million` : "price unknown"}${m.tools === false ? ", no tools" : ""}${m.context ? `, ${Math.round(m.context / 1000)}k context` : ""}`;
}

function effortOptions(levels: string[], effort: string): string {
  return `<option value="auto" ${effort === "auto" ? "selected" : ""}>Auto</option>` +
    (effort !== "auto" && !levels.includes(effort) ? `<option value="${esc(effort)}" selected disabled>${esc(effort)} (not in available choices)</option>` : "") +
    levels.map((level) => `<option value="${esc(level)}" ${level === effort ? "selected" : ""}>${esc(level === "none" ? "None (no reasoning)" : level)}</option>`).join("");
}

function reasoningHelp(info?: Obj): string {
  if (!info) return `Checking reasoning choices… ${EFFORT_HELP}`;
  if (info.unavailable) return `Reasoning lookup is unavailable. Refresh the model list to retry, or use Auto. ${EFFORT_HELP}`;
  return `${EFFORT_HELP}${!info.levels.length ? " No explicit levels are available for this model." : ""}`;
}

/** Manual ids and effort share Save/Revert, including unsaved ids not in the catalogue. */
export async function bindModelReasoning(host: HTMLElement): Promise<void> {
  reasoningBindings.get(host)?.abort();
  const controller = new AbortController(), { signal } = controller;
  reasoningBindings.set(host, controller);
  const initialLoads: Promise<void>[] = [];
  for (const path of ["fast", "deep"]) {
    const modelKey = `${path}_model`, effortKey = `${path}_reasoning_effort`;
    const row = host.querySelector<HTMLElement>(`[data-key="${fullKey(effortKey)}"]`);
    const select = row?.querySelector<HTMLSelectElement>("select");
    if (!row || !select) continue;
    const note = row.querySelector<HTMLElement>(".lab small");
    const input = host.querySelector<HTMLInputElement>(`[data-key="${fullKey(modelKey)}"] input`);
    let revision = 0, timer: number | undefined;
    const load = async () => {
      const version = ++revision, id = draft(modelKey), url = live.engineUrl, token = live.engineToken, state = live.engineState;
      const current = () => !signal.aborted && row.isConnected && version === revision && id === draft(modelKey) && url === live.engineUrl && token === live.engineToken && state === live.engineState;
      const show = (info?: Obj) => {
        const levels: string[] = info?.levels || [], effort = draft(effortKey);
        select.innerHTML = effortOptions(levels, effort);
        select.disabled = !levels.length && effort === "auto";
        if (note) {
          note.innerHTML = `${esc(reasoningHelp(info))} ${AUTO_THINKING_HELP} Save to apply.`;
          note.title = info?.note || "";
        }
      };
      show();
      if (!url || live.engineState !== "running") {
        if (note) note.innerHTML = `Reasoning lookup is unavailable while the coach is not connected. You can still edit model ids. ${EFFORT_HELP} ${AUTO_THINKING_HELP} Save to apply.`;
        return;
      }
      try {
        const response = await engineGet(`/models?reasoning_model=${encodeURIComponent(id)}`);
        const data = await response.json();
        if (!current() || live.engineState !== "running") return;
        if (!response.ok || data.model !== id || !Array.isArray(data.reasoning?.levels)) throw new Error("Capabilities unavailable");
        show(data.reasoning);
      } catch {
        if (current()) show({ levels: [], unavailable: true });
      }
    };
    select.addEventListener("model-capabilities-refresh", () => { void load(); }, { signal });
    input?.addEventListener("input", () => {
      ++revision;
      clearTimeout(timer);
      // Never carry one model's explicit effort over to another model.
      select.innerHTML = effortOptions([], "auto");
      select.value = "auto";
      // Reuse bindField's change handler, including its default and dirty-state semantics.
      select.dispatchEvent(new Event("change", { bubbles: true }));
      select.disabled = true;
      if (note) note.innerHTML = `Reasoning resets to Auto when the model changes. ${EFFORT_HELP} ${AUTO_THINKING_HELP} Save to apply.`;
      timer = window.setTimeout(() => { if (!signal.aborted && row.isConnected) void load(); }, 250);
    }, { signal });
    signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
    initialLoads.push(load());
  }
  await Promise.all(initialLoads);
}

/** One role's catalogue, grouped by provider and cheapest first, including every selected id. */
function roleHtml(models: Obj, [path, key, label, help]: [string, string, string, string], filter: string, info?: Obj): string {
  const all: Obj[] = models.models || [], cur = models.selected?.[key], q = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const costs = new Map<string, number | null>(all.map((model) => [model.id, mcost(models, path, model)]));
  const list = all.filter((m) => m.id === cur || q.every((w) => `${m.id} ${m.name}`.toLowerCase().includes(w)))
    .sort((a, b) => (MORDER[a.provider] ?? 9) - (MORDER[b.provider] ?? 9) || (costs.get(a.id) ?? 1e9) - (costs.get(b.id) ?? 1e9));
  const groups: Record<string, Obj[]> = { anthropic: [], gemini: [], endpoint: [], openrouter: [] };
  list.forEach((m) => (groups[m.provider] || groups.openrouter).push(m));
  const opts = (cur && !all.some((m) => m.id === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)} (not in the list)</option>` : "") +
    Object.entries(groups).filter(([, v]) => v.length).map(([g, v]) => `<optgroup label="${MGROUP[g]}">${v.map((m) => {
      const c = costs.get(m.id);
      return `<option value="${esc(m.id)}" ${m.id === cur ? "selected" : ""}>${esc(mlabel(m))}${c != null ? `, about $${per(c)} an hour` : ""}</option>`;
    }).join("")}</optgroup>`).join("");
  const c = costs.get(cur), m = all.find((x) => x.id === cur);
  const effortKey = `${path}_reasoning_effort`, effort = models.selected?.[effortKey] || "auto", levels: string[] = info?.levels || [];
  return `<div class="field"><div class="lab">${esc(label)}<small>${esc(help)}</small></div><div class="ctl mpick" style="display:grid;width:100%">
        <input type="text" data-filter="${path}" value="${esc(filter)}" placeholder="Filter: claude, gemini, flash, ollama…" aria-label="Filter models">
        <select data-pick="${key}" aria-label="${esc(label)}">${opts}</select>
        <div class="mcost">${m && m.tools === false ? "<em>No tool use</em>: answers get the knowledge block but no lookups. " : ""}${c != null ? `About <em>$${per(c)} an hour</em> at ${(models.profile?.[path] || {}).measured ? "this session's" : "typical"} usage.` : "Price unknown."}</div>
        <label class="help" for="mp-effort-${path}">Reasoning effort</label>
        <select id="mp-effort-${path}" data-effort="${effortKey}" aria-label="${esc(label)} reasoning effort" ${!levels.length && effort === "auto" ? "disabled" : ""}>${effortOptions(levels, effort)}</select>
        <div class="mcost" title="${esc(info?.note || "")}">${esc(reasoningHelp(info))} ${AUTO_THINKING_HELP} Changes save immediately.</div></div></div>`;
}

function listHtml(models: Obj): string {
  const st = models.status || {};
  return `<div class="field"><div class="lab">The list<small>${esc(`${st.count || 0} models${st.updated ? `, listed ${new Date(st.updated * 1000).toLocaleString()}` : ""}.`)}${Object.keys(st.errors || {}).length ? ` ${esc(Object.entries(st.errors).map(([k, v]) => `${k}: ${v}`).join("; "))}` : ""}</small></div>
      <div class="ctl"><button class="btn" id="mp-refresh">${icon("refresh-cw")}Refresh the list</button><span class="help">Spent this session: $${per(models.spent_usd || 0)}</span></div></div>`;
}

export async function renderModelPicker(_g: Group, host: HTMLElement): Promise<void> {
  pickerRenders.get(host)?.abort();
  const controller = new AbortController(), { signal } = controller;
  pickerRenders.set(host, controller);
  const url = live.engineUrl, token = live.engineToken;
  const current = () => !signal.aborted && host.isConnected && url === live.engineUrl && token === live.engineToken;
  if (!filterSession || filterSession.url !== url || filterSession.token !== token) filterSession = { url, token, filters: { fast: "", deep: "" } };
  const { filters } = filterSession;
  if (live.engineState !== "running") {
    const canStart = live.engineState === "stopped" || live.engineState === "crashed";
    const message = canStart ? "Start the coach to load the model list and prices. You can also type model ids below."
      : live.engineState === "stopping" ? "The coach is stopping. You can still edit model ids below."
      : "The coach is starting. The model list will load when it connects; you can still edit model ids below.";
    host.innerHTML = `<div class="field"><div class="lab">Model list<small>${message}</small></div>${canStart ? `<div class="ctl"><button class="btn" id="mp-start">${icon("play")}Start the coach</button></div>` : ""}</div>`;
    host.querySelector<HTMLButtonElement>("#mp-start")?.addEventListener("click", () => {
      if (current() && (live.engineState === "stopped" || live.engineState === "crashed")) void call("engine:start");
    }, { signal });
    return;
  }
  let models: Obj | null = null, request = 0, saving = false;
  const connected = () => current() && live.engineState === "running";
  const draw = () => {
    if (!connected() || !models) return;
    const active = document.activeElement instanceof HTMLElement && host.contains(document.activeElement) ? document.activeElement : null;
    const attr = ["data-filter", "data-pick", "data-effort"].find((name) => active?.hasAttribute(name));
    const focusValue = attr ? active!.getAttribute(attr) : null;
    const start = active instanceof HTMLInputElement ? active.selectionStart : null, end = active instanceof HTMLInputElement ? active.selectionEnd : null;
    const scroll: [HTMLElement, number, number][] = [];
    for (let el = host.parentElement; el; el = el.parentElement) scroll.push([el, el.scrollTop, el.scrollLeft]);
    host.innerHTML = ROLES.map((role) => roleHtml(models!, role, filters[role[0]], models!.reasoning?.[role[0]])).join("") + listHtml(models);
    host.style.minHeight = "";
    host.querySelectorAll<HTMLInputElement>("[data-filter]").forEach((input) => (input.oninput = () => {
      if (!connected()) return;
      filters[input.dataset.filter!] = input.value;
      draw();
    }));
    host.querySelectorAll<HTMLSelectElement>("[data-pick], [data-effort]").forEach((select) => (select.onchange = async () => {
      if (!connected() || saving) return;
      const key = (select.dataset.pick || select.dataset.effort)!, value = select.value, isModel = !!select.dataset.pick;
      const hadFocus = document.activeElement === select;
      saving = true;
      host.querySelectorAll<HTMLSelectElement>("[data-pick], [data-effort]").forEach((control) => { control.disabled = true; });
      try {
        const response = await enginePost("/settings", { [key]: value });
        const result = await response.json();
        if (!connected()) return;
        if (!response.ok || !result.ok || (!isModel && result.applied?.[key] !== value)) throw new Error(result.error || "Not applied");
        saving = false;
        await load();
        if (!connected()) return;
        if (hadFocus && document.activeElement === document.body) {
          host.querySelector<HTMLSelectElement>(`[${isModel ? "data-pick" : "data-effort"}="${key}"]`)?.focus({ preventScroll: true });
        }
        toast(isModel ? `${key === "fast_model" ? "Fast" : "Deep"} role now uses ${value}. Saved.` : `Reasoning effort saved: ${value}. Applies to the next call.`);
        if (isModel && Object.entries(result.applied || {}).some(([k, v]) => k.endsWith("_reasoning_effort") && v === "auto")) toast("Reasoning reset to Auto for the new model.");
        modelSettingsSaved(result.applied || { [key]: value });
      } catch (error) {
        if (!connected()) return;
        saving = false;
        toast(`Couldn't change ${isModel ? "the model" : "reasoning effort"}: ${(error as Error).message}`, true);
        draw();
      }
    }));
    if (saving) host.querySelectorAll<HTMLSelectElement>("[data-pick], [data-effort]").forEach((control) => { control.disabled = true; });
    host.querySelector<HTMLButtonElement>("#mp-refresh")!.onclick = () => { void load(true); };
    if (attr && focusValue !== null) {
      const again = host.querySelector<HTMLElement>(`[${attr}="${focusValue}"]`);
      again?.focus({ preventScroll: true });
      if (again instanceof HTMLInputElement && start !== null) again.setSelectionRange(start, end);
    }
    scroll.forEach(([el, top, left]) => { el.scrollTop = top; el.scrollLeft = left; });
  };
  const load = async (refresh = false) => {
    const version = ++request;
    if (!connected()) return;
    models = null;
    host.style.minHeight = `${host.getBoundingClientRect().height}px`;
    host.innerHTML = `<div class="field"><div class="lab">Model list<small>${refresh ? "Refreshing" : "Loading"} models… You can still edit model ids below.</small></div></div>`;
    try {
      if (!url) throw new Error("Coach connection unavailable");
      const response = await engineGet(`/models${refresh ? "?refresh=1" : ""}`);
      const data = await response.json();
      if (!connected() || version !== request) return;
      if (!response.ok || data.error || !Array.isArray(data.models)) throw new Error(data.error || "Model list unavailable");
      models = data;
      if (refresh) document.querySelectorAll<HTMLSelectElement>('[data-key$="_reasoning_effort"] select').forEach((select) => select.dispatchEvent(new Event("model-capabilities-refresh")));
      draw();
    } catch {
      if (!connected() || version !== request) return;
      host.style.minHeight = "";
      host.innerHTML = `<div class="field"><div class="lab">Model list<small>The coach is running, but its model list is unavailable. You can still edit model ids below.</small></div><div class="ctl"><button class="btn" id="mp-retry">${icon("refresh-cw")}Retry model list</button></div></div>`;
      host.querySelector<HTMLButtonElement>("#mp-retry")!.onclick = () => { if (connected()) void load(true); };
    }
  };
  await load();
}
