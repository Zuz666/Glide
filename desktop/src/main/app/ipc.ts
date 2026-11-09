// What the windows can ask the main process (window.glide.invoke, preload.ts), by area, and the engine's events
// forwarded to them. Channel names and arguments are an interface: the renderer calls them as they are here.
import { app, dialog, ipcMain, shell } from "electron";
import fs from "fs";
import path from "path";
import * as addon from "../addon";
import { Engine } from "../engine";
import { EdgeTtsError } from "../edge-tts";
import { defaultHome, isCheckout, isDev, logsDir, pythonCommand, whisperDirs } from "../paths";
import { ResourceSampler } from "../resources";
import { keyStatus, KNOWN_KEYS, setKey } from "../secrets";
import { listWhisperModels, Speech } from "../speech";
import type { AppSettingsPatch, EventChannel, EventMap, InvokeArgs, InvokeChannel, InvokeResult, RxpGuideDetail, RxpGuideFacts, RxpImport, RxpImportResult, RxpRoutes, RxpRoutesRequest } from "../../shared/ipc";
import { appSettingsProblem, Obj } from "../store";
import { synth, TTS_KEYS, voices as ttsVoices } from "../tts";
import { Updater } from "../updater";
import { configPath, endpointKeyEnvs, engineHome, engineOptions, keyValue, readConfig, saveConfig } from "./config";
import { isOwnPage } from "./assets";
import { MainWindow } from "./main-window";
import { Overlay } from "./overlay";
import { settings } from "./settings";

/** The app's parts the handlers use, and the app-wide actions they share with the tray and the shortcuts. */
export interface IpcDeps {
  engine: Engine;
  updater: Updater;
  speech: Speech;
  sampler: ResourceSampler;
  mainWindow: MainWindow;
  overlay: Overlay;
  broadcast<K extends EventChannel>(channel: K, payload: EventMap[K]): void;
  setMuted(muted: boolean): void;
  pushMute(): void;
  installUpdate(): void;
  applyOverlaySettings(): void;
  updateTray(): void;
  ensureSpeech(force?: boolean): Promise<void>;
}

/** The built-in vendors' keys and the hosts they may go to (companion/openai_compat.py VENDOR_KEY_HOSTS). */
const VENDOR_KEY_HOSTS: Record<string, string[]> = {
  ANTHROPIC_API_KEY: ["api.anthropic.com"], OPENROUTER_API_KEY: ["openrouter.ai"],
  GEMINI_API_KEY: ["generativelanguage.googleapis.com"], GOOGLE_API_KEY: ["generativelanguage.googleapis.com"],
  OPENAI_API_KEY: ["api.openai.com"], GROQ_API_KEY: ["api.groq.com"], DEEPGRAM_API_KEY: ["api.deepgram.com"],
  TAVILY_API_KEY: [], BRAVE_API_KEY: [], ELEVENLABS_API_KEY: [], AZURE_SPEECH_KEY: [], GOOGLE_TTS_API_KEY: [],
};
function vendorKeyAllowed(keyEnv: string, baseUrl: string): boolean {
  const hosts = VENDOR_KEY_HOSTS[keyEnv];
  if (!hosts) return true;
  try { return hosts.includes(new URL(baseUrl).hostname.toLowerCase()); } catch { return false; }
}

/** Answer a channel; its arguments and answer are the ones shared/ipc.ts gives it (the renderer is typed the same).
 * Only Glide's own pages are answered (a page the window was somehow pointed at, or a frame, never is). */
const handle = <K extends InvokeChannel>(ch: K, fn: (...a: InvokeArgs<K>) => InvokeResult<K> | Promise<InvokeResult<K>>) =>
  ipcMain.handle(ch, (e, ...a) => {
    if (!e.senderFrame || e.senderFrame !== e.sender.mainFrame || !isOwnPage(e.senderFrame.url)) {
      throw new Error(`${ch}: not from one of Glide's own pages`);
    }
    return fn(...(a as InvokeArgs<K>));
  });

/** A folder on this PC (never a network share: opening or reading one sends Windows' sign-in to that server). */
const isUnc = (p: string) => /^[\\/]{2}/.test(String(p || ""));

export function registerIpc(d: IpcDeps): void {
  engineChannels(d);
  appChannels(d);
  configChannels(d);
  keyChannels();
  addonChannels(d);
  folderChannels(d);
  speechChannels(d);
  overlayChannels(d);
  forwardEngineEvents(d);
}

function engineChannels({ engine }: IpcDeps): void {
  handle("engine:get", () => ({ state: engine.state, detail: engine.detail, port: engine.port, url: engine.url, token: engine.token }));
  handle("engine:log", () => engine.log);
  handle("engine:start", () => engine.start(engineOptions()));
  handle("engine:stop", () => engine.stop());
  handle("engine:restart", () => engine.restart(engineOptions()));
}

/** The app itself: its info and settings, the voice, the footer's resource meter, updates and release notes. */
function appChannels(d: IpcDeps): void {
  const { engine, updater, sampler } = d;
  handle("app:info", () => ({
    version: app.getVersion(), dev: isDev, home: engineHome(), defaultHome, checkout: isCheckout(engineHome()),
    python: pythonCommand(), configPath: configPath(), electron: process.versions.electron,
  }));
  handle("app:settings:get", () => settings.current);
  handle("app:settings:set", (patch: AppSettingsPatch) => {
    if (patch && typeof patch !== "object") throw new Error("app settings take an object");
    if (patch.engineHome != null) {
      const home = String(patch.engineHome);
      if (isUnc(home) || !path.isAbsolute(home) || !fs.existsSync(home) || !fs.statSync(home).isDirectory()) {
        throw new Error(`the engine home must be an existing folder on this PC: ${home}`);
      }
    }
    const prevHome = engineHome();
    settings.merge(patch);
    d.applyOverlaySettings();
    d.updateTray();
    if (patch.autoUpdate !== undefined) updater.start(settings.current.autoUpdate !== false);
    if (engineHome() !== prevHome) engine.note(`engine home is now ${engineHome()} (restart the coach to use it)`);
    const problem = appSettingsProblem();
    if (problem) throw new Error(problem);           // applied for this run, not saved: the toast says why
    return settings.current;
  });
  handle("app:note", (line: string) => engine.note(String(line).slice(0, 300)));     // the window's own log lines (voice)
  handle("app:logs", async () => {
    // the Logs page on disk (userData/logs): written to only once there is a line, so it may not exist yet
    await engine.file?.flush();
    fs.mkdirSync(logsDir(), { recursive: true });
    return shell.openPath(logsDir());
  });
  handle("app:mute", (muted: boolean) => { d.setMuted(!!muted); return !!settings.current.muted; });
  handle("app:changelog", () => {
    try {
      return JSON.parse(fs.readFileSync(path.join(app.getAppPath(), "changelog.json"), "utf8"));
    } catch {
      return { versions: [] };
    }
  });

  const ttsKey = (provider: unknown) => {
    const env = TTS_KEYS[provider as keyof typeof TTS_KEYS];
    return env ? keyValue(env) : "";
  };
  let ttsSequence = 0, ttsActive = 0;
  handle("tts:synth", async (text: string, style?: string, tone?: string) => {
    const s = settings.current.speech || {}, id = ++ttsSequence, started = performance.now();
    const input = String(text || "").slice(0, 4000), provider = s.provider || "unknown";
    const voice = provider === "edge" ? s.edge?.voice : provider === "openai" ? s.openai?.voice
      : provider === "azure" ? s.azure?.voice : provider === "google" ? s.google?.voice : s.elevenlabs?.voiceId;
    ttsActive++;
    engine.note(`[voice] TTS #${id} start provider=${provider} voice=${JSON.stringify(String(voice || "default").slice(0, 120))} chars=${input.length} active=${ttsActive}`);
    try {
      const data = await synth(s, input, ttsKey(s.provider),
        String(style || "").slice(0, 1000), String(tone || "").slice(0, 200));
      engine.note(`[voice] TTS #${id} ready elapsed_ms=${Math.round(performance.now() - started)} audio_bytes=${data.length}`);
      return { mime: "audio/mpeg", data };
    } catch (error) {
      const detail = error instanceof EdgeTtsError
        ? { reason: error.reason, audio_bytes: error.audioBytes, exit_code: error.exitCode, http_status: error.httpStatus }
        : { reason: "ProviderError" };
      engine.note(`[voice] TTS #${id} failed elapsed_ms=${Math.round(performance.now() - started)} ${JSON.stringify(detail)}`);
      // Electron only transports the message; keep its request id for the renderer's fallback log.
      throw new Error(`TTS #${id}: ${error instanceof Error ? error.message : "synthesis failed"}`);
    } finally {
      ttsActive--;
    }
  });
  handle("tts:voices", (provider) => ttsVoices(provider, ttsKey(provider), settings.current.speech?.azure?.region));

  handle("res:watch", (on: boolean) => (on ? sampler.start() : sampler.stop()));
  handle("update:get", () => updater.status);
  handle("update:check", () => updater.check());
  handle("update:install", () => d.installUpdate());
  handle("update:download", () => updater.download());
}

function configChannels({ engine, overlay, ensureSpeech }: IpcDeps): void {
  handle("config:get", () => readConfig());
  handle("config:set", async (patch: Obj) => {
    const next = await saveConfig(patch, engine);      // through the running coach, which owns the file
    overlay.readDragKey();
    const companion = patch.companion as Obj | undefined;
    if (companion?.ptt || (companion?.voice as Obj | undefined)?.language !== undefined) ensureSpeech();
    return next;
  });
}

/** API keys (values go in, never out: the window sees only which are set) and custom endpoint checks. */
function keyChannels(): void {
  handle("keys:list", () => ({ known: KNOWN_KEYS, status: keyStatus(endpointKeyEnvs()) }));
  handle("keys:set", async (env: string, value: string) => {
    await setKey(env, value);
    return keyStatus(endpointKeyEnvs());
  });
  handle("keys:test", async (env: string, value?: string) => {
    const key = String(value || "").trim() || keyValue(String(env));
    if (!key) return { ok: false, error: "no key to test: paste one first" };
    return testModelKey(String(env), key);
  });
  handle("endpoint:test", async (baseUrl: string, keyEnv: string) => {
    const base = String(baseUrl || "").replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(base)) return { ok: false, error: "the address must start with https:// (or http:// for one on this PC)" };
    // a built-in vendor's key only goes to that vendor's own host (as the engine does: openai_compat.VENDOR_KEY_HOSTS)
    const key = vendorKeyAllowed(keyEnv, base) ? keyValue(keyEnv) : "";
    const url = base + "/models";
    try {
      const r = await fetch(url, { headers: key ? { authorization: `Bearer ${key}` } : {}, signal: AbortSignal.timeout(8000) });
      if (!r.ok) return { ok: false, error: `HTTP ${r.status}: ${(await r.text()).slice(0, 200)}` };
      const d = (await r.json()) as { data?: { id?: string }[]; models?: { id?: string; name?: string }[] };
      const ids = (d.data || d.models || []).map((m: any) => m.id || m.name).filter(Boolean);
      return { ok: true, models: ids.slice(0, 200) };
    } catch (e) {
      return { ok: false, error: String((e as Error).message || e) };
    }
  });
}

/** A model key against its own vendor: a key that lists models works. Answers the ids it can use, newest first where
 * the vendor orders them, in the engine's form (Gemini's as gemini:<id>, OpenAI's as ep:openai:<id>). OpenRouter's list is public, so its key is
 * checked on its own route. */
async function testModelKey(env: string, key: string): Promise<{ ok: boolean; models?: string[]; error?: string }> {
  const get = async (url: string, headers: Record<string, string>) => {
    const r = await fetch(url, { headers, signal: AbortSignal.timeout(10000) });
    if (r.status === 401 || r.status === 403) throw new Error("the key was refused: check it was copied whole");
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return (await r.json()) as any;
  };
  try {
    if (env === "ANTHROPIC_API_KEY") {
      const d = await get("https://api.anthropic.com/v1/models?limit=100", { "x-api-key": key, "anthropic-version": "2023-06-01" });
      return { ok: true, models: (d.data || []).map((m: any) => m.id).filter(Boolean) };
    }
    if (env === "GEMINI_API_KEY" || env === "GOOGLE_API_KEY") {
      const d = await get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", { "x-goog-api-key": key });
      return { ok: true, models: (d.models || []).map((m: any) => String(m.name || "").replace(/^models\//, "gemini:")).filter((id: string) => id !== "gemini:") };
    }
    if (env === "OPENAI_API_KEY") {
      // OpenAI's chat models reach the coach as the "openai" custom endpoint (setup.ts adds it)
      const d = await get("https://api.openai.com/v1/models", { authorization: `Bearer ${key}` });
      return { ok: true, models: (d.data || []).map((m: any) => m.id && `ep:openai:${m.id}`).filter(Boolean) };
    }
    if (env === "OPENROUTER_API_KEY") {
      await get("https://openrouter.ai/api/v1/key", { authorization: `Bearer ${key}` });
      const d = await get("https://openrouter.ai/api/v1/models", {});
      return { ok: true, models: (d.data || []).map((m: any) => m.id).filter(Boolean) };
    }
    return { ok: false, error: `${env} isn't a model key Glide can test` };
  } catch (e) {
    return { ok: false, error: String((e as Error).message || e) };
  }
}

/** companion/rxp_import.py add|status|sync in the engine home (the string, for add, on its stdin). */
function rxpTool(engine: Engine, cmd: "add" | "status" | "sync", input = ""): Promise<Record<string, unknown>> {
  return engine.pythonJson([path.join("companion", "rxp_import.py"), cmd], input, engineOptions());
}

function addonChannels({ engine }: IpcDeps): void {
  handle("addon:status", () => addon.status(engineHome(), (readConfig().wow_dir as string) || null));
  handle("addon:detect", () => addon.detectWow());
  handle("addon:install", async () => {
    const wow = readConfig().wow_dir as string;
    if (!wow) throw new Error("set the WoW folder first");
    const done = addon.install(engineHome(), wow);
    done.forEach((line) => engine.note(line));
    // the install replaced the addon's folder: put back the RestedXP strings it hasn't handed over yet
    try {
      await rxpTool(engine, "sync");
    } catch (e) {
      engine.note(`RestedXP guides for the addon: ${(e as Error).message}`);
    }
    return { done, status: addon.status(engineHome(), wow) };
  });
  handle("addon:rxpImport", async (text: string) => {
    const r = (await rxpTool(engine, "add", String(text || ""))) as unknown as RxpImportResult;
    engine.note(r.ok ? `RestedXP guide string added: ${(r.guides || []).length} guides${r.handoff ? ", waiting in the addon for RestedXP" : ""}`
      : `RestedXP guide string not added: ${r.error}`);
    return r;
  });
  handle("addon:rxpImports", async (id?: string, index?: number) => {
    if (id !== undefined && index !== undefined) {
      const r = await engine.pythonJson([path.join("companion", "rxp_import.py"), "guide", String(id), String(index)], "", engineOptions());
      const g = (r.guide || null) as RxpGuideDetail | null;
      if (g && Array.isArray(g.zones)) {
        // the zone images by URL (glide://map/<id>.png, main.ts), for the zones and continents that have one
        const maps: Record<number, string> = {};
        const ids = new Set<number>(g.zones.map((z) => z.ui_map));
        for (const m of Object.values(g.maps || {})) if (m && m.parent) ids.add(m.parent);
        for (const id of ids) {
          for (const dir of [engineHome(), path.join(__dirname, "..", "..", "..", "..")]) {
            if (fs.existsSync(path.join(dir, "data", "gamedata", "maps", `${id}.png`))) { maps[id] = `glide://map/${id}.png`; break; }
          }
        }
        g.mapImages = maps;
      }
      return g;
    }
    return ((await rxpTool(engine, "status")).imports || []) as RxpImport[];
  });
  // walking routes between a guide's steps: its own short process at below-normal priority (rxp_routes.py), so the
  // live coach's walking map stays loaded; one request in flight per guide is the Guides page's job
  handle("addon:rxpRoutes", async (id: string, index: number, req: RxpRoutesRequest) => {
    try {
      const r = await engine.pythonJson([path.join("companion", "rxp_import.py"), "routes", String(id), String(index)],
        JSON.stringify(req || {}), engineOptions());
      if (r.ok && r.routes) return r.routes as RxpRoutes;
      return { legs: [], pending: [], nav: {}, error: String(r.error || "no answer") };
    } catch (e) {
      return { legs: [], pending: [], nav: {}, error: (e as Error).message };
    }
  });

  // what the knowledge base knows about one guide (quest chains, map targets, prep, plan, hazards); slower than the
  // guide itself, so the page asks after drawing it. "-" leaves the class or race unpicked.
  handle("addon:rxpFacts", async (id: string, index: number, cls?: string, race?: string) => {
    const r = await engine.pythonJson([path.join("companion", "rxp_import.py"), "facts", String(id), String(index),
      String(cls || "-"), String(race || "-")], "", engineOptions());
    return (r.facts || null) as RxpGuideFacts | null;
  });
}

/** Folders: pick one, open one (or a web page) outside the app, import another Glide folder's data. */
function folderChannels({ engine, mainWindow }: IpcDeps): void {
  handle("dialog:folder", async (title: string, defaultPath?: string) => {
    const r = await dialog.showOpenDialog(mainWindow.win!, { title, defaultPath, properties: ["openDirectory"] });
    return r.canceled ? null : r.filePaths[0];
  });
  // A web page, a local folder, or the exact Windows speech settings page; never arbitrary schemes or files.
  handle("shell:open", (target: string) => {
    const t = String(target || "");
    if (process.platform === "win32" && t === "ms-settings:speech") return shell.openExternal(t);
    if (/^https?:\/\//i.test(t)) return shell.openExternal(t);
    if (isUnc(t) || !path.isAbsolute(t) || !fs.existsSync(t) || !fs.statSync(t).isDirectory()) {
      throw new Error(`only a folder on this PC or a web page opens from Glide: ${t}`);
    }
    return shell.openPath(t);
  });
  handle("data:import", async (from: string) => {
    // copy another Glide folder's data/ and config.json into the engine home (the old folder is left as it is)
    if (isUnc(from)) throw new Error("import from a folder on this PC, not a network share");
    const src = path.resolve(from), dst = engineHome();
    if (path.resolve(src) === path.resolve(dst)) throw new Error("that is the engine home already");
    if (!fs.existsSync(path.join(src, "data"))) throw new Error(`${src} has no data folder`);
    const wasRunning = engine.state === "running";
    await engine.stop();
    fs.cpSync(path.join(src, "data"), path.join(dst, "data"), { recursive: true, force: true });
    if (fs.existsSync(path.join(src, "config.json"))) fs.copyFileSync(path.join(src, "config.json"), configPath());
    engine.note(`imported data/ and config.json from ${src}`);
    if (wasRunning) engine.start(engineOptions());
    return true;
  });
}

/** Offline speech recognition (Whisper): its state, repair, and the models on this PC. */
function speechChannels({ speech, ensureSpeech }: IpcDeps): void {
  handle("speech:status", () => speech.status);
  handle("speech:install", async () => {
    await ensureSpeech(true);
    return speech.status;
  });
  handle("speech:models", () => listWhisperModels(whisperDirs()));
  handle("speech:download", async (name: string) => {
    await speech.download(engineOptions(), String(name));
    return listWhisperModels(whisperDirs());
  });
  handle("speech:delete", (name: string) => ({ ok: speech.remove(String(name)), models: listWhisperModels(whisperDirs()) }));
}

function overlayChannels({ overlay }: IpcDeps): void {
  handle("overlay:unlock", (on: boolean) => overlay.setUnlocked(!!on));
  handle("overlay:resize", (height: number) => overlay.fitHeight(height));
  handle("overlay:dragStart", () => overlay.startDrag());
  handle("overlay:width", (delta: number) => overlay.changeWidth(delta));
}

function forwardEngineEvents(d: IpcDeps): void {
  d.engine.on("state", (s) => {
    d.broadcast("engine:state", s);
    if (s.state === "running" && settings.current.muted) d.pushMute();     // a (re)started coach starts unmuted
    d.updateTray();
  });
  d.engine.on("log", (l) => d.mainWindow.send("engine:logline", l));
}
