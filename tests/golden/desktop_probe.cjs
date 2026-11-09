// Desktop golden master, Node half: the compiled app (desktop/dist) without Electron. A stand-in `electron` module
// gives app.getPath a temp folder and records every ipcMain channel registered; nothing starts a window.
// node desktop_probe.cjs <desktop/dist> <out.json>
const Module = require("module");
const fs = require("fs");
const os = require("os");
const path = require("path");
const [dist, out] = process.argv.slice(2);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "glide-desktop-probe-"));
const handled = [], listened = [];
const anything = () => new Proxy(function () {}, { get: () => anything(), apply: () => anything() });
const electron = new Proxy({
  app: { getPath: () => tmp, getVersion: () => "0.0.0", isPackaged: false, getAppPath: () => path.dirname(dist),
         on() {}, whenReady: () => new Promise(() => {}), requestSingleInstanceLock: () => true },
  ipcMain: { handle: (name) => handled.push(name), on: (name) => listened.push(name) },
  safeStorage: { isEncryptionAvailable: () => false },
}, { get: (t, k) => (k in t ? t[k] : new Proxy(function () {}, { get: () => () => {} })) });
const resolve = Module._resolveFilename;
Module._resolveFilename = function (req, ...a) { return req === "electron" ? "electron" : resolve.call(this, req, ...a); };
require.cache.electron = { id: "electron", filename: "electron", loaded: true, exports: electron };

// every compiled main-process module, sub-folders too ("app/ipc.js"), in a fixed order; only those with a source
// file still (tsc never deletes the output of a moved or removed .ts)
const SRC = path.join(dist, "..", "src", "main");
const modules = (dir, rel = "") => fs.readdirSync(path.join(dir, rel), { withFileTypes: true })
  .flatMap((e) => (e.isDirectory() ? modules(dir, path.posix.join(rel, e.name))
    : e.name.endsWith(".js") && fs.existsSync(path.join(SRC, rel, e.name.replace(/\.js$/, ".ts")))
      ? [path.posix.join(rel, e.name)] : []))
  .sort();
// the channel names in preload.ts's `const NAME = new Set([...])` (what the windows may invoke / listen to)
const preloadSet = (src, name) => {
  const m = src.match(new RegExp(`const ${name} = new Set\\(\\[([\\s\\S]*?)\\]\\)`));
  if (!m) throw new Error(`preload.js has no ${name} set`);
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]).sort();
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
(async () => {
  const res = {};
  const main = path.join(dist, "main");
  // contract 4, the IPC channels: what app/ipc.js registers must be exactly what preload.js lets the windows invoke,
  // and the events the main process sends must be exactly the ones preload.js lets them listen to
  require(path.join(main, "app", "ipc.js")).registerIpc(anything());
  const preload = fs.readFileSync(path.join(main, "preload.js"), "utf-8");
  const sent = new Set();
  for (const f of modules(main)) {
    for (const m of fs.readFileSync(path.join(main, f), "utf-8").matchAll(/(?:broadcast|\.send)\(\s*"([^"]+)"/g)) sent.add(m[1]);
  }
  const ipc = { invoke: [...handled, ...listened].sort(), events: [...sent].sort() };
  const allowed = { invoke: preloadSet(preload, "INVOKE"), events: preloadSet(preload, "EVENTS") };
  for (const k of ["invoke", "events"]) {
    if (!same(ipc[k], allowed[k])) throw new Error(`IPC ${k}: main has ${JSON.stringify(ipc[k])}, preload allows ${JSON.stringify(allowed[k])}`);
  }
  res.ipc = ipc;
  const store = require(path.join(main, "store.js"));
  res.app_defaults = store.APP_DEFAULTS;
  fs.writeFileSync(path.join(tmp, "app-settings.json"), JSON.stringify({ overlay: { opacity: 0.5 }, speech: { provider: "openai" }, closeToTray: true }));
  res.loaded_settings = store.loadAppSettings();
  res.saved_back = JSON.parse(fs.readFileSync(path.join(tmp, "app-settings.json"), "utf-8"));
  fs.writeFileSync(out, JSON.stringify(res, null, 1));
  fs.rmSync(tmp, { recursive: true, force: true });
})().catch((e) => { console.error(e); process.exit(1); });
