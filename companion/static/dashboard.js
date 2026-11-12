// The coach dashboard: a second HUD in the grammar of the game's own interface. companion/dashboard.py serves it
// as one page, this script inlined into dashboard.html with the styles and the icon sprite.
//
// The engine streams the game state (/events); renderState() hands each state to the part of the page that shows
// it, one render function per region, top bar to bottom bar. The sections below follow the page: shared helpers,
// the top bar (unit frame, auras, pace), the quest page, the book's sections, the coach's chat, the minimap and the
// route tracker, the bottom bar; then the book's tabs and docking, arrange mode, the state stream, and the start-up.
// render*() functions draw from a state and can run again at any time; init*() functions wire a part up once.
//
// Checked against the state stream's types (desktop/src/shared/engine.ts) by desktop/tsconfig.dashboard.json, which
// npm run build runs; the types are JSDoc comments only, so the page is still served as it is.

// @ts-check
/** @typedef {import('../../desktop/src/shared/engine').EngineState} EngineState */
/** @typedef {import('../../desktop/src/shared/engine').CommunityFeed} CommunityFeed */
/** @typedef {import('../../desktop/src/shared/engine').InitEvent} InitEvent */
/** @typedef {import('../../desktop/src/shared/engine').CoachMessage} CoachMessage */
/** @typedef {import('../../desktop/src/shared/engine').GuideStep} GuideStep */
/** @typedef {import('../../desktop/src/shared/engine').GuideElement} GuideElement */
/** @typedef {import('../../desktop/src/shared/engine').QuestLogEntry} QuestLogEntry */
/** @typedef {import('../../desktop/src/shared/engine').Buff} Buff */
/** @typedef {import('../../desktop/src/shared/engine').Waypoint} Waypoint */
/** @typedef {import('../../desktop/src/shared/engine').AddonCheck} AddonCheck */
/** @typedef {import('../../desktop/src/shared/engine').GearItem} GearItem */
/** @typedef {import('../../desktop/src/shared/engine').Community} Community */
/** @typedef {import('../../desktop/src/shared/engine').CommunityEvent} CommunityEvent */
/** @typedef {import('../../desktop/src/shared/engine').CommunityNote} CommunityNote */
/** @typedef {import('../../desktop/src/shared/engine').WalkSegment} WalkSegment */
/** @typedef {import('../../desktop/src/shared/engine').TalentPick} TalentPick */
/** @typedef {import('../../desktop/src/shared/engine').LevelHistory} LevelHistory */
/** @typedef {import('../../desktop/src/shared/engine').StepWhy} StepWhy */
/** @typedef {import('../../desktop/src/shared/engine').Pace} Pace */
/** @typedef {import('../../desktop/src/shared/engine').KbStatus} KbStatus */
/** @typedef {import('../../desktop/src/shared/engine').WhyQuest} WhyQuest */
/** @typedef {import('../../desktop/src/shared/engine').WhyAlternative} WhyAlternative */
/** @typedef {import('../../desktop/src/shared/engine').WhyRisk} WhyRisk */

// ---- shared helpers: the DOM, formatting, this browser's storage ----------------------------------------------------

const $ = (id) => document.getElementById(id);

// a POST to the engine: it takes them only with this run's token, which whoever opened this page passed in the URL's
// fragment (/#t=<token>: never sent to a server, never written into the page)
const TOKEN = new URLSearchParams(location.hash.slice(1)).get('t') || '';
const post = (path, body) => fetch(path, { method: 'POST', headers: { 'X-Glide-Token': TOKEN }, body });

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// one of the sprite's icons (icons.svg, inlined into the page)
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const isTime = (s) => s != null && isFinite(s) && s >= 0;

// "1h 5m" or "4m 12s"
function formatDuration(s) {
  if (!isTime(s)) return '–';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

// "4:07", as a speedrun timer shows it
function formatClock(s) {
  if (!isTime(s)) return '–';
  s = Math.round(s);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// a time against the route model: "−0:12" ahead, "+1:05" behind
const formatDelta = (s) => `${s <= 0 ? '−' : '+'}${formatClock(Math.abs(s))}`;

// big figures stay short: 9,800 then 12.3k
const formatCompact = (v) => v >= 10000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v).toLocaleString();

const formatNumber = (v) => typeof v === 'number' ? v.toLocaleString() : esc(v ?? '–');

const formatLevel = (v) => typeof v === 'number' ? v.toFixed(2) : '–';

// Calendar strings are kept distinct from instants; only these leaves change when preferences change.
const dateLabel = (value, style) => `<time data-date-value="${esc(value)}" data-date-kind="${typeof value === 'string' ? 'calendar' : 'instant'}" data-date-style="${style}">${esc(window.glideDates.text(value, style))}</time>`;

if (window.parent !== window && new URLSearchParams(location.hash.slice(1)).get('dates') === 'native') {
  let nextDateRequest = 0;
  const dateRequests = new Map();
  const dateParentOrigin = document.referrer ? new URL(document.referrer).origin : 'null';
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.origin !== dateParentOrigin) return;
    if (event.data?.type === 'glide:datesChanged') window.glideDates.refresh();
    if (event.data?.type !== 'glide:datesResult') return;
    const pending = dateRequests.get(event.data.id);
    if (!pending) return;
    dateRequests.delete(event.data.id);
    clearTimeout(pending.timer);
    if (event.data.error) pending.reject(Error(event.data.error));
    else pending.resolve(event.data.values);
  });
  window.glideDates.useFormatter((requests) => new Promise((resolve, reject) => {
    const id = ++nextDateRequest;
    const timer = setTimeout(() => { dateRequests.delete(id); reject(Error('Date formatting response timed out')); }, 5000);
    dateRequests.set(id, { resolve, reject, timer });
    // Electron's file: parent has an opaque origin. The parent validates the exact frame and engine origin.
    window.parent.postMessage({ type: 'glide:dates', id, requests }, '*');
  }));
}

function timeAgo(ms) {
  const m = Math.round(ms / 60000);
  return m < 60 ? `${m} min ago` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''} ago`;
}

const capitalize = (t) => t ? t.charAt(0).toUpperCase() + t.slice(1) : t;

// "A, B and C"; past max, the rest is counted ("A, B and 3 more")
function andList(items, max = 9) {
  if (items.length > max) items = [...items.slice(0, max), `${items.length - max} more`];
  return items.length > 1 ? items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1] : items[0] || '';
}

const clampPercent = (v) => Math.max(0, Math.min(100, v));

// localStorage, quietly doing without when the browser refuses it
const store = {
  get(key, fallback) {
    try {
      const v = JSON.parse(localStorage.getItem(key));
      return v ?? fallback;
    } catch (e) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {}
  },
};

// ---- the unit frame: level, name, health, power, durability ---------------------------------------------------------
// Health turns amber under half and red under a quarter, power is coloured by its kind, durability turns amber at
// 30% and red at 10% (time to repair); the name is in the class's colour.

const POWER_KIND = { mana: 'mana', rage: 'rage', energy: 'energy', focus: 'focus', runic_power: 'runic', fury: 'rage', insanity: 'mana' };
const CLASS_COLOR = {
  WARRIOR: '#c69b6d', PALADIN: '#f48cba', HUNTER: '#aad372', ROGUE: '#fff468', PRIEST: '#ffffff', SHAMAN: '#2a8cff',
  MAGE: '#3fc7eb', WARLOCK: '#8788ee', DRUID: '#ff7c0a', DEATHKNIGHT: '#c41e3a', MONK: '#00ff98', DEMONHUNTER: '#a330c9',
  EVOKER: '#33937f',
};

/** @param {EngineState} d */
function renderUnitFrame(d) {
  $('live').classList.toggle('on', !!d.fresh);
  $('who').textContent = d.name || 'Connected';
  $('unit').style.setProperty('--class', CLASS_COLOR[String(d.class || '').toUpperCase().replace(/[^A-Z]/g, '')] || 'var(--ink)');
  const raceAndClass = [d.race, d.class].filter(Boolean).map((t) => String(t).charAt(0) + String(t).slice(1).toLowerCase()).join(' ');
  $('guide').textContent = d.fresh ? raceAndClass : (raceAndClass ? raceAndClass + ', game data paused' : 'Game data paused');
  $('lvl').textContent = /** @type {*} */ (d.level);
  $('medal').title = `Level ${d.level}`;
}

const percentSuffix = (v) => v != null && v >= 0 ? ': ' + v + '%' : '';

// one bar of the unit frame (#g<id>, with its fill #g<id>f and its value #g<id>v): a percentage, or unknown
function renderGauge(id, kind, value, alarm, title) {
  const known = value != null && value >= 0, gauge = $('g' + id);
  $('g' + id + 'f').style.width = known ? clampPercent(value) + '%' : '0';
  $('g' + id + 'v').textContent = known ? value + '%' : '–';
  gauge.className = `gauge ${kind}${known && alarm ? ' ' + alarm : ''}`;
  gauge.title = title;
}

/** @param {EngineState} d */
function renderVitals(d) {
  const hp = d.hp, power = d.power, durability = d.durability, kind = (d.power_type || '').toLowerCase();
  renderGauge('hp', 'hp', hp, hp == null ? '' : hp < 25 ? 'bad' : hp < 50 ? 'warn' : '', `Health${percentSuffix(hp)}`);
  const powerName = kind ? kind[0].toUpperCase() + kind.slice(1).replace('_', ' ') : 'Power';
  renderGauge('pw', 'pw', power, 'pw-' + (POWER_KIND[kind] || 'mana'), `${powerName}${percentSuffix(power)}`);

  const dura = $('gdu');
  $('gduf').style.width = durability != null ? clampPercent(durability) + '%' : '0';
  $('gduv').textContent = durability != null ? durability + '%' : '–';
  dura.className = 'dura' + (durability == null ? '' : durability <= 10 ? ' bad' : durability <= 30 ? ' warn' : '');
  dura.title = durability == null ? 'Durability'
    : durability <= 30 ? `Durability ${durability}%: repair soon` : `Durability: ${durability}%`;
}

// ---- auras: the buffs by the unit frame, and the full list in the book ----------------------------------------------
// By the unit frame as the game's own icons (read from the player's client by the engine) with the time under each,
// as many as fit, soonest to run out first; the full list with names in the book.

// an icon tile; with no icon from the client (no install found, a weapon enchant) it shows the buff's initial
/** @param {Buff} b */
const auraIcon = (b) => `<span class="ic" data-l="${esc(String(b.name || '?').trim().charAt(0))}">${
  /^\d+$/.test(b.id) ? `<img src="/icon/${b.id}.png" alt="" data-optional>` : ''}</span>`;

// the time left, as briefly as the game's buff frame puts it: 2h, 14m, 4:07
const auraTimeLeft = (s) => s == null ? '' : (s >= 3600 ? Math.floor(s / 3600) + 'h' : s >= 600 ? Math.ceil(s / 60) + 'm' : formatClock(s));

// how many icons fit by the unit frame at this width
const auraSlots = () => innerWidth >= 2200 ? 16 : innerWidth >= 1800 ? 12 : innerWidth >= 1300 ? 8 : 5;

/** @param {EngineState} d */
function renderBuffs(d) {
  const buffs = d.buffs || [];
  $('buffhdr').textContent = d.buffs_hidden ? 'Hidden in combat' : (buffs.length ? `${buffs.length} active` : '');
  const soonestFirst = [...buffs].sort((a, b) => (a.remaining ?? 1e9) - (b.remaining ?? 1e9));
  renderAuraIcons(soonestFirst, d.buffs_hidden);
  renderBuffList(soonestFirst, d.buffs_hidden);
}

// updated in place, so the icons don't reload every second
/**
 * @param {Buff[]} buffs
 * @param {boolean} hidden
 */
function renderAuraIcons(buffs, hidden) {
  const max = auraSlots(), box = $('buffs'), keep = new Set();
  for (const b of buffs.slice(0, max)) {
    const key = 'a' + b.id;
    keep.add(key);
    let el = /** @type {HTMLElement | null} */ (box.querySelector(`[data-k="${key}"]`));
    if (!el) {
      el = document.createElement('span');
      el.dataset.k = key;
      el.innerHTML = auraIcon(b) + '<b></b>';
    }
    const left = auraTimeLeft(b.remaining);
    el.className = `aura${b.mine ? ' mine' : ''}${b.remaining != null && b.remaining < 60 ? ' low' : ''}`;
    el.title = `${b.name}${left ? ', ' + left + ' left' : ''}, ${b.mine ? 'you cast this' : 'cast by someone else'}`;
    el.setAttribute('aria-label', el.title);
    el.querySelector('b').textContent = left;
    box.appendChild(el);          // appending in order keeps the sort
  }
  for (const el of /** @type {HTMLElement[]} */ ([...box.children])) if (!keep.has(el.dataset.k)) el.remove();
  if (buffs.length > max) {
    const more = document.createElement('span');
    more.className = 'aura more';
    more.dataset.k = 'more';
    more.textContent = `+${buffs.length - max}`;
    more.title = `${buffs.length - max} more in the Buffs tab`;
    box.appendChild(more);
  }
  if (hidden) {
    const note = document.createElement('span');
    note.className = 'note';
    note.dataset.k = 'note';
    note.textContent = 'Hidden in combat';
    box.prepend(note);
  }
}

/**
 * @param {Buff[]} buffs
 * @param {boolean} hidden
 */
function renderBuffList(buffs, hidden) {
  const list = $('bufflist');
  list.className = buffs.length ? '' : 'empty';
  const rows = buffs.map((b) => `<div class="row buff"><span class="n">${auraIcon(b)}${esc(b.name)} <small>${b.mine ? 'yours' : 'from others'}</small></span>`
    + `<span class="p">${auraTimeLeft(b.remaining) || 'on'}</span></div>`);
  list.innerHTML = (hidden ? '<div class="buffnote">The game hides auras in combat; this is the last list read out of combat.</div>' : '')
    + (buffs.length ? rows.join('') : 'No buffs.');
}

// ---- pace: XP per hour with its chart, time to level, pace against the route, the session ---------------------------

/** @param {EngineState} d */
function renderPace(d) {
  const pace = d.pace || {}, benchmark = pace.benchmark_xph;
  $('xph').textContent = d.xp_per_hour ? formatCompact(d.xp_per_hour) : '–';
  addXpSample(d.xp_per_hour, benchmark);
  $('xphbench').textContent = benchmark ? `, route ${formatCompact(benchmark)}` : '';
  $('ttl').textContent = formatDuration(d.ttl_seconds);
  $('ttlcap').textContent = `To level ${d.level + 1}`;

  const figure = $('pace');
  if (typeof pace.ratio === 'number') {
    const p = Math.round((pace.ratio - 1) * 100);
    figure.textContent = (p >= 0 ? '+' : '−') + Math.abs(p).toLocaleString() + '%';
    figure.className = 'figure ' + (p >= 0 ? 'up' : 'down');
    figure.parentElement.title = `${Number(pace.xp_per_hour || 0).toLocaleString()} XP/h against the route's ${Number(pace.benchmark_xph || 0).toLocaleString()}`;
  } else {
    figure.textContent = '–';
    figure.className = 'figure';
  }

  $('sessxp').textContent = d.xp_session != null ? formatCompact(d.xp_session) : '–';
  $('sesscap').textContent = d.session_minutes >= 1 ? `XP in ${formatDuration(d.session_minutes * 60).replace(/ 0s$/, '')}` : 'XP this session';
  $('pendxp').textContent = pace.pending_turnin_xp ? formatCompact(pace.pending_turnin_xp) : '–';
}

// XP per hour over time: the engine keeps the history (GET /xph, two hours, survives restarts); between fetches the
// page adds its own samples. The chart spans 30 minutes, widening to the history's length (two hours at most).
const xpChart = {
  samples: [],      // [time in ms, XP per hour], oldest first
  benchmark: 0,     // the route's XP per hour: the dashed line
  latest: null,     // the latest reading, whether it was sampled or not
};
const CHART_W = 300, CHART_H = 60;                 // the sparkline's viewBox
const CHART_SPAN_MIN = 30 * 60000, CHART_SPAN_MAX = 2 * 3600000;
const CHART_HEADROOM = 1.12;                       // the highest point sits a little under the top
const SAMPLE_EVERY = 20000;                        // ms between the page's own samples
const CHART_GAP = 2 * 60000;                       // a longer pause breaks the line

function fetchXpHistory() {
  fetch('/xph').then((r) => r.json()).then((d) => {
    if (d && Array.isArray(d.samples)) {
      xpChart.samples = d.samples;
      drawXpChart();
    }
  }).catch(() => {});
}

function addXpSample(value, benchmark) {
  const now = Date.now(), samples = xpChart.samples;
  xpChart.benchmark = benchmark || 0;
  if (typeof value === 'number' && value > 0) {
    const last = samples[samples.length - 1];
    if (!last || now - last[0] > SAMPLE_EVERY) samples.push([now, value]);
  }
  xpChart.latest = value;
  drawXpChart();
}

// what the chart shows now: its time span, its top and the samples inside it
function xpChartView() {
  const now = Date.now(), samples = xpChart.samples, first = samples.length ? samples[0][0] : now;
  const span = Math.min(CHART_SPAN_MAX, Math.max(CHART_SPAN_MIN, now - first));
  const points = samples.filter((p) => now - p[0] <= span);
  const top = Math.max(...points.map((p) => p[1]), xpChart.benchmark || 0, 1) * CHART_HEADROOM;
  return { now, span, t0: now - span, top, points };
}

// a gap of more than two minutes (the game was off) breaks the line instead of joining across it
function splitAtGaps(points) {
  const runs = [];
  let run = [];
  for (const p of points) {
    if (run.length && p[0] - run[run.length - 1][0] > CHART_GAP) {
      runs.push(run);
      run = [];
    }
    run.push(p);
  }
  runs.push(run);
  return runs;
}

function drawXpChart() {
  const spark = $('spark'), line = spark.querySelector('.line'), area = spark.querySelector('.area'), benchLine = spark.querySelector('.bench');
  const { span, t0, top, points } = xpChartView();
  const y = (v) => (CHART_H - 2 - (v / top) * (CHART_H - 6)).toFixed(1);
  const x = (t) => ((t - t0) / span * CHART_W).toFixed(1);
  const benchY = xpChart.benchmark > 0 ? y(xpChart.benchmark) : '-10';     // no route pace: the line sits out of view
  benchLine.setAttribute('y1', benchY);
  benchLine.setAttribute('y2', benchY);

  const drawn = points.length >= 2;
  $('xpcempty').hidden = drawn;
  $('xpctop').textContent = drawn ? formatCompact(top / CHART_HEADROOM) : '';
  $('xpcfrom').textContent = drawn ? timeAgo(span) : '';
  if (!drawn) {
    line.setAttribute('d', '');
    area.setAttribute('d', '');
    return;
  }
  const runs = splitAtGaps(points);
  const through = (run) => run.map((p) => `${x(p[0])},${y(p[1])}`).join(' L');
  line.setAttribute('d', runs.map((run) => 'M' + through(run)).join(' '));
  area.setAttribute('d', runs.filter((run) => run.length > 1)
    .map((run) => `M${x(run[0][0])},${CHART_H} L${through(run)} L${x(run[run.length - 1][0])},${CHART_H} Z`).join(' '));
  spark.classList.toggle('below', xpChart.benchmark > 0 && xpChart.latest > 0 && xpChart.latest < xpChart.benchmark);
}

// hover: the sample under the mouse, its time and value, and how it stood against the route
function initXpChartHover() {
  const plot = $('xpcplot'), cursor = $('xpccur'), dot = $('xpcdot'), tip = $('xpctip');
  const hide = () => { cursor.hidden = dot.hidden = tip.hidden = true; };
  plot.addEventListener('pointerleave', hide);
  plot.addEventListener('pointermove', (e) => {
    if (e.buttons) return hide();
    const { t0, span, top, points } = xpChartView();
    if (points.length < 2) return hide();
    const r = plot.getBoundingClientRect(), t = t0 + (e.clientX - r.left) / r.width * span;
    let nearest = points[0];
    for (const p of points) if (Math.abs(p[0] - t) < Math.abs(nearest[0] - t)) nearest = p;
    const [time, value] = nearest;
    const px = (time - t0) / span * r.width, py = (1 - (2 + (value / top) * (CHART_H - 6)) / CHART_H) * r.height;
    cursor.style.left = dot.style.left = px + 'px';
    dot.style.top = py + 'px';
    cursor.hidden = dot.hidden = false;

    const bench = xpChart.benchmark;
    const vsRoute = bench > 0
      ? ` <span class="${value >= bench ? 'up' : 'down'}">${value >= bench ? '+' : '−'}${Math.abs(Math.round((value / bench - 1) * 100))}% vs route</span>`
      : '';
    tip.innerHTML = `<b>${formatCompact(value)}</b> XP/h${vsRoute}<small>${dateLabel(time, 'shortTime')}, ${timeAgo(Date.now() - time)}</small>`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(r.width - tw, px - tw / 2)) + 'px';
  });
}

// ---- the addon: a banner when the installed Glide addon is out of date ----------------------------------------------

/** @param {AddonCheck} addon */
function renderAddonWarning(addon) {
  if (!addon) return;
  const banner = $('addonwarn');
  if (addon.ok === false) {
    banner.hidden = false;
    banner.innerHTML = `${icon('circle-alert')}<span>The Glide addon is out of date: ${esc(addon.problem)}. Fix: ${esc(addon.fix)}.</span>`;
  } else {
    banner.hidden = true;
  }
}

// ---- guide steps: what a step asks, in words (shared by the quest page and the route tracker) -----------------------

const TAG = { accept: 'Accept', turnin: 'Turn in', complete: 'Complete' };   // titles, objectives, the tracker
const VERB = { accept: 'pick up', turnin: 'hand in', complete: 'finish' };   // the quest page's sentence

/** @param {EngineState} d */
const currentStep = (d) => (d.steps || [])[0];

// the step's number: the guide's own index when the step has one
/** @param {EngineState} d */
function stepNumber(d) {
  const step = currentStep(d);
  return step && step.index != null ? step.index : d.step;
}

/** @param {GuideStep} step */
const elementsOf = (step) => (step && step.elements) || [];

// the work a step asks for (accept, turn in, complete), without its travel
/** @param {GuideStep} step */
const tasksOf = (step) => elementsOf(step).filter((e) => e.tag !== 'goto' && (e.name || e.quest != null));
/** @param {GuideElement} e */
const taskKey = (e) => e.tag + ':' + (e.name || e.quest);

// the quest log's entry for a step's quest
/**
 * @param {EngineState} d
 * @param {GuideElement} e
 */
const questOf = (d, e) => e.quest != null ? (d.quests || {})[String(e.quest)] : null;
/** @param {QuestLogEntry} q */
const hasObjectives = (q) => !!(q && q.obj && q.obj.length);
/** @param {QuestLogEntry} q */
const objectiveCounts = (q, separator = ', ') => (q.obj || []).map((o) => o[0] + '/' + o[1]).join(separator);

// one verb per kind of work, so two quests to finish read "Complete A and B", not "Complete A, Complete B"
/**
 * @param {GuideElement[]} tasks
 * @param {Record<string, string>} verbs
 * @param {number} [max]
 */
function groupByVerb(tasks, verbs, max) {
  const namesByTag = new Map();
  for (const e of tasks) {
    const name = String(e.name || e.quest);
    if (!namesByTag.has(e.tag)) namesByTag.set(e.tag, []);
    if (!namesByTag.get(e.tag).includes(name)) namesByTag.get(e.tag).push(name);
  }
  return [...namesByTag].map(([tag, names], i) => {
    const verb = verbs[tag] || tag;
    return `${i ? verb.toLowerCase() : verb} ${andList(names, max)}`;
  });
}

// a step's title: RestedXP's own text, else what the step asks
/** @param {GuideStep} step */
function stepLabel(step) {
  // RestedXP's class filter ("<< Warrior/Paladin") isn't for reading
  if (step && step.text) return step.text.replace(/\s*<<.*$/, '') || step.text;
  const parts = groupByVerb(tasksOf(step), TAG);
  return parts.length ? parts.join(', ') : `Step ${step && step.index != null ? step.index : ''}`;
}

// the quest-giver's mark for a step: ! to accept, ? to turn in, crossed swords to complete, a map pin to travel
/** @param {GuideStep} step */
function stepGlyph(step) {
  const tag = (elementsOf(step).find((e) => e.tag !== 'goto') || {}).tag;
  return tag === 'accept' ? '!' : tag === 'turnin' ? '?' : tag === 'complete' ? icon('swords') : icon('map');
}

// ---- splits: each finished step's time against the route model, recorded as you play --------------------------------

let splits = store.get('glide.splits', []);                         // the last four finished steps, newest first
const onStep = { n: null, text: '', seconds: null, model: null };   // the step being played now

/** @param {EngineState} d */
function trackStep(d) {
  const n = stepNumber(d), why = d.why;
  if (onStep.n != null && n != null && n !== onStep.n && onStep.seconds != null) {
    splits.unshift({ n: onStep.n, text: onStep.text, actual: onStep.seconds, model: onStep.model });
    splits = splits.slice(0, 4);
    store.set('glide.splits', splits);
    onStep.model = null;
  }
  if (n !== onStep.n) {
    onStep.n = n;
    if (!(why && why.n === n)) onStep.model = null;
  }
  // read after the finished step is recorded: on a step change `why` already describes the new step
  if (why && why.n === n && typeof why.time_s === 'number') onStep.model = why.time_s;
  onStep.text = stepLabel(currentStep(d));
  onStep.seconds = d.on_step_seconds;
}

// time on a step against the route model (negative is ahead), or null without a usable model
function timeAgainstModel(actual, model) {
  if (actual == null || model == null || !isFinite(model) || model <= 0) return null;
  return actual - model;
}

function deltaHtml(delta, best) {
  if (delta == null) return '';
  const s = Math.round(delta), cls = best ? 'best' : s <= 0 ? 'ahead' : 'behind';
  return `<span class="${cls}" title="${s <= 0 ? 'faster' : 'slower'} than the route model${best ? ', your best of these' : ''}">${formatDelta(s)}</span>`;
}

// ---- the quest page: the current step on parchment ------------------------------------------------------------------

// the arrow on the quest page turns to where the waypoint is, relative to where you face
const BEARING = {
  'straight ahead': 0, 'ahead to your right': 45, 'to your right': 90, 'behind you to the right': 135, 'behind you': 180,
  'behind you to the left': -135, 'to your left': -90, 'ahead to your left': -45,
};
const COMPASS_DEG = { north: 0, northeast: 45, east: 90, southeast: 135, south: 180, southwest: -135, west: -90, northwest: -45 };
let arrowDeg = 0;

/** @param {Waypoint} waypoint */
function arrowAngle(waypoint) {
  let angle = BEARING[waypoint.relative];
  if (angle == null) {
    angle = COMPASS_DEG[waypoint.direction];
    if (angle == null) return null;
  }
  // turn the short way round from the last angle, so the arrow never spins a full circle
  arrowDeg += ((angle - arrowDeg) % 360 + 540) % 360 - 180;
  return arrowDeg;
}

// one sentence of what the step asks: "Hand in Camping 101 and pick up Into the Thicket with Naleeia Tattermend."
/**
 * @param {GuideStep} step
 * @param {Waypoint} waypoint
 * @param {EngineState} d
 */
function instruction(step, waypoint, d) {
  const tasks = [...new Map(tasksOf(step).map((e) => [taskKey(e), e])).values()];
  // the place is already the heading under the page; this says who, how far along, and what comes after
  const landmark = waypoint.landmark ? String(waypoint.landmark).replace(/^(at|near|by)\s+/i, '') : '';
  const out = [];
  // you finish a quest out in the world, not with someone
  const withSomeone = landmark && tasks.every((e) => e.tag === 'accept' || e.tag === 'turnin');
  if (tasks.length) out.push(capitalize(andList(groupByVerb(tasks, VERB, 3))) + (withSomeone ? ` with ${landmark}` : '') + '.');
  else if (landmark) out.push(`Go to ${landmark}.`);

  const progress = tasks.filter((e) => e.tag === 'complete').map((e) => objectiveProgress(questOf(d, e))).filter(Boolean)[0];
  if (progress) out.push(progress);

  // the next step that asks something new: a later step that only repeats part of this one is skipped
  const mine = new Set(tasks.map(taskKey));
  const next = (d.steps || []).slice(1).find((later) => {
    if (!later || stepLabel(later) === stepLabel(step)) return false;
    const theirs = tasksOf(later);
    return !theirs.length || theirs.some((e) => !mine.has(taskKey(e)));
  });
  if (next) out.push(`Then ${stepLabel(next).replace(/^./, (c) => c.toLowerCase())}.`);
  return out.join(' ');
}

// "2 of 5 done so far.", from a quest's objective counts
/** @param {QuestLogEntry} q */
function objectiveProgress(q) {
  if (!hasObjectives(q)) return null;
  const [done, needed] = q.obj.reduce((sum, o) => [sum[0] + o[0], sum[1] + o[1]], [0, 0]);
  if (!needed) return null;
  return done >= needed ? 'All objectives done, ready to hand in.' : `${done} of ${needed} done so far.`;
}

/** @param {EngineState} d */
function renderQuestPage(d) {
  const page = $('plate'), step = currentStep(d);
  if (!step) {
    page.className = 'leaf idle';
    page.innerHTML = '<h1>No guide step yet</h1><p class="lede">The current RestedXP step shows here once the game is connected and RestedXP is running.</p>';
    return;
  }
  const n = stepNumber(d), waypoint = d.waypoint || {}, why = d.why && d.why.n === n ? d.why : null;
  const here = waypoint.yards != null && waypoint.yards < 5;
  // the page reads like a quest log: what to do and where, then why the route does it now (the knowledge base's
  // why codes in plain words)
  const reasons = why ? Object.values(why.why_text || {}).filter((t) => t && !/does not apply/.test(t)) : [];
  const lede = [instruction(step, waypoint, d), reasons.length ? `Why now: ${reasons.join('; ')}.` : ''].filter(Boolean).map(esc).join(' ');
  const html = questPageHead(d, step, lede) + questPageWay(d, waypoint, here) + questPageColumns(d, step, why) + questPageFoot(d);

  // a new step turns the page; the first step shown, and the first after waiting for the game, just appear
  const turned = page.dataset.step !== String(n);
  page.dataset.step = String(n);
  page.className = 'leaf' + (turned && !page.classList.contains('idle') && page.dataset.seen ? ' turn' : '');
  page.dataset.seen = '1';
  page.innerHTML = html;
}

/**
 * @param {EngineState} d
 * @param {GuideStep} step
 * @param {string} lede
 */
function questPageHead(d, step, lede) {
  return `<div class="head"><div class="glyph" aria-hidden="true">${stepGlyph(step)}</div>`
    + `<div><h1>${esc(stepLabel(step))}</h1>${lede ? `<p class="lede">${lede}</p>` : ''}</div>`
    + `<div class="step-no">Step<b>${esc(stepNumber(d) ?? '?')}</b>${d.guide ? esc(d.guide) : ''}</div></div>`;
}

// the way there: the arrow, the distance, the place, and the time against the route
/**
 * @param {EngineState} d
 * @param {Waypoint} waypoint
 * @param {boolean} here
 */
function questPageWay(d, waypoint, here) {
  const angle = here ? 0 : arrowAngle(waypoint);
  const landmark = waypoint.landmark ? String(waypoint.landmark) : '';
  const at = landmark ? (/^(at|near|by)\s/i.test(landmark) ? landmark : 'at ' + landmark) : '';
  const where = [at, here ? '' : capitalize(waypoint.relative || waypoint.direction || '')].filter(Boolean).join(', ');
  const delta = timeAgainstModel(d.on_step_seconds, onStep.model);
  return '<div class="go">'
    + (angle != null && !here
      ? `<div class="arrow" aria-hidden="true"><svg viewBox="0 0 24 24" style="transform:rotate(${angle || 0}deg)"><path d="M12 2 20 21 12 16.5 4 21Z"/></svg></div>`
      : '')
    + (here ? '<div class="dist">Here</div>' : waypoint.yards != null ? `<div class="dist">${waypoint.yards}<small>yd</small></div>` : '')
    + `<div class="where"><b>${esc(waypoint.area || d.standing_in || d.subzone || d.zone || '')}</b><span>${esc(where)}</span></div>`
    + (delta != null
      ? `<div class="delta ${delta <= 0 ? 'ahead' : 'behind'}" title="Time on this step against the route model"><b>${formatDelta(delta)}</b>`
        + `<span>${delta <= 0 ? 'Ahead of the route' : 'Behind the route'}</span></div>`
      : '')
    + '</div>';
}

/**
 * @param {EngineState} d
 * @param {GuideStep} step
 * @param {StepWhy} why
 */
function questPageColumns(d, step, why) {
  const objectives = questPageObjectives(d, step), rewards = questPageRewards(why);
  return '<div class="cols">'
    + (objectives.length ? `<div><h3>Objectives</h3><ul class="objs">${objectives.join('')}</ul></div>` : '')
    + (rewards.length ? `<div><h3>Rewards</h3><div class="rewards">${rewards.join('')}</div></div>` : '')
    + '</div>';
}

/**
 * @param {EngineState} d
 * @param {GuideStep} step
 */
function questPageObjectives(d, step) {
  const elements = elementsOf(step).filter((e) => e.tag !== 'goto');
  const unique = [...new Map(elements.map((e) => [e.tag + ':' + (e.quest ?? e.name), e])).values()];
  return unique.map((e) => {
    const q = questOf(d, e);
    const counts = e.tag === 'complete' && hasObjectives(q) ? objectiveCounts(q) : '';
    const done = e.done || (e.tag === 'complete' && q && q.done);
    const ready = e.tag === 'turnin' && q && q.done && !e.done;   // objectives met, the turn-in is open
    return `<li class="${done ? 'done' : ''}"><span><span class="tag">${esc(TAG[e.tag] || e.tag)}</span> ${esc(e.name || e.quest || '')}</span>`
      + `<span class="count">${done ? 'Done' : ready ? 'Ready' : esc(counts)}</span></li>`;
  });
}

// the XP the step's quests give at their level (and at yours, when it differs), and the XP it unlocks
/** @param {StepWhy} why */
function questPageRewards(why) {
  const rewards = [];
  for (const q of why ? (why.quests || []) : []) {
    if (!q.xp_at_level) continue;
    const yours = q.xp_your_level != null && q.xp_your_level !== q.xp_at_level
      ? ` <span class="you">(${formatNumber(q.xp_your_level)} for you)</span>` : '';
    rewards.push(`<span><b>${formatNumber(q.xp_at_level)}</b> XP${yours}, ${esc(q.name)}</span>`);
  }
  if (why && why.unlocks_xp) rewards.push(`<span>Unlocks <b>${formatNumber(why.unlocks_xp)}</b> XP later</span>`);
  return rewards;
}

/** @param {EngineState} d */
function questPageFoot(d) {
  return `<div class="foot"><span>On this step <b>${formatClock(d.on_step_seconds)}</b></span>`
    + `<span>Route model <b>${onStep.model != null ? formatClock(onStep.model) : '–'}</b></span>`
    + (d.durability != null && d.durability <= 30 ? `<span class="warn">Durability ${d.durability}%: repair soon</span>` : '')
    + (d.free_bags === 0 ? '<span class="warn">Bags full</span>' : '')
    + '</div>';
}

// before the game connects, the quest page says how to connect it; the empty readouts and docked columns stay away
/** @param {EngineState} d */
function renderWaitingPage(d) {
  const addon = /** @type {AddonCheck} */ ((d && d.addon) || {}), page = $('plate');
  const outdated = addon.installed && addon.expected && addon.installed !== addon.expected;
  const first = !addon.installed ? 'Install the Glide addon from the Addon page.'
    : outdated ? `Update the Glide addon to ${addon.expected}; you have ${addon.installed}.` : `The Glide addon ${addon.installed} is installed.`;
  const items = [
    [first, addon.installed && !outdated],
    ['Log in to a character with the addon enabled, and keep WoW visible: not minimised, with nothing covering its data strip.', false],
    ['Start a RestedXP guide. Its current step opens on this page.', false],
  ];
  const now = items.findIndex(([, done]) => !done);
  page.className = 'leaf idle';
  page.dataset.step = '';
  $('who').textContent = 'No character yet';
  $('guide').textContent = '';      // the page says what to do; the frame doesn't repeat it
  const checklist = items.map(([text, done], i) => done
    ? `<li class="done"><svg class="icon tick" aria-hidden="true"><use href="#i-check"/></svg><span>${esc(text)}<span class="sr"> (done)</span></span></li>`
    : `<li${i === now ? ' class="now" aria-current="step"' : ''}><span>${esc(text)}</span></li>`);
  page.innerHTML = '<h1>Waiting for the game</h1><p class="lede">Glide reads your character from the WoW window and coaches each RestedXP step as you play.</p>'
    + `<ol class="checklist">${checklist.join('')}</ol>`;
}

// ---- the book's sections: why this step, the quest log, gear, talents and levels, community, the session ------------
// (the buffs list is drawn with the auras)

// a row of a section: a name on the left, a value on the right
const row = (name, value, valueClass = '') => `<div class="row"><span class="n">${name}</span><span class="p${valueClass}">${value}</span></div>`;

// a section's body: its rows, or a quiet line when it has none
function fillSection(id, html, emptyText) {
  const el = $(id);
  el.className = html ? '' : 'empty';
  el.innerHTML = html || emptyText;
}

// the analyzer's codes in plain words (same wording as knowledge/api.WHY_NOT_TEXT)
const WHY_NOT = {
  far_objectives: 'objectives far away', low_drop_rate: 'low drop rate', elite: 'elite', group: 'needs a group', long_chain: 'long chain',
  out_of_level: 'wrong level', class_restricted: 'class only', low_xp_per_min: 'low XP per minute', routed_later: 'the route does it later',
  unknown_objective_location: 'objective location unknown',
};
function whyNot(code) {
  const [base, ...rest] = String(code).split(':'), detail = rest.join(':').trim();
  return (WHY_NOT[base] || base.replace(/_/g, ' ')) + (detail ? ` (${detail})` : '');
}
const ROLE = { accept: 'pick up', turnin: 'turn in', objective: 'objectives', gate: 'the route waits on it' };
const roleText = (role) => String(role || '').split('+').map((r) => ROLE[r] || r).join(', ');

const whyLine = (label, body) => `<div class="ln"><span class="lbl">${label}</span>${body}</div>`;

/**
 * @param {StepWhy} why
 * @param {Pace} pace
 * @param {KbStatus} kb
 */
function renderWhy(why, pace, kb) {
  const el = $('why'), header = $('whyhdr');
  if (!why || why.unavailable) {
    el.className = 'why empty';
    header.textContent = '';
    el.textContent = why ? why.unavailable
      : kb && kb.ok === false ? `Knowledge base not loaded: ${kb.reason || 'it builds on the first start'}` : 'Waiting for the first analysed step…';
    return;
  }
  el.className = 'why' + (why.stale ? ' stale' : '');
  header.textContent = `Step ${why.n ?? '?'}${why.kind ? ', ' + why.kind : ''}${why.stale ? ', updating' : ''}`;
  el.innerHTML = [whyFigures(why), whyReasons(why), ...whyDetails(why), whyPlan(why, pace), whyNext(why)].join('');
}

/** @param {StepWhy} w */
function whyFigures(w) {
  const figure = (value, caption) => `<div><b>${value}</b><span>${caption}</span></div>`;
  return '<div class="head">'
    + figure(formatNumber(w.xp_now), 'XP now') + figure(formatNumber(w.unlocks_xp), 'XP it unlocks')
    + figure(formatDuration(w.time_s), 'Modelled time') + figure(typeof w.xp_per_min === 'number' ? Math.round(w.xp_per_min) : '–', 'XP per minute')
    + '</div>';
}

// as on the quest page, the reasons that don't apply to you are left out
/** @param {StepWhy} w */
function whyReasons(w) {
  const text = w.why_text || {}, codes = (w.why || []).filter((c) => !/does not apply/.test(text[c] || ''));
  const reasons = codes.length
    ? codes.map((c) => `<span class="reason" title="${esc(text[c] || c)}">${esc(text[c] || c)}</span>`).join('')
    : '<span class="reason">Quest work only</span>';
  return whyLine('Why', `<span>${reasons}</span>`);
}

/** @param {StepWhy} w */
function whyDetails(w) {
  const lines = [], any = (list) => (list || []).length;
  if (any(w.quests)) lines.push(whyLine('Quests', `<ul>${w.quests.map(whyQuest).join('')}</ul>`));
  if (any(w.enables)) lines.push(whyLine('Unlocks', `<span>${w.enables.map((e) => `${esc(e.quest)} <span class="empty">(step ${e.step ?? '?'})</span>`).join(', ')}</span>`));
  if (any(w.rxp_notes)) lines.push(whyLine('RestedXP notes', `<ul>${w.rxp_notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`));
  if (any(w.co_located)) lines.push(whyLine('Same spot', `<span>${w.co_located.map(esc).join(', ')}</span>`));
  if (any(w.alternatives)) lines.push(whyLine('Instead', `<ul>${w.alternatives.map(whyAlternative).join('')}</ul>`));
  if (any(w.risks)) lines.push(whyLine('Risks', `<ul>${w.risks.map(whyRisk).join('')}</ul>`));
  if (any(w.live)) lines.push(whyLine('You', `<span>${w.live.map(esc).join(', ')}</span>`));
  return lines;
}

/** @param {WhyQuest} q */
function whyQuest(q) {
  return `<li>${esc(q.name)} <span class="empty">${esc(roleText(q.role))}</span>, <span class="num">${formatNumber(q.xp_at_level)} XP</span>`
    + (q.xp_your_level != null ? ` <span class="wn">(you: ${formatNumber(q.xp_your_level)})</span>` : '')
    + (q.unlocks_xp ? `, unlocks ${formatNumber(q.unlocks_xp)}` : '')
    + (q.decay_at_level ? `, <span class="empty">drops at level ${q.decay_at_level}</span>` : '')
    + (q.turnin_step != null && q.role && String(q.role).indexOf('turnin') < 0 ? `, <span class="empty">turn in at step ${q.turnin_step}</span>` : '')
    + (q.turnin_guide ? `, <span class="empty">turn in: ${esc(q.turnin_guide)}</span>` : '')
    + '</li>';
}

/** @param {WhyAlternative} a */
function whyAlternative(a) {
  return `<li>${esc(a.name)} <span class="empty">${esc(a.kind)}</span>`
    + (a.xp != null ? `, ${formatNumber(a.xp)} XP` : '')
    + (a.xp_per_min != null ? `, ${Math.round(a.xp_per_min)} per minute` : '')
    + ((a.why_not || []).length ? `, <span class="wn">skipped: ${a.why_not.map((code) => esc(whyNot(code))).join(', ')}</span>` : '')
    + '</li>';
}

/** @param {WhyRisk} r */
function whyRisk(r) {
  return `<li class="risk">${esc(r.name)}${r.level ? ' level ' + esc(r.level) : ''}${r.elite ? ' (elite)' : ''}`
    + `${r.dist_yd != null ? `, ${Math.round(r.dist_yd)} yd` : ''}${r.note ? ', ' + esc(r.note) : ''}</li>`;
}

// where the plan has you against where you are, and the pace behind it
/**
 * @param {StepWhy} w
 * @param {Pace} pace
 */
function whyPlan(w, pace) {
  const plan = w.plan || {}, p = pace || {}, parts = [];
  if (plan.exp_level != null) {
    const delta = plan.delta != null ? ` (<b class="${plan.delta < -0.5 ? 'wn' : ''}">${plan.delta >= 0 ? '+' : ''}${plan.delta.toFixed(2)}</b>)` : '';
    parts.push(`The plan has you at level ${formatLevel(plan.exp_level)} here; you're ${formatLevel(plan.level_now)}${delta}.`);
  }
  if (p.eta_next_level_s != null || p.your_eta_s != null) {
    parts.push(`Next level in ${formatDuration(p.eta_next_level_s)} on the route, ${formatDuration(p.your_eta_s)} at your pace.`);
  }
  if (p.benchmark_xph) {
    parts.push(`Pace ${formatNumber(p.xp_per_hour)} XP/h against ${formatNumber(p.benchmark_xph)}${typeof p.ratio === 'number' ? ` (${Math.round(p.ratio * 100)}%)` : ''}.`);
  }
  if (p.pending_turnin_xp) parts.push(`${formatNumber(p.pending_turnin_xp)} XP waiting in turn-ins.`);
  return parts.length ? `<div class="plan">${parts.join(' ')}</div>` : '';
}

/** @param {StepWhy} w */
function whyNext(w) {
  const next = w.next;
  if (!next) return '';
  const quests = (next.quests || []).length ? ', ' + next.quests.map(esc).join(', ') : '';
  return whyLine('Next', `<span class="empty">Step ${next.n} ${esc(next.kind || '')}${next.xp ? `, ${formatNumber(next.xp)} XP` : ''}${quests}</span>`);
}

// quest codes: one short code per quest (A to Z, then AA, AB and on), kept in this browser so a quest keeps its code
const questCodes = store.get('glide.qcodes', {});

function qcode(questId) {
  if (questId == null) return '';
  questId = String(questId);
  if (!questCodes[questId]) {
    let code = '', i = Object.keys(questCodes).length;
    do {
      code = String.fromCharCode(65 + i % 26) + code;
      i = Math.floor(i / 26) - 1;
    } while (i >= 0);
    questCodes[questId] = code;
    store.set('glide.qcodes', questCodes);
  }
  return questCodes[questId];
}

// the quest log: ready to turn in first, then the highest level
/** @param {EngineState} d */
function renderQuestLog(d) {
  const entries = Object.entries(d.quests || {});
  $('qhdr').textContent = entries.length ? `${entries.length} quests, ${entries.filter(([, q]) => q.done).length} ready to turn in` : '';
  entries.sort((a, b) => (Number(b[1].done) - Number(a[1].done)) || ((b[1].level || 0) - (a[1].level || 0)));
  const rows = entries.map(([id, q]) => `<div class="row ${q.done ? 'done' : ''}">`
    + `<span class="n"><span class="qcode" title="Quest code">${qcode(id)}</span>${esc(q.name || id)} ${q.level ? `<small>level ${esc(q.level)}</small>` : ''}</span>`
    + `<span class="p">${q.done ? '<span class="gain">Ready to turn in</span>' : objectiveCounts(q, '  ')}</span></div>`);
  fillSection('quests', rows.join(''), 'Your quest log is empty.');
}

// the quest rewards to choose from (the first underlined when it's a gain), the upgrades in your bags, the rest
/** @param {EngineState} d */
function renderGear(d) {
  const upgrades = d.upgrades || [], rewards = d.rewards || [];
  $('wprof').textContent = d.weights_profile || '';
  const others = (d.bag_items || []).filter((b) => !String(b.verdict || '').startsWith('upgrade'));
  const el = $('gear');
  el.className = upgrades.length || rewards.length ? '' : 'empty';
  const reward = (r, i) => `<span class="${i === 0 && r.gain > 0 ? 'best' : ''}">[${esc(r.item)}]${r.gain > 0 ? ` <span class="gain">+${r.gain}</span>` : ''}</span>`;
  const rewardLine = rewards.length ? `<div class="reward"><b>Quest reward</b>${rewards.map(reward).join('')}</div>` : '';
  const upgradeRows = upgrades.length
    ? upgrades.map((u) => row(`[${esc(u.item)}] <small>${esc(u.slot_name)}${u.replaces ? ', over ' + esc(u.replaces) : ''}</small>`,
      u.wait_level ? 'at level ' + u.wait_level : `<span class="gain">+${u.gain}</span>`)).join('')
    : (rewards.length ? '' : 'No upgrades in your bags.');
  const bagNote = others.length ? `<div class="bagnote">In your bags: ${others.map((b) => esc(b.item) + ' (' + esc(b.verdict) + ')').join(', ')}</div>` : '';
  el.innerHTML = rewardLine + upgradeRows + bagNote;
}

/** @param {TalentPick} t */
function talentText(t) {
  if (!t) return null;
  if (t.done) return `${esc(t.build)} is complete`;
  return `${esc(t.talent)}, rank ${t.rank}${t.due ? '' : ` <small>at level ${t.level}</small>`}`;
}

// the next talent of the build, the account's best time for this level, then each level's time against the best
/** @param {EngineState} d */
function renderLevels(d) {
  const talents = d.talent_plan, history = /** @type {LevelHistory} */ (d.level_history || {}), rows = [];
  $('lvhdr').textContent = talents && talents.build ? talents.build + ' build' : '';
  if (talents && !talents.done) {
    const build = talents.matches_forever ? 'RestedXP build' : 'RestedXP Classic build; Forever differs here';
    rows.push(row(`Next talent <small>${build}</small>`, talentText(talents), talents.due && talents.matches_forever ? ' gain' : ''));
  } else if (talents) {
    rows.push(row('Talents', talentText(talents)));
  }
  for (const l of (history.levels || []).slice().reverse()) {
    const best = l.best_minutes;
    const vsBest = best != null && l.minutes > best ? ` <small>best ${best} min${l.best_character ? ' (' + esc(l.best_character) + ')' : ''}</small>`
      : (best != null ? ' <small>your best</small>' : '');
    rows.push(row(`Level ${l.level}${l.deaths ? ` <small>${l.deaths} death${l.deaths > 1 ? 's' : ''}</small>` : ''}`, `${l.minutes} min${vsBest}`));
  }
  const best = history.best_for_current_level;
  if (best) rows.splice(talents ? 1 : 0, 0, row(`Best for level ${d.level} <small>${esc(best.character)}</small>`, `${best.minutes} min`));
  fillSection('levels', rows.join(''), 'No talent plan or level times yet. Level times come from RestedXP and update when you log out or /reload.');
}

/** @param {Community | null | undefined} c */
function renderCommunity(c) {
  if (!c) return;
  $('commhdr').innerHTML = `${c.total} notes, last scan ${c.last_scan ? dateLabel(c.last_scan, 'date') : 'never'}`;
  fillSection('comm', (c.notes || []).map(communityNote).join(''), 'No community notes yet.');
}

/** @param {CommunityNote} note */
function communityNote(note) {
  const source = (note.sources || [])[0] || {};
  return `<div class="cn"><span class="pill ${esc(note.status)}">${esc(note.status)}</span><b>${esc(note.kind)}</b> ${esc(note.claim)}`
    + `<small>${note.published ? dateLabel(note.published, 'date') : ''}${note.levels ? ', levels ' + note.levels.join('–') : ''}${source.medium ? ', ' + esc(source.medium) : ''}`
    + `${/^https?:\/\//i.test(source.url || '') ? `, <a href="${esc(source.url)}" target="_blank" rel="noopener">source</a>` : ''}</small></div>`;
}

// ---- new community notes land: the level-up ---------------------------------------------------------------------
// When the published feed brings notes this install didn't have (community_feed.fresh), the Community emblem rises in
// a column of gold light with rings and rays, as a character levels up, and the game's level-up fanfare plays (read
// from the player's own install: /sound/levelup.ogg; the narrator's mute and volume apply). The emblem keeps a slow
// golden breath until the player opens Community or points at it; that tells the engine they're seen.

const feed = { celebrated: '', seen: '' };

/** @param {CommunityFeed | null | undefined} f */
function renderFeed(f) {
  const fresh = f && f.fresh && f.fresh.at !== feed.seen ? f.fresh : null;
  document.body.classList.toggle('feed-fresh', !!fresh);
  const sec = sectionById.community;
  if (sec) sec.dataset.fresh = fresh ? `${fresh.count} new` : '';
  if (!fresh || feed.celebrated === fresh.at) return;
  feed.celebrated = fresh.at;
  levelUp(fresh);
}

function levelUp(fresh) {
  const emblems = [...document.querySelectorAll('[data-panel="community"] > .emblem')]
    .filter((c) => c.getBoundingClientRect().width > 0);      // on screen (an SVG has no offsetWidth)
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    for (const emblem of emblems) {
      const r = emblem.getBoundingClientRect();
      const burst = Object.assign(document.createElement('div'), { className: 'levelup' });
      Object.assign(burst.style, { left: r.left + r.width / 2 + 'px', top: r.top + r.height / 2 + 'px' });
      burst.innerHTML = '<i class="lu-pillar"></i><i class="lu-rays"></i><i class="lu-ring"></i><i class="lu-ring two"></i>';
      document.body.append(burst);
      setTimeout(() => burst.remove(), 3400);
    }
    const sec = sectionById.community, panel = sec && sec.closest('.dock') ? sec : $('book');
    panel.classList.remove('lu-glow');
    void panel.offsetWidth;                               // restart the glow
    panel.classList.add('lu-glow');
    setTimeout(() => panel.classList.remove('lu-glow'), 3400);
  }
  announce(`${fresh.count} new community note${fresh.count === 1 ? '' : 's'}`);
  if (!voice.muted && voice.volume > 0) {
    const fanfare = new Audio('/sound/levelup.ogg');
    fanfare.volume = Math.min(1, voice.volume);
    fanfare.play().catch(() => {});                       // no install to read it from: the light alone
  }
}

function feedSeen() {
  if (!document.body.classList.contains('feed-fresh')) return;
  document.body.classList.remove('feed-fresh');
  feed.seen = feed.celebrated;
  post('/community/seen', '{}').catch(() => {});
}

// the coach's calls and spend, how grounded it was, the knowledge base and the game link
/** @param {EngineState} d */
function renderSession(d) {
  if (!d.coach) return;
  const c = d.coach, kb = /** @type {KbStatus} */ (d.kb || {});
  const promptTokens = (c.cache_read_tokens || 0) + (c.uncached_in_tokens || 0) + (c.cache_write_tokens || 0);
  const stats = $('stats');
  stats.className = '';
  stats.innerHTML = [
    ['Coach', `${esc(c.mode)}, ${c.calls} calls, $${(c.cost_usd || 0).toFixed(3)}`],
    ['Web searches', c.web_searches || 0],
    ['Cache hits', promptTokens ? Math.round(100 * (c.cache_read_tokens || 0) / promptTokens) + '%' : '–'],
    ['Tokens', `${formatNumber(c.in_tokens)} in, ${formatNumber(c.out_tokens)} out`],
    ['Tool calls', `${c.tool_calls || 0}, ${c.cited || 0} cited, ${c.unsupported || 0} unsupported`],
    ['Errors', c.errors],
    ['Knowledge base', kb.ok ? `${kb.guides || '?'} guides` : 'missing'],
    ['Game link', `${formatNumber(d.frames)} frames read, ${d.decode_rate}% decoded`],
  ].map(([name, value]) => row(name, value)).join('');
}

// ---- the coach's chat: its messages (newest at the bottom, like the game's), the question box, the narrator ---------

// who speaks, by the kind of message
const SPEAKER = {
  callout: 'Coach', answer: 'Coach', banter: 'Coach', review: 'Pace review', briefing: 'Briefing', reminder: 'Reminder', info: 'Glide',
  error: 'Problem', question: 'You',
};
const FEED_MAX = 80;

/** @param {CoachMessage} m */
function showMessage(m) {
  const el = document.createElement('div'), unchecked = new Set(m.facts_unknown || []);
  el.className = `msg ${m.kind} ${m.urgent ? 'urgent' : ''} ${unchecked.size ? 'ungrounded' : ''}`;
  // the knowledge base's facts the answer used; ones it doesn't know are marked
  const fact = (x) => `<span class="${unchecked.has(x) ? 'factbad' : ''}" title="${unchecked.has(x) ? 'not in the knowledge base' : 'fact id'}">${esc(x)}</span>`;
  const facts = (m.facts || []).length ? `<span class="facts">Facts used: ${m.facts.map(fact).join(', ')}</span>` : '';
  const video = m.url && /^https:\/\//.test(m.url) ? ` <a href="${esc(m.url)}" target="_blank" rel="noopener">Open video</a>` : '';
  // each web source by its site's name
  const source = (w) => `<a href="${esc(w.url)}" target="_blank" rel="noopener" title="${esc(w.title || w.url)}">${esc((w.url.split('/')[2] || '').replace(/^www\./, ''))}</a>`;
  const web = (m.web_sources || []).length
    ? `<span class="facts">Web: ${m.web_sources.filter((w) => /^https?:\/\//.test(w.url)).map(source).join(', ')}</span>` : '';
  const speaker = SPEAKER[m.kind] || m.kind;
  el.title = m.model ? `${speaker}, ${m.model}` : '';
  el.innerHTML = `<span class="ts">${dateLabel(m.t * 1000, 'shortTime')}</span><span class="by">${esc(speaker)}:</span>${esc(m.text)}${video}${web}${facts}`;
  const feed = $('feed');
  feed.appendChild(el);
  while (feed.children.length > FEED_MAX) feed.firstElementChild.remove();
  followFeed();
}

// The chat follows the newest message unless you've scrolled up to read. Whether it follows is kept, and only your own
// scrolling changes it (the wheel, a touch, the keys, the scroll bar): the feed's size and scroll also change under it
// (the window or the app's frame resized, longer lines wrapping, old messages trimmed off the top), and taking those
// for "scrolled up" used to leave it short of the bottom for good.
let feedFollows = true, feedTouched = -1e9, feedHeld = false;
const FEED_USER_MS = 800;              // a scroll this soon after your wheel, touch or key is yours
const feedGap = (/** @type {HTMLElement} */ f) => f.scrollHeight - f.scrollTop - f.clientHeight;
function followFeed() {
  const f = $('feed');
  if (feedGap(f) <= 2) feedFollows = true;        // right at the bottom (or all of it in view) is following
  if (feedFollows) f.scrollTop = f.scrollHeight;
}
function initFeedFollow() {
  const f = $('feed'), touched = () => { feedTouched = performance.now(); };
  for (const ev of ['wheel', 'touchmove', 'keydown']) f.addEventListener(ev, touched, { passive: true });
  // pushing down at the bottom scrolls nothing (no scroll event), and still means: follow
  f.addEventListener('wheel', (e) => { if (e.deltaY > 0 && feedGap(f) < 40) feedFollows = true; }, { passive: true });
  f.addEventListener('pointerdown', () => { feedHeld = true; touched(); }, { passive: true });
  addEventListener('pointerup', () => { if (feedHeld) { feedHeld = false; touched(); } }, { passive: true });
  f.addEventListener('scroll', () => {
    if (feedHeld || performance.now() - feedTouched < FEED_USER_MS) feedFollows = feedGap(f) < 40;   // you scrolled
    else followFeed();                 // the layout moved it: back to the newest, if it follows
  }, { passive: true });
  if (typeof ResizeObserver === 'function') new ResizeObserver(followFeed).observe(f);
}

// the engine's messages once each: a reconnect replays its recent ones in "init" (the page's own "You:" lines stay)
const seenMessages = new Set();
/** @param {CoachMessage} m */
function showEngineMessage(m) {
  const key = `${m.t}|${m.kind}|${m.text}`;
  if (seenMessages.has(key)) return;
  seenMessages.add(key);
  if (seenMessages.size > 200) seenMessages.delete(seenMessages.values().next().value);
  showMessage(m);
}

// what the coach won't say: all commentary off, or the kinds of remark that are
/** @param {EngineState} d */
function renderCoachFlags(d) {
  const flags = d.coach_flags || {}, off = Object.keys(flags).filter((k) => k !== 'coach' && !flags[k]);
  $('sw').textContent = flags.coach === false ? 'Commentary off (/coach on)' : (off.length ? 'Muted: ' + off.join(', ') : '');
}

function initAskForm() {
  $('ask').onsubmit = (e) => {
    e.preventDefault();
    const q = /** @type {HTMLInputElement} */ ($('q')).value.trim();
    if (!q) return;
    post('/ask', JSON.stringify({ q })).then((r) => { if (!r.ok) throw new Error(String(r.status)); }).catch(() =>
      showMessage({ kind: 'error', text: "The coach didn't get that question; ask again.", t: Date.now() / 1000 }));
    showMessage({ kind: 'question', text: q, t: Date.now() / 1000 });
    /** @type {HTMLInputElement} */ ($('q')).value = '';
  };
}

// the narrator's volume and mute: the desktop app owns them (its footer) and tells this page (postMessage); the
// level-up fanfare follows them
const voice = { volume: 1, muted: false };
function initVoice() {
  if (window.parent === window) return;
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (d && d.glide === 'voice' && e.source === window.parent) Object.assign(voice, { volume: Number(d.volume ?? 1), muted: !!d.muted });
  });
  window.parent.postMessage({ glide: 'voice', ask: true }, '*');
}

// ---- the minimap: the zone's map, the way to the step, the waypoints and you ----------------------------------------

// positions arrive as percentages of the zone; the map image is 1002 x 668
const MAP_W = 1002;
const mapX = (v) => (v * 10.02).toFixed(1), mapY = (v) => (v * 6.68).toFixed(1);
const ROUTE_FLOW_S = 1.1;       // the length of the rt-flow animation (dashboard.css)

const minimap = {
  shown: null,      // the state last drawn: zooming and resizing redraw it
  mapId: null,      // the map whose image is loaded
  pulseKey: '',     // the target that pulsed last, and when that began
  pulseAt: 0,
};

const redrawMap = () => { if (minimap.shown) drawMap(minimap.shown); };

// after the layout moves the map: fit the zoom to its new size and redraw the marks at their new scale
function refitMap(delay) {
  if (minimap.shown) setTimeout(() => { applyZoom(false); drawMap(minimap.shown); }, delay);
}

function mapImage() {
  let img = /** @type {HTMLImageElement | null} */ ($('mapimg'));
  if (!img) {
    img = document.createElement('img');
    img.id = 'mapimg';
    img.alt = '';
    img.draggable = false;
    $('mapview').prepend(img);
  }
  return img;
}

/** @param {EngineState} d */
function showMapImage(d) {
  if (d.map_image) {
    const img = mapImage();
    if (minimap.mapId !== d.map) {
      minimap.mapId = d.map;
      img.src = '/map/' + d.map + '.png';
      img.style.display = 'block';
    }
    $('mapnote').textContent = '';
  } else {
    if ($('mapimg')) $('mapimg').style.display = 'none';
    minimap.mapId = null;
  }
}

/** @param {EngineState} d */
function drawMap(d) {
  if (!d || !d.map) return;
  minimap.shown = d;
  showMapImage(d);
  // marks are sized in screen pixels (u = one pixel in map units at this zoom), in colours the map art doesn't use,
  // each with a dark outline, so they read at a glance whatever the terrain under them
  const u = MAP_W / (($('mapbox').clientWidth || 380) * (zoom.z || 1)), px = (v) => (v * u).toFixed(2);
  const walk = d.walk;
  const legs = walk && walk.map === d.map ? (walk.segments || []).filter((g) => (g.points || []).length > 1) : [];
  const marks = routeMarks(legs, px);
  $('maphdr').textContent = routeHeading(d, legs, u);
  marks.push(...waypointMarks(d, legs, px), playerMark(d, px));
  $('mapsvg').innerHTML = marks.join('');
  if (zoom.follow && zoom.z > 1 && d.x != null) centreMapOn(d.x / 100, d.y / 100);
}

// the way to the step: walking legs over the walkable-space graph (a white line with gold dashes flowing toward
// the target), flights along the path the gryphon really flies (cyan, a cyan landing point)
/**
 * @param {WalkSegment[]} legs
 * @param {(v: number) => string} px
 */
function routeMarks(legs, px) {
  const marks = [];
  for (const leg of legs) {
    const points = leg.points.map((p) => mapX(p[0]) + ',' + mapY(p[1])).join(' ');
    const line = (cls, extra) => `<polyline class="${cls}" points="${points}" ${extra}/>`;
    if (leg.mode === 'fly') {
      const end = leg.points[leg.points.length - 1];
      marks.push(line('rt-case', `stroke-width="${px(9)}"`), line('rt-fly', `stroke-width="${px(4)}" stroke-dasharray="${px(1)} ${px(9)}"`),
        `<circle class="rt-land" cx="${mapX(end[0])}" cy="${mapY(end[1])}" r="${px(6)}" stroke-width="${px(2.5)}"/>`);
    } else {
      // the map is redrawn on every update: a delay from the clock keeps the dashes flowing instead of restarting
      const phase = (performance.now() / 1000 % ROUTE_FLOW_S).toFixed(2);
      marks.push(line('rt-case', `stroke-width="${px(9)}"`), line('rt-walk', `stroke-width="${px(4)}"`),
        line('rt-flow', `stroke-width="${px(4)}" stroke-dasharray="${px(8)} ${px(10)}" style="--dash:${px(18)};animation-delay:-${phase}s"`));
    }
  }
  return marks;
}

// the line under the zone's name: how the drawn route goes (the quest page owns the distance, so there is one number
// on screen), else the zone
/**
 * @param {EngineState} d
 * @param {WalkSegment[]} legs
 * @param {number} u
 */
function routeHeading(d, legs, u) {
  // how long the drawn route is on screen: a few pixels (you're there) isn't "a walking route"
  const routePx = legs.reduce((total, leg) => total + leg.points.slice(1)
    .reduce((sum, p, i) => sum + Math.hypot((p[0] - leg.points[i][0]) * 10.02, (p[1] - leg.points[i][1]) * 6.68), 0), 0) / u;
  if (!legs.length || routePx < 24) return d.zone || '';
  const flight = legs.find((g) => g.mode === 'fly'), yards = (d.waypoint || {}).yards, walk = d.walk;
  if (flight) {
    return legs[0].mode === 'fly' ? `Flying to ${flight.to}, ${formatClock(flight.s)} left`
      : `Walk to the flight master, then fly to ${flight.to} (${formatClock(flight.s)})`;
  }
  if (yards != null && yards < 5) return d.zone || '';
  return walk.through && walk.through.length > 1 ? `Walking route via ${walk.through.slice(1, 4).join(', ')}` : `Walking route to step ${walk.step ?? ''}`;
}

// waypoints: the current step's is the target (red-orange; it pulses three times when it changes, then holds);
// later ones are small white dots. A route always ends on a target: with no waypoint of the step on this map, its
// last point is the target. Arrived (under 5 yd): a red ring around you, so the arrow stays readable.
/**
 * @param {EngineState} d
 * @param {WalkSegment[]} legs
 * @param {(v: number) => string} px
 */
function waypointMarks(d, legs, px) {
  const waypoints = [];       // [goto element, is the target]
  (d.steps || []).forEach((step, i) => {
    for (const e of elementsOf(step)) if (e.tag === 'goto' && (!e.map || e.map == d.map) && e.x != null) waypoints.push([e, i === 0]);
  });
  if (legs.length && !waypoints.some(([, isTarget]) => isTarget)) {
    const leg = legs[legs.length - 1], end = leg.points[leg.points.length - 1];
    waypoints.unshift([{ x: end[0], y: end[1] }, true]);
  }
  const arrived = (d.waypoint || {}).yards != null && d.waypoint.yards < 5 && d.x != null;
  const marks = [];
  for (const [e, isTarget] of waypoints.reverse()) {
    const cx = mapX(e.x), cy = mapY(e.y);
    if (!isTarget) {
      marks.push(`<circle class="wp-next" cx="${cx}" cy="${cy}" r="${px(4.5)}" stroke-width="${px(2)}"/>`);
      continue;
    }
    if (arrived) {
      marks.push(`<circle class="wp-here" cx="${mapX(d.x)}" cy="${mapY(d.y)}" r="${px(15)}" stroke-width="${px(3)}"/>`);
      continue;
    }
    const key = `${d.map}:${e.x.toFixed(1)},${e.y.toFixed(1)}`;
    if (key !== minimap.pulseKey) {
      minimap.pulseKey = key;
      minimap.pulseAt = performance.now();
    }
    // the pulse runs 5.4 s (three times 1.8 s); redrawn meanwhile, it carries on where it was
    const age = (performance.now() - minimap.pulseAt) / 1000;
    if (age < 5.4) marks.push(`<circle class="wp-pulse" cx="${cx}" cy="${cy}" r="${px(9)}" stroke-width="${px(2.5)}" style="animation-delay:-${age.toFixed(2)}s"/>`);
    marks.push(`<circle class="wp-cur" cx="${cx}" cy="${cy}" r="${px(8)}" stroke-width="${px(3)}"/>`);
  }
  return marks;
}

// you: a white arrowhead turned the way you face (a dot when unknown)
/**
 * @param {EngineState} d
 * @param {(v: number) => string} px
 */
function playerMark(d, px) {
  if (d.x == null || d.y == null) return '';
  const facing = d.facing ?? null;
  const shape = facing != null
    ? `<path class="me" d="M0,-13 L9,10 L0,5 L-9,10 Z" transform="rotate(${(-facing * 180 / Math.PI).toFixed(1)})"/>`
    : '<circle class="me" r="7"/>';
  return `<g transform="translate(${mapX(d.x)} ${mapY(d.y)}) scale(${px(1)})">${shape}</g>`;
}

// the zone over the map, where you stand, and the waypoint (its coordinates, else its distance)
/** @param {EngineState} d */
function renderPosition(d) {
  $('zone').textContent = d.standing_in || d.subzone || d.zone || '–';
  // with no route drawn, the line under a subzone's name names the zone
  if (!(d.walk && d.walk.segments && d.walk.segments.length)) $('maphdr').textContent = (d.standing_in || d.subzone) && d.zone ? d.zone : '';
  $('pos').textContent = d.x != null ? `${d.x.toFixed(1)}, ${d.y.toFixed(1)}` : '–';
  const waypoint = d.waypoint || {};
  const target = d.arrow && d.arrow.x != null ? `${d.arrow.x.toFixed(1)}, ${d.arrow.y.toFixed(1)}` : null;
  $('dist').textContent = target || (waypoint.yards != null ? (waypoint.yards < 5 ? 'Here' : `${waypoint.yards} yd`) : '–');
}

// ---- map zoom: the wheel and the buttons zoom (1x to 8x), dragging pans, a double-click resets; "follow" keeps you
// in the middle

const zoom = { z: 2.4, tx: 0, ty: 0, follow: true };      // a minimap: zoomed in on you by default
Object.assign(zoom, store.get('glide.minimap', {}));
const clampZoom = (z) => Math.max(1, Math.min(8, z));

function applyZoom(save) {
  const box = $('mapbox'), W = box.clientWidth, H = box.clientHeight;
  zoom.z = clampZoom(zoom.z);
  // the map image is 3:2 inside a round (square) box: letterboxed when it's shorter
  const viewW = W * zoom.z, viewH = W / 1.5 * zoom.z;
  zoom.tx = Math.min(0, Math.max(W - viewW, zoom.tx));
  zoom.ty = viewH <= H ? (H - viewH) / 2 : Math.min(0, Math.max(H - viewH, zoom.ty));
  $('mapview').style.transform = `translate(${zoom.tx}px,${zoom.ty}px) scale(${zoom.z})`;
  $('mz_follow').classList.toggle('on', zoom.follow);
  if (save) store.set('glide.minimap', { z: zoom.z, follow: zoom.follow });
}

// zoom by a factor about a point of the box (its middle by default), keeping that point still
function zoomBy(factor, cx, cy) {
  const box = $('mapbox');
  cx = cx ?? box.clientWidth / 2;
  cy = cy ?? box.clientHeight / 2;
  const z0 = zoom.z, z1 = clampZoom(z0 * factor);
  zoom.tx = cx - (cx - zoom.tx) * z1 / z0;
  zoom.ty = cy - (cy - zoom.ty) * z1 / z0;
  zoom.z = z1;
  applyZoom(true);
  redrawMap();
}

function resetZoom() {
  zoom.z = 1;
  zoom.tx = zoom.ty = 0;
  applyZoom(true);
  redrawMap();
}

// put a point of the map (fractions of its width and height) in the middle of the box
function centreMapOn(fx, fy) {
  const box = $('mapbox'), W = box.clientWidth;
  zoom.tx = W / 2 - fx * W * zoom.z;
  zoom.ty = box.clientHeight / 2 - fy * W / 1.5 * zoom.z;
  applyZoom(false);
}

function initMapControls() {
  const box = $('mapbox');
  let drag = null;
  box.addEventListener('wheel', (e) => {
    if (e.ctrlKey) return;
    e.preventDefault();
    const r = box.getBoundingClientRect();
    zoomBy(e.deltaY < 0 ? 1.25 : 0.8, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  box.addEventListener('pointerdown', (e) => {
    if (/** @type {Element} */ (e.target).closest('.mapctl')) return;
    drag = { x: e.clientX, y: e.clientY, tx: zoom.tx, ty: zoom.ty };
    box.setPointerCapture(e.pointerId);
    box.style.cursor = 'grabbing';
  });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    zoom.tx = drag.tx + e.clientX - drag.x;
    zoom.ty = drag.ty + e.clientY - drag.y;
    // panning away from you stops following you; a click that wobbles a few pixels doesn't
    if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 4 && zoom.follow) zoom.follow = false;
    applyZoom(false);
  });
  box.addEventListener('pointerup', () => {
    drag = null;
    box.style.cursor = 'grab';
    applyZoom(true);
  });
  box.addEventListener('dblclick', (e) => {
    if (/** @type {Element} */ (e.target).closest('.mapctl')) return;
    resetZoom();
  });
  $('mz_in').onclick = () => zoomBy(1.5);
  $('mz_out').onclick = () => zoomBy(1 / 1.5);
  $('mz_reset').onclick = () => resetZoom();
  $('mz_follow').onclick = () => {
    zoom.follow = !zoom.follow;
    applyZoom(true);
    redrawMap();
  };
  new ResizeObserver(() => {
    applyZoom(false);
    redrawMap();
  }).observe(box);
  applyZoom(false);
}

// ---- the route tracker: finished steps (time and delta), the live one with its objectives, the ones coming up -------

/** @param {EngineState} d */
function renderRouteTracker(d) {
  const current = currentStep(d), seen = new Set([String(stepNumber(d))]);
  const firstTime = (n) => {                                       // each step number shows once
    const key = String(n);
    if (n == null || seen.has(key)) return false;
    seen.add(key);
    return true;
  };
  // the last finished step (two on a tall screen), then the ones coming up; a repeat of the step before it reads once
  const finished = splits.slice(0, innerHeight >= 1200 ? 2 : 1).filter((s) => firstTime(s.n));
  const upcoming = (d.steps || []).slice(1).filter((s) => s && firstTime(s.index))
    .filter((s, i, a) => stepLabel(s) !== stepLabel(i ? a[i - 1] : current));
  const bestDelta = Math.min(...splits.map((s) => timeAgainstModel(s.actual, s.model)).filter((v) => v != null), Infinity);
  const next = (d.why || {}).next;

  const rows = [
    ...[...finished].reverse().map((s) => finishedRow(s, bestDelta)),
    ...(current ? [stepRow(d, current, 'class="trk live" aria-current="step"', formatClock(d.on_step_seconds))] : []),
    ...upcoming.map((s, i) => stepRow(d, s, 'class="trk"', i === 0 && next && next.xp ? `${Number(next.xp).toLocaleString()} XP` : '')),
  ];
  $('steps').innerHTML = rows.join('') || '<div class="empty">The next steps show here.</div>';
  const numbers = [...finished.map((s) => s.n), stepNumber(d), ...upcoming.map((s) => s.index)]
    .filter((v) => v != null).map(Number).filter(isFinite);
  $('splitspan').textContent = numbers.length ? `Steps ${Math.min(...numbers)} to ${Math.max(...numbers)}` : '';
}

function finishedRow(split, bestDelta) {
  const delta = timeAgainstModel(split.actual, split.model);
  const best = delta != null && delta === bestDelta && delta < 0;
  return `<div class="trk done"><span class="g" aria-hidden="true">${split.n ?? ''}</span><span class="t">${esc(split.text)}</span>`
    + `<span class="tm">${formatClock(split.actual)} ${deltaHtml(delta, best)}</span></div>`;
}

/**
 * @param {EngineState} d
 * @param {GuideStep} step
 * @param {string} attributes
 * @param {string} time
 */
function stepRow(d, step, attributes, time) {
  return `<div ${attributes}><span class="g" aria-hidden="true">${stepGlyph(step)}</span><span class="t">${esc(stepLabel(step))}</span>`
    + `<span class="tm">${time}</span>${taskLines(d, step)}</div>`;
}

// the step's tasks under its title, with objective counts; left out when they would only repeat the title
/**
 * @param {EngineState} d
 * @param {GuideStep} step
 */
function taskLines(d, step) {
  const tasks = tasksOf(step);
  if (!tasks.length || !step.text) return '';
  const onlyTheTitle = groupByVerb(tasks, TAG).join(', ') === step.text
    || tasks.length === 1 && step.text.endsWith(String(tasks[0].name || tasks[0].quest));
  if (onlyTheTitle) return '';
  const lines = tasks.map((e) => {
    const q = questOf(d, e), counts = hasObjectives(q) && e.tag === 'complete' ? ' ' + objectiveCounts(q) : '';
    return `<div class="${e.done ? 'ok' : ''}">${esc(TAG[e.tag] || e.tag)} ${esc(e.name || e.quest)}${esc(counts)}</div>`;
  });
  return `<div class="sub">${[...new Set(lines)].join('')}</div>`;
}

// ---- the bottom bar: the XP bar, bags and the hearthstone -----------------------------------------------------------

const XPBAR_TITLE = 'XP to next level; the lighter blue is rested XP';

/** @param {EngineState} d */
function renderXpBar(d) {
  const pct = d.xp_max ? Math.min(100, d.xp / d.xp_max * 100) : 0;
  const restedPct = d.xp_max ? Math.min(100, (d.xp + (d.rested || 0)) / d.xp_max * 100) : 0;
  $('xp').style.width = pct + '%';
  $('rest').style.width = restedPct + '%';
  renderPlanMark(d);
  $('xptxt').innerHTML = d.xp_max
    ? `<b class="figure">${d.xp.toLocaleString()} / ${d.xp_max.toLocaleString()}</b> XP to level ${d.level + 1}, ${pct.toFixed(1)}%`
    : '–';
  $('restxt').innerHTML = d.rested ? `${Number(d.rested).toLocaleString()} rested` : '';
}

// the gold tick on the XP bar: where the guide expects you at this step
/** @param {EngineState} d */
function renderPlanMark(d) {
  const mark = $('xpplan'), bar = $('xpbarbox'), expected = ((d.why || {}).plan || {}).exp_level;
  if (typeof expected !== 'number' || !d.xp_max) {
    mark.hidden = true;
    bar.title = XPBAR_TITLE;
    return;
  }
  const now = d.level + Math.min(1, d.xp / d.xp_max), gap = now - expected, expectedLevel = Math.floor(expected);
  mark.hidden = false;
  // a gold tick on this level's bar; grey when the plan is a level ahead or behind
  mark.className = 'plan' + (expectedLevel > d.level ? ' past' : expectedLevel < d.level ? ' before' : '');
  mark.style.left = ((expected - expectedLevel) * 100).toFixed(1) + '%';
  const how = Math.abs(gap) < 0.01 ? 'right on plan' : `${Math.abs(gap).toFixed(2)} levels ${gap > 0 ? 'ahead' : 'behind'}`;
  bar.title = `The gold tick is where the guide expects you at this step (level ${expected.toFixed(2)}); you're ${now.toFixed(2)}, ${how}. The lighter blue is rested XP.`;
}

// bags (amber at 2 or fewer free, red when full) and the hearthstone (ready, or its cooldown), by the XP bar
/** @param {EngineState} d */
function renderKit(d) {
  const bags = d.free_bags, cooldown = d.hearth_cd, bagsItem = $('kbags'), hearthItem = $('khs');
  $('sbags').textContent = bags == null ? '–' : bags === 0 ? 'Full' : `${bags} free`;
  bagsItem.className = 'kit-item' + (bags == null ? '' : bags === 0 ? ' bad' : bags <= 2 ? ' warn' : '');
  bagsItem.title = bags == null ? 'Free bag slots'
    : bags === 0 ? 'Bags full: sell or destroy something before looting' : `${bags} free bag slot${bags === 1 ? '' : 's'}`;

  // no bag count means the game hasn't reported yet, so the hearthstone isn't known to be ready either
  const ready = bags != null && !cooldown;
  $('shs').textContent = cooldown ? formatDuration(cooldown) : bags == null ? '–' : 'Hearth ready';
  hearthItem.className = 'kit-item' + (ready ? ' ok' : '');
  hearthItem.title = cooldown ? `Hearthstone on cooldown: ${formatDuration(cooldown)} left` : ready ? 'Hearthstone ready' : 'Hearthstone';
}

// ---- the book: one tab per section; with room to spare, sections dock into always-open columns on the left
// (the wing), most useful first. The open tab is remembered in this browser; the order is the player's (arrange).

const SECTIONS = /** @type {HTMLElement[]} */ ([...document.querySelectorAll('#shelf>section[data-panel]')]);
for (const s of SECTIONS) s.dataset.book = s.dataset.panel;
const sectionById = Object.fromEntries(SECTIONS.map((s) => [s.dataset.panel, s]));
// the default order, in columns (tests/test_dashboard_layout.py reads this declaration as written)
const DOCK_ORDER=[['quests', 'gear', 'buffs'], ['why', 'levels'], ['community', 'session']];
const BOOK_DEFAULT = DOCK_ORDER.flat();

const book = {
  order: [...BOOK_DEFAULT],                  // the sections in the player's order
  docked: -1,                                // columns docked in the wing; -1 until the first layout
};

const isWaiting = () => document.body.classList.contains('waiting');

// the player's order cut into columns of the default sizes (3, 2, 2)
function dockColumns() {
  let i = 0;
  return DOCK_ORDER.map((column) => book.order.slice(i, i += column.length));
}

// one column per 400px past 1440; none in a narrow window, or while waiting for the game
function columnsToDock() {
  const width = innerWidth;
  if (width < 860 || isWaiting()) return 0;
  return Math.max(0, Math.min(DOCK_ORDER.length, Math.floor((width - 1440) / 400)));
}

function dock() {
  const n = columnsToDock();
  if (n === book.docked) return;
  book.docked = n;
  const wing = $('wing'), shelf = $('shelf');
  // every section back on the shelf, then the first n columns out into the wing
  for (const s of SECTIONS) {
    s.classList.remove('tip', 'open');
    delete s.dataset.move;
    s.removeAttribute('tabindex');
    shelf.appendChild(s);
  }
  wing.innerHTML = '';
  for (const ids of dockColumns().slice(0, n)) {
    const column = document.createElement('div');
    column.className = 'dock';
    for (const id of ids) {
      const s = sectionById[id];
      s.classList.add('tip', 'open');
      s.removeAttribute('role');
      s.dataset.move = 'sec-' + id;
      s.dataset.label = s.dataset.title;
      if (arrange.on) s.tabIndex = 0;
      column.appendChild(s);
    }
    wing.appendChild(column);
  }
  renderShelf();
  if (minimap.shown) setTimeout(() => applyZoom(false), 0);
}

// lay the book out again, whether or not the number of docked columns changed (a new order, waiting for the game)
function relayoutBook() {
  book.docked = -1;
  dock();
}

// the shelf: every section not docked, one after another in the book's order, each named only by its emblem; while
// waiting for the game only the community notes have anything to say
function renderShelf() {
  const shelf = $('shelf'), waiting = isWaiting();
  for (const s of SECTIONS) s.hidden = waiting && s.parentElement === shelf && s.dataset.panel !== 'community';
  const shown = book.order.map((id) => sectionById[id]).filter((s) => s && s.parentElement === shelf);
  for (const s of shown) {
    shelf.appendChild(s);                                   // in the book's order
    s.id = 'sec-' + s.dataset.panel;
    s.setAttribute('role', 'region');
    s.dataset.move = 'sec-' + s.dataset.panel;
    s.dataset.label = s.dataset.title;
    if (arrange.on) s.tabIndex = 0;
  }
  $('book').classList.toggle('bare', !shown.some((s) => !s.hidden));
}

function initShelf() {
  sectionById.community?.addEventListener('pointerenter', feedSeen);      // pointing at the new notes sees them
}

// ---- arrange mode: the player drags pieces around within the top bar, the middle or the bottom bar. Lists (the top
// bar, the pace block, the bottom bar and its info line) reorder; the middle's five big panels trade slots; the book's
// sections reorder by their grips, on the shelf or in the docked columns. Saved with the engine (/layout), so it outlives the browser data.

const arrange = {
  on: false,
  layout: {},       // { lists: {group: [piece ids]}, slots: {slot: panel id} }, as saved
};
const SLOTS = ['page', 'chat', 'book', 'map', 'route'];
const PANEL_OF = { page: 'steps', chat: 'coach', book: 'book', map: 'map', route: 'route' };   // each slot's own panel
const slotEl = (slot) => /** @type {HTMLElement} */ (document.querySelector(`.slot[data-slot="${slot}"]`));
const movers = (box) => [...box.children].filter((c) => c.dataset.move);

// the saved order of the pieces still on the page, then any others in their current order
function mergeOrder(saved, current) {
  saved = saved || [];
  return [...saved.filter((id) => current.includes(id)), ...current.filter((id) => !saved.includes(id))];
}

function loadArrangement() {
  fetch('/layout').then((r) => r.json()).then((j) => {
    arrange.layout = j.arrange || {};
    applyArrangement();
  }).catch(() => {});
}

function applyArrangement() {
  const layout = arrange.layout || {}, lists = layout.lists || {};
  book.order = mergeOrder(lists.book, BOOK_DEFAULT);
  for (const [group, ids] of Object.entries(lists)) {
    const box = document.querySelector(`[data-group="${group}"]`);
    if (!box) continue;
    const pieces = movers(box), byId = Object.fromEntries(pieces.map((c) => [c.dataset.move, c]));
    // reinserted where the last piece was, so whatever else is in the box keeps its place
    const anchor = pieces.length ? pieces[pieces.length - 1].nextSibling : null;
    for (const id of mergeOrder(ids, pieces.map((c) => c.dataset.move))) box.insertBefore(byId[id], anchor);
  }
  // only a full, valid assignment of the five panels is used
  const slots = layout.slots || {}, panels = Object.values(PANEL_OF);
  if (SLOTS.every((k) => panels.includes(slots[k])) && new Set(SLOTS.map((k) => slots[k])).size === SLOTS.length) {
    for (const k of SLOTS) slotEl(k).appendChild(document.querySelector(`[data-move="${slots[k]}"]`));
  }
  relayoutBook();
  refitMap(0);
}

function saveArrangement() {
  const lists = { book: [...book.order] }, slots = {};
  for (const group of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('[data-group]'))) lists[group.dataset.group] = movers(group).map((c) => c.dataset.move);
  for (const k of SLOTS) slots[k] = /** @type {HTMLElement | null} */ (slotEl(k).firstElementChild)?.dataset.move;
  arrange.layout = { lists, slots };
  post('/layout', JSON.stringify({ arrange: arrange.layout })).catch(() => {});
}

// what a piece is: a book section (by its tab or its docked column), a panel in a slot, or an item of a list
const kindOf = (el) => el.dataset.book ? 'book' : el.parentElement.classList.contains('slot') ? 'slot' : 'list';
const labelOf = (el) => el.dataset.label || el.dataset.move;
const announce = (text) => { $('arrange_live').textContent = text; };

function setArrangeMode(on) {
  arrange.on = on;
  document.body.classList.toggle('arrange', on);
  $('arrangebar').hidden = !on;
  $('arrange').setAttribute('aria-pressed', String(on));
  // every piece can take the focus while arranging; each gets its own tabindex back afterwards (the tabs are
  // redrawn with theirs)
  for (const el of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('[data-move]'))) {
    if (on) {
      el.dataset.ti = el.getAttribute('tabindex') ?? '';
      el.tabIndex = 0;
    } else if (el.dataset.ti !== undefined) {
      if (el.dataset.ti) el.setAttribute('tabindex', el.dataset.ti);
      else el.removeAttribute('tabindex');
      delete el.dataset.ti;
    }
  }
  renderShelf();
  if (on) {
    announce('Arrange mode. Drag a piece, or focus it and press Alt with an arrow key.');
    $('arrange_done').focus();
  }
}

// moved pieces glide from where they were to where they land
function flip(els, move) {
  const before = els.map((e) => e.getBoundingClientRect());
  move();
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  els.forEach((e, i) => {
    const r = e.getBoundingClientRect(), dx = before[i].left - r.left, dy = before[i].top - r.top;
    if (!dx && !dy) return;
    e.classList.remove('moved');
    e.style.transform = `translate(${dx}px,${dy}px)`;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      e.classList.add('moved');
      e.style.transform = '';
      setTimeout(() => e.classList.remove('moved'), 320);
    }));
  });
}

// move a piece before or after another of its kind (a slot's panel trades places with the other), save, and say so
function moveItem(item, target, after) {
  const kind = kindOf(item);
  if (kind === 'book') {
    const moving = item.dataset.book, order = book.order.filter((id) => id !== moving);
    order.splice(order.indexOf(target.dataset.book) + (after ? 1 : 0), 0, moving);
    book.order = order;
    relayoutBook();
  } else if (kind === 'slot') {
    const from = item.parentElement, to = target.parentElement;
    flip([item, target], () => {
      from.appendChild(target);
      to.appendChild(item);
    });
    refitMap(60);
  } else {
    const box = item.parentElement;
    flip(movers(box), () => box.insertBefore(item, after ? target.nextSibling : target));
    refitMap(60);
  }
  saveArrangement();
  announce(kind === 'slot' ? `${labelOf(item)} and ${labelOf(target)} traded places.` : `Moved ${labelOf(item)} ${after ? 'after' : 'before'} ${labelOf(target)}.`);
}

// the drop target under the pointer: a piece of the same kind (and, for lists, the same list). A piece of a list
// inside another piece (a stat of the pace block, the bags on the XP line) dropped among that piece's neighbours
// lifts the whole block: { target, lift: the block } then.
function dropTargetAt(x, y, drag) {
  const outer = drag.kind === 'list' ? /** @type {HTMLElement | null} */ (drag.item.parentElement.closest('[data-move]')) : null;
  let lifted = null;
  for (const el of document.elementsFromPoint(x, y)) {
    const t = /** @type {HTMLElement | null} */ (el.closest('[data-move]'));
    if (!t || t === drag.item || drag.item.contains(t)) continue;
    const fits = drag.kind === 'book' ? !!t.dataset.book
      : drag.kind === 'slot' ? kindOf(t) === 'slot'
        : t.parentElement === drag.item.parentElement && !t.dataset.book;
    if (fits) return { target: t, lift: null };
    if (!lifted && outer && !outer.contains(t) && t.parentElement === outer.parentElement && !t.dataset.book) lifted = t;
  }
  return { target: lifted, lift: lifted && outer };
}

// a list laid out in a row takes its drop mark beside a piece; a column, above or below it
function laidOutInRow(el) {
  const style = getComputedStyle(el.parentElement);
  return style.display.includes('flex') && style.flexDirection.startsWith('row');
}

let drag = null;      // the piece being dragged: { item, kind, x, y, moved, ghost, mark, target, after, lift }
let dragged = false;  // a drag just ended: the click that follows it isn't a click

// outside arrange mode the big panels and the book's sections move by their grip, an invisible strip along their
// top edge (the quest page also by its title), and every
// piece of the top and bottom bars by itself; the controls stay controls
const HANDLES = '.grip, .leaf .glyph, .leaf h1, .leaf .step-no, .top [data-move], .xpfoot [data-move]';
const CONTROLS = 'button:not([role=tab]), input, select, textarea, a, label';

function startDrag(e) {
  if (e.button !== 0 || e.target.closest('#arrangebar,#arrange')) return;
  if (!arrange.on && (!e.target.closest(HANDLES) || e.target.closest(CONTROLS))) return;
  const item = e.target.closest('[data-move]');
  if (!item) return;
  e.preventDefault();
  e.stopPropagation();
  drag = { item, kind: kindOf(item), x: e.clientX, y: e.clientY, moved: false };
}

function continueDrag(e) {
  if (!drag) return;
  if (!drag.moved) {
    if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 6) return;     // still a click
    drag.moved = true;
    drag.item.classList.add('dragging');
    document.body.classList.add('moving');
    drag.ghost = Object.assign(document.createElement('div'), { className: 'drag-ghost', textContent: labelOf(drag.item) });
    drag.mark = Object.assign(document.createElement('div'), { className: 'dropmark' });
    document.body.append(drag.ghost, drag.mark);
  }
  drag.ghost.style.left = e.clientX + 'px';
  drag.ghost.style.top = e.clientY + 'px';
  const { target, lift } = dropTargetAt(e.clientX, e.clientY, drag);
  if (drag.lift !== lift) {
    drag.lift = lift;
    drag.ghost.textContent = labelOf(lift || drag.item);
  }
  if (drag.target !== target) {
    drag.target?.classList.remove('drop-target');
    drag.target = target;
  }
  // a slot's panel trades places, so its target is outlined; in a list the mark shows where the piece will go
  drag.mark.hidden = !target || drag.kind === 'slot';
  if (!target) return;
  if (drag.kind === 'slot') {
    target.classList.add('drop-target');
    return;
  }
  const r = target.getBoundingClientRect(), inRow = laidOutInRow(target);
  drag.after = inRow ? e.clientX > r.left + r.width / 2 : e.clientY > r.top + r.height / 2;
  Object.assign(drag.mark.style, inRow
    ? { left: (drag.after ? r.right + 4 : r.left - 6) + 'px', top: r.top + 'px', width: '3px', height: r.height + 'px' }
    : { left: r.left + 'px', top: (drag.after ? r.bottom + 5 : r.top - 7) + 'px', width: r.width + 'px', height: '3px' });
}

function endDrag() {
  if (!drag) return;
  drag.item.classList.remove('dragging');
  document.body.classList.remove('moving');
  drag.ghost?.remove();
  drag.mark?.remove();
  drag.target?.classList.remove('drop-target');
  drag = null;
}

function drop() {
  if (!drag) return;
  const { moved, item, target, after, lift } = drag;
  endDrag();
  if (moved) {
    dragged = true;
    setTimeout(() => { dragged = false; }, 0);
  }
  if (moved && target) moveItem(lift || item, target, after);
}

// Alt and an arrow key move the focused piece one place; Escape leaves arrange mode
function arrangeKey(e) {
  if (!arrange.on) return;
  if (e.key === 'Escape') {
    setArrangeMode(false);
    $('arrange').focus();
    return;
  }
  if (!e.altKey || !/^Arrow/.test(e.key)) return;
  const item = /** @type {HTMLElement | null} */ (document.activeElement?.closest('[data-move]'));
  if (!item) return;
  e.preventDefault();
  const forward = e.key === 'ArrowRight' || e.key === 'ArrowDown', kind = kindOf(item);
  if (kind === 'book') {
    const id = item.dataset.book, j = book.order.indexOf(id) + (forward ? 1 : -1);
    if (j < 0 || j >= book.order.length) return;
    // the section itself may not be in the page (waiting for the game), so the label comes from its title
    moveItem(item, { dataset: { book: book.order[j], label: sectionById[book.order[j]]?.dataset.title } }, forward);
    /** @type {HTMLElement | null} */ (document.querySelector(`[data-book="${id}"][data-move]`))?.focus();     // the book was redrawn
    return;
  }
  let target;
  if (kind === 'slot') {
    const j = SLOTS.indexOf(item.parentElement.dataset.slot) + (forward ? 1 : -1);
    if (j < 0 || j >= SLOTS.length) return;
    target = slotEl(SLOTS[j]).firstElementChild;
  } else {
    const siblings = movers(item.parentElement);
    target = siblings[siblings.indexOf(item) + (forward ? 1 : -1)];
  }
  if (target) {
    moveItem(item, target, forward);
    item.focus();
  }
}

function initArrangeMode() {
  $('arrange').onclick = () => setArrangeMode(!arrange.on);
  $('arrange_done').onclick = () => setArrangeMode(false);
  $('arrange_reset').onclick = () => {
    post('/layout', '{}').finally(() => location.reload());
  };
  document.addEventListener('pointerdown', startDrag, true);
  document.addEventListener('pointermove', continueDrag);
  document.addEventListener('pointerup', drop);
  document.addEventListener('pointercancel', endDrag);
  // while arranging, clicks don't reach the panels (no tab switches, no map pans, no sends); nor does a drag's end
  document.addEventListener('click', (e) => {
    if (dragged || (arrange.on && !/** @type {Element} */ (e.target).closest('#arrangebar,#arrange'))) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
  document.addEventListener('keydown', arrangeKey, true);
}

// ---- the state stream: the engine's events drive every panel --------------------------------------------------------

let lastState = /** @type {EngineState} */ ({});

/** @param {EngineState | undefined} d */
function renderState(d) {
  if (d) {
    renderAddonWarning(d.addon);
    drawMap(d);
    if (d.community !== undefined) renderCommunity(d.community);  // init's state; the state event leaves it out
    renderFeed(d.community_feed);
  }
  $('mapbox').classList.toggle('blank', !(d && d.map && d.map_image));
  const waiting = !d || !d.level;
  if (waiting !== isWaiting()) {
    document.body.classList.toggle('waiting', waiting);
    relayoutBook();
  }
  if (waiting) {
    renderWaitingPage(d);
    return;
  }
  trackStep(d);
  renderUnitFrame(d);
  renderXpBar(d);
  renderPace(d);
  renderQuestPage(d);
  renderRouteTracker(d);
  renderBuffs(d);
  renderGear(d);
  renderLevels(d);
  renderQuestLog(d);
  renderPosition(d);
  renderVitals(d);
  renderKit(d);
  renderCoachFlags(d);
  renderWhy(d.why, d.pace, d.kb);
  renderSession(d);
}

function connectToEngine() {
  const events = new EventSource('/events');
  events.addEventListener('init', (e) => {
    /** @type {InitEvent} */
    const d = JSON.parse(e.data);
    lastState = d.state || /** @type {EngineState} */ ({});
    renderState(d.state);
    (d.messages || []).forEach(showEngineMessage);
  });
  events.addEventListener('state', (e) => {
    /** @type {EngineState} */
    const d = JSON.parse(e.data);
    lastState = d;
    renderState(d);
  });
  // the community notes come on their own event, when they change
  events.addEventListener('community', (e) => renderCommunity(/** @type {CommunityEvent} */ (JSON.parse(e.data))));
  // push-to-talk as it happens: the ask box glows and says it's listening (or turning the words into text)
  events.addEventListener('ptt', (e) => {
    const st = JSON.parse(e.data).state, q = /** @type {HTMLInputElement} */ ($('q'));
    document.body.classList.toggle('ptt-listening', st === 'listening');
    document.body.classList.toggle('ptt-transcribing', st === 'transcribing');
    q.dataset.placeholder ??= q.placeholder;
    q.placeholder = st === 'listening' ? 'Listening… let go of the key when you\'re done' : st === 'transcribing' ? 'Turning that into words…'
      : q.dataset.placeholder;
  });
  events.addEventListener('message', (e) => showEngineMessage(JSON.parse(e.data)));
  events.onerror = () => $('live').classList.remove('on');
}

// the auras and the docked columns depend on the width, and the map's marks on its size
function onResize() {
  dock();
  if (lastState.level) renderBuffs(lastState);
  redrawMap();
}

// ---- start-up: wire the page up, then listen to the engine ----------------------------------------------------------

fetchXpHistory();
setInterval(fetchXpHistory, 60000);
initXpChartHover();
initVoice();
// an optional image (a buff's icon) that isn't there goes, leaving the tile's letter (no inline handlers: the page's
// security policy allows only its own script)
document.addEventListener('error', (e) => {
  const img = /** @type {HTMLElement} */ (e.target);
  if (img instanceof HTMLImageElement && img.hasAttribute('data-optional')) img.remove();
}, true);
initMapControls();
initShelf();
addEventListener('resize', onResize);
loadArrangement();
dock();
initArrangeMode();
connectToEngine();
initAskForm();
initFeedFollow();
