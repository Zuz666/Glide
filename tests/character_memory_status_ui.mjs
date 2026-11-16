import fs from 'node:fs';
import path from 'node:path';

// Runs against the real renderer and its existing IPC/stream seams, never a paid provider.
export async function runCharacterMemoryChecks(send) {
  const evaluate = async (fn, ...args) => {
    const result = await send('Runtime.evaluate', {awaitPromise: true, returnByValue: true,
      expression: `(${fn.toString()})(...${JSON.stringify(args)})`});
    if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result?.value;
  };
  await evaluate(installCharacterMemoryFixture);
  try {
    for (const [width, height] of [[2560, 1440], [1100, 800], [800, 800]]) {
      await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
      await evaluate(checkCharacterMemoryStates, width);
      await evaluate(() => { document.getElementById('sb-mode-wrap').focus(); });
      await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r'});
      await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
      await evaluate(() => {
        if (!document.getElementById('character-memory-details').matches(':popover-open')) throw Error('Enter did not open AI details');
      });
      if (process.env.GLIDE_CHARACTER_MEMORY_PROOF_DIR) {
        fs.mkdirSync(process.env.GLIDE_CHARACTER_MEMORY_PROOF_DIR, {recursive: true});
        const image = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
        fs.writeFileSync(path.join(process.env.GLIDE_CHARACTER_MEMORY_PROOF_DIR, `character-memory-${width}.png`), Buffer.from(image.data, 'base64'));
      }
      await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
      await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
      await evaluate(() => {
        if (document.getElementById('character-memory-details').matches(':popover-open')
            || document.activeElement !== document.getElementById('sb-mode-wrap')) throw Error('Escape did not dismiss details and return focus');
      });
      await send('Input.dispatchKeyEvent', {type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, text: ' '});
      await send('Input.dispatchKeyEvent', {type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32});
      await evaluate(() => {
        if (!document.getElementById('character-memory-details').matches(':popover-open')) throw Error('Space did not open AI details');
      });
      // Native light dismiss in the desktop document, rather than inside its separate coach iframe.
      const outside = await evaluate(() => {
        const r = document.querySelector('#rail .brand').getBoundingClientRect();
        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
      });
      await send('Input.dispatchMouseEvent', {type: 'mousePressed', ...outside, button: 'left', clickCount: 1});
      await send('Input.dispatchMouseEvent', {type: 'mouseReleased', ...outside, button: 'left', clickCount: 1});
      await evaluate(() => {
        if (document.getElementById('character-memory-details').matches(':popover-open')) throw Error('Outside click did not dismiss AI details');
      });
    }
    // A page shortcut (such as Guides' selected-inspector Escape) must not consume a popup close.
    await evaluate(() => {
      const fixture = window.__characterMemoryFixture;
      fixture.pageEscapeCount = 0;
      fixture.pageShortcut = event => {
        if (event.key === 'Escape') { fixture.pageEscapeCount++; event.preventDefault(); }
      };
      document.addEventListener('keydown', fixture.pageShortcut);
      document.getElementById('sb-mode-wrap').focus();
      document.getElementById('sb-mode-wrap').click();
    });
    await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
    await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
    await evaluate(() => {
      if (window.__characterMemoryFixture.pageEscapeCount
          || document.getElementById('character-memory-details').matches(':popover-open')
          || document.activeElement !== document.getElementById('sb-mode-wrap'))
        throw Error('Popup Escape reached an underlying page shortcut instead of closing and returning focus');
    });
    await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
    await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
    await evaluate(() => {
      const fixture = window.__characterMemoryFixture;
      if (fixture.pageEscapeCount !== 1) throw Error('Closed popup suppressed the normal page shortcut');
      document.removeEventListener('keydown', fixture.pageShortcut);
    });
    await evaluate(checkCharacterMemoryLifecycle);
    await evaluate(prepareCharacterMemoryNavigation);
    const key = async (name, code, virtualKey) => {
      await send('Input.dispatchKeyEvent', {type: 'keyDown', key: name, code, windowsVirtualKeyCode: virtualKey, ...(name === 'Enter' ? {text: '\r'} : {})});
      await send('Input.dispatchKeyEvent', {type: 'keyUp', key: name, code, windowsVirtualKeyCode: virtualKey});
    };
    await key('Tab', 'Tab', 9);
    await evaluate(() => {
      if (document.activeElement?.id !== 'character-memory-roleplay') throw Error('Details Tab order did not reach Roleplay settings');
    });
    await key('Enter', 'Enter', 13);
    await evaluate(async () => {
      const {until, nav} = window.__characterMemoryFixture;
      await until(() => nav.currentPage() === 'settings' && document.activeElement?.closest('[data-key="companion.llm.persona"]'),
        'Keyboard Roleplay navigation did not focus Character field');
      if (document.getElementById('character-memory-details').matches(':popover-open')) throw Error('Roleplay navigation left details open');
      document.getElementById('sb-mode-wrap').focus();
    });
    await key('Enter', 'Enter', 13);
    await key('Tab', 'Tab', 9);
    await key('Tab', 'Tab', 9);
    await evaluate(() => {
      if (document.activeElement?.id !== 'character-memory-memory') throw Error('Details Tab order did not reach Memory page');
    });
    await key('Enter', 'Enter', 13);
    await evaluate(checkCharacterMemoryNavigation);
  } finally {
    await evaluate(async () => { await window.__characterMemoryFixture.restore(); delete window.__characterMemoryFixture; });
    await send('Emulation.setDeviceMetricsOverride', {width: 2560, height: 1440, deviceScaleFactor: 1, mobile: false});
  }
}

async function installCharacterMemoryFixture() {
  const {live} = await import('/renderer/state.js');
  const sb = await import('/renderer/session-bar.js');
  const coach = await import('/renderer/coach.js');
  const nav = await import('/renderer/nav.js');
  const fields = await import('/renderer/settings/fields.js');
  const savedLive = {...live}, savedPage = nav.currentPage();
  const savedEventSource = window.EventSource, savedInvoke = window.glide.invoke, savedFetch = window.fetch;
  const frame = document.getElementById('dash');
  const savedFrame = {src: frame.getAttribute('src'), className: frame.className};
  const streams = [], calls = [], requests = [];
  class FixtureStream extends EventTarget {
    constructor(url) { super(); this.url = url; this.closed = false; streams.push(this); }
    close() { this.closed = true; }
    emit(type, value) { this.dispatchEvent(type === 'error' ? new Event(type) : new MessageEvent(type, {data: JSON.stringify(value)})); }
  }
  window.EventSource = FixtureStream;
  window.glide.invoke = async (channel, ...args) => {
    calls.push(channel);
    return savedInvoke(channel, ...args);
  };
  window.fetch = async (input, init) => {
    const url = String(input); requests.push([url, init?.method || 'GET']);
    if (url.includes('/character-memory-engine/')) return new Response('null', {headers: {'Content-Type': 'application/json'}});
    return savedFetch(input, init);
  };
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const until = async (condition, message) => {
    const deadline = performance.now() + 5000;
    while (!condition()) {
      if (performance.now() > deadline) throw Error(message);
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  const base = {character: {enabled: true, name: 'Thrall'}, learning: {enabled: true, available: true, reason: ''},
    recall: {method: 'meaning', state: 'ready', source: 'OpenAI', model: 'text-embedding-3-small', detail: 'Current service access observed.'}};
  const state = (projection = base, mode = 'live') => ({session_minutes: 42,
    coach: {mode, calls: 7, cost_usd: 0.125, ...(projection === undefined ? {} : {character_memory: projection})},
    models: {fast: 'claude-haiku-5-5', fast_name: 'Claude Haiku 5.5', deep: 'claude-sonnet-4-5', deep_name: 'Sonnet 4.5', listen: 'whisper small'}});
  nav.show('coach');
  window.__characterMemoryFixture = {live, sb, coach, nav, fields, streams, calls, requests, check, until, base, state,
    async restore() {
      if (window.__characterMemoryFixture.pageShortcut)
        document.removeEventListener('keydown', window.__characterMemoryFixture.pageShortcut);
      document.getElementById('character-memory-details').hidePopover();
      coach.onEngine({state: 'stopped', url: null, token: '', detail: '', port: 0});
      window.EventSource = savedEventSource; window.glide.invoke = savedInvoke; window.fetch = savedFetch;
      Object.assign(live, savedLive);
      if (savedFrame.src === null) frame.removeAttribute('src'); else frame.src = savedFrame.src;
      frame.className = savedFrame.className;
      nav.show(savedPage);
    }};
}

async function checkCharacterMemoryStates(width) {
  const {check, sb, base, state, live, fields} = window.__characterMemoryFixture;
  const button = document.getElementById('sb-mode-wrap'), popup = document.getElementById('character-memory-details');
  const icons = [...document.querySelectorAll('#sb-character-memory > [role="img"]')];
  check(icons.length === 3 && new Set(icons.map(el => el.querySelector('use')?.getAttribute('href'))).size === 3,
    'Character, learning and recall need three distinct shared-sprite icons');
  check(button.tagName === 'BUTTON' && button.parentElement.id === 'sessionbar' && button.dataset.sb === 'mode',
    'AI details must retain one draggable root mode item');
  const children = [...button.parentElement.children];
  sb.sessionBar(state());
  const meaningIcon = icons[2].querySelector('use').getAttribute('href');
  sb.sessionBar(state({...base, recall: {method: 'words', state: 'words', source: '', model: '', detail: ''}}));
  check(icons[2].querySelector('use').getAttribute('href') !== meaningIcon, 'Word and meaning recall must differ visibly without opening details');
  sb.sessionBar(state());
  const geometry = children.map(el => { const r = el.getBoundingClientRect(); return [r.left, r.width]; });
  const projection = (patch) => ({...base, ...patch});
  const cases = [
    [base, ['Enabled', 'Enabled', 'Meaning-based recall available']],
    [projection({character: {enabled: false, name: ''}, recall: {...base.recall, state: 'waiting_character'}}), ['Disabled', 'Enabled', 'Waiting for Character']],
    [projection({learning: {enabled: false, available: false, reason: 'Automatic learning disabled.'}, recall: {method: 'words', state: 'words', source: '', model: '', detail: ''}}), ['Enabled', 'Disabled', 'Word-based recall']],
    [projection({learning: {enabled: true, available: false, reason: 'AI is paused.'}}), ['Enabled', 'Paused', 'Meaning-based recall available']],
    ...[
      ['unavailable', 'Unavailable'], ['unchecked', null], ['preparing', 'Preparing'],
      ['partial', 'Partially prepared'], ['empty', 'No memories to prepare'], ['error', 'Warning'],
    ].map(([state, label]) => [projection({recall: {...base.recall, state, detail: 'Preparation failed. Query failed; word-based fallback.'}}), ['Enabled', 'Enabled', label]]),
    [projection({character: {enabled: true, name: 'A very long Character name '.repeat(20)}}), ['Enabled', 'Enabled', 'Meaning-based recall available']],
  ];
  const marks = new Map();
  for (const [value, labels] of cases) {
    sb.sessionBar(state(value));
    icons.forEach((el, i) => {
      if (labels[i]) check(el.getAttribute('aria-label').includes(labels[i]) && el.title.includes(labels[i]), 'Missing accessible feature state: ' + labels[i]);
      check(el.querySelector('.sb-feature-mark').textContent.trim(), 'State needs a noncolor marking');
    });
    if (value.recall.state === 'unchecked') check(icons[2].dataset.state === 'waiting', 'Unconfirmed recall must remain waiting, not disabled or ready');
    const recallLines = icons[2].title.split('\n');
    check(recallLines.length >= 2 && /[.!?]$/.test(recallLines[0])
      && recallLines[1].endsWith('.'), 'Recall status and selected method need separate complete sentences');
    marks.set(icons[2].dataset.state, icons[2].querySelector('.sb-feature-mark').textContent);
    button.click();
    check(popup.matches(':popover-open') && popup.textContent.includes(value.character.name), 'Details lost the current Character name');
    if (labels[2]) check(popup.textContent.includes(labels[2]), 'Details omitted recall state ' + labels[2]);
    if (value.recall.method === 'meaning') check(popup.textContent.includes(value.recall.source) && popup.textContent.includes(value.recall.model), 'Details lost safe recall source/model');
    if (value.recall.detail) check(popup.textContent.includes(value.recall.detail), 'Independent operation details were hidden');
    const p = popup.getBoundingClientRect();
    check(p.left >= 0 && p.right <= innerWidth && p.bottom <= innerHeight, 'Details overflow viewport at ' + width);
    popup.hidePopover();
    children.forEach((el, i) => {
      const r = el.getBoundingClientRect();
      check(Math.abs(r.left - geometry[i][0]) < .5 && Math.abs(r.width - geometry[i][1]) < .5, 'AI transition changed footer width/position at ' + width);
    });
  }
  check(marks.get('enabled') !== marks.get('warning') && marks.get('waiting') !== marks.get('warning'), 'Recall warning/waiting/enabled markings must differ');
  const previousConfig = live.config, previousDraft = fields.edits.get('companion.llm.persona');
  live.config = {companion: {llm: {persona: 'Unsaved draft', memory_learning: false}}};
  fields.edits.set('companion.llm.persona', 'Unsaved draft');
  sb.sessionBar(state(base));
  check(!button.title.includes('Unsaved draft') && button.title.includes('Thrall'), 'Applied stream must override unsaved settings');
  if (previousDraft === undefined) fields.edits.delete('companion.llm.persona'); else fields.edits.set('companion.llm.persona', previousDraft);
  live.config = previousConfig;
  const reason = 'offline: Spending limit reached — ' + 'long pause reason '.repeat(30);
  sb.sessionBar(state({...base, learning: {enabled: true, available: false, reason: 'AI paused.'}, recall: {...base.recall, state: 'error', detail: 'Preparation failed. Query failed; word-based fallback.'}}, reason));
  check(document.getElementById('sb-mode').textContent === 'Paused: ' + reason.slice(8).trim() && button.title.includes(reason.slice(8).trim()), 'Paused reason must be preserved, only visually truncated');
  button.click();
  check(popup.textContent.includes(reason.slice(8).trim()) && popup.textContent.includes('Query failed'), 'AI pause hid recall warning');
  popup.hidePopover();
  sb.sessionBar(state(base, 'offline'));
  check(document.getElementById('sb-mode').textContent === 'Offline, rules only' && icons[0].getAttribute('aria-label').includes('Enabled'), 'Offline AI must not remove configured Character');
  const bounds = button.parentElement.getBoundingClientRect();
  const visible = children.filter(el => el.dataset.sb !== 'grow' && el.getBoundingClientRect().width > 0);
  visible.forEach((el, i) => {
    const r = el.getBoundingClientRect();
    check(r.left >= bounds.left && r.right <= bounds.right, 'Footer overflow at ' + width);
    visible.slice(i + 1).forEach(other => { const b = other.getBoundingClientRect(); check(Math.min(r.right, b.right) - Math.max(r.left, b.left) <= 1, 'Footer overlap at ' + width); });
  });
  const top = document.getElementById('sb-mode').getBoundingClientRect(), bottom = document.getElementById('sb-character-memory').getBoundingClientRect();
  check(bottom.top >= top.bottom && bottom.bottom <= bounds.bottom && Math.round(bounds.height) === 48, 'AI must fit two stable lines in the 48px footer');
  sb.sessionBar(state({...base, recall: {...base.recall, state: 'error', detail: 'Preparation failed. Query failed; word-based fallback.'}}));
}

async function checkCharacterMemoryLifecycle() {
  const {check, until, sb, state, base, coach, streams, live} = window.__characterMemoryFixture;
  const button = document.getElementById('sb-mode-wrap'), popup = document.getElementById('character-memory-details');
  const unknown = () => [...document.querySelectorAll('#sb-character-memory > [role="img"]')].every(el => el.getAttribute('aria-label').includes('Unknown'));
  for (const value of [null, {}, {coach: {mode: 'live'}}, {coach: {mode: 'live', character_memory: null}}]) {
    sb.sessionBar(state()); button.click(); sb.sessionBar(value);
    check(unknown() && !popup.textContent.includes('Thrall') && popup.textContent.includes('Unknown'), 'Null/missing state left stale success in icons or open details');
    popup.hidePopover();
  }
  const url = location.origin + '/character-memory-engine';
  coach.onEngine({state: 'running', url, token: '', detail: '', port: 1});
  check(unknown(), 'Starting stream kept prior feature success');
  await until(() => streams.length > 0, 'No light stream created');
  const first = streams.at(-1);
  check(first.url.endsWith('/events?lite=1'), 'Status must use existing lightweight stream');
  first.emit('init', {state: state(), messages: []});
  check(button.title.includes('Thrall'), 'Stream init did not render projection');
  button.click(); first.emit('error');
  check(unknown() && !popup.textContent.includes('Thrall'), 'Lost stream left stale success in currently open details');
  first.emit('init', {state: state({...base, character: {enabled: true, name: 'Jaina'}}), messages: []});
  check(button.title.includes('Jaina') && popup.textContent.includes('Jaina'), 'Reconnection did not replace open details');
  coach.onEngine({state: 'running', url: url + '/replacement', token: '', detail: '', port: 2});
  check(first.closed && unknown(), 'Replacing stream must close and clear prior projection');
  await until(() => streams.length > 1, 'Replacement stream not created');
  const second = streams.at(-1);
  second.emit('state', state({...base, character: {enabled: true, name: 'Sylvanas'}}));
  first.emit('state', state()); first.emit('error');
  check(button.title.includes('Sylvanas') && !unknown(), 'Obsolete stream callback overrode current projection');
  coach.onEngine({state: 'stopped', url: null, token: '', detail: '', port: 0});
  second.emit('init', {state: state(), messages: []});
  check(unknown() && !popup.textContent.includes('Sylvanas'), 'Stopped engine accepted obsolete success');
  popup.hidePopover();
  live.engineState = 'stopped';
}

async function prepareCharacterMemoryNavigation() {
  const fixture = window.__characterMemoryFixture;
  const {check, sb, state, calls, requests, fields} = fixture;
  const button = document.getElementById('sb-mode-wrap'), popup = document.getElementById('character-memory-details');
  sb.sessionBar(state());
  fixture.navigationBefore = {edits: JSON.stringify([...fields.edits]), config: await window.glide.invoke('config:get'),
    calls: calls.length, requests: requests.length};
  button.focus(); button.click();
  check(popup.querySelectorAll('input, select, textarea, [role="switch"]').length === 0, 'Details must not expose configuration controls');
  check(calls.length === fixture.navigationBefore.calls && requests.length === fixture.navigationBefore.requests, 'Opening details performed a request');
}

async function checkCharacterMemoryNavigation() {
  const {check, calls, requests, fields, nav, navigationBefore: before} = window.__characterMemoryFixture;
  check(nav.currentPage() === 'memory' && !document.getElementById('character-memory-details').matches(':popover-open'), 'Keyboard Memory link did not navigate/dismiss');
  const configAfter = await window.glide.invoke('config:get');
  check(JSON.stringify(before.config) === JSON.stringify(configAfter) && JSON.stringify([...fields.edits]) === before.edits, 'Details navigation mutated config or drafts');
  check(!calls.slice(before.calls).some(channel => channel.endsWith(':set') || /restart|:start|:test/.test(channel)), 'Details navigation sent a mutating IPC');
  check(requests.slice(before.requests).every(([url, method]) => method === 'GET' ? new URL(url, location.href).origin === location.origin : url === '/ipc'),
    'Details navigation wrote engine state or contacted a provider');
  nav.show('coach');
}
