// Shared presentation engine for the desktop window and Coach screen. Only timestamp leaves are updated.
// @ts-check
(() => {
  /** @typedef {import('../../desktop/src/shared/date-time').DateTimeRequest} DateTimeRequest */
  /** @typedef {import('../../desktop/src/shared/date-time').DateTimeStyle} DateTimeStyle */
  const selector = '[data-date-value][data-date-style]';
  /** @type {Map<string, string>} */
  const cache = new Map();
  /** @type {Set<HTMLElement>} */
  const queued = new Set();
  /** @type {WeakMap<HTMLElement, string>} */
  let painted = new WeakMap();
  let generation = 0, scheduled = false, busy = false;
  let failureReported = false;
  const native = document.documentElement.hasAttribute('data-date-native')
    || (window.parent !== window && new URLSearchParams(location.hash.slice(1)).get('dates') === 'native');
  /** @type {import('../../desktop/src/shared/date-time').DateTimeFormatter | null} */
  let formatter = native ? null : async (requests) => requests.map(browserText);

  /** @param {DateTimeRequest} request */
  function browserText({ value, style }) {
    let date;
    if (typeof value === 'string') {
      if (style !== 'date' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return '';
      const [year, month, day] = value.split('-').map(Number);
      date = new Date(0);
      date.setUTCFullYear(year, month - 1, day);
      if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return '';
      return date.toLocaleDateString(undefined, { timeZone: 'UTC' });
    }
    date = new Date(value);
    if (!Number.isFinite(value) || !Number.isFinite(date.getTime())) return '';
    if (style === 'date') return date.toLocaleDateString();
    if (style === 'shortTime') return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    if (style === 'longTime') return date.toLocaleTimeString();
    return `${date.toLocaleDateString()} ${date.toLocaleTimeString()}`;
  }

  /** @param {HTMLElement} el @returns {DateTimeRequest} */
  function requestFor(el) {
    const raw = el.dataset.dateValue;
    return { value: el.dataset.dateKind === 'calendar' ? raw : Number(raw),
      style: /** @type {DateTimeStyle} */ (el.dataset.dateStyle) };
  }
  /** @param {DateTimeRequest} request */
  const keyFor = (request) => `${request.style}:${typeof request.value}:${request.value}`;

  /** @param {HTMLElement} el */
  function enqueue(el) {
    if (!el.matches(selector)) return;
    const signature = `${generation}:${keyFor(requestFor(el))}:${el.dataset.datePrefix || ''}:${el.dataset.dateSuffix || ''}:${el.dataset.dateAttribute || ''}`;
    if (painted.get(el) === signature) return;
    queued.add(el);
    schedule();
  }
  function schedule() {
    if (!scheduled && !busy && formatter && queued.size) {
      scheduled = true;
      queueMicrotask(() => { scheduled = false; void flush(); });
    }
  }
  /** @param {ParentNode} root */
  function scan(root) {
    if (root instanceof HTMLElement) enqueue(root);
    root.querySelectorAll(selector).forEach((el) => enqueue(/** @type {HTMLElement} */ (el)));
  }
  async function flush() {
    if (busy || !formatter) return;
    busy = true;
    const version = generation, format = formatter;
    const nodes = [...queued].filter((el) => el.isConnected && el.matches(selector));
    queued.clear();
    const unique = new Map(nodes.map((el) => { const req = requestFor(el); return [keyFor(req), req]; }));
    const missing = [...unique].filter(([key]) => !cache.has(key));
    try {
      for (let offset = 0; offset < missing.length; offset += 256) {
        const batch = missing.slice(offset, offset + 256);
        const values = await format(batch.map(([, req]) => req));
        if (version !== generation) return;
        if (!Array.isArray(values) || values.length !== batch.length || values.some((v) => typeof v !== 'string'))
          throw Error('Invalid date formatting response');
        failureReported = false;
        batch.forEach(([key], i) => cache.set(key, values[i]));
      }
      if (version !== generation) return;
      for (const el of nodes) {
        if (!el.isConnected || !el.matches(selector)) continue;
        const key = keyFor(requestFor(el));
        if (!cache.has(key)) { enqueue(el); continue; }
        const value = `${el.dataset.datePrefix || ''}${cache.get(key)}${el.dataset.dateSuffix || ''}`;
        if (el.dataset.dateAttribute === 'title') {
          if (el.title !== value) el.title = value;
        } else if (el.textContent !== value) el.textContent = value;
        painted.set(el, `${generation}:${key}:${el.dataset.datePrefix || ''}:${el.dataset.dateSuffix || ''}:${el.dataset.dateAttribute || ''}`);
      }
      // Bound historical labels while keeping current DOM values stable; the next render can refill a value.
      while (cache.size > 4096) cache.delete(cache.keys().next().value);
    } catch (error) {
      if (version === generation && !failureReported) {
        failureReported = true;
        document.dispatchEvent(new CustomEvent('date-format-error', { detail: String(error) }));
      }
    } finally {
      busy = false;
      schedule();
    }
  }
  function refresh() {
    ++generation;
    cache.clear();
    painted = new WeakMap();
    scan(document);
  }
  window.glideDates = {
    useFormatter(format) { formatter = format; refresh(); },
    refresh,
    text(value, style) { return cache.get(keyFor({ value, style })) || ''; },
  };
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') {
        const el = /** @type {HTMLElement} */ (record.target);
        // Clearing and rebinding the same value can happen within one observer delivery.
        painted.delete(el);
        enqueue(el);
      }
      else for (const node of record.addedNodes) if (node instanceof HTMLElement) scan(node);
    }
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true,
    attributeFilter: ['data-date-value', 'data-date-style', 'data-date-kind', 'data-date-prefix', 'data-date-suffix', 'data-date-attribute'] });
  window.addEventListener('languagechange', refresh);
  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  scan(document);
})();
