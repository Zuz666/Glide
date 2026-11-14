"""Per-attempt, fileless WASAPI capture. Only the owner thread touches COM/audio packets."""
from __future__ import annotations

import ctypes
import io
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from ptt_audio import Recording

_MAX_SECONDS = 120.0
_MAX_FRAMES = 1_920_000
_NO_PROGRESS_SECONDS = 5.0
_SILENT = 0x2
_DISCONTINUITY = 0x1
_ZEROES = bytes(4096)


@dataclass(frozen=True)
class _Packet:
    """Borrowed native storage: valid only until release_packet on the same thread."""
    frames: int
    data: memoryview | None
    flags: int = 0


class _NativeDevice(Protocol):
    def open(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def next_packet(self) -> _Packet | None: ...
    def release_packet(self, packet: _Packet) -> None: ...
    def wait(self, timeout: float) -> None: ...
    def wake(self) -> None: ...
    def close(self) -> None: ...


class _Cancelled(Exception):
    pass


class Capture:
    """One microphone attempt; finish/cancel never wait for native initialization or cleanup.

    ready acknowledges successful native Start, while done acknowledges full cleanup.
    Read error/recording only after done. The private adapter/clock seam is for controlled
    native-contract tests, not endpoint selection or production settings.
    """
    def __init__(self, *, _native_factory: Callable[[], _NativeDevice] | None = None,
                 _clock: Callable[[], float] = time.monotonic, _max_seconds: float = _MAX_SECONDS):
        if not 0 < _max_seconds <= _MAX_SECONDS:
            raise ValueError("Capture duration must be positive and at most 120 seconds")
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error: str | None = None
        self.recording: Recording | None = None
        self._factory = _native_factory or _Wasapi
        self._clock = _clock
        self._max_seconds = _max_seconds
        self._lock = threading.Lock()
        self._native: _NativeDevice | None = None
        self._request = 0  # 0: collecting, 1: normal finish, 2: discard (dominates finish)
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("Capture can only be started once")
            self._started = True
        try:
            threading.Thread(target=self._run, name="ptt-capture", daemon=True).start()
        except Exception as exc:
            with self._lock:
                self.error = "Cannot start microphone capture: %s" % exc
                self.done.set()

    def finish(self) -> None:
        self._control(1)

    def cancel(self) -> None:
        self._control(2)

    def _control(self, request: int) -> None:
        with self._lock:
            if self.done.is_set():
                return
            self._request = max(request, self._request)
            if self._native is not None:
                # The only cross-thread native operation is signaling a control event.
                self._native.wake()

    def _requested(self) -> int:
        with self._lock:
            return self._request

    def _run(self) -> None:
        native = None
        recording = None
        error = None
        pcm = io.BytesIO()
        try:
            if self._requested() == 2:
                raise _Cancelled()
            native = self._factory()
            with self._lock:
                self._native = native
            native.open()
            if self._requested() == 2:
                raise _Cancelled()
            began = self._clock()
            native.start()
            last_progress = self._clock()
            self.ready.set()
            frames = 0
            first_packet = True
            stopped = False
            while True:
                request = self._requested()
                if request == 2:
                    raise _Cancelled()
                now = self._clock()
                if now - began >= self._max_seconds:
                    raise RuntimeError("Microphone recording reached the capture duration limit")
                if request == 1 and not stopped:
                    native.stop()
                    stopped = True
                packet = native.next_packet()
                if packet is None:
                    if stopped:
                        break
                    now = self._clock()
                    if now - last_progress >= _NO_PROGRESS_SECONDS:
                        raise RuntimeError("Microphone stopped delivering audio")
                    native.wait(min(0.25, self._max_seconds - (now - began),
                                    _NO_PROGRESS_SECONDS - (now - last_progress)))
                    continue
                try:
                    # A continually full device must not starve control/deadline checks.
                    if self._requested() == 2:
                        raise _Cancelled()
                    if self._clock() - began >= self._max_seconds:
                        raise RuntimeError("Microphone recording reached the capture duration limit")
                    if packet.frames <= 0:
                        raise RuntimeError("Microphone returned an empty audio packet")
                    if frames + packet.frames >= _MAX_FRAMES:
                        raise RuntimeError("Microphone recording reached the 120-second frame limit")
                    # WASAPI may mark its first packet discontinuous at stream startup.
                    # Later flags indicate actual missing data. TIMESTAMP_ERROR alone
                    # says nothing about sample loss, so timestamps are not a loss test.
                    if packet.flags & _DISCONTINUITY and not first_packet:
                        raise RuntimeError("Microphone audio was lost (data discontinuity)")
                    size = packet.frames * 2
                    if packet.flags & _SILENT:
                        while size:
                            count = min(size, len(_ZEROES))
                            pcm.write(memoryview(_ZEROES)[:count])
                            size -= count
                    else:
                        if packet.data is None or packet.data.nbytes != size:
                            raise RuntimeError("Microphone returned invalid PCM packet data")
                        pcm.write(packet.data)
                    frames += packet.frames
                    first_packet = False
                    last_progress = self._clock()
                finally:
                    native.release_packet(packet)
            if frames:
                recording = Recording(pcm.getvalue())
        except _Cancelled:
            pass
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        finally:
            # Stop exposing the wake target before closing its control handle.
            with self._lock:
                self._native = None
            if native is not None:
                try:
                    native.close()
                except Exception as exc:
                    detail = str(exc) or type(exc).__name__
                    error = "%s; cleanup: %s" % (error, detail) if error else detail
            pcm.close()
            with self._lock:
                if self._request == 2 or error is not None:
                    recording = None
                self.error = error
                self.recording = recording
                self.done.set()


# Do not use ctypes.c_long / wintypes.DWORD here: widths must remain Windows widths
# even when the controlled-device tests import this module on another OS.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_HRESULT = ctypes.c_int32
_UINT64 = ctypes.c_uint64
_VOID = ctypes.c_void_p
_CALL = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", _DWORD), ("Data2", _WORD), ("Data3", _WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _WaveFormat(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("wFormatTag", _WORD), ("nChannels", _WORD), ("nSamplesPerSec", _DWORD),
                ("nAvgBytesPerSec", _DWORD), ("nBlockAlign", _WORD),
                ("wBitsPerSample", _WORD), ("cbSize", _WORD)]


_ENUMERATOR_CLASS = _GUID.parse("bcde0395-e52f-467c-8e3d-c4579291692e")
_ENUMERATOR_IID = _GUID.parse("a95664d2-9614-4f35-a746-de8db63617e6")
_AUDIO_IID = _GUID.parse("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
_CAPTURE_IID = _GUID.parse("c8adbd64-e71e-48a0-a4de-185c395cd317")
_HRESULT_REASONS = {
    0x88890004: "audio device invalidated",
    0x88890008: "16 kHz mono PCM conversion unsupported",
    0x8889000A: "audio device in use",
    0x8889000F: "audio service unavailable",
    0x88890026: "audio resources invalidated",
    0x80070490: "no default input device",
    0x80070005: "microphone access denied",
}


def _check(result: int, operation: str) -> None:
    if result < 0:
        code = result & 0xFFFFFFFF
        reason = _HRESULT_REASONS.get(code)
        suffix = ": " + reason if reason else ""
        raise RuntimeError("WASAPI %s failed (0x%08X%s)" % (operation, code, suffix))


def _method(pointer: _VOID, index: int, result, *arguments):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_VOID))).contents
    return _CALL(result, _VOID, *arguments)(table[index])


class _Wasapi:
    """Minimal shared/event-driven WASAPI binding. Every resource is per attempt.

    All methods except wake belong to the capture-owner STA thread. wake's event
    lifetime is protected separately, and never holds a lock during COM operations.
    """
    def __init__(self):
        self._enum = _VOID()
        self._device = _VOID()
        self._audio = _VOID()
        self._capture = _VOID()
        self._audio_event = None
        self._control_event = None
        self._wake_lock = threading.Lock()
        self._com_initialized = False
        self._started = False
        self._owner = None
        self._ole = None
        self._kernel = None
        self._wake_error: str | None = None

    def _owned(self) -> None:
        if self._owner != threading.get_ident():
            raise RuntimeError("WASAPI used outside capture-owner thread")

    def open(self) -> None:
        self._owner = threading.get_ident()
        if sys.platform != "win32":
            raise RuntimeError("WASAPI microphone capture requires Windows")
        self._ole = ctypes.WinDLL("ole32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ole.CoInitializeEx.argtypes = [_VOID, _DWORD]
        self._ole.CoInitializeEx.restype = _HRESULT
        self._ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), _VOID, _DWORD,
                                              ctypes.POINTER(_GUID), ctypes.POINTER(_VOID)]
        self._ole.CoCreateInstance.restype = _HRESULT
        self._ole.CoUninitialize.argtypes = []
        self._ole.CoUninitialize.restype = None
        self._kernel.CreateEventW.argtypes = [_VOID, ctypes.c_int32, ctypes.c_int32, ctypes.c_wchar_p]
        self._kernel.CreateEventW.restype = _VOID
        self._kernel.SetEvent.argtypes = [_VOID]
        self._kernel.SetEvent.restype = ctypes.c_int32
        self._kernel.CloseHandle.argtypes = [_VOID]
        self._kernel.CloseHandle.restype = ctypes.c_int32
        self._kernel.WaitForMultipleObjects.argtypes = [_DWORD, ctypes.POINTER(_VOID), ctypes.c_int32, _DWORD]
        self._kernel.WaitForMultipleObjects.restype = _DWORD
        _check(self._ole.CoInitializeEx(None, 2), "CoInitializeEx(STA)")
        self._com_initialized = True  # S_FALSE also requires CoUninitialize.
        with self._wake_lock:
            self._control_event = self._new_event()
        self._audio_event = self._new_event()
        _check(self._ole.CoCreateInstance(ctypes.byref(_ENUMERATOR_CLASS), None, 23,
                                         ctypes.byref(_ENUMERATOR_IID), ctypes.byref(self._enum)),
               "create device enumerator")
        _check(_method(self._enum, 4, _HRESULT, ctypes.c_int32, ctypes.c_int32,
                       ctypes.POINTER(_VOID))(self._enum, 1, 0, ctypes.byref(self._device)),
               "open default input (eCapture/eConsole)")
        _check(_method(self._device, 3, _HRESULT, ctypes.POINTER(_GUID), _DWORD,
                       _VOID, ctypes.POINTER(_VOID))(self._device, ctypes.byref(_AUDIO_IID),
                                                    23, None, ctypes.byref(self._audio)),
               "activate input audio client")
        wave = _WaveFormat(1, 1, 16000, 32000, 2, 16, 0)
        # Shared mode; event callback plus Windows PCM/channel/rate conversion.
        # Both duration and periodicity MUST be zero for shared event capture.
        flags = 0x00040000 | 0x80000000
        _check(_method(self._audio, 3, _HRESULT, ctypes.c_int32, _DWORD,
                       ctypes.c_int64, ctypes.c_int64, ctypes.POINTER(_WaveFormat),
                       ctypes.POINTER(_GUID))(self._audio, 0, flags, 0, 0, ctypes.byref(wave), None),
               "initialize shared 16 kHz mono PCM16 input")
        _check(_method(self._audio, 13, _HRESULT, _VOID)(self._audio, self._audio_event),
               "SetEventHandle")
        _check(_method(self._audio, 14, _HRESULT, ctypes.POINTER(_GUID),
                       ctypes.POINTER(_VOID))(self._audio, ctypes.byref(_CAPTURE_IID),
                                             ctypes.byref(self._capture)), "get capture client")

    def _new_event(self):
        assert self._kernel is not None  # DLL binding precedes event creation.
        handle = self._kernel.CreateEventW(None, False, False, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def start(self) -> None:
        self._owned()
        _check(_method(self._audio, 10, _HRESULT)(self._audio), "Start")
        self._started = True

    def stop(self) -> None:
        self._owned()
        if self._started:
            try:
                _check(_method(self._audio, 11, _HRESULT)(self._audio), "Stop")
            finally:
                self._started = False

    def next_packet(self) -> _Packet | None:
        self._owned()
        count = _DWORD()
        _check(_method(self._capture, 5, _HRESULT, ctypes.POINTER(_DWORD))(
            self._capture, ctypes.byref(count)), "GetNextPacketSize")
        if not count.value:
            return None
        data = _VOID()
        frames = _DWORD()
        flags = _DWORD()
        # Timing observations are optional and not used to infer lost samples.
        result = _method(self._capture, 3, _HRESULT, ctypes.POINTER(_VOID),
                         ctypes.POINTER(_DWORD), ctypes.POINTER(_DWORD),
                         ctypes.POINTER(_UINT64), ctypes.POINTER(_UINT64))(
            self._capture, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
        _check(result, "GetBuffer")
        if result == 0x08890001:  # AUDCLNT_S_BUFFER_EMPTY: no packet acquired/released.
            return None
        try:
            view = None
            # Validate bounds before constructing a view onto driver-owned memory.
            if frames.value > _MAX_FRAMES:
                raise RuntimeError("Microphone packet exceeds recording frame limit")
            if frames.value and not flags.value & _SILENT:
                address = data.value
                if not address:
                    raise RuntimeError("Microphone returned a null audio packet")
                storage = (ctypes.c_ubyte * (frames.value * 2)).from_address(address)
                view = memoryview(storage).cast("B")
            return _Packet(frames.value, view, flags.value)
        except Exception:
            _check(_method(self._capture, 4, _HRESULT, _DWORD)(self._capture, frames.value),
                   "ReleaseBuffer")
            raise

    def release_packet(self, packet: _Packet) -> None:
        self._owned()
        _check(_method(self._capture, 4, _HRESULT, _DWORD)(self._capture, packet.frames),
               "ReleaseBuffer")

    def wait(self, timeout: float) -> None:
        self._owned()
        assert self._kernel is not None  # Only reached after successful open/Start.
        if self._wake_error:
            raise RuntimeError(self._wake_error)
        events = (_VOID * 2)(self._control_event, self._audio_event)
        result = self._kernel.WaitForMultipleObjects(2, events, False, max(1, int(timeout * 1000)))
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if result not in (0, 1, 0x102):
            raise RuntimeError("WASAPI event wait failed (0x%08X)" % result)

    def wake(self) -> None:
        with self._wake_lock:
            if self._control_event is not None:
                assert self._kernel is not None  # A live event implies its DLL was bound.
                if not self._kernel.SetEvent(self._control_event):
                    # Surface failure on the owner, not the keyboard polling thread.
                    self._wake_error = "WASAPI control event failed (%s)" % ctypes.get_last_error()

    def close(self) -> None:
        self._owned()
        errors = []
        try:
            self.stop()
        except Exception as exc:
            errors.append(str(exc))
        for name in ("_capture", "_audio", "_device", "_enum"):
            pointer = getattr(self, name)
            if pointer.value:
                try:
                    _method(pointer, 2, _DWORD)(pointer)  # IUnknown.Release returns ULONG, not HRESULT.
                except Exception as exc:
                    errors.append("release %s: %s" % (name, exc))
                finally:
                    setattr(self, name, _VOID())
        with self._wake_lock:
            control = self._control_event
            self._control_event = None
        for handle in (self._audio_event, control):
            if handle is not None:
                try:
                    assert self._kernel is not None  # Every event was created by this DLL.
                    if not self._kernel.CloseHandle(handle):
                        errors.append("close capture event: %s" % ctypes.get_last_error())
                except Exception as exc:
                    errors.append("close capture event: %s" % exc)
        self._audio_event = None
        if self._com_initialized:
            try:
                assert self._ole is not None  # Paired with successful CoInitializeEx.
                self._ole.CoUninitialize()
            except Exception as exc:
                errors.append("CoUninitialize: %s" % exc)
            finally:
                self._com_initialized = False
        if errors:
            raise RuntimeError("; ".join(errors))
