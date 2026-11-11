// The overlay over the game: the current step line (state.overlay_step) and the coach's last line. Click-through
// while locked; Ctrl+Alt+O (or the tray / Settings) unlocks it to move it and change its width.
import { glide as g } from "./bridge.js";
import { $ } from "./dom.js";
import { AppSettings, CoachMessage, LiteInitEvent, LiteState, PttEvent, TtsStatus } from "./types.js";

const SHOWN = new Set(["callout", "answer", "banter", "review", "briefing", "reminder"]);   // msg_kinds.COACH_SAID
let es: EventSource | null = null;
let url: string | null = null;
let settings: AppSettings = {};
let lastMsgAt = 0;
let ttsHoldTimer: number | undefined;

function showTts(s: TtsStatus | null | undefined): void {
  window.clearTimeout(ttsHoldTimer);
  const el = $("tts");
  if (!s || (s.phase !== "generating" && s.phase !== "speaking" && s.phase !== "fallback" && s.phase !== "error")) {
    el.hidden = true;
    el.className = "";
    return;
  }
  const isTimedHold = s.phase === "error" || (s.phase === "fallback" && s.holding);
  const remaining = isTimedHold ? 5000 - Math.max(0, Date.now() - (s.updatedAt || Date.now())) : 1;
  if (isTimedHold && remaining <= 0) {
    el.hidden = true;
    el.className = "";
    return;
  }
  $("ttsl").textContent = s.phase === "generating" ? "Generating…"
    : s.phase === "speaking" ? "Speaking"
    : s.phase === "fallback" ? "SAPI5 fallback"
    : "TTS error";
  el.className = s.phase;
  el.hidden = false;
  if (isTimedHold) {
    ttsHoldTimer = window.setTimeout(() => {
      el.hidden = true;
      el.className = "";
    }, remaining);
  }
}

function apply(s: AppSettings): void {
  settings = s;
  const o = s.overlay || {};
  document.documentElement.style.setProperty("--op", String(o.opacity ?? 0.85));
  document.documentElement.style.setProperty("--fs", `${o.fontSize ?? 13}px`);
  $("box").classList.toggle("collapsed", !!o.collapsed);
  $("collapse").textContent = o.collapsed ? "expand" : "collapse";
  fit();
}

/** The window follows the content's height (the main process keeps the width and position). */
function fit(): void {
  requestAnimationFrame(() => g.invoke("overlay:resize", Math.ceil($("box").getBoundingClientRect().height) + 2));
}

function connect(u: string | null): void {
  if (u === url) return;
  url = u;
  es?.close();
  es = null;
  if (!u) {
    $("state").textContent = "coach not running";
    fit();
    return;
  }
  $("state").textContent = "";
  es = new EventSource(u + "/events?lite=1");          // the light stream: the step line's keys of the state
  const onState = (s: LiteState) => {
    if (!s) return;
    $("step").textContent = s.overlay_step || (s.frames ? `L${s.level ?? "?"}  ${s.zone || ""}` : "Glide: waiting for the game (is the data strip on?)");
    fit();
  };
  es.addEventListener("init", (e) => {
    const d: LiteInitEvent = JSON.parse((e as MessageEvent).data);
    onState(d.state);
    const last = [...(d.messages || [])].reverse().find((m) => SHOWN.has(m.kind));
    if (last) showMsg(last);
  });
  es.addEventListener("state", (e) => onState(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("ptt", (e) => {
    const st = (JSON.parse((e as MessageEvent).data) as PttEvent).state;
    document.body.classList.toggle("ptt-listening", st === "listening");
    document.body.classList.toggle("ptt-transcribing", st === "transcribing");
    $("pttl").textContent = st === "transcribing" ? "Transcribing" : "Listening";
    fit();
  });
  es.addEventListener("message", (e) => {
    const m: CoachMessage = JSON.parse((e as MessageEvent).data);
    if (SHOWN.has(m.kind)) showMsg(m);
  });
  es.onerror = () => {
    $("state").textContent = "reconnecting…";
    fit();
  };
  es.onopen = () => {
    $("state").textContent = "";
    fit();
  };
}

function showMsg(m: CoachMessage): void {
  const el = $("msg");
  el.textContent = m.text || "";
  el.className = m.urgent ? "urgent" : "";
  lastMsgAt = Date.now();
  fit();
}
setInterval(() => $("msg").classList.toggle("old", Date.now() - lastMsgAt > 60_000), 5000);

async function setOverlay(patch: NonNullable<AppSettings["overlay"]>): Promise<void> {
  apply(await g.invoke("app:settings:set", { overlay: patch }));
}
$("narrow").onclick = () => g.invoke("overlay:width", -40);
$("wide").onclick = () => g.invoke("overlay:width", 40);
$("collapse").onclick = () => setOverlay({ collapsed: !settings.overlay?.collapsed });
$("lock").onclick = () => g.invoke("overlay:unlock", false);

// The overlay only gets the mouse while Alt is held over it (or while unlocked): drag moves it (the main process
// follows the cursor until the button is released), the wheel changes its width, right-click collapses it.
const interactive = () => document.body.classList.contains("held") || document.body.classList.contains("unlocked");
document.addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !interactive() || (e.target as HTMLElement).closest("button")) return;
  e.preventDefault();
  g.invoke("overlay:dragStart");
});
document.addEventListener("wheel", (e) => {
  if (interactive()) g.invoke("overlay:width", e.deltaY < 0 ? 40 : -40);
}, { passive: true });
document.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  if (interactive()) setOverlay({ collapsed: !settings.overlay?.collapsed });
});

g.on("overlay:unlocked", (on: boolean) => {
  document.body.classList.toggle("unlocked", on);
  fit();
});
g.on("overlay:held", (on: boolean) => document.body.classList.toggle("held", on));
g.on("app:settings", apply);
g.on("engine:state", (s) => connect(s.url));
g.on("tts:status", showTts);

(async () => {
  apply(await g.invoke("app:settings:get"));
  showTts(await g.invoke("tts:status:get"));
  connect((await g.invoke("engine:get")).url);
})();
