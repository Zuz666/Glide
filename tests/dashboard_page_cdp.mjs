// Browser half of tests/test_dashboard_page.py: drive the served dashboard in headless Edge, one step at a time.
// node dashboard_page_cdp.mjs <edge.exe> <profile dir> <url>
// Each step prints "STEP <name> <json>" and waits for a line on stdin (the Python half publishes to the hub, then
// answers) before the next one. Between steps it waits for what the step needs (the state shown, the layout saved, the
// stream back) rather than a fixed time; a wait that runs out records what the page shows, and the Python half's
// checks say what's wrong.
import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import readline from "readline";

const [edge, prof, url] = process.argv.slice(2);
// port 0: Edge picks a free one and writes it to DevToolsActivePort (no fixed port shared with other runs)
// Keep an elevated Windows browser attached to this test, with sync/extensions disabled in its fresh profile.
const p = spawn(edge, ["--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars", "--force-prefers-reduced-motion",
  "--do-not-de-elevate", "--disable-sync", "--disable-extensions", "--disable-background-networking",
  `--user-data-dir=${prof}`, "--remote-debugging-port=0", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const lines = readline.createInterface({ input: process.stdin });
const queued = [];
let waiter = null;
lines.on("line", (l) => (waiter ? (waiter(l), (waiter = null)) : queued.push(l)));
const answer = () => new Promise((r) => (queued.length ? r(queued.shift()) : (waiter = r)));
const step = async (name, value) => { console.log(`STEP ${name} ${JSON.stringify(value ?? null)}`); await answer(); };

let ws;
try {
  for (let i = 0; i < 400 && !ws; i++) {                    // up to 20 s for the browser to come up
    await sleep(50);
    try {
      const port = fs.readFileSync(path.join(prof, "DevToolsActivePort"), "utf8").split("\n")[0].trim();
      const t = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((x) => x.type === "page" && x.url === "about:blank");
      if (t) ws = new WebSocket(t.webSocketDebuggerUrl);
    } catch { /* not up yet */ }
  }
  if (!ws) throw new Error("headless Edge did not expose the test page in 20 s");
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0; const wait = {}; const errors = [];
  ws.addEventListener("message", (m) => {
    const d = JSON.parse(m.data);
    if (d.id && wait[d.id]) { wait[d.id](d.result); delete wait[d.id]; }
    if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  });
  const send = (method, params = {}) => new Promise((r) => { const n = ++id; wait[n] = r; ws.send(JSON.stringify({ id: n, method, params })); });
  const js = async (e) => (await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true })).result?.value;
  const size = (w, h) => send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
  const feed = () => js("document.querySelectorAll('#feed > *').length");
  // until(): poll a page expression until it's true (false after `ms`), then let two frames draw (layout done; a
  // window the system hides may not draw: 250 ms at most)
  const frames = () => js("new Promise((r) => { requestAnimationFrame(() => requestAnimationFrame(() => r(true))); setTimeout(() => r(true), 250); })");
  const until = async (expr, ms = 5000) => {
    for (const t0 = Date.now(); Date.now() - t0 < ms; await sleep(20)) if (await js(expr)) return frames();
    return false;
  };
  // the page's requests in flight and its event-stream inits, counted (the init listener goes on after the page's own)
  const watch = `(() => { window.__inFlight = 0; window.__inits = 0;
    const f = window.fetch; window.fetch = (...a) => { __inFlight++; return f(...a).finally(() => __inFlight--); };
    const ES = window.EventSource; window.EventSource = function (...a) { const es = new ES(...a);
      queueMicrotask(() => es.addEventListener("init", () => __inits++)); return es; }; })();`;
  const settled = () => until("__inFlight === 0");
  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", { source: "try{localStorage.clear()}catch(e){}" });
  await send("Page.addScriptToEvaluateOnNewDocument", { source: watch });

  await size(2560, 1440);
  await send("Page.navigate", { url });
  await until("document.readyState === 'complete' && __inits > 0 && document.querySelectorAll('#wing section').length > 0", 15000);
  await settled();
  await step("loaded", { docked: await js("document.querySelectorAll('#wing section').length"), feed: await feed() });

  // the split for a finished step: the Python half plays step 76 (model 90 s), then step 77 (model 300 s)
  await step("ready-steps");
  await until("onStep.n === 76");
  await step("played-76");
  await until("onStep.n === 77");
  await step("splits", await js("localStorage.getItem('glide.splits')"));

  // a question goes out with the page's token
  await js("document.getElementById('q').value = 'which way?'; document.getElementById('ask').requestSubmit(); true");
  await until("[...document.querySelectorAll('#feed .msg.question')].some((m) => m.textContent.includes('which way?'))");
  await settled();
  await step("asked", await js("[...document.querySelectorAll('#feed .msg.question')].map((m) => m.textContent)"));

  // keyboard arranging: Alt+Right on the first tab moves it one place; the live region names both sections
  await size(1150, 800);
  await until("innerWidth === 1150");
  await js("document.getElementById('arrange').click(); document.querySelector('#shelf [data-book]').focus(); true");
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39, modifiers: 1 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39, modifiers: 1 });
  await until("document.getElementById('arrange_live').textContent !== ''");
  await step("moved", await js("document.getElementById('arrange_live').textContent"));
  await js("document.getElementById('arrange_done').click(); true");

  // outside arrange mode a big panel moves by its header (the mouse, not a script): the minimap's heading dragged
  // onto the route tracker trades their slots; a drag in the chat's text only selects it
  const mouse = (type, [x, y], buttons = 1) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons, clickCount: 1 });
  const centre = (sel, fy = 0.5) => js(`(() => { const r = document.querySelector(${JSON.stringify(sel)}).getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height * ${fy}]; })()`);
  const mouseDrag = async (from, to) => {
    await mouse("mousePressed", from);
    for (let i = 1; i <= 10; i++) await mouse("mouseMoved", [from[0] + (to[0] - from[0]) * i / 10, from[1] + (to[1] - from[1]) * i / 10]);
    await mouse("mouseReleased", to, 0);
    await settled();                                        // the layout it saved is with the engine
  };
  await size(2560, 1440);
  await until("innerWidth === 2560");
  await js("document.getElementById('arrange_live').textContent = ''; true");
  await mouseDrag(await centre("#feed"), await centre("#mapsec"));
  await sleep(400);                                         // nothing to wait for when nothing should move: a dwell
  const feedDrag = await js("document.getElementById('arrange_live').textContent");
  // a piece of the top bar moves by itself; a stat dropped on the character frame lifts the whole pace block
  const topOrder = () => js("[...document.querySelector('[data-group=top]').children].map((c) => c.dataset.move).join(',')");
  await mouseDrag(await centre("[data-move=auras]"), await centre("[data-move=unit]", 0.5).then(([x, y]) => [x - 80, y]));
  const aurasFirst = await topOrder();
  await mouseDrag(await centre("[data-move=ttl]"), await centre("[data-move=unit]"));
  const barDrag = { aurasFirst, lifted: await topOrder(), live: await js("document.getElementById('arrange_live').textContent") };
  await mouseDrag(await centre("#mapsec > .grip"), await centre("[data-move=route]", 0.7));
  await step("header-drag", { bar: barDrag, feed: feedDrag, live: await js("document.getElementById('arrange_live').textContent"),
    map: await js("document.querySelector('[data-move=map]').parentElement.dataset.slot"),
    arranging: await js("document.body.classList.contains('arrange')") });

  // the stream ends (the hub dropped this client): the page reconnects, and the replayed chat isn't added again
  const before = await feed(), inits = await js("__inits");
  await step("drop", before);
  await until(`__inits > ${inits}`, 15000);                 // the browser's own retry delay (about 3 s) sets the pace
  await step("reconnected", await feed());
  await until("document.getElementById('feed').textContent.includes('after the reconnect')");
  await step("after-message", await feed());

  // the chat follows the newest message: after the window shrinks (its resize scrolls the feed by itself), while you
  // scroll up with the wheel it stays where you read, and back at the bottom it follows again
  const gap = "(() => { const f = document.getElementById('feed'); return Math.round(f.scrollHeight - f.scrollTop - f.clientHeight); })()";
  const has = (text) => `document.getElementById('feed').textContent.includes(${JSON.stringify(text)})`;
  const wheel = async (dy) => { const [x, y] = await centre("#feed");
    await send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY: dy }); await frames(); };
  // (the drags above scrolled it, as yours would): back at the bottom it follows again; then the window shrinks
  await js("(() => { const f = document.getElementById('feed'); f.scrollTop = f.scrollHeight; })()");
  await frames();
  await sleep(900);                                          // (a scroll within 800 ms of your wheel is yours)
  await size(1280, 720);
  await until("innerWidth === 1280");
  await step("resized", await js(gap));
  await until(has("follow 9"));
  await step("followed", await js(gap));
  await wheel(-400);
  await step("read-up", await js(gap));
  await until(has("reading 4"));
  await step("kept", await js(gap));
  await wheel(100000);
  await step("back-down", await js(gap));
  await until(has("again 2"));
  await step("refollowed", await js(gap));

  // wide again (sections docked into the wing), then the game goes away: only the community notes stay on show
  await size(2560, 1440);
  await until("innerWidth === 2560 && document.querySelectorAll('#wing section').length > 0");
  await step("wide", await js("document.querySelectorAll('#wing section').length"));
  await until("document.body.classList.contains('waiting')");
  await step("waiting", await js(`({ waiting: document.body.classList.contains('waiting'),
    shown: [...document.querySelectorAll('#shelf > section')].filter((s) => getComputedStyle(s).display !== 'none').map((s) => s.dataset.panel) })`));
  await step("errors", [...new Set(errors)]);
} finally {
  ws?.close();
  p.kill();
  lines.close();
}
