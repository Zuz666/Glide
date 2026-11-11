// Glide to 60 desktop app: one window for the coach console, settings, addon and logs, a click-through overlay
// over the game, a tray icon, and the Python engine running behind them.
//
// This file puts the parts together (app/: the windows, the tray, the IPC channels) and runs the app's life:
// start, the actions shared by the tray, the shortcuts and the windows, and quitting.
import { app, globalShortcut, Menu, net, protocol, screen } from "electron";
import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";
import type { EventChannel, EventMap } from "../shared/ipc";
import * as addon from "./addon";
import { Engine } from "./engine";
import { EdgeTts } from "./edge-tts";
import { GameIcons, iconId } from "./game-icons";
import { LogFile } from "./logfile";
import { isDev, logsDir, shippedEngine, syncEngine } from "./paths";
import { ResourceSampler } from "./resources";
import { loadSecrets, secretsProblem } from "./secrets";
import { showUpdateSplash } from "./splash";
import { Speech } from "./speech";
import { appSettingsProblem } from "./store";
import { Updater } from "./updater";
import { WindowsTts } from "./windows-tts";
import { ICON } from "./app/assets";
import { companionSection, engineHome, engineOptions, readConfig, updateConfig } from "./app/config";
import { registerIpc } from "./app/ipc";
import { MainWindow } from "./app/main-window";
import { Overlay } from "./app/overlay";
import { settings } from "./app/settings";
import { AppTray, MUTE_SHORTCUT, OVERLAY_SHORTCUT } from "./app/tray";

// glide://map/<uiMapID>.png: the zone images the engine extracted from the player's own client
// (data/gamedata/maps), loaded by URL so a guide doesn't ship them over IPC; registered before the app is ready.
// glide://icon/<fileDataID>.png: the Guides page's game icons (data/gamedata/icons, decoded on a miss: game-icons.ts)
protocol.registerSchemesAsPrivileged([{ scheme: "glide", privileges: { standard: true, secure: true } }]);

const gameDirs = () => [engineHome(), path.join(__dirname, "..", "..", "..")];
const gameIcons = new GameIcons(gameDirs, async (fdids) => {
  const r = await engine.pythonJson([path.join("companion", "rxp_import.py"), "icons"], JSON.stringify({ fdids }), engineOptions());
  if (!r.ok) throw new Error(String(r.error || "no answer"));
  return (r.ready || []) as number[];
});

/** Answer glide://map/<id>.png from the engine home's map images (else the development checkout's), and
 * glide://icon/<id>.png from the decoded game icons. */
function serveGlideScheme(): void {
  protocol.handle("glide", async (req) => {
    const u = new URL(req.url);
    const icon = iconId(u.host, u.pathname);
    if (icon != null) {
      const p = await gameIcons.get(icon);
      if (!p) return new Response("no icon", { status: 404 });
      const r = await net.fetch(pathToFileURL(p).href);
      return new Response(r.body, { headers: { "content-type": "image/png", "cache-control": "max-age=31536000, immutable" } });
    }
    const m = u.host === "map" ? /^\/(\d{1,6})\.png$/.exec(u.pathname) : null;
    if (!m) return new Response("not found", { status: 404 });
    for (const dir of gameDirs()) {
      const p = path.join(dir, "data", "gamedata", "maps", `${m[1]}.png`);
      if (fs.existsSync(p)) return net.fetch(pathToFileURL(p).href);
    }
    return new Response("no map image", { status: 404 });
  });
}

let quitting = false;
const engine = new Engine();
engine.file = new LogFile(logsDir());
const updater = new Updater((m) => engine.note(m));
const speechMgr = new Speech(engine);
const windowsTts = new WindowsTts(path.join(shippedEngine, "companion", "sapi5.ps1"),
  (line, attribution) => engine.note(line, attribution));
const edgeTts = new EdgeTts(() => path.join(engineHome(), "companion", "edge_tts_worker.py"),
  (line, attribution) => engine.note(line, attribution));
const sampler = new ResourceSampler();

const mainWindow = new MainWindow(() => !quitting && !!settings.current.closeToTray && tray.exists);
const overlay = new Overlay(broadcast, () => tray.update());
const tray = new AppTray(engine, updater, overlay, {
  showMain: () => mainWindow.show(),
  startEngine: () => engine.start(engineOptions()),
  stopEngine: () => engine.stop(),
  restartEngine: () => engine.restart(engineOptions()),
  setMuted,
  showOverlay: (on) => {
    settings.current.overlay = { ...settings.current.overlay, enabled: on };
    settings.save();
    applyOverlaySettings();
    tray.update();
  },
  installUpdate,
  quit: () => { quitting = true; app.quit(); },
});

sampler.on("sample", (s) => mainWindow.send("res:sample", s));

/** Send to every open window (the main window, then the overlay). */
function broadcast<K extends EventChannel>(channel: K, payload: EventMap[K]): void {
  mainWindow.send(channel, payload);
  overlay.send(channel, payload);
}

function applyOverlaySettings(): void {
  overlay.sync();
  broadcast("app:settings", settings.current);
}

// ---- mute the narrator --------------------------------------------------------------------------------------
/** Tell the coach whether the narrator is muted, when the coach speaks itself (voice output "windows": its own
 * speech process). With output "app" (the default) mute is the app's alone: the window's narrator drops the coach's
 * lines while muted, so the engine isn't told. */
function pushMute(): void {
  if (!engine.url || engine.state !== "running") return;
  if ((companionSection("voice").output || "app") === "app") return;
  fetch(engine.url + "/mute", { method: "POST", headers: { "X-Glide-Token": engine.token }, body: JSON.stringify({ muted: !!settings.current.muted }),
                                signal: AbortSignal.timeout(5000) }).catch(() => undefined);
}

function setMuted(muted: boolean): void {
  settings.current.muted = muted;
  settings.save();
  if (muted) windowsTts.stop();
  const sp = settings.current.speech;
  windowsTts.configure(!muted && (sp?.provider || "windows") === "windows", sp?.voiceName);
  edgeTts.configure(!muted && sp?.provider === "edge");
  broadcast("app:settings", settings.current);         // the window's voices stop and stay quiet
  pushMute();
  tray.update();
}

// ---- silent self-update ----------------------------------------------------------------------------------
/** Install the downloaded update now (the user clicked Restart to update): Glide's "Updating Glide" window shows
 * while the installer runs silently, then Glide starts again (in the tray if it was hidden there). */
let installing = false;                  // a second click while it starts installing does nothing
function installUpdate(): void {
  if (updater.status.state !== "ready" || installing) return;
  installing = true;
  settings.current.resumeHidden = mainWindow.outOfSight;
  settings.save();
  engine.note(`[update] installing Glide ${updater.status.version} and restarting`);
  // the installer runs silently; Glide's own "Updating Glide" window shows instead, over where Glide's window is,
  // until the new version is up. Glide closes once that window is on screen (or after 5 s whatever happens).
  const win = mainWindow.win && !mainWindow.win.isDestroyed() && mainWindow.win.isVisible() ? mainWindow.win : null;
  let centre: { x: number; y: number } | undefined;
  if (win) {
    const r = screen.dipToScreenRect(win, win.getBounds());
    centre = { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
  const install = () => {
    if (!updater.installNow()) {
      installing = false;
      settings.current.resumeHidden = false;
      settings.save();
    }
  };
  try {
    showUpdateSplash(String(updater.status.version || ""), ICON, centre).then((shown) => {
      if (!shown) engine.note("[update] the update window didn't show in time; installing anyway");
      install();
    });
  } catch (e) {
    engine.note(`[update] couldn't show the update window: ${(e as Error).message}`);
    install();
  }
}

/** Offline speech recognition: find it, install / download what's missing, restart the coach to use it. */
function ensureSpeech(force = false): Promise<void> {
  return speechMgr.ensure(engineOptions(), companionSection("ptt"), () => {
    if (engine.state === "running") {
      engine.note("[speech] restarting the coach so push-to-talk uses the new speech model");
      engine.restart(engineOptions());
    }
  }, force, companionSection("voice").language);
}

// ---- lifecycle --------------------------------------------------------------------------------------------
/** A fresh install: find the WoW client before the first start, so the first knowledge build already reads
 * RestedXP's guides (prefer a client with RestedXP installed). */
async function findWowOnce(): Promise<void> {
  if (readConfig().wow_dir) return;
  await addon.detectWow().then((found) => {
    const withRxp = found.find((f) => fs.existsSync(path.join(f.path, "Interface", "AddOns", "RXPGuides")));
    const pick = withRxp || (found.length === 1 ? found[0] : null);
    if (pick) {
      updateConfig({ wow_dir: pick.path });         // before the coach starts: the app writes config.json itself
      engine.note(`found World of Warcraft (${pick.flavor}) at ${pick.path}${withRxp ? ", with RestedXP" : ""}`);
    } else {
      engine.note(found.length ? "several WoW clients found: pick one under Addon" : "no WoW install found: set the folder under Addon");
    }
  }).catch((e) => engine.note(`could not save the WoW folder found: ${(e as Error).message}`));
}

/** First start after an update: say so once. */
function announceUpdate(): void {
  const s = settings.current;
  if (s.lastVersion && s.lastVersion !== app.getVersion()) {
    const msg = `Glide updated to ${app.getVersion()} (from ${s.lastVersion}).`;
    engine.note(`[update] ${msg}`);
    mainWindow.win?.webContents.once("did-finish-load", () => broadcast("app:notice", msg));
  }
  if (s.lastVersion !== app.getVersion()) {
    s.lastVersion = app.getVersion();
    settings.save();
  }
}

async function start(): Promise<void> {
  Menu.setApplicationMenu(null);   // no File/Edit menu bar popping up on Alt (the overlay's drag key)
  settings.load();
  serveGlideScheme();
  // first-run setup is for new installs: a player whose WoW folder was set before it existed doesn't get it
  if (!settings.current.setupDone && readConfig().wow_dir) {
    settings.current.setupDone = "before-setup";
    settings.save();
  }
  engine.file?.header("app start", `Glide ${app.getVersion()}${isDev ? " (dev)" : ""}`, `Electron ${process.versions.electron}`);
  for (const problem of [appSettingsProblem(), secretsProblem()]) if (problem) engine.note(`[settings] ${problem}`);
  await loadSecrets().catch((e: Error) => engine.note(`[settings] the saved API keys can't be read: ${e.message}`));
  try {
    syncEngine(engineHome(), app.getVersion()).forEach((d) => engine.note(d));
  } catch (e) {
    engine.note(`could not prepare the engine home ${engineHome()}: ${(e as Error).message}`);
  }
  const sp0 = settings.current.speech;
  windowsTts.configure(!settings.current.muted && (sp0?.provider || "windows") === "windows", sp0?.voiceName);
  edgeTts.configure(!settings.current.muted && sp0?.provider === "edge");
  registerIpc({ engine, updater, speech: speechMgr, windowsTts, edgeTts, sampler, mainWindow, overlay, broadcast, setMuted, pushMute,
                installUpdate, applyOverlaySettings, updateTray: () => tray.update(), ensureSpeech });
  mainWindow.create();
  mainWindow.win!.webContents.on("render-process-gone", () => windowsTts.stop());
  mainWindow.win!.webContents.once("destroyed", () => windowsTts.stop());
  mainWindow.win!.webContents.on("did-start-navigation", (_event, _url, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) windowsTts.stop();
  });
  tray.create();
  applyOverlaySettings();
  globalShortcut.register(OVERLAY_SHORTCUT, () => overlay.setUnlocked(!overlay.unlocked));
  globalShortcut.register(MUTE_SHORTCUT, () => setMuted(!settings.current.muted));
  overlay.readDragKey();
  if (!overlay.startKeyPolling()) engine.note("key polling unavailable: move the overlay with Ctrl+Alt+O");
  await findWowOnce();
  if (settings.current.autoStartEngine !== false) engine.start(engineOptions());
  speechMgr.on("status", (s) => broadcast("speech:status", s));
  setTimeout(() => ensureSpeech(), 3000);
  updater.on("status", (s) => {
    broadcast("update:status", s);
    tray.update();
  });
  updater.start(settings.current.autoUpdate !== false);
  announceUpdate();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => mainWindow.show());
  app.whenReady().then(start);
  app.on("before-quit", () => { quitting = true; sampler.stop(); windowsTts.dispose(); edgeTts.close(); });
  app.on("quit", () => engine.file?.flushSync());        // the last lines (the coach stopping) reach the file
  // stop the coach first, then quit for real (app.quit, not app.exit: a downloaded update installs on quit)
  let engineStopped = false;
  app.on("will-quit", async (e) => {
    globalShortcut.unregisterAll();
    if (!engineStopped && engine.state !== "stopped" && engine.state !== "crashed") {
      e.preventDefault();
      await engine.stop();
      engineStopped = true;
      app.quit();
    }
  });
  app.on("window-all-closed", () => {
    if (!settings.current.closeToTray) app.quit();
  });
}
