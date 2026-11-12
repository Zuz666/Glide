// Windows owns desktop date/time presentation. Keep its patterns inside the main process;
// neither a locale identifier nor Intl can reproduce the account's custom regional formats.
import type { DateTimeRequest } from "../shared/date-time";
import type koffi from "koffi";

const DATE_SHORTDATE = 0x00000001;
const LOCALE_SSHORTTIME = 0x00000079;
const LOCALE_STIMEFORMAT = 0x00001003;
const MIN_EPOCH = -11644473600000; // 1601-01-01: GetDateFormatEx's earliest supported date
const MAX_EPOCH = 253402300799999; // 9999-12-31: the four-digit calendar-date contract

interface WindowsApi {
  locale(locale: null, type: number, buffer: Buffer | null, count: number): number;
  date(locale: null, flags: number, date: Buffer, pattern: null, buffer: Buffer | null, count: number, calendar: null): number;
  time(locale: null, flags: number, time: Buffer, pattern: string, buffer: Buffer | null, count: number): number;
  zone(zone: Buffer): number;
  local(zone: Buffer, utc: Buffer, local: Buffer): number;
  error(): number;
}
let windowsApi: WindowsApi | undefined;

function nativeApi(): WindowsApi {
  if (!windowsApi) {
    // Lazy and ordinary package resolution: importing IPC in dependency-free Node fixtures must
    // not load a native addon, and non-Windows execution never needs Koffi.
    const ffi: typeof koffi = require("koffi");
    const kernel = ffi.load("kernel32.dll");
    windowsApi = {
      locale: kernel.func("int __stdcall GetLocaleInfoEx(const char16_t *locale, uint32_t type, void *buffer, int count)"),
      date: kernel.func("int __stdcall GetDateFormatEx(const char16_t *locale, uint32_t flags, const void *date, const char16_t *pattern, void *buffer, int count, const char16_t *calendar)"),
      time: kernel.func("int __stdcall GetTimeFormatEx(const char16_t *locale, uint32_t flags, const void *time, const char16_t *pattern, void *buffer, int count)"),
      zone: kernel.func("uint32_t __stdcall GetDynamicTimeZoneInformation(void *zone)"),
      local: kernel.func("int __stdcall SystemTimeToTzSpecificLocalTimeEx(const void *zone, const void *utc, void *local)"),
      error: kernel.func("uint32_t __stdcall GetLastError()"),
    };
  }
  return windowsApi;
}

function nativeFailure(api: WindowsApi, operation: string): never {
  throw new Error(`${operation} failed (Windows error ${api.error()})`);
}

function nativeString(api: WindowsApi, operation: string, call: (buffer: Buffer | null, count: number) => number): string {
  const size = call(null, 0);
  if (!size) return nativeFailure(api, operation);
  if (size < 1 || size > 32768) throw new Error(`${operation} returned an invalid string size`);
  const buffer = Buffer.allocUnsafe(size * 2);
  const written = call(buffer, size);
  if (!written) return nativeFailure(api, operation);
  if (written < 1 || written > size) throw new Error(`${operation} returned an invalid string length`);
  return buffer.toString("utf16le", 0, (written - 1) * 2);
}

function calendar(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const year = Number(value.slice(0, 4)), month = Number(value.slice(5, 7)), day = Number(value.slice(8, 10));
  if (year < 1601 || month < 1 || month > 12 || day < 1 || day > 31) return null;
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? date : null;
}

function valueDate(request: DateTimeRequest): Date | null {
  if (typeof request.value === "string") return request.style === "date" ? calendar(request.value) : null;
  return Number.isFinite(request.value) && request.value >= MIN_EPOCH && request.value <= MAX_EPOCH
    ? new Date(request.value) : null;
}

function systemTime(date: Date, buffer: Buffer): void {
  buffer.writeUInt16LE(date.getUTCFullYear(), 0);
  buffer.writeUInt16LE(date.getUTCMonth() + 1, 2);
  buffer.writeUInt16LE(date.getUTCDay(), 4);
  buffer.writeUInt16LE(date.getUTCDate(), 6);
  buffer.writeUInt16LE(date.getUTCHours(), 8);
  buffer.writeUInt16LE(date.getUTCMinutes(), 10);
  buffer.writeUInt16LE(date.getUTCSeconds(), 12);
  buffer.writeUInt16LE(date.getUTCMilliseconds(), 14);
}

/** Bounded, aligned output; malformed batches reject, missing/invalid date values have no label. */
export function formatDateTimes(requests: DateTimeRequest[]): string[] {
  if (!Array.isArray(requests) || requests.length > 256) throw new TypeError("Date/time batch must be an array of at most 256 requests");
  for (let index = 0; index < requests.length; index++) {
    const request = requests[index];
    if (!request || typeof request !== "object" || Array.isArray(request)
      || (request.style !== "date" && request.style !== "shortTime" && request.style !== "longTime" && request.style !== "dateTime")
      || (typeof request.value !== "number" && typeof request.value !== "string")) {
      throw new TypeError(`Invalid date/time request at index ${index}`);
    }
  }
  if (!requests.length) return [];
  if (process.platform !== "win32") {
    return requests.map((request) => {
      const date = valueDate(request);
      if (!date) return "";
      // A calendar day is formatted in UTC, never shifted by the host's time zone.
      const day = () => new Intl.DateTimeFormat(undefined, { dateStyle: "short",
        ...(typeof request.value === "string" ? { timeZone: "UTC" } : {}) }).format(date);
      const time = () => new Intl.DateTimeFormat(undefined, { timeStyle: request.style === "shortTime" ? "short" : "medium" }).format(date);
      return request.style === "date" ? day() : request.style === "dateTime" ? `${day()} ${time()}` : time();
    });
  }
  const api = nativeApi();
  // Bindings alone are cached. Read both independent patterns and the current dynamic zone
  // for every batch, including changes made while the application was hidden or asleep.
  const shortTime = nativeString(api, "GetLocaleInfoEx", (buffer, count) => api.locale(null, LOCALE_SSHORTTIME, buffer, count));
  const longTime = nativeString(api, "GetLocaleInfoEx", (buffer, count) => api.locale(null, LOCALE_STIMEFORMAT, buffer, count));
  const zone = Buffer.alloc(432); // Win32 DYNAMIC_TIME_ZONE_INFORMATION, including trailing alignment
  if (api.zone(zone) === 0xffffffff) nativeFailure(api, "GetDynamicTimeZoneInformation");
  const utc = Buffer.allocUnsafe(16), local = Buffer.allocUnsafe(16);
  return requests.map((request) => {
    const date = valueDate(request);
    if (!date) return "";
    systemTime(date, utc);
    let wall = utc;
    if (typeof request.value === "number") {
      if (!api.local(zone, utc, local)) nativeFailure(api, "SystemTimeToTzSpecificLocalTimeEx");
      if (local.readUInt16LE(0) < 1601 || local.readUInt16LE(0) > 9999) return "";
      wall = local;
    }
    const day = () => nativeString(api, "GetDateFormatEx", (buffer, count) => api.date(null, DATE_SHORTDATE, wall, null, buffer, count, null));
    const time = () => nativeString(api, "GetTimeFormatEx", (buffer, count) => api.time(null, 0, wall,
      request.style === "shortTime" ? shortTime : longTime, buffer, count));
    return request.style === "date" ? day() : request.style === "dateTime" ? `${day()} ${time()}` : time();
  });
}
