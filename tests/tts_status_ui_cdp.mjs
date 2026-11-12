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

  // Default reasoning badges ("auto") and role icons (#i-mic on Fast, #i-keyboard on Deep when voice_question !== 'deep').
  const fastEff = document.getElementById('sb-fast-eff');
  const deepEff = document.getElementById('sb-deep-eff');
  check(fastEff && deepEff && !fastEff.hidden && !deepEff.hidden, 'Expected visible #sb-fast-eff and #sb-deep-eff badges');
  check(fastEff.textContent === 'auto' && fastEff.dataset.auto === '1', 'Expected default fast effort badge auto, got ' + fastEff.textContent);
  check(deepEff.textContent === 'auto' && deepEff.dataset.auto === '1', 'Expected default deep effort badge auto, got ' + deepEff.textContent);
  check(document.querySelector('#sb-fast-ic use')?.getAttribute('href') === '#i-mic', 'Expected #i-mic on Fast when voice_question defaults to fast');
  check(document.querySelector('#sb-deep-ic use')?.getAttribute('href') === '#i-keyboard', 'Expected #i-keyboard on Deep when voice_question defaults to fast');
  check(fastBtn.title.includes('Reasoning effort: auto') && fastBtn.title.includes('spoken (push-to-talk) questions'),
    'Expected Fast tooltip to include reasoning effort and spoken PTT routing: ' + fastBtn.title);
  check(deepBtn.title.includes('Reasoning effort: auto') && !deepBtn.title.includes('spoken'),
    'Expected Deep tooltip to include reasoning effort without spoken PTT when voice_question=fast: ' + deepBtn.title);

  // Explicit reasoning overrides (low / medium -> med / minimal -> min) + voice_question: 'deep' routing update live.
  sb.sessionBar({
    session_minutes: 42,
    coach: { cost_usd: 0.125, calls: 7, cache_read_tokens: 800, uncached_in_tokens: 200, mode: 'offline' },
    models: {
      fast: 'ep:openai:gpt-6-luna', fast_name: 'gpt-6-luna (OpenAI)', fast_reasoning: 'low',
      deep: 'ep:openai:gpt-6.1-terra', deep_name: 'gpt-6.1-terra (OpenAI)', deep_reasoning: 'medium',
      voice_question: 'deep',
      listen: 'whisper small',
      translation: { language: 'ru', available: true },
    },
  });
  check(fastEff.textContent === 'low' && fastEff.dataset.auto === '0', 'Expected explicit fast effort badge low, got ' + fastEff.textContent);
  check(deepEff.textContent === 'med' && deepEff.dataset.auto === '0', 'Expected compact deep effort badge med for medium, got ' + deepEff.textContent);
  check(document.querySelector('#sb-fast-ic use')?.getAttribute('href') === '#i-zap', 'Expected #i-zap on Fast when voice_question=deep');
  check(document.querySelector('#sb-deep-ic use')?.getAttribute('href') === '#i-mic', 'Expected #i-mic on Deep when voice_question=deep');
  check(fastBtn.title.includes('Reasoning effort: low') && fastBtn.title.includes('spoken questions routed to Deep'),
    'Expected Fast tooltip to report low effort and Deep PTT routing: ' + fastBtn.title);
  check(deepBtn.title.includes('Reasoning effort: medium') && deepBtn.title.includes('Typed & spoken (push-to-talk) questions'),
    'Expected Deep tooltip to report medium effort and spoken PTT routing: ' + deepBtn.title);

  // Stopping coach hides effort badges cleanly, and re-emitting state restores them.
  sb.sessionBar(null);
  check(fastEff.hidden && deepEff.hidden, 'Expected effort badges hidden when coach state is null');
  sb.sessionBar({
    session_minutes: 42,
    coach: { cost_usd: 0.125, calls: 7, cache_read_tokens: 800, uncached_in_tokens: 200, mode: 'offline' },
    models: {
      fast: 'ep:openai:gpt-6-luna', fast_name: 'gpt-6-luna (OpenAI)', fast_reasoning: 'minimal',
      deep: 'ep:openai:gpt-6.1-terra', deep_name: 'gpt-6.1-terra (OpenAI)', deep_reasoning: 'high',
      voice_question: 'deep',
      listen: 'whisper small',
      translation: { language: 'ru', available: true },
    },
  });
  check(!fastEff.hidden && fastEff.textContent === 'min' && deepEff.textContent === 'high',
    'Expected minimal -> min and high badges restored after sessionBar(d)');

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
  // Verify SAPI5 generation latency propagates through tts:status -> speech.onTtsStatus -> voiceLastLine().
  const narratorMod = await import('/renderer/narrator.js');
  narratorMod.speech.configure(false, { provider: 'windows', voiceName: 'Xenia (Russian)' });
  // Simulate an active SAPI5 sample line receiving its started synthMs from WindowsTts via TtsStatus.
  let resolveSapi5;
  const origInvoke = window.glide.invoke;
  window.glide.invoke = async (ch, ...args) => {
    if (ch === 'tts:windowsSpeak') {
      const reqId = args[0];
      emit('tts:status', {
        provider: 'windows', voiceLabel: 'SAPI5 · Xenia', fullVoice: 'Windows SAPI5 (Xenia (Russian))',
        phase: 'speaking', requestId: reqId, actualVoice: 'Xenia (Russian)', synthMs: 2294, updatedAt: Date.now(),
      });
      check(narratorMod.voiceLastLine().includes('Xenia (Russian) (SAPI5), generated in 2.29s,'),
        'Expected immediate SAPI5 generation time on started, got: ' + narratorMod.voiceLastLine());
      return new Promise((r) => { resolveSapi5 = () => r({ voice: 'Xenia (Russian)' }); });
    }
    return origInvoke(ch, ...args);
  };
  const sampleDone = narratorMod.speech.speakAndWait('Тестовая фраза.', 'sample');
  await until(() => !!resolveSapi5);
  resolveSapi5();
  await sampleDone;
  window.glide.invoke = origInvoke;
  check(narratorMod.voiceLastLine().includes('Xenia (Russian) (SAPI5), generated in 2.29s,'),
    'Expected SAPI5 generation time retained after completion, got: ' + narratorMod.voiceLastLine());

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


  // Deep-link navigation for missing voice & model API keys and unconfigured custom endpoints.
  const [keysMod, voicesMod, endpointsMod, mpMod, langMod, whisperMod, stateMod] = await Promise.all([
    import('/renderer/settings/keys.js'),
    import('/renderer/settings/voices.js'),
    import('/renderer/settings/endpoints.js'),
    import('/renderer/settings/model-picker.js'),
    import('/renderer/settings/language.js'),
    import('/renderer/settings/whisper.js'),
    import('/renderer/state.js'),
  ]);
  stateMod.live.speech = { state: 'ready', package: true, model: true, modelName: 'small' };
  page.registerPanels({
    keys: keysMod.renderKeys,
    voices: voicesMod.renderVoices,
    endpoints: endpointsMod.renderEndpoints,
    'model-picker': mpMod.renderModelPicker,
    language: langMod.renderLanguage,
    'speech-install': whisperMod.renderSpeechInstall,
  });

  // Item 1: Credentials tab title, 4 standalone .sgroup panel cards, OpenAI multi-service in model group, and 0 duplicate inputs until expanded.
  page.openSettingsTab('keys');
  await until(() => document.querySelectorAll('.sgroup[data-key-group]').length === 4
    && !!document.querySelector('.sgroup[data-key-group="model"] .fields [data-key-row="OPENAI_API_KEY"]'),
    'Expected 4 standalone .sgroup panel cards in Credentials with OPENAI_API_KEY in model card');
  check(document.querySelector('#set-tabs button[data-tab="keys"]')?.textContent?.includes('Credentials'),
    'Expected keys tab title to read Credentials');
  check(document.querySelectorAll('.key-editor').length === 0, 'Expected 0 expanded duplicate input editors initially');
  document.querySelector('[data-edit-env="OPENAI_API_KEY"]').click();
  await until(() => document.querySelectorAll('.key-editor').length === 1
    && document.activeElement?.getAttribute('data-env') === 'OPENAI_API_KEY',
    'Expected clicking Replace on OPENAI_API_KEY to mount single inline editor');
  document.querySelector('[data-edit-env="GROQ_API_KEY"]').click();
  await until(() => document.querySelectorAll('.key-editor').length === 1
    && document.activeElement?.getAttribute('data-env') === 'GROQ_API_KEY',
    'Expected clicking another row to move the single inline editor rather than duplicating inputs');
  // Async ownership guard: delayed Save on GEMINI_API_KEY -> open GROQ_API_KEY & type draft -> GEMINI settles without discarding GROQ draft/focus; Remove updates row.
  document.querySelector('[data-edit-env="GEMINI_API_KEY"]').click();
  await until(() => document.activeElement?.getAttribute('data-env') === 'GEMINI_API_KEY',
    'Expected GEMINI_API_KEY editor focused');
  const gemInp = document.querySelector('input[data-env="GEMINI_API_KEY"]');
  gemInp.value = 'AIza-delayed-save';
  gemInp.dispatchEvent(new Event('input', { bubbles: true }));
  document.querySelector('[data-save="GEMINI_API_KEY"]').click();
  // Immediately switch to TAVILY_API_KEY and type an unsaved draft while GEMINI_API_KEY save is still in-flight (120ms).
  document.querySelector('[data-edit-env="TAVILY_API_KEY"]').click();
  await until(() => document.activeElement?.getAttribute('data-env') === 'TAVILY_API_KEY',
    'Expected TAVILY_API_KEY editor focused while GEMINI save is in-flight');
  const tavInp = document.querySelector('input[data-env="TAVILY_API_KEY"]');
  tavInp.value = 'tvly-unsaved-draft';
  tavInp.dispatchEvent(new Event('input', { bubbles: true }));
  await until(() => document.querySelector('[data-key-row="GEMINI_API_KEY"] .key-pill')?.textContent?.includes('Saved · …save'),
    'Expected delayed GEMINI_API_KEY save to update GEMINI status pill');
  check(document.querySelectorAll('.key-editor').length === 1
    && document.querySelector('input[data-env="TAVILY_API_KEY"]')?.value === 'tvly-unsaved-draft'
    && document.activeElement?.getAttribute('data-env') === 'TAVILY_API_KEY',
    'Delayed save on GEMINI_API_KEY clobbered TAVILY_API_KEY draft or focus');
  // Same-row edit during delayed Save: editing GEMINI_API_KEY while its save is in-flight preserves the newer draft and keeps editor open.
  document.querySelector('[data-edit-env="GEMINI_API_KEY"]').click();
  await until(() => document.activeElement?.getAttribute('data-env') === 'GEMINI_API_KEY',
    'Expected GEMINI_API_KEY reopened');
  const gemInp2 = document.querySelector('input[data-env="GEMINI_API_KEY"]');
  gemInp2.value = 'AIza-delayed-save';
  gemInp2.dispatchEvent(new Event('input', { bubbles: true }));
  document.querySelector('[data-save="GEMINI_API_KEY"]').click();
  gemInp2.value = 'AIza-newer-same-row-draft';
  gemInp2.dispatchEvent(new Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 170));
  await until(() => document.querySelector('input[data-env="GEMINI_API_KEY"]')?.value === 'AIza-newer-same-row-draft'
    && document.activeElement?.getAttribute('data-env') === 'GEMINI_API_KEY',
    'Same-row edit during delayed save lost newer draft or closed editor');
  // Test Remove on GROQ_API_KEY
  document.querySelector('[data-edit-env="GROQ_API_KEY"]').click();
  await until(() => !!document.querySelector('[data-del="GROQ_API_KEY"]'), 'Expected Remove button on saved GROQ_API_KEY');
  document.querySelector('[data-del="GROQ_API_KEY"]').click();
  await until(() => document.querySelector('[data-key-row="GROQ_API_KEY"] .key-pill')?.textContent === 'Not set'
    && document.querySelectorAll('.key-editor').length === 0,
    'Expected Remove on GROQ_API_KEY to clear saved status and close its editor');

  // Item 2: Language popup heading clarified + TTS setup guide moved to Voice > Coach voice.
  page.openSettingsTab('language');
  await until(() => document.getElementById('language-provider-title')?.textContent === 'Language support by voice service',
    'Expected clarified language help popup heading');
  check(!document.querySelector('#set-body #language-silero'), 'Expected Silero TTS setup guide removed from Language tab');

  // Item 3: Voice service credential note sits consistently below #sv-prov for both OpenAI and Google.
  for (const prov of ['openai', 'google']) {
    await window.glide.invoke('app:settings:set', { speech: { provider: prov } });
    page.openSettingsTab('voice', 'tts');
    await until(() => !!document.getElementById('sv-key-note') && !!document.getElementById('sv-silero'),
      'Expected #sv-key-note and voice setup guide under Coach voice for ' + prov);
    const provRect = document.getElementById('sv-prov').getBoundingClientRect();
    const noteRect = document.getElementById('sv-key-note').getBoundingClientRect();
    check(noteRect.top >= provRect.bottom - 1,
      'Expected #sv-key-note below #sv-prov for ' + prov + ': noteTop=' + noteRect.top + ', provBottom=' + provRect.bottom);
  }
  // Unified Speed & Volume in Voice output (no duplicate sv-rate/sv-vol or companion.voice.rate/volume) + Whisper 3-column alignment.
  check(!!document.querySelector('[data-group="voice:Voice output"] [data-key="app:speech.rate"]')
    && !!document.querySelector('[data-group="voice:Voice output"] [data-key="app:speech.volume"]')
    && !document.getElementById('sv-rate')
    && !document.getElementById('sv-vol')
    && !document.querySelector('[data-key="companion.voice.rate"]')
    && !document.querySelector('[data-key="companion.voice.volume"]'),
    'Expected Speed and Volume unified inside Voice output without duplicates in Coach voice or Windows speech process');
  const fieldsMod = await import('/renderer/settings/fields.js');
  const volumeRow = () => document.querySelector('[data-key="app:speech.volume"]');
  check(volumeRow().querySelector('.rangeval').textContent === '80%',
    'Expected stored narrator volume 0.8 to display as 80%');
  for (const [value, label] of [[0, '0%'], [0.35, '35%'], [0.5, '50%'], [1, '100%']]) {
    const input = volumeRow().querySelector('input');
    input.value = String(value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    check(volumeRow().querySelector('.rangeval').textContent === label,
      'Expected volume label ' + label);
    check(fieldsMod.edits.get('app:speech.volume') === value,
      'Volume edits must retain the fractional storage scale');
  }
  page.openSettingsTab('voice');
  check(volumeRow().querySelector('.rangeval').textContent === '100%',
    'Expected percentage volume label after settings rerender');
  const volumeInput = volumeRow().querySelector('input');
  volumeInput.value = '0.8';
  volumeInput.dispatchEvent(new Event('input', { bubbles: true }));
  check(volumeRow().querySelector('.rangeval').textContent === '80%'
    && !fieldsMod.edits.has('app:speech.volume'),
    'Returning to stored volume must restore 80% and clear the volume edit');
  check(document.querySelector('[data-key="app:speech.rate"] .rangeval').textContent === '1',
    'Speed must remain a multiplier, not a percentage');
  // Test text selection uses the currently displayed source, without sending real speech requests.
  const speech = narratorMod.speech;
  const originalSay = speech.say;
  const samples = [];
  speech.say = (...args) => samples.push(args);
  try {
    await page.renderSettings();
    const source = document.getElementById('sv-sample-source');
    const play = document.getElementById('sv-test');
    await until(() => !play.disabled, 'Expected prepared template to be playable');
    const preview = document.getElementById('sv-sample');
    const template = preview.value;
    check(play.closest('.field').querySelector('.lab small').textContent.includes('Russian')
      && play.closest('.field').querySelector('.lab small').textContent.includes('(ru)'),
      'Voice test must identify the saved Russian speech language');
    check(play.getBoundingClientRect().width < 190,
      'Voice preview button must remain compact');
    play.click();
    check(samples.at(-1)[0] === template, 'Template playback must use the displayed template');
    speech.noteVoice('Windows SAPI5', '', Date.now());
    source.value = 'last';
    source.dispatchEvent(new Event('change', { bubbles: true }));
    check(play.disabled, 'Last-line playback must wait when no line has been spoken');
    speech.noteVoice('Google en-US-Chirp3-HD-Charon', 'Последняя произнесённая фраза.', Date.now(), 1730);
    await until(() => !play.disabled, 'An arriving last-spoken line must enable replay');
    play.click();
    check(samples.at(-1)[0] === 'Последняя произнесённая фраза.',
      'Replay must use the actual last-spoken text rather than the template');
    speech.noteVoice('OpenAI gpt-4o-mini-tts, coral', 'Новая последняя фраза.', Date.now(), 2700);
    await until(() => document.getElementById('sv-sample').value === 'Новая последняя фраза.',
      'Replay preview must follow a newly spoken line');
    play.click();
    check(samples.at(-1)[0] === 'Новая последняя фраза.', 'Replay must snapshot the latest text at click time');
    source.value = 'template';
    source.dispatchEvent(new Event('change', { bubbles: true }));
    play.click();
    check(samples.at(-1)[0] === template, 'Switching back must restore template playback');
    const custom = 'Проверка моего голоса.\\nТекст <без разметки> & без перевода.';
    preview.value = custom;
    preview.dispatchEvent(new Event('input', { bubbles: true }));
    check(source.value === 'custom', 'Editing the preview must select custom text automatically');
    play.click();
    check(samples.at(-1)[0] === custom, 'Custom playback must preserve multiline text literally');
    preview.setSelectionRange(3, 3);
    speech.noteVoice('Windows SAPI5', 'Другая произнесённая фраза.', Date.now(), 400);
    await new Promise((resolve) => requestAnimationFrame(resolve));
    check(preview.value === custom && preview.selectionStart === 3,
      'Incoming speech must not overwrite a custom draft or move its caret');
    source.value = 'last';
    source.dispatchEvent(new Event('change', { bubbles: true }));
    check(preview.value === 'Другая произнесённая фраза.', 'Last-line selection must still follow speech');
    source.value = 'custom';
    source.dispatchEvent(new Event('change', { bubbles: true }));
    check(preview.value === custom, 'Switching sources must preserve the custom draft');
    preview.value = ' \\n ';
    preview.dispatchEvent(new Event('input', { bubbles: true }));
    const played = samples.length;
    play.click();
    check(play.disabled && samples.length === played, 'Whitespace-only custom text must not start speech');
    preview.value = custom;
    preview.dispatchEvent(new Event('input', { bubbles: true }));
    page.openSettingsTab('voice');
    await until(() => document.getElementById('sv-sample') !== preview
      && !!document.getElementById('sv-sample-source'), 'Expected a fresh voice settings render');
    check(document.getElementById('sv-sample-source').value === 'custom'
      && document.getElementById('sv-sample').value === custom,
      'Settings rerenders must retain the selected custom draft');
    document.getElementById('sv-test').click();
    check(samples.at(-1)[0] === custom, 'Custom text must remain playable after rerendering');
    const restoredSource = document.getElementById('sv-sample-source');
    restoredSource.value = 'template';
    restoredSource.dispatchEvent(new Event('change', { bubbles: true }));
  } finally {
    speech.say = originalSay;
  }
  document.getElementById('sv-test').closest('.field').querySelector('button[data-jump-tab="language"]').click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'language'
    && inSetBodyView('[data-key="companion.voice.language"]')
    && !!document.querySelector('[data-key="companion.voice.language"].jump-target'),
    'The displayed speech language must navigate to and highlight its editable setting');
  const draftLanguage = document.querySelector('[data-key="companion.voice.language"] select');
  draftLanguage.value = 'de';
  draftLanguage.dispatchEvent(new Event('change', { bubbles: true }));
  page.openSettingsTab('voice', 'tts');
  await until(() => !!document.getElementById('sv-sample-source'),
    'Expected the voice preview after changing the language draft');
  const languageHelp = document.getElementById('sv-sample-source').closest('.field').querySelector('.lab').textContent;
  check(languageHelp.includes('(de)') && languageHelp.includes('(ru)'),
    'An unsaved German selection must be distinguished from the saved Russian sample language');
  fieldsMod.edits.delete('companion.voice.language');
  const beforeProcessSettings = await window.glide.invoke('app:settings:get');
  const beforeProcessConfig = await window.glide.invoke('config:get');
  await window.glide.invoke('config:set', { companion: { voice: { voice: 'Xenia (Russian)', output: 'app' } } });
  await window.glide.invoke('app:settings:set', { speech: { provider: 'windows', voiceName: 'Xenia (Russian)', windowsProcessUseAppVoice: true } });
  page.openSettingsTab('voice');
  await until(() => document.getElementById('sv-voice')?.options.length >= 3
    && !!document.querySelector('[data-key="companion.voice.voice"] select'),
    'Expected native voice choices and nested process voice controls');
  check(!document.querySelector('[data-group="voice:Windows speech process"]'),
    'Windows speech process must no longer be a separate settings card');
  const processRow = document.querySelector('[data-key="companion.voice.voice"]');
  const customToggle = document.querySelector('[data-key="app:speech.windowsProcessUseAppVoice"] input');
  check(processRow.closest('[data-group]').dataset.group === 'voice:Coach voice',
    'Process voice controls must live inside Coach voice');
  check(!customToggle.checked && getComputedStyle(processRow).display === 'none',
    'Linked process voice must hide the redundant independent selector');
  const processSummary = () => document.getElementById('sv-process-summary').textContent;
  check(processSummary().includes('Xenia (Russian)'),
    'Linked voice summary must identify the app Windows voice');
  customToggle.checked = true;
  customToggle.dispatchEvent(new Event('change', { bubbles: true }));
  check(!processRow.hidden && !processRow.querySelector('select').disabled,
    'Separate voice mode must expose an editable installed-voice selector');
  const processSelect = processRow.querySelector('select');
  processSelect.value = 'Microsoft Zira';
  processSelect.dispatchEvent(new Event('change', { bubbles: true }));
  check(fieldsMod.edits.get('companion.voice.voice') === 'Microsoft Zira',
    'A custom process voice must be staged for the engine settings save');
  check(processSummary().includes('Microsoft Zira') && !processSummary().includes('Xenia (Russian)'),
    'Separate voice summary must show the selected process voice, not the app voice');
  customToggle.checked = false;
  customToggle.dispatchEvent(new Event('change', { bubbles: true }));
  check(!fieldsMod.edits.has('companion.voice.voice'),
    'Returning to shared mode must discard an unsaved independent voice');
  check(processSummary().includes('Xenia (Russian)') && !processSummary().includes('Microsoft Zira'),
    'Returning to shared mode must update the summary to the app voice');
  await window.glide.invoke('config:set', { companion: { voice: { output: 'windows' } } });
  await window.glide.invoke('app:settings:set', { speech: { provider: 'google' } });
  page.openSettingsTab('voice');
  await until(() => !!document.getElementById('sv-process-load'), 'Active Windows process voice must remain configurable with an online provider selected');
  await window.glide.invoke('config:set', { companion: { voice: {
    voice: beforeProcessConfig.companion?.voice?.voice ?? null,
    output: beforeProcessConfig.companion?.voice?.output ?? null,
  } } });
  await window.glide.invoke('app:settings:set', { speech: {
    ...beforeProcessSettings.speech,
    voiceName: beforeProcessSettings.speech?.voiceName ?? '',
    windowsProcessUseAppVoice: beforeProcessSettings.speech?.windowsProcessUseAppVoice ?? null,
  } });
  fieldsMod.edits.delete('app:speech.windowsProcessUseAppVoice');
  page.openSettingsTab('voice');
  await until(() => document.querySelectorAll('#sp-models .rem-row').length === 3,
    'Expected 3 Whisper model rows rendered');
  const spRows = [...document.querySelectorAll('#sp-models .rem-row')];
  const stateLefts = spRows.map((r) => Math.round(r.querySelector('.sp-state').getBoundingClientRect().left));
  check(stateLefts[0] === stateLefts[1] && stateLefts[1] === stateLefts[2],
    'Expected Not downloaded, On this PC, and On this PC, in use to share identical left X, got: ' + JSON.stringify(stateLefts));
  check(!spRows[1].querySelector('.sp-state svg') && !spRows[2].querySelector('.sp-state svg'),
    'Expected no checkmark SVG inside On this PC status labels');
  const dlRect = spRows[0].querySelector('.sp-act').getBoundingClientRect();
  const delRect = spRows[1].querySelector('.sp-act').getBoundingClientRect();
  check(Math.abs(dlRect.width - delRect.width) <= 0.5 && Math.abs(dlRect.left - delRect.left) <= 0.5,
    'Expected Download and Delete buttons to have identical width and X alignment');
  await window.glide.invoke('app:settings:set', { speech: { provider: 'elevenlabs' } });
  page.openSettingsTab('voice', 'tts');
  await until(() => !!document.querySelector('#sv-key-note button[data-jump-target="key:ELEVENLABS_API_KEY"]'),
    'Expected ElevenLabs voice service to render clickable ELEVENLABS_API_KEY jump link');
  document.querySelector('#sv-key-note button[data-jump-target="key:ELEVENLABS_API_KEY"]').click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'keys'
    && !!document.querySelector('[data-key-row="ELEVENLABS_API_KEY"].jump-target')
    && inSetBodyView('[data-key-row="ELEVENLABS_API_KEY"]')
    && document.activeElement?.getAttribute('data-env') === 'ELEVENLABS_API_KEY',
    'Voice jump failed');

  // Model ids: missing ANTHROPIC_API_KEY jumps to ANTHROPIC_API_KEY row; missing ep:srv jumps to Custom endpoints.
  page.openSettingsTab('models', 'fast');
  await until(() => !!document.querySelector('[data-model-req="fast"] button[data-jump-target="key:ANTHROPIC_API_KEY"]'),
    'Expected unconfigured Anthropic fast_model to render ANTHROPIC_API_KEY jump link');
  document.querySelector('[data-model-req="fast"] button[data-jump-target="key:ANTHROPIC_API_KEY"]').click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'keys'
    && document.activeElement?.getAttribute('data-env') === 'ANTHROPIC_API_KEY',
    'Expected model missing-key link to focus ANTHROPIC_API_KEY input');

  page.openSettingsTab('models', 'fast');
  await until(() => !!document.querySelector('[data-key="companion.llm.fast_model"] input'));
  const fastInp = document.querySelector('[data-key="companion.llm.fast_model"] input');
  fastInp.value = 'ep:missing-srv:gpt-4o';
  fastInp.dispatchEvent(new Event('input', { bubbles: true }));
  await until(() => !!document.querySelector('[data-model-req="fast"] button[data-jump-target="Custom endpoints"]'),
    'Expected unconfigured ep:missing-srv model to render Custom endpoints jump link');
  document.querySelector('[data-model-req="fast"] button[data-jump-target="Custom endpoints"]').click();
  await until(() => inSetBodyView('[data-group="models:Custom endpoints"]'),
    'Expected Custom endpoints jump link to scroll Custom endpoints group into view');

  // Non-OpenAI endpoint configured with key_env="OPENAI_API_KEY" must require GLIDE_ENDPOINT_PROXY_KEY (not saved OPENAI_API_KEY).
  fastInp.value = 'ep:proxy:gpt-4o';
  fastInp.dispatchEvent(new Event('input', { bubbles: true }));
  await until(() => !!document.querySelector('[data-model-req="fast"] button[data-jump-target="key:GLIDE_ENDPOINT_PROXY_KEY"]'),
    'Expected non-OpenAI endpoint with key_env=OPENAI_API_KEY to require GLIDE_ENDPOINT_PROXY_KEY');
  document.querySelector('[data-model-req="fast"] button[data-jump-target="key:GLIDE_ENDPOINT_PROXY_KEY"]').click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'keys'
    && document.activeElement?.getAttribute('data-env') === 'GLIDE_ENDPOINT_PROXY_KEY',
    'Expected non-OpenAI endpoint key jump to focus GLIDE_ENDPOINT_PROXY_KEY input');

  // Session bar TTS chip in phase="no-key" jumps straight to that provider's API key row.
  nav.show('coach');
  emit('tts:status', {
    provider: 'elevenlabs', voiceLabel: 'ElevenLabs · George', fullVoice: 'ElevenLabs (eleven_flash_v2_5 · JBFqnCBsd6RMkjVDRZzb)',
    phase: 'no-key', updatedAt: 9000,
  });
  ttsEl.click();
  await until(() => document.querySelector('#set-tabs button.on')?.dataset.tab === 'keys'
    && inSetBodyView('[data-key-row="ELEVENLABS_API_KEY"]')
    && document.activeElement?.getAttribute('data-env') === 'ELEVENLABS_API_KEY',
    'Expected No API key session-bar TTS chip click to jump directly to ELEVENLABS_API_KEY');
  return 'main-ok';
})()`,
  });
  if (mainResult.exceptionDetails) {
    throw Error(mainResult.exceptionDetails.exception?.description || mainResult.exceptionDetails.text);
  }
  for (const width of [2560, 1280]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height: 1440, deviceScaleFactor: 1, mobile: false });
    const jumps = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  const page = await import('/renderer/settings/page.js');
  const keys = await window.glide.invoke('keys:list');
  const routes = [
    ['language', 'companion.voice.language'],
    ['models', 'fast'], ['models', 'deep'],
    ['voice', 'tts'], ['voice', 'stt'],
    ['models', 'Custom endpoints'],
    ...keys.status.map((key) => ['keys', 'key:' + key.env]),
  ];
  for (const [tab, target] of routes) {
    const previous = document.querySelector('#set-body .jump-target');
    page.openSettingsTab(tab, target);
    let frame;
    for (let attempt = 0; attempt < 100; attempt++) {
      frame = document.querySelector('#set-body .jump-target');
      if (frame && frame !== previous) break;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const name = tab + ' > ' + target;
    if (!frame || frame === previous) throw Error('Missing navigation highlight: ' + name);
    const row = frame.matches('.field, .keyrow');
    const boundary = row ? frame.closest('.sgroup').querySelector(':scope > .fields') : frame.closest('.sgroup');
    const actual = frame.getBoundingClientRect(), outer = boundary.getBoundingClientRect();
    if (Math.abs(actual.left - outer.left) > 1 || Math.abs(actual.right - outer.right) > 1) {
      throw Error('Highlight must match the outer card edges: ' + name + ' ' + JSON.stringify({
        frame: [actual.left, actual.right], card: [outer.left, outer.right],
      }));
    }
    const content = [...frame.querySelectorAll('input, select, textarea, button, .lab, .keyrow-head')];
    const before = content.map((element) => element.getBoundingClientRect());
    frame.classList.remove('jump-target');
    const after = content.map((element) => element.getBoundingClientRect());
    frame.classList.add('jump-target');
    for (let i = 0; i < before.length; i++) {
      if (['left', 'top', 'width', 'height'].some((edge) => Math.abs(before[i][edge] - after[i][edge]) > 1)) {
        throw Error('Navigation highlight must not shift or resize its content: ' + name);
      }
    }
    if (target.startsWith('key:') && document.activeElement?.dataset.env !== target.slice(4)) {
      throw Error('Credential navigation must keep focus on its requested editor: ' + name);
    }
  }
  return routes.length;
})()` });
    if (jumps.exceptionDetails) throw Error(jumps.exceptionDetails.exception?.description || jumps.exceptionDetails.text);
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

  let activeEs = null;
  window.EventSource = class {
    constructor(u) { this.url = u; this.handlers = new Map(); activeEs = this; }
    addEventListener(type, fn) {
      if (!this.handlers.has(type)) this.handlers.set(type, new Set());
      this.handlers.get(type).add(fn);
    }
    dispatch(type, payload) {
      for (const fn of this.handlers.get(type) || []) fn({ data: JSON.stringify(payload) });
    }
    close() {}
  };
  await import('/renderer/overlay.js');
  emit('engine:state', { state: 'running', url: 'http://127.0.0.1:59999', port: 59999, detail: '', token: 't' });
  await raf();

  const box = document.getElementById('box');
  const tts = document.getElementById('tts');
  const link = document.getElementById('link');
  check(tts && link, 'Expected inline #tts and #link badges in overlay.html');
  check(tts.hidden && link.hidden, '#tts and #link must be hidden when idle and healthy');

  for (const collapsed of [false, true]) {
    emit('app:settings', { overlay: { collapsed, opacity: 0.85, fontSize: 13 } });
    await raf();
    const baseHeight = Math.round(box.getBoundingClientRect().height * 10) / 10;

    for (const [phase, expectedText] of [
      ['generating', 'TTS synth…'],
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
    // Verify AI thinking… (LLM turn) -> TTS synth… -> Speaking -> idle AND #link ("Game data stopped 4m ago") with 0 px height bounce!
    activeEs.dispatch('thinking', { role: 'question' });
    await raf();
    check(!tts.hidden && tts.classList.contains('thinking') && tts.textContent.trim() === 'AI thinking…',
      'Expected AI thinking… pill during LLM question turn, got: ' + tts.textContent);
    check(Math.abs(Math.round(box.getBoundingClientRect().height * 10) / 10 - baseHeight) === 0,
      'Overlay height bounced during AI thinking… (collapsed=' + collapsed + ')');

    emit('tts:status', {
      provider: 'edge', voiceLabel: 'Edge · Svetlana', fullVoice: 'Edge-TTS (ru-RU-SvetlanaNeural)',
      phase: 'generating', updatedAt: Date.now(),
    });
    await raf();
    check(tts.textContent.trim() === 'TTS synth…', 'Active TTS synth… must take precedence over AI thinking…');
    activeEs.dispatch('thinking', { role: null });
    emit('tts:status', {
      provider: 'edge', voiceLabel: 'Edge · Svetlana', fullVoice: 'Edge-TTS (ru-RU-SvetlanaNeural)',
      phase: 'ready', updatedAt: Date.now(),
    });
    await raf();
    check(tts.hidden, 'Expected #tts hidden once both AI thinking and TTS return to idle');

    // Game data link loss indicator (#link) in both warn and error states with 0 px vertical bounce.
    activeEs.dispatch('state', {
      overlay_step: 'L12  Turn in Kobold Camp Cleanup',
      frames: 45,
      coach: { link_issue: 'Game data stopped 4m ago', link_status: 'warn', thinking: null },
    });
    await raf();
    const afterStepHeight = Math.round(box.getBoundingClientRect().height * 10) / 10;
    check(!link.hidden && link.textContent.trim() === 'Game data stopped 4m ago' && box.classList.contains('link-lost'),
      'Expected visible Game data stopped 4m ago badge, got: ' + link.textContent);
    activeEs.dispatch('state', {
      overlay_step: 'L12  Turn in Kobold Camp Cleanup',
      frames: 45,
      coach: { link_issue: 'Addon mismatch', link_status: 'error', thinking: null },
    });
    await raf();
    check(!link.hidden && link.classList.contains('error') && link.textContent.trim() === 'Addon mismatch',
      'Expected Addon mismatch error badge');
    activeEs.dispatch('state', {
      overlay_step: 'L12  Turn in Kobold Camp Cleanup',
      frames: 46,
      coach: { link_issue: null, link_status: null, thinking: null },
    });
    await raf();
    check(link.hidden && !box.classList.contains('link-lost'), 'Expected #link hidden when frames resume');
    const clearHeight = Math.round(box.getBoundingClientRect().height * 10) / 10;
    check(Math.abs(clearHeight - afterStepHeight) === 0,
      'Overlay height bounced across #link show/hide (collapsed=' + collapsed + '): ' + afterStepHeight + ' -> ' + clearHeight);
    // Push-to-talk pill: its own badge between #link and #tts (red dot while the key is held, gold while
    // transcribing), coexisting with AI thinking… and the link badge, with 0 px height bounce.
    const ptt = document.getElementById('ptt');
    activeEs.dispatch('state', { overlay_step: 'L12  Turn in Kobold Camp Cleanup', frames: 46,
      coach: { link_issue: 'Patchy data (44%)', link_status: 'warn', thinking: null } });
    activeEs.dispatch('thinking', { role: 'question' });
    activeEs.dispatch('ptt', { state: 'listening' });
    await raf();
    check(!link.hidden && getComputedStyle(link).display !== 'none', 'Link pill must show at 560px');
    check(!ptt.hidden && ptt.classList.contains('listening') && ptt.textContent.trim() === 'Listening',
      'Expected a Listening pill, got: ' + ptt.textContent);
    check(getComputedStyle(ptt.querySelector('i')).backgroundColor === 'rgb(255, 98, 87)',
      'The Listening dot must be red');
    check(!tts.hidden && tts.textContent.trim() === 'AI thinking…',
      'AI thinking… must stay up beside the PTT pill');
    check(document.body.classList.contains('ptt-listening'), 'The box glow class must be set while listening');
    check(Math.abs(Math.round(box.getBoundingClientRect().height * 10) / 10 - baseHeight) === 0,
      'Overlay height bounced showing the PTT pill (collapsed=' + collapsed + ')');
    activeEs.dispatch('ptt', { state: 'transcribing' });
    await raf();
    check(!ptt.hidden && ptt.classList.contains('transcribing') && ptt.textContent.trim() === 'Transcribing',
      'Expected a Transcribing pill, got: ' + ptt.textContent);
    check(getComputedStyle(ptt.querySelector('i')).animationDuration === '0.6s',
      'The Transcribing dot must pulse faster than Listening (0.6s), got: '
        + getComputedStyle(ptt.querySelector('i')).animationDuration);
    check(!document.body.classList.contains('ptt-listening'), 'The glow is only while the key is held');
    check(Math.abs(Math.round(box.getBoundingClientRect().height * 10) / 10 - baseHeight) === 0,
      'Overlay height bounced during Transcribing (collapsed=' + collapsed + ')');
    activeEs.dispatch('ptt', { state: 'idle' });
    activeEs.dispatch('thinking', { role: null });
    activeEs.dispatch('state', { overlay_step: 'L12  Turn in Kobold Camp Cleanup', frames: 47,
      coach: { link_issue: null, link_status: null, thinking: null } });
    await raf();
    check(ptt.hidden && tts.hidden && link.hidden, 'All three pills must hide when idle');
  }
  // Post-fallback hold on overlay auto-hides after 5 s from updatedAt without vertical height bounce.
  emit('tts:status', {
    provider: 'edge', voiceLabel: 'Edge · Dmitry', fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)',
    phase: 'fallback', fallbackVoice: 'Xenia', holding: true, updatedAt: Date.now() - 4940,
  });
  check(!tts.hidden, '#tts should still show just before 5s hold expiry');
  await new Promise((r) => setTimeout(r, 90));
  check(tts.hidden, '#tts must auto-hide once 5s overlay hold expires');
  window.__drive = { es: activeEs, emit };   // the narrow-width phase below drives the same live page
  return 'OK: 48px session bar, TTS synth latency, AI thinking -> TTS synth -> Speaking, #link addon loss, PTT pill, and 0px bounce';
})()`,
  });
  if (overlayResult.exceptionDetails) {
    throw Error(overlayResult.exceptionDetails.exception?.description || overlayResult.exceptionDetails.text);
  }
  // Narrow overlay: 240 px is the main process's minimum width; the link pill gives way (the amber box border
  // still reports it) and the remaining two pills — at their widest, Transcribing + SAPI5 fallback — fit whole.
  await send("Emulation.setDeviceMetricsOverride", { width: 240, height: 220, deviceScaleFactor: 1, mobile: false });
  const narrowOverlayResult = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `
(async () => {
  const check = (cond, why) => { if (!cond) throw Error(why); };
  const raf = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const { es, emit } = window.__drive;
  const box = document.getElementById('box');
  const link = document.getElementById('link');
  const ptt = document.getElementById('ptt');
  const tts = document.getElementById('tts');
  const before = Math.round(box.getBoundingClientRect().height * 10) / 10;
  es.dispatch('state', { overlay_step: 'L12  Turn in Kobold Camp Cleanup', frames: 48,
    coach: { link_issue: 'Patchy data (44%)', link_status: 'warn', thinking: null } });
  es.dispatch('ptt', { state: 'transcribing' });
  emit('tts:status', { provider: 'edge', voiceLabel: 'Edge · Dmitry',
    fullVoice: 'Edge-TTS (ru-RU-DmitryNeural)', phase: 'fallback', fallbackVoice: 'Xenia',
    holding: true, updatedAt: Date.now() });
  await raf();
  check(getComputedStyle(link).display === 'none', 'The link pill must give way at 240px');
  check(box.classList.contains('link-lost'), 'The amber link-lost border still reports it');
  check(!ptt.hidden && !tts.hidden, 'PTT and TTS pills must stay visible at 240px');
  const r = (el) => el.getBoundingClientRect();
  check(r(ptt).left >= r(box).left + 10 && r(tts).right <= r(box).right - 10,
    'The two pills must fit inside the box at 240px: ptt.left=' + r(ptt).left
      + ' tts.right=' + r(tts).right + ' box.right=' + r(box).right);
  const h = Math.round(box.getBoundingClientRect().height * 10) / 10;
  check(Math.abs(h - before) === 0, 'Overlay height changed at 240px: ' + before + ' -> ' + h);
  emit('app:settings', { overlay: { collapsed: false, opacity: 0.85, fontSize: 24 } });
  await raf();
  check(r(ptt).left >= r(box).left + 10 && r(tts).right <= r(box).right - 10,
    'The two pills must still fit at 240px with the largest overlay font (24px): tts.right=' + r(tts).right
      + ' box.right=' + r(box).right);
  return 'narrow-overlay-ok';
})()`,
  });
  if (narrowOverlayResult.exceptionDetails) {
    throw Error(narrowOverlayResult.exceptionDetails.exception?.description || narrowOverlayResult.exceptionDetails.text);
  }
  console.log(overlayResult.result.value);
} finally {
  ws?.close();
  if (process.platform === "win32") spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  else child.kill();
}
