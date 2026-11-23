// Settings > AI models & keys > Choose models: the coach's model list (Anthropic, Gemini, custom endpoints,
// OpenRouter) from its /models route, with prices and what each would cost an hour at this player's usage. A pick
// applies to the coach at once (its /settings route), which saves it.
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { MANIFEST } from "../settings-manifest.js";
import { Group } from "../settings-schema.js";
import { engineGet, enginePost, live } from "../state.js";
import { Obj } from "../types.js";
import { edits, getPath, updateDirty } from "./fields.js";

const MORDER: Record<string, number> = { anthropic: 0, gemini: 1, endpoint: 2, openrouter: 3 };
const MGROUP: Record<string, string> = { anthropic: "Anthropic (direct)", gemini: "Google Gemini (direct)", endpoint: "Custom endpoints", openrouter: "OpenRouter" };
const ROLES: [string, string, string, string][] = [   // path, config key, label, help
  ["fast", "fast_model", "Fast role", "Callouts, quest briefings and translation. Spoken questions use the role selected below."],
  ["deep", "deep_model", "Deep role", "Typed questions and pace reviews. Spoken questions use the role selected below."]];
const per = (v: number | null | undefined) => (v == null ? "?" : v < 0.01 && v > 0 ? v.toFixed(4) : (+v).toFixed(v < 1 ? 3 : 2));
let MODELS: Obj | null = null;

/** Dollars an hour for a model in a role, from its prices and the role's token profile (null: price unknown). */
function mcost(path: string, id: string): number | null {
  const m = (MODELS?.models || []).find((x: Obj) => x.id === id), p = m && m.price, pr = (MODELS?.profile || {})[path];
  if (!p || p.in == null || !pr) return null;
  const rd = p.cache_read != null ? p.cache_read : p.in;
  return (pr.cache_read * rd + pr.uncached * p.in + pr.output * (p.out || 0)) / 1e6;
}

function mlabel(m: Obj): string {
  const p = m.price;
  return `${m.name}${m.provider !== "anthropic" ? `  [${m.id}]` : ""}, ${p ? `$${per(p.in)} in / $${per(p.out)} out per million` : "price unknown"}${m.tools === false ? ", no tools" : ""}${m.context ? `, ${Math.round(m.context / 1000)}k context` : ""}`;
}

async function loadModels(refresh = false): Promise<void> {
  try { MODELS = await (await engineGet(`/models${refresh ? "?refresh=1" : ""}`)).json(); } catch { MODELS = null; }
}

function effortOptions(levels: string[], effort: string): string {
  return `<option value="auto" ${effort === "auto" ? "selected" : ""}>Auto</option>` +
    (effort !== "auto" && !levels.includes(effort) ? `<option value="${esc(effort)}" selected disabled>${esc(effort)} (not supported)</option>` : "") +
    levels.map((level) => `<option value="${esc(level)}" ${level === effort ? "selected" : ""}>${esc(level === "none" ? "None (no reasoning)" : level)}</option>`).join("");
}

/** Live picks supersede drafts for the same settings; unrelated pending edits remain. */
function modelSettingsSaved(applied: Obj): void {
  for (const path of ["fast", "deep"]) {
    if (`${path}_model` in applied || `${path}_reasoning_effort` in applied) {
      edits.delete(`companion.llm.${path}_model`);
      edits.delete(`companion.llm.${path}_reasoning_effort`);
    }
  }
  document.dispatchEvent(new Event("model-settings-saved"));
}

/** Manual ids and effort share Save/Revert, including unsaved ids not in the catalogue. */
export function bindModelReasoning(host: HTMLElement): void {
  for (const path of ["fast", "deep"]) {
    const modelKey = `companion.llm.${path}_model`, effortKey = `companion.llm.${path}_reasoning_effort`;
    const row = host.querySelector<HTMLElement>(`[data-key="${effortKey}"]`);
    const select = row?.querySelector<HTMLSelectElement>("select");
    if (!row || !select) continue;
    const note = row.querySelector<HTMLElement>(".lab small")!;
    const input = host.querySelector<HTMLInputElement>(`[data-key="${modelKey}"] input`);
    const model = () => String(edits.get(modelKey) ?? getPath(live.config, modelKey) ?? MANIFEST[`llm.${path}_model`].default);
    const effort = () => String(edits.get(effortKey) ?? getPath(live.config, effortKey) ?? "auto");
    let revision = 0, timer: number | undefined;
    const load = async () => {
      const version = ++revision, id = model();
      row.style.display = "";
      select.disabled = true;
      select.innerHTML = effortOptions([], effort());
      note.textContent = live.engineUrl ? "Checking supported reasoning levels…" : "Start the coach to check supported reasoning levels.";
      if (!live.engineUrl) return;
      try {
        const data = await (await engineGet(`/models?reasoning_model=${encodeURIComponent(id)}`)).json();
        if (!row.isConnected || version !== revision || id !== model()) return;
        if (data.model !== id || !data.reasoning) throw new Error(data.error || "Capabilities unavailable");
        const levels: string[] = data.reasoning.levels;
        select.innerHTML = effortOptions(levels, effort());
        select.disabled = !levels.length && effort() === "auto";
        row.style.display = !levels.length && effort() === "auto" ? "none" : "";
        note.textContent = `${data.reasoning.note} Save to apply.`;
      } catch {
        if (row.isConnected && version === revision) note.textContent = "Couldn't check reasoning levels. Start the coach and reopen settings to try again.";
      }
    };
    input?.addEventListener("input", () => {
      ++revision;
      clearTimeout(timer);
      // A new model must not inherit an effort chosen for the previous one.
      edits.set(effortKey, "auto");
      row.classList.add("changed");
      row.style.display = "";
      select.innerHTML = effortOptions([], "auto");
      select.disabled = true;
      note.textContent = "Reasoning resets to Auto when the model changes.";
      updateDirty();
      timer = window.setTimeout(load, 250);
    });
    void load();
  }
}

/** One role's filter box, model list (grouped by provider, cheapest first) and its cost. */
function roleHtml([path, key, label, help]: [string, string, string, string], filter: string): string {
  const all: Obj[] = MODELS!.models || [], sel = MODELS!.selected || {};
  const cur = sel[key], q = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const list = all.filter((m) => m.id === cur || q.every((w) => `${m.id} ${m.name}`.toLowerCase().includes(w)))
    .sort((a, b) => (MORDER[a.provider] ?? 9) - (MORDER[b.provider] ?? 9) || (mcost(path, a.id) ?? 1e9) - (mcost(path, b.id) ?? 1e9));
  const groups: Record<string, Obj[]> = { anthropic: [], gemini: [], endpoint: [], openrouter: [] };
  list.slice(0, 500).forEach((m) => (groups[m.provider] || groups.openrouter).push(m));
  const opts = (cur && !all.find((m) => m.id === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)} (not in the list)</option>` : "") +
    Object.entries(groups).filter(([, v]) => v.length).map(([g, v]) => `<optgroup label="${MGROUP[g]}">${v.map((m) => {
      const c = mcost(path, m.id);
      return `<option value="${esc(m.id)}" ${m.id === cur ? "selected" : ""}>${esc(mlabel(m))}${c != null ? `, about $${per(c)} an hour` : ""}</option>`;
    }).join("")}</optgroup>`).join("");
  const c = mcost(path, cur), m = all.find((x) => x.id === cur);
  const effortKey = `${path}_reasoning_effort`, effort = sel[effortKey] || "auto", info = MODELS!.reasoning?.[path];
  const levels: string[] = info?.levels || [];
  const reasoning = info ? `<label class="help" for="mp-effort-${path}">Reasoning effort</label>
        <select id="mp-effort-${path}" data-effort="${effortKey}" aria-label="${esc(label)} reasoning effort" ${!levels.length && effort === "auto" ? "disabled" : ""}>
          ${effortOptions(levels, effort)}
        </select><div class="mcost">${esc(info.note)}${levels.length ? " Higher effort can take longer and use more output tokens." : ""}</div>` : "";
  return `<div class="field"><div class="lab">${esc(label)}<small>${esc(help)}</small></div><div class="ctl mpick" style="display:grid;width:100%">
        <input type="text" data-filter="${path}" value="${esc(filter)}" placeholder="Filter: claude, gemini, flash, ollama…" aria-label="Filter models">
        <select data-pick="${key}" aria-label="${esc(label)}">${opts}</select>
        <div class="mcost">${m && m.tools === false ? "<em>No tool use</em>: answers get the knowledge block but no lookups. " : ""}${c != null ? `About <em>$${per(c)} an hour</em> at ${(MODELS!.profile?.[path] || {}).measured ? "this session's" : "typical"} usage.` : "Price unknown."}</div>${reasoning}</div></div>`;
}

/** How many models, when listed, what failed, and Refresh. */
function listHtml(): string {
  const st = MODELS!.status || {};
  return `<div class="field"><div class="lab">The list<small>${esc(`${st.count || 0} models${st.updated ? `, listed ${new Date(st.updated * 1000).toLocaleString()}` : ""}.`)}${Object.keys(st.errors || {}).length ? ` ${esc(Object.entries(st.errors).map(([k, v]) => `${k}: ${v}`).join("; "))}` : ""}</small></div>
      <div class="ctl"><button class="btn" id="mp-refresh">${icon("refresh-cw")}Refresh the list</button><span class="help">Spent this session: $${per(MODELS!.spent_usd || 0)}</span></div></div>`;
}

export async function renderModelPicker(_g: Group, host: HTMLElement): Promise<void> {
  if (!live.engineUrl) {
    host.innerHTML = `<div class="field"><div class="lab">Model list<small>Start the coach to load the models your keys can use, with prices. You can also type model ids below.</small></div><div class="ctl"><button class="btn" id="mp-start">${icon("play")}Start the coach</button></div></div>`;
    $("mp-start").onclick = () => call("engine:start");
    return;
  }
  await loadModels();
  const filters: Record<string, string> = { fast: "", deep: "" };
  const draw = () => {
    if (!MODELS || MODELS.error) {
      host.innerHTML = `<div class="field"><div class="lab">Model list</div><div class="ctl bad">${esc(MODELS?.error || "Couldn't load the model list from the coach.")}</div></div>`;
      return;
    }
    host.innerHTML = ROLES.map((role) => roleHtml(role, filters[role[0]])).join("") + listHtml();
    // filtering redraws the lists: keep the caret in the box being typed in
    host.querySelectorAll<HTMLInputElement>("[data-filter]").forEach((inp) => (inp.oninput = () => {
      filters[inp.dataset.filter!] = inp.value;
      const pos = inp.selectionStart;
      draw();
      const again = host.querySelector<HTMLInputElement>(`[data-filter="${inp.dataset.filter}"]`)!;
      again.focus();
      again.setSelectionRange(pos, pos);
    }));
    host.querySelectorAll<HTMLSelectElement>("[data-pick]").forEach((s) => (s.onchange = async () => {
      const key = s.dataset.pick!, v = s.value;
      try {
        const r = await (await enginePost("/settings", { [key]: v })).json();
        if (!r.ok) throw new Error(r.error || "not applied");
        const reset = Object.entries(r.applied || {}).some(([k, value]) => k.endsWith("_reasoning_effort") && value === "auto");
        await loadModels();
        toast(`${key === "fast_model" ? "Fast" : "Deep"} role now uses ${v}. Saved.`);
        if (reset) toast("Reasoning reset to Auto for the new model.");
        draw();
        modelSettingsSaved(r.applied || { [key]: v });
      } catch (e) {
        toast(`Couldn't switch the model: ${(e as Error).message}`, true);
        s.value = MODELS?.selected?.[key] || "";
      }
    }));
    host.querySelectorAll<HTMLSelectElement>("[data-effort]").forEach((s) => (s.onchange = async () => {
      const key = s.dataset.effort!, value = s.value;
      s.disabled = true;
      try {
        const r = await (await enginePost("/settings", { [key]: value })).json();
        if (!r.ok || r.applied?.[key] !== value) throw new Error(r.error || "not applied");
        await loadModels();
        toast(`Reasoning effort saved: ${value}. Applies to the next call.`);
        draw();
        modelSettingsSaved(r.applied);
      } catch (e) {
        s.value = MODELS?.selected?.[key] || "auto";
        s.disabled = false;
        toast(`Couldn't change reasoning effort: ${(e as Error).message}`, true);
      }
    }));
    $("mp-refresh").onclick = async () => { $("mp-refresh").textContent = "Refreshing…"; await loadModels(true); draw(); };
  };
  draw();
}
