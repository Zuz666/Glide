// Desktop main-process checks without Electron (test_desktop_main.py runs this): the engine home sync, starting and
// stopping the coach, and config.json writes. A stand-in `electron` module, as in golden/desktop_probe.cjs.
// node desktop_main_check.cjs <root>: <root>/desktop/dist/main holds the compiled main process, so <root> is the
// app's "project folder" (paths.devRoot) and the shipped engine; the test makes a fake one there.
const Module = require("module");
const assert = require("assert");
const child = require("child_process");
const fs = require("fs");
const path = require("path");
const root = path.resolve(process.argv[2]);
const dist = path.join(root, "desktop", "dist", "main");
const userData = fs.mkdtempSync(path.join(root, "userdata-"));
const handlers = new Map();
const electron = new Proxy({
  app: { getPath: () => userData, getVersion: () => "1.0.0", isPackaged: false, getAppPath: () => path.dirname(dist),
         on() {}, whenReady: () => new Promise(() => {}), requestSingleInstanceLock: () => true },
  ipcMain: { handle(name, fn) { handlers.set(name, fn); }, on() {} }, safeStorage: { isEncryptionAvailable: () => false },
}, { get: (t, k) => (k in t ? t[k] : new Proxy(function () {}, { get: () => () => {} })) });
const resolve = Module._resolveFilename;
Module._resolveFilename = function (req, ...a) { return req === "electron" ? "electron" : resolve.call(this, req, ...a); };
require.cache.electron = { id: "electron", filename: "electron", loaded: true, exports: electron };

// what Python the engine gets: one that doesn't exist, or a stand-in that says it's ready and exits when stdin closes
let python = "missing";
const realSpawn = child.spawn;
child.spawn = (cmd, args, opts) => python === "missing"
  ? realSpawn("glide-no-such-python-xyz", args, opts)
  : realSpawn(process.execPath, ["-e", "console.log('GLIDE_READY port=1'); process.stdin.resume(); process.stdin.on('end', () => process.exit(0))"], opts);

const write = (p, text = "x") => { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, text); };
const read = (p) => fs.readFileSync(p, "utf8");
const home = (name) => path.join(root, "homes", name);
const CODE = ["companion", "knowledge", "tools", "wowfiles", "addon"];
for (const d of CODE) write(path.join(root, d, d === "companion" ? "app.py" : "shipped.txt"), "shipped");

const paths = require(path.join(dist, "paths.js"));
const { Engine } = require(path.join(dist, "engine.js"));
const { LogFile } = require(path.join(dist, "logfile.js"));
const store = require(path.join(dist, "store.js"));
const { settings } = require(path.join(dist, "app", "settings.js"));
const config = require(path.join(dist, "app", "config.js"));

const until = async (what, fn, ms = 5000) => {
  const t0 = Date.now();
  while (!fn()) {
    if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 20));
  }
};
// the time limit is cleared once p settles: a pending one would keep Node running (6 s after the last check)
const within = (p, ms, what) => {
  let timer;
  const limit = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`${what} took over ${ms} ms`)), ms); });
  return Promise.race([p, limit]).finally(() => clearTimeout(timer));
};

function syncChecks() {
  // an empty folder gets the shipped code and a stamp
  const fresh = home("fresh");
  paths.syncEngine(fresh, "1.0.0");
  assert.strictEqual(read(path.join(fresh, "companion", "app.py")), "shipped");
  assert.strictEqual(read(path.join(fresh, ".engine-version")), "1.0.0");

  // code folders Glide didn't install (no stamp): refused, nothing replaced or stamped
  const own = home("own");
  write(path.join(own, "companion", "mine.py"), "my edits");
  assert.throws(() => paths.syncEngine(own, "1.0.0"), /won't replace them/);
  assert.strictEqual(read(path.join(own, "companion", "mine.py")), "my edits");
  assert.ok(!fs.existsSync(path.join(own, ".engine-version")));
  assert.ok(!fs.existsSync(path.join(own, "tools")));

  // a home Glide set up for another version: replaced, as before
  const old = home("old");
  write(path.join(old, ".engine-version"), "0.9.0");
  for (const d of CODE) write(path.join(old, d, "old.txt"), "old");
  write(path.join(old, "config.json"), "{}");
  assert.deepStrictEqual(paths.syncEngine(old, "1.0.0"), ["engine code 0.9.0 -> 1.0.0"]);
  assert.ok(!fs.existsSync(path.join(old, "companion", "old.txt")));
  assert.strictEqual(read(path.join(old, "companion", "app.py")), "shipped");
  assert.strictEqual(read(path.join(old, "config.json")), "{}");

  // an engine home from before wowfiles/ existed (stamped, the rest there): the shipped wowfiles/ is copied in
  const pre = home("pre-wowfiles");
  paths.syncEngine(pre, "1.0.0");
  fs.rmSync(path.join(pre, "wowfiles"), { recursive: true });
  paths.syncEngine(pre, "1.0.0");
  assert.strictEqual(read(path.join(pre, "wowfiles", "shipped.txt")), "shipped");

  // an install that was cut short is still Glide's own
  const cut = home("cut");
  write(path.join(cut, ".engine-version"), "installing");
  write(path.join(cut, "companion", "half.txt"));
  paths.syncEngine(cut, "1.0.0");
  assert.ok(!fs.existsSync(path.join(cut, "companion", "half.txt")));
  assert.strictEqual(read(path.join(cut, ".engine-version")), "1.0.0");
}

async function engineChecks() {
  const opts = (h) => ({ home: h, env: {}, sim: true });

  // Python can't be launched: crashed with the reason (not stuck "starting"), stop() returns, Start tries again
  python = "missing";
  const e = new Engine();
  await e.start(opts(root));
  await until("crashed", () => e.state === "crashed");
  assert.match(e.detail, /could not start Python/);
  await within(e.stop(), 1000, "stop() after a failed launch");
  assert.strictEqual(e.state, "stopped");
  await e.start(opts(root));
  await until("crashed again", () => e.state === "crashed");
  await within(e.stop(), 1000, "stop()");

  // a coach that runs: ready, then stop() waits for it to exit
  python = "fake";
  const ok = new Engine();
  ok.file = new LogFile(path.join(userData, "logs"));
  await ok.start(opts(root));
  await until("running", () => ok.state === "running");
  await within(ok.stop(), 6000, "stop() of a running coach");
  assert.strictEqual(ok.state, "stopped");
  // ... and its output and the app's lines are on disk too, after a header naming what ran
  await ok.file.flush();
  const logs = fs.readdirSync(path.join(userData, "logs"));
  assert.strictEqual(logs.length, 1);
  const onDisk = read(path.join(userData, "logs", logs[0]));
  assert.ok(onDisk.includes(`==== coach start | app: 1.0.0 | engine home: ${root} | python: `), onDisk);
  assert.match(onDisk, /\[out\] GLIDE_READY port=1$/m);
  assert.match(onDisk, /==== coach ready \| GLIDE_READY port=1$/m);
  assert.match(onDisk, /\[app\] the coach exited/);
  ok.file = null;

  // a new engine home gets the engine code when the coach starts, not only when the app does
  const moved = home("moved");
  await ok.start(opts(moved));
  await until("running in the new home", () => ok.state === "running");
  assert.strictEqual(read(path.join(moved, "companion", "app.py")), "shipped");
  await within(ok.stop(), 6000, "stop()");

  // a home with someone's own code: refused, with the reason as the coach's state
  const own = home("own");
  await ok.start(opts(own));
  assert.strictEqual(ok.state, "crashed");
  assert.match(ok.detail, /won't replace them/);
  assert.strictEqual(read(path.join(own, "companion", "mine.py")), "my edits");
  await within(ok.stop(), 1000, "stop()");
}

async function configChecks() {
  const h = home("config");
  fs.mkdirSync(h, { recursive: true });
  settings.current = { engineHome: h };
  const p = path.join(h, "config.json");
  const bads = () => fs.readdirSync(h).filter((f) => f.startsWith("config.json.") && f.endsWith(".bad"));

  // missing: just {}, and the first change creates the file
  assert.deepStrictEqual(store.readJsonFile(p), { value: {} });
  config.updateConfig({ wow_dir: "C:/WoW" });
  assert.deepStrictEqual(JSON.parse(read(p)), { wow_dir: "C:/WoW" });

  // unreadable: the change is refused, the file stays, one .bad copy is kept (not one per attempt)
  const broken = '{"wow_dir": "C:/WoW", "companion": {"llm": {"fast_model": "x",}}}';
  write(p, broken);
  assert.ok(store.readJsonFile(p).bad);
  assert.deepStrictEqual(store.readJson(p), {});
  assert.throws(() => config.updateConfig({ companion: { voice: { rate: 2 } } }), /config\.json can't be read/);
  assert.throws(() => config.updateConfig({ wow_dir: "D:/WoW" }), /wasn't saved/);
  assert.strictEqual(read(p), broken);
  assert.strictEqual(bads().length, 1);
  assert.strictEqual(read(path.join(h, bads()[0])), broken);

  // valid JSON that isn't an object counts as unreadable too
  write(p, "[1]");
  assert.throws(() => config.updateConfig({ wow_dir: "D:/WoW" }), /not a JSON object/);
  assert.strictEqual(read(p), "[1]");

  // fixed (with a BOM, as Notepad saves it): changes merge again
  write(p, "\uFEFF" + JSON.stringify({ wow_dir: "C:/WoW", companion: { llm: { fast_model: "x" } } }));
  config.updateConfig({ companion: { voice: { rate: 2 } } });
  assert.deepStrictEqual(JSON.parse(read(p)), { wow_dir: "C:/WoW", companion: { llm: { fast_model: "x" }, voice: { rate: 2 } } });

  // Model ids -> Save and restart while stopped writes locally, without the engine's /settings handler.
  write(p, JSON.stringify({ companion: { llm: {
    fast_model: "ep:openai:gpt-6-luna", fast_reasoning_effort: "none",
    deep_model: "ep:openai:gpt-6-luna", deep_reasoning_effort: "low",
  } } }));
  const stopped = { state: "stopped", url: null, token: "", note() {} };
  await config.saveConfig({ companion: { llm: { fast_model: "ep:openai:gpt-6.1-sol" } } }, stopped);
  const restarted = config.readConfig().companion.llm;
  assert.strictEqual(restarted.fast_model, "ep:openai:gpt-6.1-sol");
  assert.strictEqual(restarted.fast_reasoning_effort ?? "auto", "auto");
  assert.strictEqual(restarted.deep_reasoning_effort, "low");
  await config.saveConfig({ companion: { llm: {
    fast_model: "ep:openai:gpt-6-luna", fast_reasoning_effort: "low",
  } } }, stopped);
  await config.saveConfig({ companion: { voice: { volume: 80 } } }, stopped);
  assert.strictEqual(config.readConfig().companion.llm.fast_reasoning_effort, "low");
  // Stopped-coach local save rejects excluded official OpenAI specialty models on the edited role,
  // while allowing edits to the other role and allowing the same name on non-OpenAI compatible origins.
  write(p, JSON.stringify({ companion: { llm: {
    endpoints: [{ id: "openai", base_url: "https://api.openai.com/v1" },
                { id: "local", base_url: "http://127.0.0.1:11434/v1" }],
    fast_model: "ep:openai:tts-1",
    deep_model: "ep:openai:gpt-6.1-terra",
  } } }));
  const beforeStoppedEx = read(p);
  await assert.rejects(
    config.saveConfig({ companion: { llm: { deep_model: "ep:openai:gpt-4o-mini-tts" } } }, stopped),
    /speech synthesis/
  );
  assert.strictEqual(read(p), beforeStoppedEx);
  await config.saveConfig({ companion: { llm: { deep_model: "ep:openai:gpt-5.6-luna" } } }, stopped);
  assert.strictEqual(config.readConfig().companion.llm.deep_model, "ep:openai:gpt-5.6-luna");
  await config.saveConfig({ companion: { llm: { fast_model: "ep:local:tts-1" } } }, stopped);
  assert.strictEqual(config.readConfig().companion.llm.fast_model, "ep:local:tts-1");
  // Stopped-coach local save also honors valid past shutdown_date in cached data/live/models.json (ignoring invalid dates).
  const liveDir = path.join(h, "data", "live");
  fs.mkdirSync(liveDir, { recursive: true });
  write(path.join(liveDir, "models.json"), JSON.stringify({
    models: [
      { id: "ep:openai:gpt-5", shutdown_date: "2020-01-01" },
      { id: "ep:openai:gpt-5.1", shutdown_date: "2020-02-30" },
    ],
  }));
  await assert.rejects(
    config.saveConfig({ companion: { llm: { deep_model: "ep:openai:gpt-5" } } }, stopped),
    /shut down on 2020-01-01/
  );
  await config.saveConfig({ companion: { llm: { deep_model: "ep:openai:gpt-5.1" } } }, stopped);
  assert.strictEqual(config.readConfig().companion.llm.deep_model, "ep:openai:gpt-5.1");

  // A mixed Save applies the live model while normalizing the language fallback in the same write.
  const http = require("http");
  let selectedModel = "a-model", refuse = false;
  write(p, JSON.stringify({ companion: {
    llm: { fast_model: selectedModel }, voice: { language: "en" },
    ptt: { whisper_model: "small.en" }, overlay: { drag_key: "ALT" },
  } }));
  const server = http.createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    if (refuse || body.fast_model !== "c-model") {
      res.writeHead(400, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ ok: false, error: "live selection refused" }));
    }
    write(p, JSON.stringify(store.deepMerge(JSON.parse(read(p)), body.save)));
    selectedModel = body.fast_model;
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, applied: { saved: true, fast_model: selectedModel } }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const running = { state: "running", url: `http://127.0.0.1:${server.address().port}`, token: "test", note() {} };
  const mixed = { companion: {
    llm: { fast_model: "c-model" }, voice: { language: "ru" },
    overlay: { drag_key: "CTRL" },
  } };
  try {
    const next = await config.saveConfig(mixed, running, { fast_model: "c-model" });
    assert.strictEqual(selectedModel, "c-model", "mixed Save must apply its model, not silently save for restart");
    assert.strictEqual(next.companion.ptt.whisper_model, "small", "Russian needs multilingual Whisper");
    assert.strictEqual(next.companion.voice.language, "ru");
    assert.strictEqual(next.companion.overlay.drag_key, "CTRL");
    refuse = true;
    const before = read(p);
    await assert.rejects(config.saveConfig({ companion: {
      llm: { fast_model: "bad-model" }, voice: { language: "de" }, overlay: { drag_key: "SHIFT" },
    } }, running, { fast_model: "bad-model" }), /live selection refused/);
    assert.strictEqual(read(p), before, "a refused combined Save must not fall back to a local write");
    assert.strictEqual(selectedModel, "c-model");
    // Save-and-restart (no liveChanges) refused with HTTP 400 must ALSO throw rather than write locally.
    await assert.rejects(config.saveConfig({ companion: {
      llm: { fast_model: "ep:openai:tts-1" },
    } }, running), /live selection refused/);
    assert.strictEqual(read(p), before, "a save-only HTTP 400 rejection must not fall back to updateConfig");
    await assert.rejects(config.saveConfig(mixed, stopped, { fast_model: "c-model" }), /not connected/);
    assert.strictEqual(read(p), before);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

function overlayFocusChecks() {
  let keyboardTarget = "game";
  class OverlayWindow {
    constructor(options) {
      this.focusable = options.focusable;
      this.bounds = { x: options.x, y: options.y, width: options.width, height: options.height };
      this.webContents = { setWindowOpenHandler() {}, on() {} };
    }
    setAlwaysOnTop() {}
    setIgnoreMouseEvents(on) { this.ignoreMouse = on; }
    setVisibleOnAllWorkspaces() {}
    loadFile() {}
    once() {}
    on() {}
    setFocusable(on) { this.focusable = on; }
    getBounds() { return { ...this.bounds }; }
    setBounds(bounds) { this.bounds = { ...bounds }; }
    click() {
      if (!this.ignoreMouse && this.focusable) keyboardTarget = "overlay";
    }
  }
  electron.BrowserWindow = OverlayWindow;
  const keysPath = path.join(dist, "keys.js");
  require.cache[keysPath] = { id: keysPath, filename: keysPath, loaded: true,
    exports: { VK: { ALT: 0x12 }, keyDown: () => false, keysAvailable: () => false } };
  const { Overlay } = require(path.join(dist, "app", "overlay.js"));
  const previous = settings.current;
  try {
    settings.current = { overlay: { enabled: true, width: 600, height: 80 } };
    const overlay = new Overlay(() => {}, () => {});
    overlay.sync();
    overlay.setUnlocked(true);
    overlay.changeWidth(-200);
    overlay.fitHeight(39);
    overlay.win.click();
    assert.strictEqual(keyboardTarget, "game",
      "Editing, resizing and collapsing the overlay must leave keyboard focus in the game so F8 stays eligible");
  } finally {
    settings.current = previous;
  }
}

async function windowsProcessVoiceChecks() {
  const { EventEmitter } = require("events");
  const { usesAppWindowsVoice } = require(path.join(dist, "..", "shared", "windows-process-voice.mjs"));
  assert.strictEqual(usesAppWindowsVoice(undefined, undefined), true);
  assert.strictEqual(usesAppWindowsVoice(undefined, ""), false);
  assert.strictEqual(usesAppWindowsVoice(undefined, "Legacy"), false);
  assert.strictEqual(usesAppWindowsVoice({ windowsProcessUseAppVoice: true }, "Legacy"), true);
  assert.strictEqual(usesAppWindowsVoice({ windowsProcessUseAppVoice: false }, undefined), false);
  const h = home("windows-process-voice"), p = path.join(h, "config.json");
  const deliveredDateLogs = [], dateErrors = [];
  const engine = Object.assign(new EventEmitter(), { state: "stopped", url: null, token: "test-link",
    note(text) {
      if (text.startsWith("[date/time]")) dateErrors.push(text);
      this.emit("log", { t: Date.now(), stream: "app", text });
    } });
  const { registerIpc } = require(path.join(dist, "app", "ipc.js"));
  const { OWN_PAGES } = require(path.join(dist, "app", "assets.js"));
  let configuredVoice;
  registerIpc({
    engine, updater: {}, sampler: {}, speech: {},
    mainWindow: { send(channel, line) { if (channel === "engine:logline" && line.text.startsWith("[date/time]")) deliveredDateLogs.push(line); } },
    overlay: { readDragKey() {} },
    windowsTts: { configure(_enabled, voice) { configuredVoice = voice; }, stop() {} },
    edgeTts: { configure() {} }, broadcast() {}, setMuted() {}, pushMute() {}, installUpdate() {},
    applyOverlaySettings() {}, updateTray() {}, ensureSpeech: async () => {},
  });
  const sender = { mainFrame: { url: OWN_PAGES[0] } };
  const invoke = (channel, ...args) => handlers.get(channel)({ sender, senderFrame: sender.mainFrame }, ...args);
  const appVoice = (speech) => invoke("app:settings:set", { speech });
  const processVoice = () => config.readConfig().companion?.voice?.voice;
  const reset = (voice, speech = {}) => {
    settings.current = { engineHome: h, speech: { provider: "windows", voiceName: "App A", ...speech } };
    write(p, JSON.stringify(voice === undefined ? {} : { companion: { voice: { voice } } }));
  };

  // Follow actual emitted log lines with date-format IPC requests, as the Logs renderer does.
  const originalLoad = Module._load, originalPlatform = Object.getOwnPropertyDescriptor(process, "platform");
  let failDates = true, nativeAttempts = 0;
  const textResult = (text, buffer) => {
    if (buffer) Buffer.from(text + "\0", "utf16le").copy(buffer);
    return text.length + 1;
  };
  Module._load = function (name, ...args) {
    if (name !== "koffi") return originalLoad.call(this, name, ...args);
    return { load() { return { func(declaration) {
      if (declaration.includes("GetLocaleInfoEx")) return (_locale, _type, buffer) => {
        nativeAttempts++;
        return failDates ? 0 : textResult("HH:mm", buffer);
      };
      if (declaration.includes("GetDateFormatEx")) return (_locale, _flags, _date, _pattern, buffer) => textResult("12.11.2030", buffer);
      if (declaration.includes("GetLastError")) return () => 1;
      return () => 0;
    } }; } };
  };
  Object.defineProperty(process, "platform", { configurable: true, value: "win32" });
  try {
    const requests = [{ value: "2030-11-12", style: "date" }];
    const rejectedDate = () => assert.throws(() => invoke("app:dates", requests), /GetLocaleInfoEx failed/);
    rejectedDate();
    for (let turn = 0; turn < 8 && deliveredDateLogs.length; turn++) {
      deliveredDateLogs.shift();
      rejectedDate();
    }
    assert.equal(dateErrors.length, 1, "One persistent native failure must emit only one diagnostic, not a self-generating log loop");
    assert.equal(deliveredDateLogs.length, 0, "Log delivery must settle while native formatting is unavailable");
    assert.ok(nativeAttempts >= 2, "The regression must include repeated failing IPC driven by delivered logs");
    failDates = false;
    assert.deepStrictEqual(invoke("app:dates", requests), ["12.11.2030"]);
    failDates = true;
    rejectedDate();
    assert.equal(dateErrors.length, 2, "Successful formatting must rearm diagnostics for a new failure episode");
  } finally {
    Module._load = originalLoad;
    Object.defineProperty(process, "platform", originalPlatform);
    deliveredDateLogs.length = 0;
  }

  reset(undefined);
  await appVoice({ voiceName: "App B" });
  assert.strictEqual(processVoice(), "App B", "missing engine voice inherits the app voice");
  assert.strictEqual(settings.current.speech.windowsProcessUseAppVoice, true);
  await appVoice({ voiceName: "App C" });
  assert.strictEqual(processVoice(), "App C", "materialized inheritance remains linked");
  await appVoice({ voiceName: "" });
  assert.strictEqual(processVoice(), "", "linked Windows default is a literal standalone name");

  // Settings saves engine edits first. Do not overwrite the selected custom name before the unlink patch arrives.
  await invoke("config:set", { companion: { voice: { voice: "Separate" } } });
  await appVoice({ windowsProcessUseAppVoice: false, voiceName: "App D" });
  assert.strictEqual(processVoice(), "Separate");
  await appVoice({ voiceName: "App E" });
  assert.strictEqual(processVoice(), "Separate", "unlinked app changes preserve the custom engine name");
  await appVoice({ windowsProcessUseAppVoice: true });
  assert.strictEqual(processVoice(), "App E", "linking replaces the engine name with the current app name");

  for (const legacy of ["Legacy", "", "App A"]) {
    reset(legacy);
    await appVoice({ voiceName: "New app voice" });
    assert.strictEqual(processVoice(), legacy, "explicit legacy names, including Windows default, stay custom");
    assert.strictEqual(settings.current.speech.windowsProcessUseAppVoice, false);
  }
  reset(undefined, { windowsProcessUseAppVoice: false });
  await appVoice({ voiceName: "New app voice" });
  assert.strictEqual(processVoice(), undefined, "explicit custom mode does not invent an engine voice");

  reset(undefined);
  await config.engineOptions().prepare();
  assert.strictEqual(processVoice(), "App A", "startup materializes the inherited standalone name");
  assert.strictEqual(settings.current.speech.windowsProcessUseAppVoice, true);
  write(p, JSON.stringify({ companion: { voice: { voice: "Outdated" } } }));
  await config.engineOptions().prepare();
  assert.strictEqual(processVoice(), "App A", "every engine start refreshes an explicit link");
  reset("", {});
  await config.engineOptions().prepare();
  assert.strictEqual(processVoice(), "");
  assert.strictEqual(settings.current.speech.windowsProcessUseAppVoice, false);
  const staleOptions = config.engineOptions();
  const otherHome = home("windows-process-voice-other");
  const otherConfig = path.join(otherHome, "config.json");
  write(otherConfig, JSON.stringify({ companion: { voice: { voice: "Other home voice" } } }));
  settings.current = { ...settings.current, engineHome: otherHome };
  await assert.rejects(staleOptions.prepare(), /engine home changed/);
  assert.strictEqual(JSON.parse(fs.readFileSync(otherConfig, "utf8")).companion.voice.voice, "Other home voice",
    "a stale start must not synchronize the voice in a different engine home");
  settings.current = { ...settings.current, engineHome: h };

  const http = require("http");
  let mode = "save", requests = [], release, received;
  const server = http.createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    assert.strictEqual(req.headers["x-glide-token"], "test-link");
    const body = JSON.parse(raw);
    requests.push(body);
    if (mode === "disconnect") return req.socket.destroy();
    if (mode === "hold") {
      received();
      await new Promise((resolve) => { release = resolve; });
    }
    if (mode === "refuse") {
      res.writeHead(400, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ ok: false, error: "engine voice refused" }));
    }
    if (mode === "unconfirmed") {
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ ok: true }));
    }
    write(p, JSON.stringify(store.deepMerge(JSON.parse(read(p)), body.save)));
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, applied: { saved: true } }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  engine.state = "running";
  engine.url = `http://127.0.0.1:${server.address().port}`;
  try {
    reset("Legacy");
    await appVoice({ windowsProcessUseAppVoice: true });
    assert.strictEqual(requests.length, 1);
    assert.deepStrictEqual(requests[0].save, { companion: { voice: { voice: "App A" } } });
    assert.strictEqual(processVoice(), "App A", "running engine owns the linked config write");
    await appVoice({ voiceName: "Running B" });
    assert.strictEqual(processVoice(), "Running B");
    for (mode of ["refuse", "unconfirmed", "disconnect"]) {
      const before = read(p), beforeSpeech = { ...settings.current.speech };
      await assert.rejects(appVoice({ voiceName: "Must not apply" }));
      assert.strictEqual(read(p), before, mode + " must not fall back to writing behind the running engine");
      assert.deepStrictEqual(settings.current.speech, beforeSpeech, mode + " must not report app changes as saved");
      assert.strictEqual(configuredVoice, "Running B", "failed persistence must not configure a different app voice");
    }
    mode = "hold";
    const firstReceived = new Promise((resolve) => { received = resolve; });
    const first = appVoice({ voiceName: "Older" });
    await firstReceived;
    const second = appVoice({ voiceName: "Newest" });
    const custom = invoke("config:set", { companion: { voice: { voice: "Newest custom" } } });
    const unlink = appVoice({ windowsProcessUseAppVoice: false });
    mode = "save";
    release();
    await Promise.all([first, second, custom, unlink]);
    assert.strictEqual(settings.current.speech.voiceName, "Newest");
    assert.strictEqual(settings.current.speech.windowsProcessUseAppVoice, false);
    assert.strictEqual(processVoice(), "Newest custom", "overlapping app/config saves keep the newest selection");

    const before = { ...settings.current.speech }, save = store.saveAppSettings;
    try {
      store.saveAppSettings = () => false;
      await assert.rejects(appVoice({ voiceName: "Unsaved" }), /could not be saved/);
      assert.deepStrictEqual(settings.current.speech, before, "failed app persistence does not mutate the current choice");
    } finally {
      store.saveAppSettings = save;
    }
    mode = "save";
    await appVoice({ windowsProcessUseAppVoice: true, voiceName: "Recovered" });
    assert.strictEqual(processVoice(), "Recovered", "failed writes do not poison later queued saves");
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

(async () => {
  syncChecks();
  await engineChecks();
  await configChecks();
  overlayFocusChecks();
  await windowsProcessVoiceChecks();
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
