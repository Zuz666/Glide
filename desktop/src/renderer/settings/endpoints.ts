// Settings > AI models > Custom endpoints: OpenAI-compatible servers (config.json companion.llm.endpoints),
// local ones like Ollama or online ones, each with a test that lists its models.
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { Group } from "../settings-schema.js";
import { enginePost, JSON_HEADERS, live } from "../state.js";
import { getPath, merge } from "./fields.js";
import { openSettingsTab, renderSettings } from "./page.js";

interface Endpoint { id: string; label?: string; base_url: string; key_env?: string }
const unsavedEndpoints = new WeakSet<HTMLElement>();
/** The key's variable: its own key_env, else one named after the id (the main process names it the same way). */
const keyEnvFor = (e: Endpoint) => e.key_env || `GLIDE_ENDPOINT_${e.id.toUpperCase().replace(/-/g, "_")}_KEY`;
const PRESETS: [string, Endpoint][] = [
  ["Ollama (local)", { id: "ollama", label: "Ollama", base_url: "http://127.0.0.1:11434/v1" }],
  ["LM Studio (local)", { id: "lmstudio", label: "LM Studio", base_url: "http://127.0.0.1:1234/v1" }],
  ["OpenAI", { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1" }],
  ["Groq", { id: "groq", label: "Groq", base_url: "https://api.groq.com/openai/v1" }],
  ["Mistral", { id: "mistral", label: "Mistral", base_url: "https://api.mistral.ai/v1" }],
  ["DeepSeek", { id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com/v1" }],
];

function endpointHtml(e: Endpoint, i: number): string {
  return `<div class="ep" data-i="${i}">
      <span class="lbl">Id</span><input data-k="id" value="${esc(e.id)}" spellcheck="false" placeholder="ollama" aria-label="Endpoint id">
      <span class="lbl">Name</span><input data-k="label" value="${esc(e.label || "")}" placeholder="Ollama (local)" aria-label="Endpoint name">
      <span class="lbl">Base URL</span><input data-k="base_url" value="${esc(e.base_url)}" spellcheck="false" placeholder="http://127.0.0.1:11434/v1" aria-label="Base URL">
      <span class="lbl">Key</span><span class="help">Set <code>${esc(keyEnvFor(e))}</code> in <button type="button" class="reset" data-endpoint-keys>API keys</button>. Local servers don't need one.</span>
      <div class="actions"><button class="btn" data-act="test">${icon("search")}Test and list models</button><button class="btn ghost danger" data-act="del">Remove</button>
        <span data-out></span></div>
      <div class="models" data-models></div></div>`;
}

export function renderEndpoints(g: Group, host: HTMLElement): void {
  const list: Endpoint[] = (getPath(live.config, "companion.llm.endpoints") as Endpoint[]) || [];
  host.innerHTML = list.map(endpointHtml).join("") +
    `<div class="addrow"><select id="ep-preset" aria-label="Add an endpoint"><option value="">Add an endpoint…</option>${PRESETS.map(([n], i) => `<option value="${i}">${esc(n)}</option>`).join("")}<option value="custom">Other (custom)</option></select>
     <button class="btn" id="ep-save">Save endpoints</button></div>
     <p class="help" id="ep-key-note" ${unsavedEndpoints.has(host) ? "" : "hidden"}>Save endpoints before opening API keys, so your changes are kept and new endpoint keys appear there.</p>`;
  host.querySelectorAll<HTMLButtonElement>("[data-endpoint-keys]").forEach((button) => {
    button.disabled = unsavedEndpoints.has(host);
    button.title = button.disabled ? "Save endpoints before opening API keys" : "Open service API keys";
    button.onclick = () => openSettingsTab("keys");
  });
  const markUnsaved = () => {
    unsavedEndpoints.add(host);
    host.querySelector<HTMLElement>("#ep-key-note")!.hidden = false;
    host.querySelectorAll<HTMLButtonElement>("[data-endpoint-keys]").forEach((button) => {
      button.disabled = true;
      button.title = "Save endpoints before opening API keys";
    });
  };
  host.querySelectorAll<HTMLInputElement>(".ep input").forEach((input) => input.addEventListener("input", markUnsaved));
  const collect = (): Endpoint[] => [...host.querySelectorAll<HTMLElement>(".ep")].map((el) => {
    const v = (k: string) => (el.querySelector<HTMLInputElement>(`[data-k="${k}"]`)!.value || "").trim();
    return { id: v("id").toLowerCase(), label: v("label"), base_url: v("base_url") };
  });
  const save = async (eps: Endpoint[]) => {
    const bad = eps.find((e) => !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(e.id) || !/^https?:\/\//.test(e.base_url));
    if (bad) return toast(`Endpoint "${bad.id || "?"}": the id must be lowercase letters, digits, - or _, and the URL must start with http(s)://`, true, 7000);
    if (new Set(eps.map((e) => e.id)).size !== eps.length) return toast("Two endpoints have the same id", true);
    live.config = (await call("config:set", { companion: { llm: { endpoints: eps } } })) || live.config;
    unsavedEndpoints.delete(host);
    toast("Endpoints saved. Restart the coach, then pick their models in Coach > Models (Refresh list).", false, 7000);
    renderEndpoints(g, host);
    renderSettings();
  };
  $<HTMLSelectElement>("ep-preset").onchange = (ev) => {
    const v = (ev.target as HTMLSelectElement).value;
    if (!v) return;
    const eps = collect();
    const e: Endpoint = v === "custom" ? { id: "custom", label: "", base_url: "https://" } : { ...PRESETS[Number(v)][1] };
    if (eps.some((x) => x.id === e.id)) e.id += `-${eps.length + 1}`;
    live.config = merge(live.config, { companion: { llm: { endpoints: [...eps, e] } } });
    unsavedEndpoints.add(host);
    renderEndpoints(g, host);
  };
  $("ep-save").onclick = () => save(collect());
  host.querySelectorAll<HTMLElement>(".ep").forEach((el) => {
    const i = Number(el.dataset.i);
    el.querySelector<HTMLButtonElement>('[data-act="del"]')!.onclick = () => save(collect().filter((_, j) => j !== i));
    el.querySelector<HTMLButtonElement>('[data-act="test"]')!.onclick = () => testEndpoint(el, collect()[i]);
  });
}

/** Test one endpoint: reachable or why not, and its models as buttons (click: fast model; Shift-click: deep). */
async function testEndpoint(el: HTMLElement, e: Endpoint): Promise<void> {
  const out = el.querySelector<HTMLElement>("[data-out]")!;
  out.textContent = "Testing…";
  const r = await call("endpoint:test", e.base_url, keyEnvFor(e));
  if (!r) return;
  out.innerHTML = r.ok ? `<span class="ok">Reachable, ${r.models!.length} models</span>` : `<span class="bad">${esc(r.error)}</span>`;
  const m = el.querySelector<HTMLElement>("[data-models]")!;
  m.innerHTML = r.ok ? "Click a model to make it the fast model; Shift-click for the deep model.<br>" +
    r.models!.map((id) => `<button type="button" data-id="ep:${esc(e.id)}:${esc(id)}">${esc(id)}</button>`).join("") : "";
  m.querySelectorAll<HTMLElement>("button").forEach((c) => (c.onclick = async (ev) => {
    const which = (ev as MouseEvent).shiftKey ? "deep_model" : "fast_model";
    await call("config:set", { companion: { llm: { [which]: c.dataset.id } } });
    if (live.engineUrl) enginePost("/settings", { [which]: c.dataset.id }, JSON_HEADERS).catch(() => undefined);
    toast(`${which === "fast_model" ? "Fast" : "Deep"} model: ${c.dataset.id}`);
  }));
}
