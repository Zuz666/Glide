// Browser half of test_tts_status_ui.py: real coach-screen session bar, Settings scroll targets, and overlay DOM.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const [edge, profile, baseUrl] = process.argv.slice(2);
const child = spawn(edge, [
  "--headless", "--disable-gpu", "--no-first-run", "--do-not-de-elevate",
  "--disable-sync", "--disable-extensions", "--disable-background-networking",
  `--user-data-dir=${profile}`, "--remote-debugging-port=0", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let ws;

try {
  for (let i = 0; i < 400 && !ws; i++) {
    try {
      const port = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0].trim();
      const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = pages.find((p) => p.type === "page" && p.url === "about:blank");
      if (page) ws = new WebSocket(page.webSocketDebuggerUrl);
    } catch { /* wait for isolated browser */ }
    if (!ws) await sleep(50);
  }
  if (!ws) throw Error("Headless browser did not start");
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });

  let next = 0;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { pending.delete(id); reject(Error(`CDP timeout: ${method}`)); }, 30000);
    pending.set(id, (message) => {
      clearTimeout(timer);
      if (message.error) reject(Error(JSON.stringify(message.error)));
      else resolve(message.result);
    });
    ws.send(JSON.stringify({ id, method, params }));
  });

  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 2560, height: 1440, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: `${baseUrl}/` });
  for (let i = 0; i < 200; i++) {
    const ready = await send("Runtime.evaluate", { expression: "!!document.getElementById('sessionbar')", returnByValue: true });
    if (ready.result?.value) break;
    await sleep(25);
  }

  const mainResult = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  const listeners = new Map();
  window.glide = {
    async invoke(channel, ...args) {
      const response = await fetch('/ipc', { method: 'POST', body: JSON.stringify({ channel, args }) });
      const body = await response.json();
      if (!response.ok) throw Error(body.error);
      return body;
    },
    on(channel, fn) {
      if (!listeners.has(channel)) listeners.set(channel, new Set());
      listeners.get(channel).add(fn);
      return () => listeners.get(channel).delete(fn);
    },
  };
  const emit = (channel, payload) => {
    for (const fn of listeners.get(channel) || []) fn(payload);
  };
  const check = (cond, why) => { if (!cond) throw Error(why); };
  const until = async (cond, why = 'UI condition timed out') => {
    const deadline = performance.now() + 5000;
    while (!cond()) {
      if (performance.now() > deadline) throw Error(why);
      await new Promise((r) => setTimeout(r, 10));
    }
  };

  const nav = await import('/renderer/nav.js');
  const page = await import('/renderer/settings/page.js');
  const appSettings = await import('/renderer/app-settings.js');
  const sb = await import('/renderer/session-bar.js');
  const sbOrder = await import('/renderer/session-bar-order.js');

  // Simulate an async tall Coach voice panel so Push-to-talk sits well below the initial 520px fold.
  page.registerPanels({
    voices: async (_g, host) => {
      await new Promise((r) => setTimeout(r, 25));
      host.innerHTML = '<div style="height:640px">Voice picker content</div>';
    },
    'model-picker': async (_g, host) => {
      await new Promise((r) => setTimeout(r, 15));
      host.innerHTML = '<div style="height:260px">Model picker content</div>';
    },
  });

  nav.wireNav();
  page.wireSettings();
  appSettings.wireAppSettings();
  sb.wireSessionBar();
  appSettings.applyAppSettings(await window.glide.invoke('app:settings:get'));
  sb.sessionBar({
    session_minutes: 42,
    coach: { cost_usd: 0.125, calls: 7, cache_read_tokens: 800, uncached_in_tokens: 200, mode: 'offline' },
    models: {
      fast: 'claude-haiku-4-5', fast_name: 'Haiku 4.5',
      deep: 'claude-sonnet-4-5', deep_name: 'Sonnet 4.5',
      listen: 'whisper small',
      translation: { language: 'ru', available: true },
    },
  });

  const bar = document.getElementById('sessionbar');
  check(Math.round(bar.getBoundingClientRect().height) === 48, 'Expected 48px sessionbar, got ' + bar.getBoundingClientRect().height);

  const modelsEl = document.querySelector('#sessionbar > [data-sb="models"]');
  const sttEl = document.querySelector('#sessionbar > [data-sb="stt"]');
  const ttsEl = document.querySelector('#sessionbar > [data-sb="tts"]');
  check(modelsEl && sttEl && ttsEl, 'Expected independent data-sb models, stt, and tts items');
  check(getComputedStyle(ttsEl).display !== 'none', 'TTS chip must stay visible at 1440px width');

  const fastBtn = document.getElementById('sb-fast-btn');
  const deepBtn = document.getElementById('sb-deep-btn');
  check(fastBtn && deepBtn && fastBtn.tagName === 'BUTTON' && deepBtn.tagName === 'BUTTON', 'Expected separate Fast and Deep buttons');

  // Offline coach mode dims/strikes only Fast and Deep, never STT or TTS.
  check(modelsEl.classList.contains('idle'), 'Expected models block to have idle class when coach is offline');
  check(getComputedStyle(document.getElementById('sb-fast')).textDecorationLine.includes('line-through'), 'Fast row should strike through when offline');
  check(!getComputedStyle(document.getElementById('sb-listen')).textDecorationLine.includes('line-through'), 'STT must not strike through when AI is offline');
  check(!getComputedStyle(document.getElementById('sb-speak')).textDecorationLine.includes('line-through'), 'TTS must not strike through when AI is offline');

  await until(() => document.getElementById('sb-speak').textContent.includes('Edge · Dmitry'), 'Expected initial Edge · Dmitry label');

  // Measure neighboring item horizontal positions across TtsStatus transitions (must be 0 px shift!).
  const neighbors = ['#sb-models', '#sb-listen-wrap', '#sb-speak-wrap', '#sb-translation', '#sb-vol-wrap', '#sb-mode-wrap']
    .map((sel) => document.querySelector(sel));
  const baseline = neighbors.map((el) => {
    const r = el.getBoundingClientRect();
    return { id: el.id, left: Math.round(r.left * 10) / 10, width: Math.round(r.width * 10) / 10 };
  });

  const phases = [
    { phase: 'generating', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)', readySlots: 6, totalSlots: 6, updatedAt: 2000 },
    { phase: 'speaking', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)', readySlots: 6, totalSlots: 6, updatedAt: 3000 },
    { phase: 'fallback', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)', fallbackVoice: 'VeryLongCustomVoiceIdentifierName', detail: 'zero_audio_reset:2', holding: true, updatedAt: 4000 },
    { phase: 'error', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)', detail: 'first_audio_timeout:2', holding: true, updatedAt: 5000 },
    { phase: 'ready', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)', workerState: 'ready', readySlots: 6, totalSlots: 6, updatedAt: 6000 },
  ];
  for (const st of phases) {
    emit('tts:status', { provider: 'edge', ...st });
    for (let i = 0; i < neighbors.length; i++) {
      const r = neighbors[i].getBoundingClientRect();
      const dLeft = Math.abs(Math.round(r.left * 10) / 10 - baseline[i].left);
      const dWidth = Math.abs(Math.round(r.width * 10) / 10 - baseline[i].width);
      check(dLeft === 0 && dWidth === 0,
        'Horizontal jitter on ' + baseline[i].id + ' during phase=' + st.phase + ': dLeft=' + dLeft + ', dWidth=' + dWidth);
    }
  }

  // Saved sessionBarOrder migration: legacy order with "models" but no "stt"/"tts" places "stt","tts" right after "models".
  sbOrder.orderSessionBar(['cost', 'time', 'models', 'grow', 'translation', 'mute', 'mode']);
  const migrated = [...bar.children].map((c) => c.dataset.sb);
  const modelsIdx = migrated.indexOf('models');
  check(migrated[modelsIdx + 1] === 'stt' && migrated[modelsIdx + 2] === 'tts',
    'Expected stt and tts right after models on migration, got: ' + JSON.stringify(migrated));

  // Direct Settings navigation + viewport scroll checks for all 4 session-bar targets.
  const inSetBodyView = (selector) => {
    const body = document.getElementById('set-body');
    const target = body.querySelector(selector);
    if (!target) return false;
    const br = body.getBoundingClientRect();
    const tr = target.getBoundingClientRect();
    return tr.top >= br.top - 4 && tr.top < br.bottom - 24;
  };

  fastBtn.click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'models'
    && inSetBodyView('[data-key="companion.llm.fast_model"]'), 'Fast button did not scroll fast_model into view');

  nav.show('coach');
  deepBtn.click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'models'
    && inSetBodyView('[data-key="companion.llm.deep_model"]'), 'Deep button did not scroll deep_model into view');

  nav.show('coach');
  ttsEl.click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'voice'
    && inSetBodyView('[data-group="voice:Coach voice"]'), 'TTS chip did not scroll Coach voice into view');

  nav.show('coach');
  sttEl.click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'voice'
    && document.getElementById('set-body').scrollTop > 200
    && inSetBodyView('[data-group="voice:Push-to-talk"]'), 'STT chip did not scroll Push-to-talk into view after async panel load');

  return 'main-ok';
})()`,
  });
  if (mainResult.exceptionDetails) {
    throw Error(mainResult.exceptionDetails.exception?.description || mainResult.exceptionDetails.text);
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 960, height: 520, deviceScaleFactor: 1, mobile: false });
  const narrowResult = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const ttsEl = document.querySelector('#sessionbar > [data-sb="tts"]');
  const sttEl = document.querySelector('#sessionbar > [data-sb="stt"]');
  if (getComputedStyle(ttsEl).display === 'none') throw Error('TTS chip must stay visible at 960px');
  if (getComputedStyle(sttEl).display !== 'none') throw Error('STT chip (data-prio=7) should hide at 960px');
  return 'narrow-ok';
})()`,
  });
  if (narrowResult.exceptionDetails) {
    throw Error(narrowResult.exceptionDetails.exception?.description || narrowResult.exceptionDetails.text);
  }

  // Now verify the click-through game overlay (#step + inline #tts badge) in expanded and collapsed modes!
  await send("Emulation.setDeviceMetricsOverride", { width: 560, height: 220, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: `${baseUrl}/overlay.html` });
  for (let i = 0; i < 200; i++) {
    const ready = await send("Runtime.evaluate", { expression: "!!document.getElementById('box')", returnByValue: true });
    if (ready.result?.value) break;
    await sleep(25);
  }

  const overlayResult = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  const listeners = new Map();
  window.glide = {
    async invoke(channel, ...args) {
      const response = await fetch('/ipc', { method: 'POST', body: JSON.stringify({ channel, args }) });
      return response.json();
    },
    on(channel, fn) {
      if (!listeners.has(channel)) listeners.set(channel, new Set());
      listeners.get(channel).add(fn);
      return () => listeners.get(channel).delete(fn);
    },
  };
  const emit = (channel, payload) => {
    for (const fn of listeners.get(channel) || []) fn(payload);
  };
  const check = (cond, why) => { if (!cond) throw Error(why); };
  const raf = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  await import('/renderer/overlay.js');
  await raf();

  const box = document.getElementById('box');
  const tts = document.getElementById('tts');
  check(tts, 'Expected inline #tts badge in overlay.html');
  check(tts.hidden, '#tts must be hidden when idle (ready)');

  for (const collapsed of [false, true]) {
    emit('app:settings', { overlay: { collapsed, opacity: 0.85, fontSize: 13 } });
    await raf();
    const baseHeight = Math.round(box.getBoundingClientRect().height * 10) / 10;

    for (const [phase, expectedText] of [
      ['generating', 'Generating…'],
      ['speaking', 'Speaking'],
      ['fallback', 'SAPI5 fallback'],
      ['error', 'TTS error'],
    ]) {
      emit('tts:status', {
        provider: 'edge', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)',
        phase, fallbackVoice: 'Xenia', updatedAt: Date.now(),
      });
      await raf();
      check(!tts.hidden, '#tts should be visible for phase=' + phase + ' (collapsed=' + collapsed + ')');
      check(tts.textContent.trim() === expectedText, 'Expected ' + expectedText + ', got ' + tts.textContent);
      const h = Math.round(box.getBoundingClientRect().height * 10) / 10;
      check(Math.abs(h - baseHeight) === 0,
        'Overlay #box height bounced for phase=' + phase + ' (collapsed=' + collapsed + '): ' + baseHeight + ' -> ' + h);
    }

    for (const idlePhase of ['ready', 'connecting', 'no-key', 'muted']) {
      emit('tts:status', {
        provider: 'edge', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)',
        phase: idlePhase, updatedAt: Date.now(),
      });
      await raf();
      check(tts.hidden, '#tts must hide on idle phase=' + idlePhase);
      const h = Math.round(box.getBoundingClientRect().height * 10) / 10;
      check(Math.abs(h - baseHeight) === 0, 'Overlay height changed returning to ' + idlePhase);
    }
  }
  // Post-fallback hold on overlay auto-hides after 5 s from updatedAt without vertical height bounce.
  emit('tts:status', {
    provider: 'edge', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)',
    phase: 'fallback', fallbackVoice: 'Xenia', holding: true, updatedAt: Date.now() - 4940,
  });
  check(!tts.hidden, '#tts should still show just before 5s hold expiry');
  await new Promise((r) => setTimeout(r, 90));
  check(tts.hidden, '#tts must auto-hide once 5s overlay hold expires');
  return 'OK: 48px session bar, split Fast/Deep + STT + TTS navigation, migration, 0px horizontal shift, and 0px overlay height bounce';
})()`,
  });
  if (overlayResult.exceptionDetails) {
    throw Error(overlayResult.exceptionDetails.exception?.description || overlayResult.exceptionDetails.text);
  }
  console.log(overlayResult.result.value);
} finally {
  ws?.close();
  if (process.platform === "win32") spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  else child.kill();
}
