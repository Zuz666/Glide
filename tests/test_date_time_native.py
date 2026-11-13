#!/usr/bin/env python3
"""Public date labels and live refresh, with independent read-only Win32 evidence.

The Node check exercises formatDateTimes and the actual main entrypoint. On Windows,
ctypes supplies expected strings directly from Win32, including historical DST. Explicit
patterns and enumerated timezone structures are injected only at the test's FFI adapter;
no test changes Windows settings and production offers no profile-setting API.
"""
import ctypes
import datetime
import json
import os
import subprocess
import sys

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
if not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules")
    sys.exit(77)


def run_check(compiled, mode, scenarios=None):
    result = subprocess.run(
        [desktop_ts.node, os.path.join(HERE, "date_time_native_check.cjs"), compiled, mode, desktop_ts.MODULES],
        input=json.dumps(scenarios, ensure_ascii=True) if scenarios is not None else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    print(result.stdout + result.stderr)
    assert result.returncode == 0 and "ok:" in result.stdout, "date/time public-output check failed"


def windows_evidence():
    from ctypes import wintypes

    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [(name, wintypes.WORD) for name in
                    ("year", "month", "weekday", "day", "hour", "minute", "second", "millisecond")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    locale = kernel.GetLocaleInfoEx
    locale.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int]
    locale.restype = ctypes.c_int
    date_format = kernel.GetDateFormatEx
    date_format.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SYSTEMTIME),
                           wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_int, wintypes.LPCWSTR]
    date_format.restype = ctypes.c_int
    time_format = kernel.GetTimeFormatEx
    time_format.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SYSTEMTIME),
                           wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_int]
    time_format.restype = ctypes.c_int
    current_zone = kernel.GetDynamicTimeZoneInformation
    current_zone.argtypes = [ctypes.c_void_p]
    current_zone.restype = wintypes.DWORD
    enumerate_zone = ctypes.WinDLL("advapi32", use_last_error=True).EnumDynamicTimeZoneInformation
    enumerate_zone.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    enumerate_zone.restype = wintypes.DWORD
    to_local = kernel.SystemTimeToTzSpecificLocalTimeEx
    to_local.argtypes = [ctypes.c_void_p, ctypes.POINTER(SYSTEMTIME), ctypes.POINTER(SYSTEMTIME)]
    to_local.restype = wintypes.BOOL

    def native_text(call, *args):
        count = call(*args, None, 0)
        if not count:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(count)
        if not call(*args, buffer, count):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def get_date(wall, profile):
        pattern = profile.get("date")
        # lpCalendar follows the output buffer in this API, unlike the time API.
        name = profile.get("locale")
        count = date_format(name, 0 if pattern else 1, ctypes.byref(wall), pattern, None, 0, None)
        if not count:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(count)
        if not date_format(name, 0 if pattern else 1, ctypes.byref(wall), pattern, buffer, count, None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def system_time(value):
        return SYSTEMTIME(value.year, value.month, (value.weekday() + 1) % 7, value.day,
                          value.hour, value.minute, value.second, value.microsecond // 1000)

    def zone_named(name):
        for index in range(512):
            buffer = ctypes.create_string_buffer(432)
            error = enumerate_zone(index, buffer)
            if error == 259:
                break
            if error:
                raise ctypes.WinError(error)
            # Bytes after the WCHAR terminator are not part of the native string.
            raw_key = buffer.raw[172:428]
            end = next((offset for offset in range(0, len(raw_key), 2)
                        if raw_key[offset:offset + 2] == b"\0\0"), len(raw_key))
            key = raw_key[:end].decode("utf-16le")
            if key == name:
                return buffer.raw.hex()
        raise AssertionError(f"Windows timezone fixture unavailable: {name}")

    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

    def milliseconds(iso):
        delta = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")) - epoch
        return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000

    def evidence(requests, profile):
        zone = ctypes.create_string_buffer(432)
        if profile.get("zone"):
            ctypes.memmove(zone, bytes.fromhex(profile["zone"]), 432)
        elif current_zone(zone) == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        short = profile.get("shortTime") or native_text(locale, None, 0x79)
        long = profile.get("longTime") or native_text(locale, None, 0x1003)
        labels = []
        for request in requests:
            value, style = request["value"], request["style"]
            if isinstance(value, str):
                wall = system_time(datetime.datetime.strptime(value, "%Y-%m-%d"))
            else:
                utc = system_time(epoch + datetime.timedelta(milliseconds=value))
                wall = SYSTEMTIME()
                if not to_local(zone, ctypes.byref(utc), ctypes.byref(wall)):
                    raise ctypes.WinError(ctypes.get_last_error())
            day = get_date(wall, profile) if style in ("date", "dateTime") else None
            time = native_text(time_format, profile.get("locale"), 0, ctypes.byref(wall),
                               short if style == "shortTime" else long) if style != "date" else None
            labels.append(day if style == "date" else f"{day} {time}" if style == "dateTime" else time)
        return labels

    instants = [
        "1969-12-31T23:59:59.987Z", "2006-03-20T12:04:09Z", "2007-03-20T12:04:09Z",
        "2024-02-29T00:00:00Z", "2026-01-15T12:04:09Z", "2026-07-15T12:04:09Z",
        "2026-03-08T09:59:59Z", "2026-03-08T10:00:00Z",
        "2026-11-01T08:59:59Z", "2026-11-01T09:00:00Z", "2026-11-12T00:04:09Z",
    ]
    requests = [{"value": milliseconds(instant), "style": style}
                for instant in instants for style in ("date", "shortTime", "longTime", "dateTime")]
    calendar_dates = [{"value": value, "style": "date"} for value in
                      ("2026-11-12", "2024-02-29", "2000-02-29", "1601-01-01", "9999-12-31")]
    all_requests = requests + calendar_dates
    scenarios = [{"name": "current account regional overrides and timezone",
                  "requests": all_requests, "expected": evidence(all_requests, {})}]
    zones = {name: zone_named(name) for name in ("Pacific Standard Time", "AUS Eastern Standard Time")}
    profiles = [
        {"date": "dd'/'MM'/'yyyy", "shortTime": "HH':'mm", "longTime": "HH':'mm':'ss"},
        {"date": "yyyy 'literal day' MM'~'dd", "shortTime": "h 'short seconds' ss", "longTime": "HH 'long minutes' mm"},
        {"date": "dddd, dd MMMM yyyy", "shortTime": "hh':'mm tt", "longTime": "H'.'mm'.'ss 'clock'"},
        {"date": "d'-'M'-'yyyy", "shortTime": "'first' HH'!'ss", "longTime": "'second' h'?'mm tt"},
    ]
    for name, zone in zones.items():
        for index, patterns in enumerate(profiles):
            profile = {**patterns, "zone": zone, "locale": "en-US"}
            scenarios.append({"name": f"{name}: explicit native pattern {index + 1}",
                              "fixture": profile, "requests": all_requests, "expected": evidence(all_requests, profile)})
    # Independent, human-readable historical expectations guard against an API-binding error
    # in both implementations (US DST moved from April to March starting in 2007).
    profile = {"date": "yyyy'-'MM'-'dd", "shortTime": "HH':'mm", "longTime": "HH':'mm':'ss",
               "zone": zones["Pacific Standard Time"], "locale": "en-US"}
    historical = [{"value": milliseconds(value), "style": "dateTime"} for value in (
        "2006-03-20T12:04:09Z", "2007-03-20T12:04:09Z", "2026-01-15T12:04:09Z", "2026-07-15T12:04:09Z",
        "2026-03-08T09:59:59Z", "2026-03-08T10:00:00Z", "2026-11-01T08:59:59Z", "2026-11-01T09:00:00Z")]
    expected = ["2006-03-20 04:04:09", "2007-03-20 05:04:09", "2026-01-15 04:04:09", "2026-07-15 05:04:09",
                "2026-03-08 01:59:59", "2026-03-08 03:00:00", "2026-11-01 01:59:59", "2026-11-01 01:00:00"]
    assert evidence(historical, profile) == expected, "independent ctypes historical evidence disagrees"
    scenarios.append({"name": "known historical DST boundaries", "fixture": profile,
                      "requests": historical, "expected": expected})
    return scenarios


compiled = desktop_ts.main()
run_check(compiled, "unit")
if sys.platform == "win32":
    run_check(compiled, "native", windows_evidence())
else:
    print("SKIP: real Win32 parity requires Windows (portable contract/lifecycle checks passed)")
