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
  await send("Emulation.setDeviceMetricsOverride", { width: 2560, height: 1440, deviceScaleFactor: 1, mobile: false });
  const captureProof = async name => {
    if (!process.env.GLIDE_UI_PROOF_DIR) return;
    fs.mkdirSync(process.env.GLIDE_UI_PROOF_DIR, { recursive: true });
    await send("Runtime.evaluate", {awaitPromise: true, expression: `new Promise(resolve => {
      const root = document.getElementById('toast');
      if (!root?.childElementCount) return resolve(true);
      const observer = new MutationObserver(() => {
        if (!root.childElementCount) { observer.disconnect(); resolve(true); }
      });
      observer.observe(root, {childList: true});
    })`});
    const image = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(process.env.GLIDE_UI_PROOF_DIR, name + ".png"), Buffer.from(image.data, "base64"));
  };
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
  // The isolated browser has no native Windows date IPC; keep timestamp leaves readable in proof images.
  window.glideDates?.useFormatter(async requests => requests.map(({value}) => new Date(value).toLocaleString('en-US')));
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
  // Isolate the external IPC/HTTP boundaries; all controls below remain production renderer modules.
  const realInvoke = window.glide.invoke, realFetch = window.fetch.bind(window);
  const credentialCalls = [], modelWrites = [];
  let savedKey = '', heldTest = null, holdTest = false;
  const catalogMessage = 'Authenticated catalog access confirmed. Model-request permission, available credit and generation were not verified.';
  const known = [
    {env: 'OPENROUTER_API_KEY', label: 'OpenRouter', kind: 'model', help: 'Existing provider'},
    {env: 'OPENCODE_API_KEY', label: 'OpenCode API (pay-as-you-go)', kind: 'model', help: 'Paid conversational models',
      guide: {url: 'https://opencode.ai/console', plan: 'paid', steps: ['Create a personal or service-account key with Models Request permission.'], free: 'Workspace balance is required.'}}
  ];
  window.glide.invoke = async (channel, ...args) => {
    if (channel === 'keys:list') return {known, status: known.map(k => ({env: k.env, saved: k.env === 'OPENCODE_API_KEY' && !!savedKey, fromEnv: false, hint: savedKey ? '…cret' : ''}))};
    if (channel === 'keys:set') { credentialCalls.push([channel, ...args]); savedKey = args[1]; return []; }
    if (channel === 'keys:test') {
      credentialCalls.push([channel, ...args]);
      if (holdTest) await new Promise(resolve => { heldTest = resolve; });
      return {ok: true, models: ['opencode:vendor/chat'], message: catalogMessage};
    }
    return realInvoke(channel, ...args);
  };
  const entry = {
    id: 'opencode:vendor/chat', provider: 'opencode', name: 'Imported chat', tools: true, context: 300000,
    price: {in: 0.2, out: 1.2, cache_read: 0.02}, pricing_updated: 1794780000, pricing_stale: true,
    pricing: {
      input: {price: 0.2, cache: {read: 0.02, write: {'30m': 0.25, '12h': 0.5}},
        overrides: [{when: {context: {gt: 272000}}, price: 0.4, cache: {read: 0.04, write: {'30m': 0.5, '12h': 1}}}]},
      output: {price: 1.2, overrides: [{when: {context: {gt: 272000}}, price: 1.8}]}
    }
  };
  const fixture = {
    models: [entry], selected: {fast_model: entry.id, deep_model: entry.id},
    reasoning: {fast: {levels: []}, deep: {levels: []}},
    selected_prices: {fast: entry.price, deep: entry.price},
    profile: {fast: {cache_read: 1000, uncached: 1000, output: 1000}, deep: {cache_read: 1000, uncached: 1000, output: 1000}},
    status: {count: 1, keys: {opencode: false}, errors: {opencode: 'Refresh failed; retained previous catalog'}},
    spent_usd: 0.25, cost_coverage: {reported: 1, estimated: 1, unknown: 1, stale_estimates: 1}
  };
  window.fetch = async (input, init) => {
    const pathname = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
    if (pathname === '/models') return new Response(JSON.stringify(fixture), {headers: {'Content-Type': 'application/json'}});
    if (pathname === '/settings') { modelWrites.push(init); throw Error('Unexpected model change'); }
    return realFetch(input, init);
  };
  const keys = await import('/renderer/settings/keys.js');
  const footer = await import('/renderer/session-bar.js');
  page.registerPanels({'keys': keys.renderKeys});
  const host = document.querySelector('[data-special="model-picker"]');
  await mp.renderModelPicker({}, host);
  const keyLinks = await import('/renderer/settings/key-links.js');
  check(keyLinks.endpointKeyEnv({id: 'other', base_url: 'https://opencode.ai.example/v1', key_env: 'OPENCODE_API_KEY'}) === 'GLIDE_ENDPOINT_OTHER_KEY', 'Renderer allowed an OpenCode key on a lookalike host');
  check(keyLinks.endpointKeyEnv({id: 'own', base_url: 'https://opencode.ai/inference/openai/v1', key_env: 'OPENCODE_API_KEY'}) === 'OPENCODE_API_KEY', 'Renderer rejected the OpenCode host');
  check(host.querySelector('optgroup[label="OpenCode API (pay-as-you-go)"]'), 'OpenCode provider group missing');
  check(host.querySelector('.mkey').textContent.includes('OPENCODE_API_KEY') && !host.querySelector('.mkey').textContent.includes('OPENROUTER_API_KEY'), 'Slashed OpenCode id linked the wrong credential');
  const tariff = host.querySelector('[data-tariff]').textContent;
  for (const text of ['base', '272000', 'cache write 30m', 'cache write 12h', '$0.500', '$1.00', '$1.80', 'Stale rates', 'Last successful import']) {
    check(tariff.includes(text), 'Imported tariff qualification missing: ' + text);
  }
  check(host.querySelector('time[data-date-value="1794780000000"]'), 'Last successful import timestamp lost');
  check(host.textContent.includes('Refresh failed'), 'Stale catalog failure was hidden');
  check(host.querySelector('.mcost').textContent.includes('Base-rate estimate'), 'Hourly base rates presented as universal');
  const spendingCases = [
    [0.25, {reported: 1, estimated: 1, unknown: 1, stale_estimates: 1}, ['$0.250', 'estimates', 'incomplete', 'stale rates']],
    [0.25, {reported: 0, estimated: 1, unknown: 0, stale_estimates: 0}, ['$0.250', 'estimates']],
    [0.25, {reported: 1, estimated: 0, unknown: 1, stale_estimates: 0}, ['$0.250', 'incomplete']],
    [0, {reported: 0, estimated: 0, unknown: 2, stale_estimates: 0}, ['Unknown', 'incomplete']],
    [0, {reported: 1, estimated: 0, unknown: 0, stale_estimates: 0}, ['$0.000']],
    [0.5, undefined, ['$0.500']]
  ];
  for (const [amount, coverage, expected] of spendingCases) {
    fixture.spent_usd = amount; fixture.cost_coverage = coverage;
    await mp.renderModelPicker({}, host);
    footer.sessionBar({coach: {cost_usd: amount, cost_coverage: coverage}});
    const spent = host.querySelector('[data-session-spent]').textContent;
    const bar = document.getElementById('sb-cost').textContent;
    for (const label of expected) check(spent.includes(label) && bar.includes(label), 'Cost coverage missing from settings/footer: ' + label);
    if (coverage?.unknown && coverage.reported + coverage.estimated === 0) check(!spent.includes('$0') && !bar.includes('$0'), 'Unknown costs became zero');
    if (coverage?.reported === 1 && amount === 0) check(!bar.includes('Unknown') && !bar.includes('estimate'), 'Reported zero lost its known basis');
  }
  const fullPrice = entry.price;
  for (const partial of [{in: 0.2, cache_read: 0.02}, {in: 0.2, out: 1.2}]) {
    entry.price = partial;
    fixture.selected_prices = {fast: partial, deep: partial};
    await mp.renderModelPicker({}, host);
    check(host.querySelector('.mcost').textContent.includes('Price unknown'), 'Missing required imported rate became a made-up hourly amount');
  }
  entry.price = fullPrice;
  fixture.selected_prices = {fast: fullPrice, deep: fullPrice};
  fixture.spent_usd = 0.25; fixture.cost_coverage = spendingCases[0][1];
  await mp.renderModelPicker({}, host);
  footer.sessionBar({coach: {cost_usd: 0.25, cost_coverage: fixture.cost_coverage}});
  window.__opencodeUi = {page, keys, check, until, credentialCalls, modelWrites, fixture, catalogMessage,
    hold(value) {holdTest = value;}, release() {heldTest?.(); heldTest = null;}, held() {return !!heldTest;}};
  return 'OK: saved settings defaults; OpenCode grouping, tariff qualifications and session cost coverage';
})()` });
  if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  console.log(result.result.value);
  await captureProof("opencode-models-costs-2560x1440");
  const credentials = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  const {page, check, until, credentialCalls, modelWrites, catalogMessage} = window.__opencodeUi;
  page.openSettingsTab('keys');
  const row = () => document.querySelector('[data-key-row="OPENCODE_API_KEY"]');
  await until(() => !!row());
  check(row().previousElementSibling?.dataset.keyRow === 'OPENROUTER_API_KEY', 'OpenCode row is not beside OpenRouter');
  row().querySelector('[data-edit-env]').click();
  await until(() => !!row().querySelector('input'));
  const input = () => row().querySelector('input');
  input().value = 'draft-fixture-secret'; input().dispatchEvent(new Event('input', {bubbles: true}));
  input().focus(); input().setSelectionRange(3, 7);
  row().querySelector('[data-test]').click();
  await until(() => row().querySelector('[data-test-msg]').textContent === catalogMessage);
  check(document.activeElement === input(), 'Test stole draft focus');
  check(credentialCalls.length === 1 && credentialCalls[0][0] === 'keys:test' && credentialCalls[0][2] === 'draft-fixture-secret', 'Draft Test saved or ignored entered credential');
  check(!row().textContent.includes('Valid —'), 'Catalog Test promised model reachability');
  window.__opencodeUi.hold(true);
  row().querySelector('[data-test]').click();
  await until(() => window.__opencodeUi.held());
  input().value = 'new-draft-secret'; input().dispatchEvent(new Event('input', {bubbles: true}));
  window.__opencodeUi.release();
  await until(() => !row().querySelector('[data-test]').disabled);
  check(row().querySelector('[data-test-msg]').hidden, 'Stale Test result labelled the edited draft');
  window.__opencodeUi.hold(false);
  row().querySelector('[data-save]').click();
  await until(() => !row().querySelector('input') && row().textContent.includes('Saved'));
  check(document.getElementById('toast').textContent.includes('Restart the coach'), 'Saved engine key omitted explicit restart instruction');
  check(credentialCalls.at(-1)[0] === 'keys:set' && credentialCalls.at(-1)[2] === 'new-draft-secret', 'Save discarded the current draft');
  row().querySelector('[data-edit-env]').click();
  await until(() => !!row().querySelector('[data-test]'));
  check(input().value === '', 'Saved secret returned to renderer input');
  row().querySelector('[data-test]').click();
  await until(() => row().querySelector('[data-test-msg]').textContent === catalogMessage);
  check(credentialCalls.at(-1)[2] === undefined, 'Saved-key Test sent a stale draft');
  row().querySelector('[data-del]').click();
  await until(() => !row().querySelector('input') && row().textContent.includes('Not set'));
  check(credentialCalls.at(-1)[0] === 'keys:set' && credentialCalls.at(-1)[2] === '', 'Remove did not remove credential');
  check(modelWrites.length === 0, 'Credentials silently changed model roles');
  row().querySelector('[data-edit-env]').click();
  await until(() => !!input());
  input().value = 'screenshot-fixture'; input().dispatchEvent(new Event('input', {bubbles: true}));
  row().querySelector('[data-test]').click();
  await until(() => row().querySelector('[data-test-msg]').textContent === catalogMessage);
  return 'OK: OpenCode draft/saved Test, scoped success, stale response, Save/restart and Remove without model changes';
})()` });
  if (credentials.exceptionDetails) throw Error(credentials.exceptionDetails.exception?.description || credentials.exceptionDetails.text);
  console.log(credentials.result.value);
  await captureProof("opencode-credentials-2560x1440");
} finally {
  ws?.close();
  if (process.platform === "win32") spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  else child.kill();
}
