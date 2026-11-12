// Public formatDateTimes output is the seam. Native fixtures replace only Win32 inputs,
// never owner preferences; the production module exports no pattern or timezone override.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');
const { EventEmitter } = require('node:events');
const compiled = path.resolve(process.argv[2]);
const mode = process.argv[3] || 'unit';
const modulePath = path.join(compiled, 'main', 'date-time.js');
const originalLoad = Module._load;
const originalPlatform = Object.getOwnPropertyDescriptor(process, 'platform');
const platform = value => Object.defineProperty(process, 'platform', { configurable: true, value });
let adapter;
let loads = 0;
Module._load = function (name, ...args) {
  if (name === 'koffi') { loads++; return adapter; }
  return originalLoad.call(this, name, ...args);
};
function formatter() {
  delete require.cache[modulePath];
  return require(modulePath).formatDateTimes;
}
function text(value, buffer, count) {
  if (buffer) {
    assert.ok(count >= value.length + 1);
    buffer.write(value + '\0', 'utf16le');
  }
  return value.length + 1;
}
function fields(buffer) {
  return Array.from({ length: 8 }, (_, index) => buffer.readUInt16LE(index * 2));
}
function fill(date, buffer) {
  [date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDay(), date.getUTCDate(),
    date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds(), date.getUTCMilliseconds()]
    .forEach((value, index) => buffer.writeUInt16LE(value, index * 2));
}

function unitChecks() {
  platform('linux');
  adapter = { load() { throw new Error('must not load native addon'); } };
  let format = formatter();
  assert.equal(loads, 0, 'module import must not resolve koffi');
  assert.deepEqual(format([]), []);
  const instant = Date.UTC(2026, 10, 12, 0, 4, 9);
  assert.deepEqual(format([{ value: '2024-02-29', style: 'date' }]), [
    new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeZone: 'UTC' }).format(new Date('2024-02-29T00:00:00Z')),
  ]);
  assert.deepEqual(format([{ value: instant, style: 'shortTime' }, { value: instant, style: 'longTime' }]), [
    new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(new Date(instant)),
    new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' }).format(new Date(instant)),
  ]);
  assert.equal(loads, 0, 'non-Windows formatting must not load native addon');
  platform('win32');
  let short = "h 'short literal' mm tt", long = "HH.mm 'long literal' ss";
  let failure = '';
  let offset = -8;
  let conversions = 0;
  adapter = { load(dll) {
    assert.equal(dll, 'kernel32.dll');
    return { func(declaration) {
      const name = /__stdcall (\w+)\(/.exec(declaration)[1];
      return (...args) => {
        if (failure === name) return name === 'GetDynamicTimeZoneInformation' ? 0xffffffff : 0;
        switch (name) {
          case 'GetLastError': return 87;
          case 'GetLocaleInfoEx': {
            const [locale, type, buffer, count] = args;
            assert.equal(locale, null, 'USER_DEFAULT, not system locale or language');
            assert.ok(type === 0x79 || type === 0x1003);
            return text(type === 0x79 ? short : long, buffer, count);
          }
          case 'GetDynamicTimeZoneInformation':
            args[0].writeInt32LE(offset, 0); return 0;
          case 'SystemTimeToTzSpecificLocalTimeEx': {
            conversions++;
            const [zone, utc, local] = args;
            const [y, m, , d, h, min, s, ms] = fields(utc);
            fill(new Date(Date.UTC(y, m - 1, d, h + zone.readInt32LE(0), min, s, ms)), local);
            return 1;
          }
          case 'GetDateFormatEx': {
            const [locale, flags, wall, pattern, buffer, count, calendar] = args;
            assert.equal(locale, null); assert.equal(flags, 1);
            assert.equal(pattern, null); assert.equal(calendar, null);
            const [y, m, , d] = fields(wall);
            return text(`${d} / ${m} / ${y}`, buffer, count);
          }
          case 'GetTimeFormatEx': {
            const [locale, flags, wall, pattern, buffer, count] = args;
            assert.equal(locale, null); assert.equal(flags, 0, 'do not strip native seconds or markers');
            assert.ok(pattern === short || pattern === long, 'pass opaque native pattern unchanged');
            const [, , , , h, min, s] = fields(wall);
            return text(`${pattern}: ${h},${min},${s}`, buffer, count);
          }
          default: throw new Error(`unexpected native call ${name}`);
        }
      };
    } };
  } };
  format = formatter();
  assert.equal(loads, 0, 'Windows import remains lazy');
  assert.deepEqual(format([]), []);
  assert.equal(loads, 0);
  const requests = ['date', 'shortTime', 'longTime', 'dateTime'].map(style => ({ value: instant, style }));
  assert.deepEqual(format(requests), [
    '11 / 11 / 2026', `${short}: 16,4,9`, `${long}: 16,4,9`, `11 / 11 / 2026 ${long}: 16,4,9`,
  ]);
  assert.equal(conversions, 4);
  const beforeCalendar = conversions;
  assert.deepEqual(format([{ value: '2024-02-29', style: 'date' }]), ['29 / 2 / 2024']);
  assert.equal(conversions, beforeCalendar, 'calendar dates never enter timezone conversion');
  short = "HH 'new short' ss"; long = "h 'new long' tt"; offset = 14;
  assert.deepEqual(format(requests), [
    '12 / 11 / 2026', `${short}: 14,4,9`, `${long}: 14,4,9`, `12 / 11 / 2026 ${long}: 14,4,9`,
  ], 'each batch reads both independent patterns and current zone');
  assert.deepEqual(format([{ value: '2024-02-29', style: 'date' }]), ['29 / 2 / 2024']);
  assert.equal(format([{ value: instant + 0.75, style: 'dateTime' }])[0], `12 / 11 / 2026 ${long}: 14,4,9`,
    'fractional milliseconds from engine Unix-second timestamps remain valid instants');
  const invalid = [NaN, Infinity, -Infinity, Number.MAX_SAFE_INTEGER,
    -11644473600001, 253402300800000, '', '2026-02-29', '1900-02-29', '2024-02-30',
    '2026-13-01', '2026-00-01', '2026-01-00', '2026-1-01', '2026-01-01T00:00:00Z', '1600-12-31', '0000-01-01'];
  assert.deepEqual(format(invalid.map(value => ({ value, style: 'date' }))), invalid.map(() => ''));
  assert.deepEqual(format([{ value: '2024-02-29', style: 'shortTime' }]), ['']);
  assert.deepEqual(format([{ value: '2000-02-29', style: 'date' }, { value: '1601-01-01', style: 'date' },
    { value: '9999-12-31', style: 'date' }]), ['29 / 2 / 2000', '1 / 1 / 1601', '31 / 12 / 9999']);
  for (const malformed of [null, {}, 'bad', [null], [[]], [{}], [{ value: 0, style: 'unknown' }],
    [{ value: {}, style: 'date' }], [{ value: true, style: 'date' }], new Array(1), Array(257).fill({ value: 0, style: 'date' })]) {
    assert.throws(() => format(malformed), /batch|request/);
  }
  assert.equal(format(Array(256).fill({ value: '2024-02-29', style: 'date' })).length, 256);
  for (const name of ['GetLocaleInfoEx', 'GetDynamicTimeZoneInformation', 'SystemTimeToTzSpecificLocalTimeEx',
    'GetDateFormatEx', 'GetTimeFormatEx']) {
    failure = name;
    const style = name === 'GetTimeFormatEx' ? 'longTime' : 'date';
    assert.throws(() => format([{ value: instant, style }]), new RegExp(`${name} failed \\(Windows error 87\\)`),
      'Windows failures must not silently become Intl output');
  }
}

async function lifecycleChecks() {
  // Execute the actual entrypoint with isolated Electron/engine adapters: no app installation,
  // startup, settings writes, client discovery, network, native hooks or power operations.
  const app = new EventEmitter(), powerMonitor = new EventEmitter();
  let ready;
  app.whenReady = () => ({ then(fn) { ready = fn; } });
  app.requestSingleInstanceLock = () => true;
  app.getVersion = () => '1.0.0';
  const sent = [];
  let main;
  class Window extends EventEmitter {
    constructor(nativeHooks = true) {
      super(); this.webContents = new EventEmitter(); this.hooks = new Map();
      if (nativeHooks) this.hookWindowMessage = (id, callback) => {
        assert.ok(!this.hooks.has(id), 'one hook per HWND and message'); this.hooks.set(id, callback);
      };
    }
  }
  class MainWindow {
    constructor() { main = this; }
    create() {
      const win = new Window();
      app.emit('browser-window-created', {}, win); // Electron emits during the BrowserWindow constructor
      this.win = win;
    }
    send(channel, payload) { sent.push([channel, payload]); }
    show() { if (!this.win) this.create(); this.win.emit('show'); this.win.emit('focus'); }
  }
  class Service extends EventEmitter {
    configure() {} sync() {} create() {} update() {} start() {} stop() {} header() {} note() {}
    readDragKey() {} startKeyPolling() { return true; }
    send() {}
  }
  const settings = { current: { setupDone: true, autoStartEngine: false, lastVersion: '1.0.0' }, load() {}, save() {} };
  const imports = {
    electron: { app, powerMonitor, Menu: { setApplicationMenu() {} }, protocol: { registerSchemesAsPrivileged() {}, handle() {} },
      globalShortcut: { register() {} } },
    './app/main-window': { MainWindow }, './app/settings': { settings },
    './app/config': { readConfig: () => ({ wow_dir: 'fixture-only' }), engineHome: () => '', engineOptions: () => ({}) },
    './app/ipc': { registerIpc() {} }, './paths': { logsDir: () => '', shippedEngine: '', syncEngine: () => [] },
    './secrets': { loadSecrets: async () => {}, secretsProblem: () => null }, './store': { appSettingsProblem: () => null },
    './engine': { Engine: Service }, './logfile': { LogFile: Service }, './speech': { Speech: Service },
    './windows-tts': { WindowsTts: Service }, './edge-tts': { EdgeTts: Service }, './resources': { ResourceSampler: Service },
    './updater': { Updater: Service }, './game-icons': { GameIcons: Service }, './app/overlay': { Overlay: Service },
    './app/tray': { AppTray: Service }, './app/assets': {}, './addon': {}, './splash': {},
  };
  const source = fs.readFileSync(path.join(compiled, 'main', 'main.js'), 'utf8');
  const context = { require(name) {
    if (Object.hasOwn(imports, name)) return imports[name];
    if (['fs', 'path', 'url'].includes(name)) return require(name);
    throw new Error(`unhandled main-process dependency ${name}`);
  }, exports: {}, __dirname: path.join(compiled, 'main'), process: { platform: 'win32', versions: {} },
    setTimeout() {}, clearTimeout() {}, console, URL, WeakSet };
  vm.runInNewContext(source, context, { filename: 'main.js' });
  await ready();
  const count = () => sent.filter(([channel]) => channel === 'app:datesChanged').length;
  const first = main.win;
  for (const id of [0x001a, 0x001e]) {
    const before = count(); first.hooks.get(id)(); assert.equal(count(), before + 1);
  }
  for (const event of ['focus', 'show']) {
    const before = count(); first.emit(event); assert.equal(count(), before + 1);
  }
  let before = count(); powerMonitor.emit('resume'); assert.equal(count(), before + 1);
  const other = new Window(); app.emit('browser-window-created', {}, other);
  before = count(); other.hooks.get(0x001a)(); other.emit('focus'); assert.equal(count(), before);
  main.win = null; main.show();
  assert.notEqual(main.win, first);
  before = count(); main.win.hooks.get(0x001a)(); assert.equal(count(), before + 1, 'recreated HWND receives settings changes');
  before = count(); first.hooks.get(0x001a)(); assert.equal(count(), before, 'old HWND cannot cause refresh');
  const fake = new Window(false); app.emit('browser-window-created', {}, fake);
  main.win = fake; before = count(); fake.emit('focus'); assert.equal(count(), before + 1, 'non-native fixture windows retain activation catch-up');
  assert.ok(sent.filter(([channel]) => channel === 'app:datesChanged').every(([, payload]) => payload === undefined));
}

function nativeChecks() {
  const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
  const realKoffi = originalLoad.call(module, path.join(process.argv[4], 'koffi'));
  let fixture;
  adapter = { load(dll) {
    const library = realKoffi.load(dll);
    return { func(declaration) {
      const call = library.func(declaration);
      const name = /__stdcall (\w+)\(/.exec(declaration)[1];
      return (...args) => {
        if (fixture?.zone && name === 'GetDynamicTimeZoneInformation') {
          Buffer.from(fixture.zone, 'hex').copy(args[0]); return 0;
        }
        if (fixture?.shortTime && name === 'GetLocaleInfoEx') {
          return text(args[1] === 0x79 ? fixture.shortTime : fixture.longTime, args[2], args[3]);
        }
        if (fixture?.date && name === 'GetDateFormatEx') {
          args[1] = 0; args[3] = fixture.date;
        }
        if (fixture?.locale && (name === 'GetDateFormatEx' || name === 'GetTimeFormatEx')) args[0] = fixture.locale;
        return call(...args);
      };
    } };
  } };
  const format = formatter();
  for (const scenario of payload) {
    fixture = scenario.fixture;
    assert.deepEqual(format(scenario.requests), scenario.expected, scenario.name);
  }
  console.log(`ok: ${payload.length} read-only Windows parity/pattern scenarios`);
}

(async () => {
  try {
    if (mode === 'native') nativeChecks();
    else { unitChecks(); await lifecycleChecks(); console.log('ok: native formatting contract and live lifecycle'); }
  } finally {
    Module._load = originalLoad;
    Object.defineProperty(process, 'platform', originalPlatform);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
