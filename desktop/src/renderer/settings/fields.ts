// The Settings page's ordinary fields (settings-schema.ts): where each value is stored, what its control shows,
// and the unsaved edits. A key is a path in config.json, or in the app's own settings when it starts with "app:".
import { $, esc, icon } from "../dom.js";
import { Field } from "../settings-schema.js";
import { live } from "../state.js";
import { languageCode } from "../speech-language.js";
import { Obj } from "../types.js";

/** field key -> new value, as the control shows it (unsaved) */
export const edits: Map<string, unknown> = new Map();

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
export const toStore = (f: Field, v: unknown) => (f.invert ? !v : v);
const current = (f: Field) => (edits.has(f.key) ? edits.get(f.key) : stored(f) ?? f.def);
const controlId = (f: Field) => `f_${f.key.replace(/[^a-z0-9]/gi, "_")}`;

function setEdit(f: Field, v: unknown): void {
  const base = stored(f) ?? f.def;
  if (JSON.stringify(v) === JSON.stringify(base)) edits.delete(f.key);
  else edits.set(f.key, v);
  document.querySelector(`.field[data-key="${CSS.escape(f.key)}"]`)?.classList.toggle("changed", edits.has(f.key));
  updateDirty();
}

/** The save bar: how many changes are unsaved, and its buttons. */
export function updateDirty(): void {
  const n = edits.size;
  $("set-dirty").textContent = n ? `${n} unsaved change${n > 1 ? "s" : ""}` : "";
  for (const id of ["set-save", "set-save-restart", "set-revert"]) $<HTMLButtonElement>(id).disabled = !n;
}

function controlHtml(f: Field, id: string, v: unknown): string {
  switch (f.type) {
    case "bool":
      return `<label class="switch"><input type="checkbox" id="${id}" ${v ? "checked" : ""}><span class="track"></span><span class="state">${v ? "On" : "Off"}</span></label>`;
    case "number":
      return `<input type="number" id="${id}" value="${esc(v)}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""} step="${f.step ?? 1}">`;
    case "range":
      return `<input type="range" id="${id}" value="${esc(v)}" min="${f.min}" max="${f.max}" step="${f.step ?? 1}"><span class="rangeval">${esc(v)}</span>`;
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
  const isDef = stored(f) === undefined;
  return `<div class="field${edits.has(f.key) ? " changed" : ""}" data-key="${esc(f.key)}">
    <div class="lab">${esc(f.label)}${f.help ? `<small>${esc(f.help)}</small>` : ""}</div>
    <div class="ctl">${ctl}
      ${f.restart ? `<span class="restart" title="Applies when the coach restarts">${icon("rotate-cw")}</span>` : ""}
      ${f.key === "companion.voice.voice" ? `<button type="button" class="reset" data-copy-windows-voice title="Copy the selected Windows/SAPI5 voice from Coach voice. Windows default leaves the name empty.">Copy Windows voice</button>` : ""}
      ${!isDef && !isApp(f.key) ? `<button class="reset" data-reset="${esc(f.key)}" title="Remove it from config.json and use the default (${esc(JSON.stringify(f.def))})">Use default</button>` : ""}</div></div>`;
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
  if (f.key === "companion.voice.voice") {
    el.parentElement!.querySelector<HTMLButtonElement>("[data-copy-windows-voice]")!.onclick = () => {
      const picker = document.getElementById("sv-voice") as HTMLSelectElement | null;
      el.value = picker?.value ?? live.appSettings.speech?.voiceName ?? "";
      setEdit(f, el.value);
    };
  }
  if (f.type === "range") el.addEventListener("input", () => {
    (el.nextElementSibling as HTMLElement).textContent = el.value;
    setEdit(f, read());
  });
}
