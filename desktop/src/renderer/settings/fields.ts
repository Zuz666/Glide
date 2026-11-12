// The Settings page's ordinary fields (settings-schema.ts): where each value is stored, what its control shows,
// and the unsaved edits. A key is a path in config.json, or in the app's own settings when it starts with "app:".
import { $, esc, icon } from "../dom.js";
import { Field, SCHEMA } from "../settings-schema.js";
import { live } from "../state.js";
import { languageCode } from "../speech-language.js";
import { Obj } from "../types.js";
import { AUTO_THINKING_HELP, THINKING_HELP } from "./thinking-help.js";

/** field key -> unsaved control value; null stages removing a config override */
export const edits: Map<string, unknown> = new Map();
const fieldsByKey = new Map(SCHEMA.flatMap((tab) => tab.groups.flatMap((group) => (group.fields || []).map((f) => [f.key, f] as const))));

export function getPath(o: Obj, path: string): unknown {
  return path.split(".").reduce<any>((v, k) => (v && typeof v === "object" ? v[k] : undefined), o);
}

/** {a: {b: value}} for the path "a.b". */
export function patchFor(path: string, value: unknown): Obj {
  const out: Obj = {};
  const ks = path.split(".");
  let cur = out;
  ks.slice(0, -1).forEach((k) => (cur = cur[k] = {}));
  cur[ks[ks.length - 1]] = value;
  return out;
}

/** Objects merge, everything else replaces (patches built here; the main process merges the files). */
export function merge(a: Obj, b: Obj): Obj {
  const out = { ...a };
  for (const [k, v] of Object.entries(b)) {
    out[k] = v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object" ? merge(out[k], v) : v;
  }
  return out;
}

export const isApp = (key: string) => key.startsWith("app:");
const rawStored = (f: Field) => (isApp(f.key) ? getPath(live.appSettings, f.key.slice(4)) : getPath(live.config, f.key));
// what the control shows: inverted fields display the negation of what is stored
const stored = (f: Field) => { const v = rawStored(f); return f.invert && v !== undefined ? !v : v; };
// null stages removing an override, including for inverted controls.
export const toStore = (f: Field, v: unknown) => (v === null ? null : f.invert ? !v : v);
const current = (f: Field) => (edits.has(f.key) ? edits.get(f.key) ?? f.def : stored(f) ?? f.def);
const controlId = (f: Field) => `f_${f.key.replace(/[^a-z0-9]/gi, "_")}`;
const canReset = (f: Field) => !(edits.has(f.key) && edits.get(f.key) === null)
  && (rawStored(f) != null || JSON.stringify(current(f)) !== JSON.stringify(f.def));
const resetTitle = (f: Field) => `${edits.has(f.key) && edits.get(f.key) === null ? "Default selected; Save to apply"
  : canReset(f) ? "Select the default; Save to apply" : "Already using the default"} (${JSON.stringify(f.def)})`;

function setEdit(f: Field, v: unknown): void {
  const base = stored(f) ?? f.def;
  if (JSON.stringify(v) === JSON.stringify(base)) edits.delete(f.key);
  else edits.set(f.key, v);
  updateDirty();
}

/** The save bar: how many changes are unsaved, and its buttons. */
export function updateDirty(): void {
  const n = edits.size;
  $("set-dirty").textContent = n ? `${n} unsaved change${n > 1 ? "s" : ""}` : "";
  for (const id of ["set-save", "set-revert"]) $<HTMLButtonElement>(id).disabled = !n;
  let restartRequired = false;
  for (const key of edits.keys()) {
    if (fieldsByKey.get(key)?.restart) {
      restartRequired = true;
      break;
    }
  }
  $<HTMLButtonElement>("set-save-restart").disabled = !restartRequired;
  document.querySelectorAll<HTMLElement>(".field[data-key]").forEach((row) => {
    const f = fieldsByKey.get(row.dataset.key!);
    if (!f) return;
    row.classList.toggle("changed", edits.has(f.key));
    const reset = row.querySelector<HTMLButtonElement>("[data-reset]");
    if (reset) {
      const resettable = canReset(f);
      reset.disabled = !resettable;
      reset.title = resetTitle(f);
    }
  });
}

function rangeLabel(f: Field, value: unknown): string {
  return f.format === "percent" ? `${Math.round(Number(value) * 100)}%` : String(value);
}

function controlHtml(f: Field, id: string, v: unknown): string {
  switch (f.type) {
    case "bool":
      return `<label class="switch"><input type="checkbox" id="${id}" ${v ? "checked" : ""}><span class="track"></span><span class="state">${v ? "On" : "Off"}</span></label>`;
    case "number":
      return `<input type="number" id="${id}" value="${esc(v)}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""} step="${f.step ?? 1}">`;
    case "range":
      return `<input type="range" id="${id}" value="${esc(v)}" min="${f.min}" max="${f.max}" step="${f.step ?? 1}"><span class="rangeval">${esc(rangeLabel(f, v))}</span>`;
    case "select": {
      let options = f.options || [];
      if (f.key === "companion.voice.language" && typeof v === "string" && !options.some(([code]) => code === v))
        options = [...options, [v, v]];
      if (f.key === "companion.ptt.whisper_model"
          && languageCode(edits.get("companion.voice.language") ?? getPath(live.config, "companion.voice.language")) !== "en") {
        options = options.filter(([model]) => !model.endsWith(".en"));
        // Keep a manually selected multilingual model, but never offer an English-only one for another language.
        if (typeof v === "string" && v && !v.endsWith(".en") && v !== "small") options.push([v, `${v}: saved multilingual model`]);
        if (typeof v !== "string" || !v || v.endsWith(".en")) v = "small";
      }
      return `<select id="${id}">${options.map(([val, lab]) => `<option value="${esc(val)}" ${val === v ? "selected" : ""}>${esc(lab)}</option>`).join("")}</select>`;
    }
    case "list":
      return `<textarea id="${id}" rows="3" cols="40" spellcheck="false">${esc(((v as string[]) || []).join("\n"))}</textarea>`;
    default:
      return `<input type="text" id="${id}" class="${f.wide ? "wide" : ""}" value="${esc(v ?? "")}" spellcheck="false">`;
  }
}

export function fieldHtml(f: Field): string {
  const ctl = controlHtml(f, controlId(f), current(f));
  const resettable = canReset(f);
  const thinking = f.key === "companion.llm.tune_thinking";
  const pttToggle = f.key === "app:engineArgs.noPtt";
  const hasKeyNote = f.key === "companion.ptt.engine" || f.key === "companion.llm.recall_meaning";
  const help = thinking
    ? esc(f.help).replace("lightweight", `<button type="button" class="reset thinking-help-term" data-thinking-help aria-describedby="thinking-policy-help">lightweight</button>`)
    : pttToggle
      ? esc(f.help).replace("Voice settings", `<button type="button" class="reset settings-jump" data-jump-tab="voice" data-jump-target="stt">Voice settings</button>`)
      : esc(f.help);
  const effort = f.key.endsWith("_reasoning_effort");
  return `<div class="field${edits.has(f.key) ? " changed" : ""}" data-key="${esc(f.key)}">
    <div class="lab">${esc(f.label)}${f.help ? `<small>${help}${effort ? ` ${AUTO_THINKING_HELP}` : ""}</small>` : ""}</div>
    <div class="ctl">${ctl}
      ${f.restart ? `<span class="restart" title="Applies when the coach restarts">${icon("rotate-cw")}</span>` : ""}
      ${!isApp(f.key) ? `<button type="button" class="reset" data-reset="${esc(f.key)}" ${resettable ? "" : "disabled"} title="${esc(resetTitle(f))}">Use default</button>` : ""}
      ${hasKeyNote ? `<span data-field-key-note hidden></span>` : ""}</div></div>${thinking ? THINKING_HELP : ""}`;
}

/** Follow the control drawn by fieldHtml: each change is an edit (or no longer one when it's back to the stored value). */
export function bindField(f: Field): void {
  const el = document.getElementById(controlId(f)) as HTMLInputElement | null;
  if (!el) return;
  const read = (): unknown => {
    if (f.type === "bool") return el.checked;
    if (f.type === "number" || f.type === "range") {
      const n = Number(el.value);
      return Number.isFinite(n) ? n : f.def;
    }
    if (f.type === "list") return (el as unknown as HTMLTextAreaElement).value.split("\n").map((s) => s.trim()).filter(Boolean);
    return el.value;
  };
  el.addEventListener(f.type === "text" || f.type === "list" || f.type === "number" ? "input" : "change", () => {
    if (f.type === "bool") (el.parentElement!.querySelector(".state") as HTMLElement).textContent = el.checked ? "On" : "Off";
    setEdit(f, read());
  });
  if (f.type === "range") el.addEventListener("input", () => {
    (el.nextElementSibling as HTMLElement).textContent = rangeLabel(f, el.value);
    setEdit(f, read());
  });
}
