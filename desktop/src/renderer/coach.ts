// The Coach page: the engine's own dashboard in a frame while the coach runs, else what it's doing and a Start
// button. The window follows the engine's event stream for the session bar and the narrator.
import { call, glide } from "./bridge.js";
import { $ } from "./dom.js";
import { speech } from "./narrator.js";
import { sessionBar } from "./session-bar.js";
import { live } from "./state.js";
import { refreshStatus } from "./status.js";
import { EngineStatus, LiteInitEvent, PttEvent, SpeakEvent, VoiceSpokenEvent } from "./types.js";

export const ENGINE_LABEL: Record<string, string> = { stopped: "Stopped", building: "Building", starting: "Starting", running: "Running",
                                                      stopping: "Stopping", crashed: "Stopped with an error" };
let events: EventSource | null = null;

export function onEngine(s: EngineStatus): void {
  live.engineState = s.state;
  live.engineToken = s.token || "";
  showEngineState(s);
  refreshStatus();
  if (s.url !== live.engineUrl) {
    live.engineUrl = s.url;
    events?.close();
    events = null;
    if (live.engineUrl) showDashboard(live.engineUrl);
    else hideDashboard();
  }
  document.dispatchEvent(new Event("engine-connection-changed"));
}

/** The footer's coach control and the Coach page's placeholder text. */
function showEngineState(s: EngineStatus): void {
  $("engine").className = `engine ${s.state}`;
  $("eng-state").textContent = ENGINE_LABEL[s.state] || s.state;
  $("engine").title = `Coach: ${ENGINE_LABEL[s.state] || s.state}${s.detail ? ` (${s.detail})` : ""}`;
  const busy = ["building", "starting", "running", "stopping"].includes(s.state);
  $<HTMLButtonElement>("eng-start").disabled = busy;
  $<HTMLButtonElement>("eng-stop").disabled = !busy;
  $<HTMLButtonElement>("empty-start").style.display = busy ? "none" : "";
  $("empty-title").textContent = s.state === "building" ? "Building the knowledge base"
    : s.state === "starting" ? "Starting the coach" : s.state === "crashed" ? "The coach stopped with an error" : "The coach isn't running";
  $("empty-text").textContent = s.state === "building" ? "This happens once, on the first start, and takes a minute or two."
    : s.state === "crashed" ? `${s.detail || "It exited"}. The log below shows why; Start tries again.` : s.state === "starting" ? "Loading the knowledge base and connecting to the game…" :
      "Start it to read the game and coach you. The first start builds the knowledge base, which takes a minute or two.";
}

function showDashboard(url: string): void {
  const f = $<HTMLIFrameElement>("dash");
  f.src = `${url}/#t=${encodeURIComponent(live.engineToken)}`;      // the page reads its token from the fragment
  f.classList.add("on");
  $("dash-empty").style.display = "none";
  connectEvents(url);
}

function hideDashboard(): void {
  const f = $<HTMLIFrameElement>("dash");
  f.classList.remove("on");
  f.removeAttribute("src");
  $("dash-empty").style.display = "";
  speech.stop();
  sessionBar(null);
}

/** Push-to-talk as it happens, in the session bar: a pulsing gold "Listening" while the key is held. */
function showPtt(state: PttEvent["state"]): void {
  const el = $("sb-ptt");
  el.hidden = state === "idle";
  el.className = `sb-ptt ${state}`;
  $("sb-ptt-l").textContent = state === "transcribing" ? "Transcribing" : "Listening";
}

const BARGE_MS = 150;            // as the engine's BARGE_S
let bargeTimer = 0;

/** The light stream: the session bar's keys of the state, and the speak and push-to-talk events. */
function connectEvents(url: string): void {
  events = new EventSource(url + "/events?lite=1");
  events.addEventListener("init", (e) => { try { sessionBar((JSON.parse((e as MessageEvent).data) as LiteInitEvent).state); } catch { /* ignore */ } });
  events.addEventListener("state", (e) => { try { sessionBar(JSON.parse((e as MessageEvent).data)); } catch { /* ignore */ } });
  events.addEventListener("ptt", (e) => {
    try {
      const state = (JSON.parse((e as MessageEvent).data) as PttEvent).state;
      // the player talks over the coach: it stops, like on a call (held BARGE_MS: a brush of the key isn't talking)
      if (bargeTimer) { clearTimeout(bargeTimer); bargeTimer = 0; }
      if (state === "listening") bargeTimer = window.setTimeout(() => { bargeTimer = 0; speech.bargeIn(); }, BARGE_MS);
      showPtt(state);
    } catch { /* ignore */ }
  });
  events.addEventListener("speak", (e) => {
    try {
      const d: SpeakEvent = JSON.parse((e as MessageEvent).data);
      speech.say(String(d.text || ""), !!d.interrupt, d.style ? String(d.style) : "", d.tone ? String(d.tone) : "");
    } catch { /* ignore */ }
  });
  events.addEventListener("voice_spoken", (e) => {
    try {
      const d: VoiceSpokenEvent = JSON.parse((e as MessageEvent).data);
      if (typeof d.voice === "string" && typeof d.text === "string" && Number.isFinite(d.at))
        speech.noteVoice(`${d.voice} (SAPI5)`, d.text, d.at);
    } catch { /* ignore */ }
  });
}

export function wireCoach(): void {
  $("eng-start").onclick = () => call("engine:start");
  $("empty-start").onclick = () => call("engine:start");
  $("eng-stop").onclick = () => call("engine:stop");
  $("eng-restart").onclick = () => call("engine:restart");
  glide.on("engine:state", onEngine);
}
