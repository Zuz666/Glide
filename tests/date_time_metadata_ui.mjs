// Runs inside the existing renderer/browser fixture; no second browser or server is needed.
export async function runDateMetadataChecks(send) {
  const result = await send("Runtime.evaluate", {
    awaitPromise: true,
    returnByValue: true,
    expression: `(${checkDateMetadata.toString()})()`,
  });
  if (result.exceptionDetails) {
    throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  }
  return result.result.value;
}

async function checkDateMetadata() {
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const until = async (condition, message) => {
    const deadline = performance.now() + 5000;
    while (!condition()) {
      if (performance.now() > deadline) throw Error(message);
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  };
  const settle = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const byId = (id) => {
    const element = document.getElementById(id);
    check(element, `Missing renderer fixture #${id}`);
    return element;
  };
  const [memory, addon, logs, updates, releases, models, walkingMap, state, nav] = await Promise.all([
    import('/renderer/memory.js'),
    import('/renderer/addon.js'),
    import('/renderer/logs.js'),
    import('/renderer/updates.js'),
    import('/renderer/whats-new.js'),
    import('/renderer/settings/model-picker.js'),
    import('/renderer/settings/walk-map.js'),
    import('/renderer/state.js'),
    import('/renderer/nav.js'),
  ]);
  const { live } = state;
  const savedLive = { ...live };
  const savedPage = nav.currentPage();
  const savedFocus = document.activeElement;
  const savedSelection = savedFocus instanceof HTMLInputElement || savedFocus instanceof HTMLTextAreaElement
    ? [savedFocus.selectionStart, savedFocus.selectionEnd, savedFocus.selectionDirection] : null;
  const savedScroll = [...document.querySelectorAll('*')]
    .filter((element) => element.scrollTop || element.scrollLeft)
    .map((element) => [element, element.scrollTop, element.scrollLeft]);
  const savedWindowScroll = [window.scrollX, window.scrollY];
  const originalInvoke = window.glide.invoke;
  const originalFetch = window.fetch;
  const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
  const replacements = [];
  const at = Date.UTC(2030, 10, 12, 16, 13, 27);
  const modelId = 'ep:openai:gpt-retired-2030-11-12';
  const diagnostic = 'Raw 2030-11-12T16:13:27Z; model gpt-2030-11-12; took 42s';
  const memoryData = {
    learning: true, persistent: true, kinds: [{ id: 'fact', label: 'Facts' }], characters: [],
    notes: [{ id: 90301, kind: 'fact', source: 'player', text: 'Keep this unsaved note', created: at / 1000, updated: at / 1000 }],
  };
  const guideData = [
    { id: 'date-metadata-guide', guides: ['Metadata guide'], added: at / 1000, rxp: 'done' },
    { id: 'date-metadata-undated', guides: ['Undated guide'], added: 0, rxp: 'done' },
  ];
  const changelog = { versions: [
    { version: '90.0.2', date: '2030-11-12', title: 'Dated release', items: ['Source prose keeps 2030-11-12.'] },
    { version: '90.0.1', date: '2032-02-29', title: 'Leap-day release', items: [] },
    { version: '90.0.0', title: 'Undated release', items: [] },
  ] };
  const reasoning = {
    levels: [], suitability: 'excluded', reason: 'shut down on 2030-11-12',
    note: 'Excluded official OpenAI model (shut down on 2030-11-12).',
  };
  const catalog = {
    models: [], selected: { fast_model: modelId, deep_model: modelId },
    reasoning: { fast: reasoning, deep: reasoning },
    status: { count: 0, updated: at / 1000 },
  };
  const mapData = { state: 'done', finished: at / 1000, maps_done: 2, outdated: [] };
  const logData = [
    { t: at, stream: 'out', text: diagnostic },
    { t: at + 60000, stream: 'err', text: 'Second line stays second' },
  ];
  const sourceData = [memoryData, guideData, changelog, catalog, mapData, logData];
  const sourceBefore = JSON.stringify(sourceData);
  const engineUrl = `${location.origin}/date-metadata-engine`;
  let copied = null;
  let delayedValue = null;
  let delayedEntered = false;
  let releaseDelayed = null;
  let titleObserver = null;
  let draftInput = null;
  let logsWired = false;

  try {
    // Keep the existing page nodes, handlers, settings drafts, and controls intact for the caller.
    for (const id of ['page-memory', 'page-addon', 'page-whatsnew', 'page-logs', 'empty-log', 'app-ver', 'sessionbar']) {
      const original = byId(id);
      const clone = original.cloneNode(true);
      original.replaceWith(clone);
      replacements.push([original, clone]);
    }
    window.glide.invoke = async function (channel, ...args) {
      if (channel === 'config:get') return live.config;
      if (channel === 'addon:status') return { wowDir: 'X:\\isolated-fixture', wowOk: true, installed: '1', bundled: '1', upToDate: true, rxp: true };
      if (channel === 'addon:rxpImports') return guideData;
      if (channel === 'app:changelog') return changelog;
      if (channel === 'keys:list') return { status: [] };
      if (channel === 'app:settings:set') throw Error('Metadata checks must not write application settings');
      if (channel === 'app:dates' && delayedValue !== null && args[0].some((request) => request.value === delayedValue)) {
        delayedValue = null;
        const values = await originalInvoke.call(window.glide, channel, ...args);
        delayedEntered = true;
        await new Promise((resolve) => { releaseDelayed = resolve; });
        return values;
      }
      return originalInvoke.call(window.glide, channel, ...args);
    };
    window.fetch = function (input, options) {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.href.startsWith(`${engineUrl}/`)) {
        check(!options?.method || options.method === 'GET', 'Metadata rendering must not write engine data');
        if (url.pathname === '/date-metadata-engine/memory') return Promise.resolve(Response.json({ memory: memoryData }));
        if (url.pathname === '/date-metadata-engine/models') return Promise.resolve(Response.json(catalog));
        throw Error(`Unexpected metadata engine request: ${url.href}`);
      }
      return originalFetch.call(window, input, options);
    };
    // Clipboard is an output boundary: capture the real Copy handler without changing the OS clipboard.
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { async writeText(value) { copied = value; } },
    });
    Object.assign(live, {
      engineUrl, engineToken: 'isolated-date-metadata', engineState: 'running',
      appSettings: { ...savedLive.appSettings, updateVoice: false },
      config: { wow_dir: '', companion: { llm: { fast_model: modelId, deep_model: modelId } } },
      refresh: null, nav: mapData, reminders: [], speech: {}, update: { state: 'none' },
    });
    window.__restoreDates();
    await memory.refreshMemory();
    await addon.refreshAddon();
    await releases.showWhatsNew();
    logs.wireLogs();
    logsWired = true;
    logs.loadLog(logData);
    logs.renderLog(true);
    byId('log-follow').checked = false;
    const modelHost = document.createElement('div');
    const mapHost = document.createElement('div');
    byId('page-memory').append(modelHost, mapHost);
    await models.renderModelPicker({}, modelHost);
    walkingMap.renderWalkMap({}, mapHost);
    updates.onUpdate({ state: 'none', checkedAt: at });
    nav.show('memory');

    const dateNodes = () => ({
      memory: byId('mem-notes').querySelector('.meta time'),
      guide: byId('rxp-list').querySelector('li:first-child time'),
      release: byId('wn-list').querySelector('.wn-card:first-child .wn-date'),
      leap: byId('wn-list').querySelector('.wn-card:nth-child(2) .wn-date'),
      catalogue: modelHost.querySelector('.field:last-child .lab small time'),
      map: byId('wm-status').querySelector('time'),
      shutdown: modelHost.querySelector('[data-reasoning-note="fast"] time'),
    });
    const expected = {
      memory: '12.11.2030', guide: '12.11.2030', release: '12.11.2030', leap: '29.02.2032',
      catalogue: '12.11.2030 21:13:27', map: '12.11.2030 21:13:27', shutdown: '12.11.2030',
    };
    const labelsMatch = (prefix) => Object.entries(expected)
      .every(([key, value]) => dateNodes()[key]?.textContent === prefix + value);
    const logTimes = () => [...byId('log').querySelectorAll('.ts')].map((element) => element.textContent);
    await until(() => labelsMatch('') && logTimes().join('|') === '21:13:27|21:14:27'
      && byId('app-ver').title === "What's new. Up to date (checked 21:13:27)",
    'Actual metadata consumers did not display native date/date-time/long-time literals');
    check([...byId('empty-log').querySelectorAll('.ts')].map((element) => element.textContent).join('|') === '21:13:27|21:14:27',
      'Coach log tail must use the same long-time role as Logs');
    check(byId('mem-notes').querySelector('.meta').textContent.includes('Added 12.11.2030'), 'Memory must retain the Added date label');
    check(byId('rxp-list').querySelector('li:first-child').textContent.includes('added 12.11.2030'), 'Guide import date must be formatted');
    check(!byId('rxp-list').querySelector('li:nth-child(2) time'), 'An undated guide must stay undated');
    check(byId('wn-list').querySelector('.wn-card:nth-child(3) .wn-date').textContent === '', 'An undated release must stay undated');
    check(byId('wn-list').querySelector('li').textContent === 'Source prose keeps 2030-11-12.', 'Release-note prose is not date metadata');
    check(byId('wm-status').textContent.includes('2 maps rebuilt 12.11.2030 21:13:27'), 'Walking-map build metadata must keep its count and date-time');
    const modelSelect = modelHost.querySelector('[data-pick="fast_model"]');
    const selectedOption = modelSelect.selectedOptions[0];
    const modelNote = modelHost.querySelector('[data-reasoning-note="fast"]');
    await until(() => selectedOption.textContent === `${modelId} (excluded: shut down on 12.11.2030)`
      && modelNote.title === 'Excluded official OpenAI model (shut down on 12.11.2030).',
    'Shutdown reason must localize its calendar date in the selected option and tooltip');
    check(modelSelect.value === modelId, 'A date-bearing model ID must remain unchanged');
    check(byId('log').textContent.includes(diagnostic), 'Diagnostic message bodies must not be rewritten');
    await byId('log-copy').onclick();
    const expectedCopy = '2030-11-12T16:13:27.000Z [out] ' + diagnostic
      + '\n2030-11-12T16:14:27.000Z [err] Second line stays second';
    check(copied === expectedCopy, `Logs Copy must preserve ISO instants and raw messages: ${copied}`);

    // A real editable Memory note remains the same control across preference-only leaf updates.
    byId('mem-notes').querySelector('[data-ed="90301"]').click();
    draftInput = byId('mem-notes').querySelector('[data-edit="90301"]');
    check(draftInput, 'Memory Edit must open the actual note editor');
    draftInput.value = 'An unsaved draft containing 2030-11-12';
    draftInput.dispatchEvent(new Event('input', { bubbles: true }));
    draftInput.focus();
    draftInput.setSelectionRange(3, 18, 'backward');
    await until(() => labelsMatch(''), 'Edited Memory note must retain its date');
    const leaves = dateNodes();
    const scrollPane = byId('page-memory');
    const spacer = document.createElement('div');
    spacer.style.height = '1800px';
    scrollPane.append(spacer);
    scrollPane.scrollTop = 47;
    const scrollTop = scrollPane.scrollTop;
    const assertStableControls = () => {
      check(byId('mem-notes').querySelector('[data-edit="90301"]') === draftInput
        && document.activeElement === draftInput
        && draftInput.value === 'An unsaved draft containing 2030-11-12'
        && draftInput.selectionStart === 3 && draftInput.selectionEnd === 18 && draftInput.selectionDirection === 'backward',
      'Date refresh must preserve the actual focused editor, unsaved draft, and selection');
      check(modelHost.querySelector('[data-pick="fast_model"]') === modelSelect
        && modelSelect.selectedOptions[0] === selectedOption && modelSelect.value === modelId,
      'Date refresh must not replace model controls/options or change selection');
      check(Object.entries(leaves).every(([key, element]) => dateNodes()[key] === element), 'Date refresh must update existing metadata leaves, not rerender consumers');
      check(scrollPane.scrollTop === scrollTop, 'Date refresh must preserve the page scroll position');
    };
    window.__refreshDates();
    await until(() => labelsMatch('Updated ') && logTimes().join('|') === 'Updated 21:13:27|Updated 21:14:27'
      && byId('app-ver').title === "What's new. Up to date (checked Updated 21:13:27)"
      && selectedOption.textContent === `${modelId} (excluded: shut down on Updated 12.11.2030)`
      && modelNote.title === 'Excluded official OpenAI model (shut down on Updated 12.11.2030).',
    'Preference changes must update existing dates, timestamp columns, shutdown reasons, and tooltips');
    assertStableControls();
    await byId('log-copy').onclick();
    check(copied === expectedCopy, 'Localized preference refresh must never alter Logs Copy export');
    window.__restoreDates();
    await until(() => labelsMatch('') && logTimes().join('|') === '21:13:27|21:14:27'
      && byId('app-ver').title === "What's new. Up to date (checked 21:13:27)"
      && selectedOption.textContent === `${modelId} (excluded: shut down on 12.11.2030)`
      && modelNote.title === 'Excluded official OpenAI model (shut down on 12.11.2030).',
    'Restoring preferences must repaint metadata back to native fixture output');
    assertStableControls();

    // Delay only native IPC, then change the real consumer while that older formatting is in flight.
    const paintedTitles = [];
    titleObserver = new MutationObserver((records) => {
      for (const record of records) paintedTitles.push(record.oldValue);
      paintedTitles.push(byId('app-ver').title);
    });
    titleObserver.observe(byId('app-ver'), { attributes: true, attributeFilter: ['title'], attributeOldValue: true });
    delayedValue = at + 1000;
    updates.onUpdate({ state: 'none', checkedAt: at + 1000 });
    await until(() => delayedEntered && releaseDelayed, 'Expected a delayed real app:dates IPC request');
    updates.onUpdate({ state: 'none', checkedAt: at + 60000 });
    releaseDelayed();
    releaseDelayed = null;
    await until(() => byId('app-ver').title === "What's new. Up to date (checked 21:14:27)",
      'Newer update-check timestamp must win over delayed older native formatting');
    await settle();
    check(!paintedTitles.some((title) => title?.includes('21:13:28')), 'A delayed stale timestamp must never repaint the newer update-check label');
    updates.onUpdate({ state: 'checking' });
    check(byId('app-ver').title === 'checking for updates…', 'Checking state must clear the previous dated tooltip');
    updates.onUpdate({ state: 'none', checkedAt: at + 60000 });
    await until(() => byId('app-ver').title === "What's new. Up to date (checked 21:14:27)",
      'Returning to the same checkedAt after clearing must restore its dated tooltip');
    updates.onUpdate({ state: 'checking' });
    window.__refreshDates();
    await until(() => labelsMatch('Updated '), 'Other metadata must still refresh after clearing update tooltip');
    check(byId('app-ver').title === 'checking for updates…', 'Preference refresh must not resurrect a cleared update-check timestamp');
    updates.onUpdate({ state: 'none' });
    check(byId('app-ver').title === "What's new. Up to date", 'Missing checkedAt must keep the existing undated tooltip behavior');
    window.__restoreDates();
    await until(() => labelsMatch(''), 'Restore date preferences before returning to the shared browser checks');
    check(byId('app-ver').title === "What's new. Up to date", 'Restoring preferences must not recreate a missing checkedAt');
    check(JSON.stringify(sourceData) === sourceBefore, 'Date presentation must preserve stored timestamps, calendar dates, model IDs, and diagnostic messages');
    // Native failure reporting must not multiply notifications when Logs requests another timestamp.
    const invokeBeforeFailure = window.glide.invoke;
    let failNativeDates = true, failedCalls = 0;
    const failureToasts = () => [...byId('toast').children].filter((el) => el.textContent.includes('Windows date/time formatting failed')).length;
    const beforeFailureToasts = failureToasts();
    window.glide.invoke = async function (channel, ...args) {
      if (channel === 'app:dates' && failNativeDates) {
        failedCalls++;
        throw Error('GetLocaleInfoEx failed (Windows error 1)');
      }
      return invokeBeforeFailure.call(window.glide, channel, ...args);
    };
    logs.loadLog([{ t: at + 120000, stream: 'out', text: 'Date failure seed' }]);
    logs.renderLog(true);
    await until(() => failedCalls >= 1 && failureToasts() >= beforeFailureToasts + 1, 'Native failure must be visible');
    window.__dateLog({ t: at + 120001, stream: 'app', text: '[date/time] GetLocaleInfoEx failed' });
    await until(() => failedCalls >= 2, 'Delivered Logs must exercise another real failing formatting request');
    await settle();
    check(failureToasts() === beforeFailureToasts + 1, 'Repeated native IPC failures must show only one notification per failure episode');
    failNativeDates = false;
    logs.loadLog([{ t: at + 120000, stream: 'out', text: 'Recovered date formatting' }]);
    logs.renderLog(true);
    await until(() => byId('log').querySelector('.ts')?.textContent === '21:15:27', 'Native formatting must recover without restarting');
    failNativeDates = true;
    logs.loadLog([{ t: at + 180000, stream: 'out', text: 'New failure episode' }]);
    logs.renderLog(true);
    await until(() => failureToasts() === beforeFailureToasts + 2, 'A successful format must rearm notification for the next failure episode');
    window.glide.invoke = invokeBeforeFailure;
    return 'metadata-dates-ok';
  } finally {
    titleObserver?.disconnect();
    releaseDelayed?.();
    delayedValue = null;
    if (draftInput?.isConnected) draftInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    if (logsWired) logs.loadLog([]);
    window.glide.invoke = originalInvoke;
    window.fetch = originalFetch;
    if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor);
    else delete navigator.clipboard;
    Object.assign(live, savedLive);
    for (const [original, clone] of replacements.reverse()) clone.replaceWith(original);
    nav.show(savedPage);
    window.__restoreDates();
    if (savedFocus instanceof HTMLElement && savedFocus.isConnected) {
      savedFocus.focus({ preventScroll: true });
      if (savedSelection && savedSelection[0] !== null) savedFocus.setSelectionRange(...savedSelection);
    }
    savedScroll.forEach(([element, top, left]) => { element.scrollTop = top; element.scrollLeft = left; });
    window.scrollTo(...savedWindowScroll);
  }
}
