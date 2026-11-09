// Dashboard golden master, browser half: load the page in headless Edge at each size with the clock frozen and
// animations off, then read back what it shows. node dash_cdp.mjs <edge.exe> <profile dir> <url> <out.json>
import { spawn } from "child_process";
import fs from "fs";
import path from "path";

const [edge, prof, url, out] = process.argv.slice(2);
const SIZES = [[2560, 1440], [1920, 1080], [1150, 800]];
const FROZEN = 1791300000000;      // every Date in the page reads this moment
// port 0: Edge picks a free one and writes it to DevToolsActivePort. A fixed port collided with another headless
// Edge still running (tests/test_dashboard_page.py, an earlier probe) and read that one's page instead.
// Elevated Windows launches must remain our child; isolate profile services and ignore Edge's sign-in pages.
const p = spawn(edge, ["--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars", "--force-prefers-reduced-motion",
  "--do-not-de-elevate", "--disable-sync", "--disable-extensions", "--disable-background-networking",
  "--lang=en-US", "--accept-lang=en-US", // Pin number/time formatting independently of the host locale.
  `--user-data-dir=${prof}`, "--remote-debugging-port=0", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws;
for (let i = 0; i < 800 && !ws; i++) {
  await sleep(50);
  try {
    const port = fs.readFileSync(path.join(prof, "DevToolsActivePort"), "utf8").split("\n")[0].trim();
    const t = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((x) => x.type === "page" && x.url === "about:blank");
    if (t) ws = new WebSocket(t.webSocketDebuggerUrl);
  } catch { /* not up yet */ }
}
if (!ws) { p.kill(); throw new Error("headless Edge did not come up in 40 s"); }
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const wait = {}; const errors = [];
ws.addEventListener("message", (m) => { const d = JSON.parse(m.data); if (d.id && wait[d.id]) { wait[d.id](d.result); delete wait[d.id]; }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text); });
const send = (method, params = {}) => new Promise((r) => { const n = ++id; wait[n] = r; ws.send(JSON.stringify({ id: n, method, params })); });
const js = async (e) => (await send("Runtime.evaluate", { expression: e, returnByValue: true })).result?.value;
await send("Page.enable"); await send("Runtime.enable");
// times are printed in local time: pin the zone the snapshot was recorded in, so CI (UTC) reads the same
await send("Emulation.setTimezoneOverride", { timezoneId: "America/New_York" });
// and count the engine's "init" events the page has rendered (window.__inits), so the reading waits for the first
// state on screen instead of a fixed time
await send("Page.addScriptToEvaluateOnNewDocument", { source: `(()=>{const T=${FROZEN};const D=Date;
  class F extends D{constructor(...a){super(...(a.length?a:[T]))} static now(){return T}}
  window.Date=F;try{localStorage.clear()}catch(e){}
  window.__inits=0;const add=EventSource.prototype.addEventListener;
  EventSource.prototype.addEventListener=function(type,fn,...rest){
    if(type!=='init')return add.call(this,type,fn,...rest);
    return add.call(this,type,function(...a){try{return fn.apply(this,a)}finally{window.__inits++}},...rest)};})();` });
const result = {};
const READ = `(() => {
  const txt = (el) => el ? el.innerText.replace(/\\s+/g, " ").trim() : null;
  const q = (s) => document.querySelector(s);
  const regions = {};
  for (const el of document.querySelectorAll("[data-move], [data-panel], #plate, .top, .xpfoot, #tabs, #wing"))
    regions[(el.dataset.move ? "move:" + el.dataset.move : el.dataset.panel ? "panel:" + el.dataset.panel : el.id || el.className)] = txt(el);
  return {
    body: document.body.className,
    title: document.title,
    regions,
    wing: [...document.querySelectorAll("#wing .dock")].map((d) => [...d.children].map((s) => s.dataset.book)),
    tabs: [...document.querySelectorAll("#tabs [data-book]")].map((b) => [b.dataset.book, b.getAttribute("aria-selected")]),
    slots: [...document.querySelectorAll(".slot")].map((s) => [s.dataset.slot, s.firstElementChild?.dataset.move]),
    auras: [...document.querySelectorAll("#buffs .aura")].map((a) => a.title),
    map: { blank: q("#mapbox")?.classList.contains("blank"), marks: [...document.querySelectorAll("#mapsvg > *")].map((e) => e.getAttribute("class") || e.tagName).join(",") },
    feed: [...document.querySelectorAll("#feed > *")].length,
  };
})()`;
// what the page shows once it has settled: loaded, the engine's first state rendered (the init event's handler has
// run: window.__inits), and then the same reading twice in a row 100 ms apart. It used to wait a fixed 1.5 s and
// compare readings half a second apart, about 2 s a size whatever the machine. Gives up after 20 s and keeps the last
// reading.
const settled = async () => {
  let last = null;
  for (let i = 0; i < 200; i++) {
    const ready = await js("document.readyState === 'complete' && window.__inits > 0 && document.body.innerText.length > 0");
    const now = ready ? JSON.stringify(await js(READ)) : null;
    if (now && now === last) break;
    last = now;
    await sleep(100);
  }
  return last ? JSON.parse(last) : await js(READ);
};
for (const [w, h] of SIZES) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  result[`${w}x${h}`] = await settled();
}
result.errors = [...new Set(errors)];
fs.writeFileSync(out, JSON.stringify(result, null, 1));
ws.close(); p.kill();
