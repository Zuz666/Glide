// Browser half of test_settings_defaults.py. All Settings modules are production code; only IPC is replaced.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const [edge, profile, url] = process.argv.slice(2);
const child = spawn(edge, ["--headless", "--disable-gpu", "--no-first-run", "--do-not-de-elevate",
  "--disable-sync", "--disable-extensions", "--disable-background-networking",
  `--user-data-dir=${profile}`, "--remote-debugging-port=0", "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let ws;
try {
  for (let i = 0; i < 400 && !ws; i++) {
    try {
      const port = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0].trim();
      const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = pages.find(p => p.type === "page" && p.url === "about:blank");
      if (page) ws = new WebSocket(page.webSocketDebuggerUrl);
    } catch { /* wait for this isolated browser */ }
    if (!ws) await sleep(50);
  }
  if (!ws) throw Error("Headless browser did not start");
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let next = 0;
  const pending = new Map();
  ws.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { pending.delete(id); reject(Error(`CDP timeout: ${method}`)); }, 30000);
    pending.set(id, message => {
      clearTimeout(timer);
      if (message.error) reject(Error(JSON.stringify(message.error)));
      else resolve(message.result);
    });
    ws.send(JSON.stringify({ id, method, params }));
  });
  await send("Page.enable");
  await send("Page.navigate", { url });
  for (let i = 0; i < 200; i++) {
    const ready = await send("Runtime.evaluate", { expression: "!!document.getElementById('set-body')", returnByValue: true });
    if (ready.result?.value) break;
    await sleep(25);
  }
  const result = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  window.glide = {
    async invoke(channel, ...args) {
      const response = await fetch('/ipc', {method: 'POST', body: JSON.stringify({channel, args})});
      const body = await response.json();
      if (!response.ok) throw Error(body.error);
      return body;
    },
    on() { return () => {}; }
  };
  const page = await import('/renderer/settings/page.js');
  const stateMod = await import('/renderer/state.js');
  const mp = await import('/renderer/settings/model-picker.js');
  page.registerPanels({ 'model-picker': mp.renderModelPicker });
  const check = (condition, why) => { if (!condition) throw Error(why); };
  const until = async condition => {
    const deadline = performance.now() + 5000;
    while (!condition()) {
      if (performance.now() > deadline) throw Error('UI condition timed out');
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  const probe = async () => (await fetch('/probe')).json();
  const row = () => document.querySelector('[data-key="companion.llm.voice_question_model"]');
  const value = () => row()?.querySelector('select').value;
  const reset = () => row().querySelector('[data-reset]').click();
  page.wireSettings();
  page.openSettingsTab('models');
  await until(() => value() === 'deep');
  reset();
  await until(() => value() === 'fast');
  let stored = await probe();
  check(stored.attempts === 0, 'Use default wrote before Save');
  check(stored.config.companion.llm.voice_question_model === 'deep', 'Reset changed persisted selection');
  check(!document.getElementById('set-save').disabled, 'Reset is not an unsaved edit');
  document.getElementById('set-revert').click();
  await until(() => value() === 'deep');
  check((await probe()).attempts === 0, 'Revert wrote settings');
  reset();
  await until(() => value() === 'fast');
  document.getElementById('set-save').click();
  await until(() => document.getElementById('toast').textContent.includes('Test save rejected'));
  stored = await probe();
  check(stored.config.companion.llm.voice_question_model === 'deep', 'Rejected save changed persisted selection');
  check(value() === 'fast' && row().classList.contains('changed'), 'Rejected save lost the reset draft');
  check(!document.getElementById('set-save').disabled, 'Rejected save cannot be retried');
  document.getElementById('set-save').click();
  await until(() => document.getElementById('set-save').disabled);
  stored = await probe();
  check(stored.attempts === 2, 'Unexpected persistence outside Save');
  check(!('voice_question_model' in stored.config.companion.llm), 'Default pinned instead of removing override');
  // Verify real model-picker rendering against App.on_models(): excluded saved ID, first-party price/context, and , unverified.
  stateMod.live.engineState = 'running';
  stateMod.live.engineUrl = location.origin;
  stateMod.live.engineToken = 'test';
  page.openSettingsTab('voice');
  page.openSettingsTab('models');
  await until(() => !!document.querySelector('select[data-pick="fast_model"]'));
  const fastOpts = [...document.querySelectorAll('select[data-pick="fast_model"] option')].map(o => o.textContent);
  check(!fastOpts.some(t => t.includes('whisper-1')), 'Excluded whisper-1 leaked into picker: ' + JSON.stringify(fastOpts));
  check(fastOpts.some(t => t.includes('ep:openai:tts-1 (excluded: speech synthesis model')),
    'Expected saved excluded fast_model to show exclusion reason, got: ' + JSON.stringify(fastOpts));
  check(fastOpts.some(t => t.includes('gpt-5.6-luna') && t.includes('$0.200 in / $1.20 out') && t.includes('1050k context') && !t.includes('unverified')),
    'Expected first-party price and context on gpt-5.6-luna without unverified tag, got: ' + JSON.stringify(fastOpts));
  check(fastOpts.some(t => t.includes('future-model-x') && t.includes(', unverified')),
    'Expected , unverified label on unclassified official OpenAI model, got: ' + JSON.stringify(fastOpts));
  const deepCost = document.querySelector('select[data-pick="deep_model"]')?.parentElement?.querySelector('.mcost')?.textContent || '';
  check(deepCost.includes('an hour') && !deepCost.includes('Price unknown'),
    'Expected uncached selected deep_model to show hourly cost from static fallback price, got: ' + deepCost);
  return 'OK: reset waits for Save; Revert restores; rejected Save retains draft; retry removes override';
})()` });
  if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  console.log(result.result.value);
} finally {
  ws?.close();
  if (process.platform === "win32") spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  else child.kill();
}
