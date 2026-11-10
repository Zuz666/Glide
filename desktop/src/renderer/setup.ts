// First-run setup: the one walk through what Glide needs from a new player (their game, talking to the coach, the
// coach's voice, AI help), each step with its recommended choice already picked, so Continue all the way through
// works. It covers the window until it's finished or skipped (app settings setupDone); Settings > General runs it
// again, starting from what's saved. Each step saves its choices when the player continues; the coach restarts
// once at the end to take them all.
import { updateAppSettings } from "./app-settings.js";
import { call } from "./bridge.js";
import { $, esc, icon, toast } from "./dom.js";
import { speech } from "./narrator.js";
import { show } from "./nav.js";
import { live } from "./state.js";
import { filterEdgeVoices, speechLanguage as language, voiceSample } from "./speech-language.js";
import { AddonStatus, KeyStatus, KnownKey, Obj, TtsProvider, WowClient } from "./types.js";

type Ai = "anthropic" | "gemini" | "openai" | "openrouter" | "none";
interface Step { id: string; title: string; icon: string; draw(host: HTMLElement): void | Promise<void>; next(): Promise<boolean> }

const AI_KEY: Record<Exclude<Ai, "none">, string> = { anthropic: "ANTHROPIC_API_KEY", gemini: "GEMINI_API_KEY", openai: "OPENAI_API_KEY",
  openrouter: "OPENROUTER_API_KEY" };
// OpenAI's chat models go through the engine's custom endpoints; picking OpenAI adds this one
const OPENAI_ENDPOINT = { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", key_env: "OPENAI_API_KEY" };
const STT_KEY: Record<string, string> = { groq: "GROQ_API_KEY", openai: "OPENAI_API_KEY", deepgram: "DEEPGRAM_API_KEY" };
const TTS_KEY: Record<string, string> = { openai: "OPENAI_API_KEY", elevenlabs: "ELEVENLABS_API_KEY", azure: "AZURE_SPEECH_KEY", google: "GOOGLE_TTS_API_KEY" };
// [fallback id, its family]: the newest model of the family the key lists, else the fallback. [fast, deep] per quality.
type Preset = [string, RegExp][];
const PRESETS: Record<Exclude<Ai, "none">, Record<"balanced" | "best", Preset>> = {
  anthropic: {
    balanced: [["claude-haiku-5-5", /^claude-haiku-/], ["claude-sonnet-5-5", /^claude-sonnet-/]],
    best: [["claude-sonnet-5-5", /^claude-sonnet-/], ["claude-opus-5-5", /^claude-opus-/]],
  },
  gemini: {
    balanced: [["gemini:gemini-3.5-flash-lite", /^gemini:gemini-[\d.]+-flash-lite$/], ["gemini:gemini-3.8-flash", /^gemini:gemini-[\d.]+-flash$/]],
    best: [["gemini:gemini-3.8-flash", /^gemini:gemini-[\d.]+-flash$/], ["gemini:gemini-3.1-pro", /^gemini:gemini-[\d.]+-pro$/]],
  },
  openai: {
    balanced: [["ep:openai:gpt-5-mini", /^ep:openai:gpt-[\d.]+-mini$/], ["ep:openai:gpt-5", /^ep:openai:gpt-[\d.]+$/]],
    best: [["ep:openai:gpt-5", /^ep:openai:gpt-[\d.]+$/], ["ep:openai:gpt-5", /^ep:openai:gpt-[\d.]+$/]],
  },
  openrouter: {
    balanced: [["anthropic/claude-haiku-5.5", /^anthropic\/claude-haiku-/], ["anthropic/claude-sonnet-5.5", /^anthropic\/claude-sonnet-/]],
    best: [["anthropic/claude-sonnet-5.5", /^anthropic\/claude-sonnet-/], ["anthropic/claude-opus-5.5", /^anthropic\/claude-opus-/]],
  },
};
const FLAVOR: Record<string, string> = { forever: "WoW Forever", classic_beta: "Classic beta", classic: "Classic", classic_era: "Classic Era",
  retail: "Retail", ptr: "Test realm (PTR)", beta: "Beta" };

// what the player has chosen so far (read from the saved settings when setup opens)
const pick = {
  stt: "auto", sttOnline: "groq", pttKey: "F8",
  voice: "windows" as "windows" | "online" | "off", tts: "elevenlabs" as TtsProvider, voiceName: "", edgeVoice: "",
  ai: "anthropic" as Ai, quality: "balanced" as "balanced" | "best", models: null as string[] | null,
};
let known: KnownKey[] = [];
let keyState: KeyStatus[] = [];
let at = 0;
let mic: { stream: MediaStream; ctx: AudioContext; raf: number; stop: number } | null = null;
let capture: ((e: KeyboardEvent | MouseEvent) => void) | null = null;

// ---- pieces ------------------------------------------------------------------------------------------------
const head = (title: string, lede: string) => `<header class="su-head"><h1 id="su-title">${title}</h1><p>${lede}</p></header>`;
const choice = (name: string, value: string, cur: string, title: string, text: string, tag = "") =>
  `<label class="su-choice"><input type="radio" name="${name}" value="${esc(value)}" ${value === cur ? "checked" : ""}>
    <span class="su-dot" aria-hidden="true"></span><span class="su-ct"><b>${title}${tag ? ` <span class="su-tag">${tag}</span>` : ""}</b><small>${text}</small></span></label>`;
const onChoice = (host: HTMLElement, name: string, fn: (v: string) => void) =>
  host.querySelectorAll<HTMLInputElement>(`input[name="${name}"]`).forEach((i) => (i.onchange = () => fn(i.value)));
const saved = (env: string) => keyState.find((s) => s.env === env);
const hasKey = (env: string) => !!(saved(env)?.saved || saved(env)?.fromEnv);
const pasted = (env: string) => (document.getElementById(`su-key-${env}`) as HTMLInputElement | null)?.value.trim() || "";

/** A key's box: whether one is saved, where to paste one, and how to get it (its guide from the main process). */
function keyBox(env: string): string {
  const k = known.find((x) => x.env === env), g = k?.guide, s = saved(env);
  const state = s?.saved ? `<span class="ok">${icon("check")}Saved, ending ${esc(s.hint.replace("…", ""))}</span>`
    : s?.fromEnv ? `<span class="ok">${icon("check")}From a Windows environment variable</span>` : "";
  return `<div class="su-key">
    <div class="row"><input type="password" id="su-key-${esc(env)}" class="grow" autocomplete="off" spellcheck="false"
      placeholder="${state ? "Paste a new key to replace it" : `Paste your ${esc(k?.label || env)} key`}" aria-label="${esc(k?.label || env)} key">
      ${g ? `<button type="button" class="btn" data-open="${esc(g.url)}">${icon("external-link")}Get a key</button>` : ""}</div>
    ${state ? `<div class="note-line">${state}</div>` : ""}
    ${g ? `<ol class="su-steps-how">${g.steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>${g.cost ? `<p class="help">${esc(g.cost)}</p>` : ""}` : ""}
  </div>`;
}
function bindOpen(host: HTMLElement): void {
  host.querySelectorAll<HTMLButtonElement>("[data-open]").forEach((b) => (b.onclick = () => call("shell:open", b.dataset.open!)));
}
async function saveKey(env: string): Promise<boolean> {
  const v = pasted(env);
  if (!v) return true;
  const r = await call("keys:set", env, v);
  if (r) keyState = r;
  return !!r;
}
const hint = (t: string, bad = false) => { $("su-hint").innerHTML = t ? `<span class="${bad ? "bad" : "dim"}">${t}</span>` : ""; };

// ---- 1. the game ---------------------------------------------------------------------------------------------
let clients: WowClient[] = [];
let addon: AddonStatus | null = null;

async function drawGame(host: HTMLElement): Promise<void> {
  host.innerHTML = head("Your game", "Glide reads the game through its own small addon, and follows your RestedXP guide. Pick the World of Warcraft you level in.")
    + `<div class="su-choices" id="su-clients"><p class="dim">Looking for World of Warcraft on this PC…</p></div><div id="su-addon"></div>`;
  clients = (await call("addon:detect")) || [];
  await drawClients();
}

async function drawClients(): Promise<void> {
  const cur = String(live.config.wow_dir || "");
  const list = clients.some((c) => c.path === cur) || !cur ? clients : [...clients, { path: cur, flavor: cur.split(/[\\/]/).filter(Boolean).pop()!.replace(/^_|_$/g, "") }];
  const host = $("su-clients");
  host.innerHTML = (list.length ? "" : `<p class="warn">No World of Warcraft found in the usual places. Show Glide where it is.</p>`)
    + list.map((c) => choice("su-wow", c.path, cur, esc(FLAVOR[c.flavor] || c.flavor), `<code>${esc(c.path)}</code>`)).join("")
    + choice("su-wow", "*browse", "", "Somewhere else", "Pick the client folder yourself, such as <code>…\\World of Warcraft\\_forever_</code>.");
  onChoice(host, "su-wow", async (v) => {
    let p: string | null = v;
    if (v === "*browse") p = (await call("dialog:folder", "World of Warcraft client folder (e.g. _forever_)", cur || undefined)) || null;
    if (p) live.config = (await call("config:set", { wow_dir: p })) || live.config;
    drawClients();
  });
  addon = await call("addon:status") || null;
  const a = addon, el = $("su-addon");
  if (!a?.wowDir) { el.innerHTML = ""; return; }
  if (!a.wowOk) { el.innerHTML = `<p class="bad su-line">${icon("circle-alert")}That folder isn't a WoW client: there's no Wow*.exe, Interface or WTF in it.</p>`; return; }
  el.innerHTML = `<ul class="su-facts">
      <li>${icon(a.upToDate ? "check" : "download")}${a.upToDate ? `The Glide addon ${esc(a.installed)} is installed.`
        : a.installed ? `Continue updates the Glide addon from ${esc(a.installed)} to ${esc(a.bundled)}.` : `Continue installs the Glide addon ${esc(a.bundled)} in this game.`}</li>
      <li class="${a.rxp ? "" : "warn"}">${icon(a.rxp ? "check" : "circle-alert")}${a.rxp ? "RestedXP is installed: the coach follows its guides."
        : `RestedXP isn't in this game yet. The coach follows its guides, so install it before you play. <button type="button" class="btn ghost" data-open="https://www.restedxp.com/">${icon("external-link")}Get RestedXP</button>`}</li>
    </ul>`;
  bindOpen(el);
}

async function nextGame(): Promise<boolean> {
  if (addon?.wowOk && !addon.upToDate) {
    const r = await call("addon:install");
    if (!r) return false;
    addon = r.status;
  }
  return true;
}

// ---- 2. talking to the coach -----------------------------------------------------------------------------------
function drawTalk(host: HTMLElement): void {
  stopMic();
  const off = pick.stt === "off";
  host.innerHTML = head("Talking to the coach", "Hold a key and ask out loud: where next, what's this quest for, remind me to buy water. Glide turns what you say into text first.")
    + `<div class="su-choices">`
    + choice("su-stt", "auto", pick.stt, "On this PC", "Private and offline, and the best at game names like Goldshire or Hogger. Downloads once in the background (about 600 MB); Windows speech fills in until it's ready.", "Recommended")
    + choice("su-stt", "windows", pick.stt, "Windows speech", "Built in, nothing to download. It gets game names wrong more often.")
    + choice("su-stt", "online", pick.stt, "Online", "Fast and accurate, with a key from Groq (free tier), OpenAI or Deepgram. When it can't be reached, this PC's recognition answers instead.")
    + (pick.stt === "online" ? `<div class="su-more"><div class="row"><span class="help">Service</span><select id="su-stt-svc" aria-label="Online recognition service">
        ${[["groq", "Groq (free tier, fastest)"], ["openai", "OpenAI"], ["deepgram", "Deepgram"]].map(([v, l]) => `<option value="${v}" ${v === pick.sttOnline ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        ${keyBox(STT_KEY[pick.sttOnline])}</div>` : "")
    + choice("su-stt", "off", pick.stt, "I'll only type", "No push-to-talk. You can still ask the coach in its chat.")
    + `</div>`
    + (off ? "" : `<div class="su-panel">
        <div class="su-row"><div><b>Push-to-talk key</b><small>WoW sees the key too: pick one you haven't bound in the game. A side mouse button works well.</small></div>
          <div class="row"><kbd class="su-kbd" id="su-ptt">${esc(pick.pttKey)}</kbd><button type="button" class="btn" id="su-ptt-set">${icon("keyboard")}Change</button></div></div>
        <div class="su-row"><div><b>Your microphone</b><small>Push-to-talk records from Windows' default microphone.</small></div>
          <div class="su-mic"><button type="button" class="btn" id="su-mic">${icon("mic")}Test my microphone</button><div class="su-meter" aria-hidden="true"><i id="su-meter"></i></div><span id="su-mic-msg" class="help" role="status"></span></div></div>
      </div>`);
  onChoice(host, "su-stt", (v) => { pick.stt = v; drawTalk(host); });
  const svc = document.getElementById("su-stt-svc") as HTMLSelectElement | null;
  if (svc) svc.onchange = () => { pick.sttOnline = svc.value; drawTalk(host); };
  bindOpen(host);
  if (off) return;
  $("su-ptt-set").onclick = () => listenForKey();
  $("su-mic").onclick = () => testMic();
}

/** The next key (with Ctrl, Alt or Shift) or side mouse button becomes the push-to-talk key, in the names the
 * engine reads (companion/ptt.py VK). Esc keeps the old one. */
function listenForKey(): void {
  const k = $("su-ptt");
  k.textContent = "Press a key…";
  k.classList.add("on");
  const done = (name: string | null, why = "") => {
    document.removeEventListener("keydown", capture!, true);
    document.removeEventListener("mousedown", capture!, true);
    capture = null;
    k.classList.remove("on");
    if (name) pick.pttKey = name;
    k.textContent = pick.pttKey;
    hint(why, !!why);
  };
  capture = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e instanceof MouseEvent) {
      if (e.button === 3 || e.button === 4) done(e.button === 3 ? "MOUSE4" : "MOUSE5");
      else if (e.button === 1) done("MOUSE3");
      return;
    }
    if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return;     // wait for the key they go with
    if (e.key === "Escape") return done(null);
    const c = e.code;
    const main = /^F\d+$/.test(c) ? c : /^Key[A-Z]$/.test(c) ? c.slice(3) : /^Digit\d$/.test(c) ? c.slice(5)
      : /^Numpad\d$/.test(c) ? "NUM" + c.slice(6) : ({ Backquote: "BACKQUOTE", Space: "SPACE", Insert: "INSERT", Delete: "DELETE", Home: "HOME",
          End: "END", PageUp: "PAGEUP", PageDown: "PAGEDOWN", Pause: "PAUSE", ScrollLock: "SCROLLLOCK", CapsLock: "CAPSLOCK" } as Obj)[c];
    if (!main) return done(null, "Glide can't use that key: try an F key, a side mouse button, or Ctrl with a letter.");
    const mods = [e.ctrlKey && "CTRL", e.altKey && "ALT", e.shiftKey && "SHIFT"].filter(Boolean);
    if (!mods.length && /^([A-Z0-9]|SPACE|BACKQUOTE)$/.test(main)) return done(null, "That key fires while you type in chat too: hold Ctrl with it, or pick an F key.");
    done([...mods, main].join("+"));
  };
  document.addEventListener("keydown", capture, true);
  document.addEventListener("mousedown", capture, true);
}

async function testMic(): Promise<void> {
  stopMic();
  const msg = $("su-mic-msg");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ctx = new AudioContext(), an = ctx.createAnalyser();
    an.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(an);
    const buf = new Float32Array(an.fftSize), name = stream.getAudioTracks()[0]?.label || "your microphone";
    let heard = false;
    msg.textContent = `Say something. Listening on ${name}.`;
    const tick = () => {
      an.getFloatTimeDomainData(buf);
      const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length), level = Math.min(1, rms * 8);
      const bar = document.getElementById("su-meter");
      if (!bar) return stopMic();
      bar.style.width = `${Math.round(level * 100)}%`;
      if (!heard && level > 0.2) { heard = true; msg.innerHTML = `<span class="ok">${icon("check")}Glide hears you on ${esc(name)}.</span>`; }
      mic!.raf = requestAnimationFrame(tick);
    };
    mic = { stream, ctx, raf: requestAnimationFrame(tick), stop: window.setTimeout(() => {
      stopMic();
      if (!heard) msg.innerHTML = `<span class="warn">Nothing came through from ${esc(name)}. Check it's Windows' default recording device and not muted.</span>`;
    }, 12000) };
  } catch (e) {
    msg.innerHTML = `<span class="bad">No microphone Glide can use: ${esc((e as Error).message)}</span>`;
  }
}

function stopMic(): void {
  if (!mic) return;
  cancelAnimationFrame(mic.raf);
  clearTimeout(mic.stop);
  mic.stream.getTracks().forEach((t) => t.stop());
  mic.ctx.close();
  mic = null;
  const bar = document.getElementById("su-meter");
  if (bar) bar.style.width = "0";
}

async function nextTalk(): Promise<boolean> {
  stopMic();
  if (capture) return false;
  const online = pick.stt === "online", env = STT_KEY[pick.sttOnline];
  if (online && !pasted(env) && !hasKey(env)) { hint("Paste the key for online recognition, or pick another way.", true); return false; }
  if (online && !(await saveKey(env))) return false;
  const ptt = pick.stt === "off" ? { enabled: false } : { enabled: true, key: pick.pttKey, engine: online ? pick.sttOnline : pick.stt };
  return !!(await call("config:set", { companion: { ptt } }));
}

// ---- 3. the coach's voice -------------------------------------------------------------------------------------
function drawVoice(host: HTMLElement): void {
  host.innerHTML = head("The coach's voice", "The coach says the next step, warnings and its answers out loud, so your eyes stay on the game.")
    + `<div class="su-choices">`
    + choice("su-voice", "windows", pick.voice, "Windows voices (SAPI5)", "Offline speech from installed Windows and third-party SAPI5 voices, including SileroTTS.", "Recommended")
    + (pick.voice === "windows" ? `<div class="su-more"><div class="row"><span class="help">Voice</span><select id="su-wvoice" aria-label="Windows voice">
        <option value="">Windows default</option>${pick.voiceName ? `<option value="${esc(pick.voiceName)}" selected>${esc(pick.voiceName)}</option>` : ""}</select></div></div>` : "")
    + choice("su-voice", "online", pick.voice, "A natural online voice", "Edge-TTS is experimental and free without registration or a key; other services may charge per line. If a service fails, that line uses the Windows SAPI5 voice.")
    + (pick.voice === "online" ? `<div class="su-more"><div class="row"><span class="help">Service</span><select id="su-tts" aria-label="Voice service">
        ${[["edge", "Edge-TTS (experimental, free)"], ["elevenlabs", "ElevenLabs"], ["openai", "OpenAI"], ["azure", "Azure Speech (free tier)"], ["google", "Google Cloud"]].map(([v, l]) => `<option value="${v}" ${v === pick.tts ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        ${pick.tts === "edge" ? `<p class="help">Experimental support, still being worked on. Free online, no registration or API key. Text is sent to Microsoft. Natural-sounding and multilingual voices are available; the unofficial service may fail or limit usage. Windows SAPI5 remains the fallback.</p>
          <div class="row"><span class="help">Voice</span><select id="su-edge-voice" aria-label="Edge-TTS voice"><option value="${esc(pick.edgeVoice)}">${esc(pick.edgeVoice || "Choose a voice")}</option></select></div>
          <div class="row"><span class="help">Show all voice languages</span><label class="switch"><input type="checkbox" id="su-edge-all-languages" aria-label="Show all voice languages"><span class="track"></span><span class="state">Off</span></label></div>
          <div class="row"><span class="help">Multilingual voices only</span><label class="switch"><input type="checkbox" id="su-edge-multilingual-only" aria-label="Multilingual voices only"><span class="track"></span><span class="state">Off</span></label></div>
          <p class="help">Multilingual voices are shown regardless of locale. Language support varies by voice; the current voice stays visible.</p>` : keyBox(TTS_KEY[pick.tts])}
        ${pick.tts === "azure" ? `<p class="help">Azure also needs your resource's region: it starts as eastus; change it in Settings &gt; Voice.</p>` : ""}</div>` : "")
    + choice("su-voice", "off", pick.voice, "Text only", "The coach writes in its chat and on the overlay, without speaking.")
    + `</div>`
    + (pick.voice === "off" ? "" : `<p class="help">Samples and voice filtering use the saved language in Settings &gt; Language. Sample translation needs an available LLM; otherwise it stays English.</p><div class="row su-sample"><button type="button" class="btn" id="su-sample">${icon("volume-2")}Play a sample</button></div>`);
  onChoice(host, "su-voice", (v) => { pick.voice = v as typeof pick.voice; drawVoice(host); });
  const wv = document.getElementById("su-wvoice") as HTMLSelectElement | null;
  if (wv) {
    wv.onchange = () => { pick.voiceName = wv.value; };
    wv.disabled = true;
    call("tts:voices", "windows").then((voices) => {
      if (!voices || !wv.isConnected) return;
      const selected = wv.value;
      wv.innerHTML = `<option value="">Windows default</option>`
        + voices.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("")
        + (selected && !voices.some((v) => v.id === selected) ? `<option value="${esc(selected)}">${esc(selected)} (not installed)</option>` : "");
      wv.value = selected;
    }).finally(() => { if (wv.isConnected) wv.disabled = false; });
  }
  const tts = document.getElementById("su-tts") as HTMLSelectElement | null;
  if (tts) tts.onchange = () => { pick.tts = tts.value as TtsProvider; drawVoice(host); };
  const ev = document.getElementById("su-edge-voice") as HTMLSelectElement | null;
  if (ev) {
    ev.onchange = () => { pick.edgeVoice = ev.value; };
    call("tts:voices", "edge").then((voices) => {
      if (!voices || !ev.isConnected) return;
      if (!pick.edgeVoice) pick.edgeVoice = voices.find((v) => v.id.startsWith(`${language()}-`))?.id || "";
      const all = $<HTMLInputElement>("su-edge-all-languages");
      const multilingual = $<HTMLInputElement>("su-edge-multilingual-only");
      const draw = () => {
        const visible = filterEdgeVoices(voices, language(), all.checked, multilingual.checked);
        const saved = pick.edgeVoice && !visible.some((v) => v.id === pick.edgeVoice)
          ? `<option value="${esc(pick.edgeVoice)}" selected>${esc(pick.edgeVoice)} (saved)</option>` : "";
        ev.innerHTML = saved + visible.map((v) => `<option value="${esc(v.id)}" ${v.id === pick.edgeVoice ? "selected" : ""}>${esc(v.name)}</option>`).join("")
          + (visible.length ? "" : `<option value="" disabled>No matching voices</option>`);
      };
      for (const toggle of [all, multilingual]) toggle.onchange = () => {
        toggle.parentElement!.querySelector(".state")!.textContent = toggle.checked ? "On" : "Off";
        all.disabled = multilingual.checked;
        draw();
      };
      draw();
    });
  }
  bindOpen(host);
  const s = document.getElementById("su-sample");
  if (s) s.onclick = async () => { if (await saveVoice()) speech.say(await voiceSample(), true); };
}

/** The voice choice into the app settings (the sample plays from them). */
async function saveVoice(): Promise<boolean> {
  if (pick.voice === "off") return true;
  if (pick.voice === "online") {
    if (pick.tts === "edge" && !pick.edgeVoice) { hint("Load and choose an Edge-TTS voice first.", true); return false; }
    const env = TTS_KEY[pick.tts];
    if (env && !pasted(env) && !hasKey(env)) { hint("Paste the key for the voice service, or pick Edge-TTS or a Windows voice.", true); return false; }
    if (env && !(await saveKey(env))) return false;
    await updateAppSettings({ speech: { provider: pick.tts, ...(pick.tts === "edge" ? { edge: { voice: pick.edgeVoice } } : {}) } });
  } else {
    await updateAppSettings({ speech: { provider: "windows", voiceName: pick.voiceName } });
  }
  hint("");
  return true;
}

async function nextVoice(): Promise<boolean> {
  if (!(await saveVoice())) return false;
  return !!(await call("config:set", { companion: { voice: { enabled: pick.voice !== "off", ...(pick.voice === "off" ? {} : { output: "app" }) } } }));
}

// ---- 4. AI help ----------------------------------------------------------------------------------------------
/** The newest model of a preset's family the key can use (the fallback when it lists none, or wasn't tested). */
function resolve([pref, family]: [string, RegExp]): string {
  const ms = pick.models;
  if (!ms?.length) return pref;
  const num = (id: string) => (id.match(/\d+/g) || []).map(Number);
  const newer = (a: string, b: string) => { const x = num(a), y = num(b); for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); return false; };
  return ms.filter((m) => family.test(m) && !/preview|exp|latest|:free$/.test(m)).reduce((best, m) => (!best || newer(m, best) ? m : best), "") || pref;
}
const modelsFor = (ai: Exclude<Ai, "none">, q: "balanced" | "best") => PRESETS[ai][q].map(resolve);
const short = (id: string) => id.replace(/^gemini:|^ep:openai:|^anthropic\//, "");

function drawAi(host: HTMLElement): void {
  const ai = pick.ai;
  host.innerHTML = head("AI help", "With an AI model the coach explains why, answers what you ask and reviews your pace. It uses your own key: you pay the provider directly, and the bar at the bottom shows what each session costs.")
    + `<div class="su-choices">`
    + choice("su-ai", "anthropic", ai, "Anthropic (Claude)", "The best coaching. Prepaid credit on your Anthropic account.", "Recommended")
    + (ai === "anthropic" ? aiMore("anthropic") : "")
    + choice("su-ai", "gemini", ai, "Google Gemini", "Free tier, and about a minute to set up.")
    + (ai === "gemini" ? aiMore("gemini") : "")
    + choice("su-ai", "openai", ai, "OpenAI (GPT)", "GPT models, on prepaid credit on your OpenAI account.")
    + (ai === "openai" ? aiMore("openai") : "")
    + choice("su-ai", "openrouter", ai, "OpenRouter", "One key for models from every vendor.")
    + (ai === "openrouter" ? aiMore("openrouter") : "")
    + choice("su-ai", "none", ai, "Not now", "Glide still coaches every step for free: callouts, the route and reminders. Questions get answers once you add a key in Settings.")
    + `</div>`;
  onChoice(host, "su-ai", (v) => { pick.ai = v as Ai; pick.models = null; drawAi(host); });
  bindOpen(host);
  if (ai === "none") return;
  onChoice(host, "su-q", (v) => { pick.quality = v as "balanced" | "best"; drawAi(host); });
  $("su-test").onclick = () => testAiKey(host);
}

function aiMore(ai: Exclude<Ai, "none">): string {
  const [bf, bd] = modelsFor(ai, "balanced"), [xf, xd] = modelsFor(ai, "best");
  return `<div class="su-more">${keyBox(AI_KEY[ai])}
      <div class="row"><button type="button" class="btn" id="su-test">${icon("check")}Test the key</button><span id="su-test-msg" class="help" role="status"></span></div>
      <div class="su-choices su-sub">
        ${choice("su-q", "balanced", pick.quality, "Balanced", `${esc(short(bf))} for callouts and spoken questions, ${esc(short(bd))} for typed questions.`, "Recommended")}
        ${choice("su-q", "best", pick.quality, "Best answers", xf === xd ? `${esc(short(xf))} for everything: better answers, and costs more.`
          : `${esc(short(xf))} and ${esc(short(xd))}: better answers, and costs more.`)}
      </div>
      <p class="help">Any other model, and what each costs an hour: Settings &gt; AI models &amp; keys.</p></div>`;
}

/** Try the pasted key (or the saved one); a working pasted key is saved. */
async function testAiKey(host?: HTMLElement): Promise<boolean> {
  if (pick.ai === "none") return true;
  const env = AI_KEY[pick.ai], v = pasted(env), msg = document.getElementById("su-test-msg");
  if (!v && !hasKey(env)) { hint("Paste your key, or choose Not now.", true); return false; }
  if (msg) msg.textContent = "Testing…";
  const r = await call("keys:test", env, v || undefined);
  if (!r?.ok) {
    if (msg) msg.innerHTML = `<span class="bad">${esc(r?.error || "the test failed")}</span>`;
    hint("");
    return false;
  }
  if (!(await saveKey(env))) {
    if (msg) msg.innerHTML = `<span class="bad">The key works, but it couldn't be saved.</span>`;
    return false;
  }
  pick.models = r.models || [];
  if (host) {
    drawAi(host);
    $("su-test-msg").innerHTML = `<span class="ok">${icon("check")}The key works${pick.models.length ? `: ${pick.models.length} models` : ""}. Saved.</span>`;
  }
  return true;
}

async function nextAi(): Promise<boolean> {
  if (pick.ai === "none") return true;
  if (!pick.models && !(await testAiKey())) return false;
  const [fast, deep] = modelsFor(pick.ai, pick.quality);
  const llm: Obj = { enabled: true, fast_model: fast, deep_model: deep };
  const eps: Obj[] = live.config.companion?.llm?.endpoints || [];
  if (pick.ai === "openai" && !eps.some((e) => e.id === OPENAI_ENDPOINT.id)) llm.endpoints = [...eps, OPENAI_ENDPOINT];   // the whole list, as Settings saves it
  const next = await call("config:set", { companion: { llm } });
  if (next) live.config = next;
  return !!next;
}

// ---- 5. ready ----------------------------------------------------------------------------------------------
function drawReady(host: HTMLElement): void {
  const talk = pick.stt !== "off";
  host.innerHTML = head("Ready to play", "Glide is set. A few things in the game, and it starts coaching.")
    + `<div class="su-leaf"><h2>Before you play</h2><ol>
        ${addon?.wowOk ? "" : `<li><b>Set your game folder</b> on the Addon page: Glide couldn't install its addon without it.</li>`}
        <li><b>Close WoW completely and start it again.</b> A new addon only loads when the game starts; /reload isn't enough.</li>
        <li><b>On the character screen, open AddOns</b> and check that Glide and RestedXP are both enabled.</li>
        <li><b>Play in Windowed or Windowed (Fullscreen) mode</b> (Options &gt; Graphics), so Glide can see the game.</li>
        ${talk ? `<li><b>Hold ${esc(pick.pttKey)} and ask</b> "where do I go next?"</li>` : `<li><b>Ask the coach</b> in its chat whenever you're unsure.</li>`}
      </ol></div>
      <p class="help su-again">You can change any of this in Settings, or run this setup again from Settings &gt; General.</p>`;
}

const STEPS: Step[] = [
  { id: "game", title: "Your game", icon: "puzzle", draw: drawGame, next: nextGame },
  { id: "talk", title: "Talking", icon: "mic", draw: drawTalk, next: nextTalk },
  { id: "voice", title: "The coach's voice", icon: "audio-lines", draw: drawVoice, next: nextVoice },
  { id: "ai", title: "AI help", icon: "sparkles", draw: drawAi, next: nextAi },
  { id: "ready", title: "Ready", icon: "flag", draw: drawReady, next: async () => true },
];

// ---- the frame ---------------------------------------------------------------------------------------------
function drawSteps(): void {
  $("su-steps").innerHTML = STEPS.map((s, i) => `<li class="${i < at ? "done" : i === at ? "on" : ""}" ${i === at ? 'aria-current="step"' : ""}>
      <span class="su-n" aria-hidden="true">${i < at ? icon("check") : i + 1}</span><span>${esc(s.title)}</span>${i < at ? '<span class="sr"> (done)</span>' : ""}</li>`).join("");
}

async function go(i: number): Promise<void> {
  at = Math.max(0, Math.min(STEPS.length - 1, i));
  drawSteps();
  hint("");
  $<HTMLButtonElement>("su-back").hidden = at === 0;
  $("su-next").innerHTML = at === STEPS.length - 1 ? `${icon("play")}Start coaching` : "Continue";
  const body = $("su-body");
  body.scrollTop = 0;
  await STEPS[at].draw(body);
}

async function next(): Promise<void> {
  const b = $<HTMLButtonElement>("su-next");
  b.disabled = true;
  try {
    if (!(await STEPS[at].next())) return;
    if (at === STEPS.length - 1) return finish(true);
    await go(at + 1);
  } finally {
    b.disabled = false;
  }
}

/** Done or skipped: never shown again by itself; a finished setup restarts the coach on the new settings. */
async function finish(done: boolean): Promise<void> {
  stopMic();
  const info = await call("app:info");
  await updateAppSettings({ setupDone: String(info?.version || "done") });
  $("setup").hidden = true;
  document.body.classList.remove("in-setup");
  if (done) {
    await call(live.engineState === "running" || live.engineState === "starting" ? "engine:restart" : "engine:start");
    show("coach");
  } else if (!live.config.wow_dir) {
    show("addon");
  }
}

/** Open the setup at its first step, with the choices as they're saved now. */
export async function openSetup(): Promise<void> {
  live.config = (await call("config:get")) || live.config;
  const keys = await call("keys:list");
  known = keys?.known || [];
  keyState = keys?.status || [];
  const c: Obj = live.config.companion || {}, ptt: Obj = c.ptt || {}, llm: Obj = c.llm || {}, sp = live.appSettings.speech || {};
  pick.stt = ptt.enabled === false ? "off" : STT_KEY[ptt.engine] ? "online" : ptt.engine === "windows" ? "windows" : "auto";
  if (STT_KEY[ptt.engine]) pick.sttOnline = ptt.engine;
  pick.pttKey = ptt.key || "F8";
  pick.voice = c.voice?.enabled === false ? "off" : sp.provider && sp.provider !== "windows" ? "online" : "windows";
  if (sp.provider && sp.provider !== "windows") pick.tts = sp.provider;
  pick.voiceName = sp.voiceName || "";
  pick.edgeVoice = sp.edge?.voice || "";
  const fast = String(llm.fast_model || "");
  pick.ai = fast.startsWith("gemini:") && hasKey("GEMINI_API_KEY") ? "gemini" : fast.startsWith("ep:openai:") && hasKey("OPENAI_API_KEY") ? "openai"
    : fast.includes("/") && hasKey("OPENROUTER_API_KEY") ? "openrouter" : "anthropic";
  pick.models = null;
  document.body.classList.add("in-setup");
  $("setup").hidden = false;
  await go(0);
  $("su-next").focus();
}

export function wireSetup(): void {
  $("su-next").onclick = next;
  $("su-back").onclick = () => { stopMic(); go(at - 1); };
  $("su-skip").onclick = () => finish(false);
}
