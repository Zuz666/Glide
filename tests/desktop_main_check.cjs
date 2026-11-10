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
const electron = new Proxy({
  app: { getPath: () => userData, getVersion: () => "1.0.0", isPackaged: false, getAppPath: () => path.dirname(dist),
         on() {}, whenReady: () => new Promise(() => {}), requestSingleInstanceLock: () => true },
  ipcMain: { handle() {}, on() {} }, safeStorage: { isEncryptionAvailable: () => false },
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
    await assert.rejects(config.saveConfig(mixed, stopped, { fast_model: "c-model" }), /not connected/);
    assert.strictEqual(read(p), before);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

(async () => {
  syncChecks();
  await engineChecks();
  await configChecks();
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
