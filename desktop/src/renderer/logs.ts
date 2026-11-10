// The Logs page shows per-operation attribution; the Coach tail stays text-only.
import { formatLogText, normalizeLogAttribution } from "../shared/logging.mjs";
import type { LogAttribution, LogLine } from "../shared/ipc.js";
import { call, glide } from "./bridge.js";
import { $, esc, toast } from "./dom.js";
import { currentPage } from "./nav.js";

const KEEP = 3000;
let logLines: LogLine[] = [];
let logTimer: number | null = null;
const lineIds = new WeakMap<LogLine, number>();
let nextLineId = 0;

const FIELDS: [keyof LogAttribution, string][] = [
  ["category", "Category"],
  ["operation", "Operation"],
  ["model", "Requested model"],
  ["reported_model", "Provider-reported model"],
  ["provider", "Provider"],
  ["role", "Role"],
  ["reasoning", "Requested reasoning"],
  ["sent_reasoning", "Sent reasoning"],
  ["previous_reasoning", "Previous reasoning"],
  ["application", "Application"],
  ["status", "Status"],
  ["voice", "Voice"],
  ["language", "Language"],
  ["request_id", "Local request ID"],
  ["elapsed_ms", "Elapsed (ms)"],
  ["rate", "Speech rate"],
  ["volume", "Speech volume"],
];

function attributionHtml(attribution: LogAttribution, id: number): string {
  const fields = FIELDS.filter(([key]) => attribution[key] !== undefined).map(([key, label]) => {
    if (key === "model" && attribution.category !== "AI") label = "Model";
    if (key === "rate") label = attribution.provider === "SAPI5" ? "Native SAPI rate (−10…10)" : "Playback speed (×)";
    if (key === "volume") label = attribution.provider === "SAPI5" ? "Native volume (%)" : "Playback volume (0–1)";
    return [key, label] as const;
  });
  const full = fields.map(([key, label]) => `${label}: ${attribution[key]}`).join("\n");
  return `<button type="button" class="log-category log-${attribution.category.toLowerCase()}" data-log-id="${id}" aria-label="${esc(full)}" title="${esc(full)}">${attribution.category}</button> `;
}

function lineHtml(l: LogLine, details = false): string {
  const ts = new Date(l.t).toLocaleTimeString();
  const attribution = details ? normalizeLogAttribution(l.attribution) : undefined;
  let badge = "";
  if (attribution) {
    let id = lineIds.get(l);
    if (id === undefined) { id = ++nextLineId; lineIds.set(l, id); }
    badge = attributionHtml(attribution, id);
  }
  return `<span class="${esc(l.stream)}"><span class="ts">${esc(ts)}</span>  ${badge}${esc(l.text)}</span>\n`;
}


/** Draw the Logs page (when it shows, or `force`) and the Coach page's plain tail. */
export function renderLog(force = false): void {
  const q = ($<HTMLInputElement>("log-filter").value || "").toLowerCase();
  const lines = q ? logLines.filter((l) => formatLogText(l.text, l.attribution).toLowerCase().includes(q)) : logLines;
  if (currentPage() === "logs" || force) {
    const focused = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.logId : undefined;
    $("log").innerHTML = lines.slice(-1500).map((l) => lineHtml(l, true)).join("");
    if (focused) $("log").querySelector<HTMLButtonElement>(`[data-log-id="${Number(focused)}"]`)?.focus({ preventScroll: true });
    if ($<HTMLInputElement>("log-follow").checked) $("page-logs").scrollTop = 1e9;
  }
  $("empty-log").innerHTML = logLines.slice(-40).map((l) => lineHtml(l)).join("");
  $("empty-log").scrollTop = 1e9;
}

/** The log so far (at start; new lines arrive as events). */
export function loadLog(lines: LogLine[]): void {
  logLines = lines;
  renderLog();
}

export function wireLogs(): void {
  glide.on("engine:logline", (l: LogLine) => {
    logLines.push(l);
    if (logLines.length > KEEP) logLines = logLines.slice(-KEEP);
    if (logTimer == null) logTimer = window.setTimeout(() => { logTimer = null; renderLog(); }, 150);
  });
  $("log-filter").oninput = () => renderLog();
  $("log-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(logLines.map((l) => `${new Date(l.t).toISOString()} [${l.stream}] ${formatLogText(l.text, l.attribution)}`).join("\n"));
      toast("Log copied");
    } catch { toast("Couldn't copy the log", true); }
  };
  $("log-folder").onclick = async () => {
    const info = await call("app:info");
    if (info) call("shell:open", info.home + "\\data\\live");
  };
  $("log-files").onclick = async () => {
    const problem = await call("app:logs");
    if (problem) toast(`Couldn't open the log folder: ${problem}`, true);
  };
}
