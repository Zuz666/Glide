#!/usr/bin/env python3
"""WASAPI boundary regression: a scheduling pause must not truncate a question."""
import ctypes
import os
import struct
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
import ptt_capture as audio


class Endpoint:
    """Finite native ring with a 10 ms producer, independent of the Python reader.

    The minimum event buffer reproduces the measured 356-frame Windows endpoint.
    Timer-mode capacity comes from Initialize, not from a test-selected buffer size.
    Only the operating-system boundary is controlled: Capture and _Wasapi are real.
    """
    def __init__(self):
        self.now = 0.0
        self.expected = struct.pack("<6400h", *range(-3200, 3200))
        self.capacity = 0
        self.flags = 0
        self.read_frame = 0
        self.produced = 0
        self.frames = 0
        self.gap = False
        self.delayed = False
        self.stopped = False
        self.closed = set()
        self.handles = set()
        self.storage = None
        self.capture = audio.Capture(_clock=lambda: self.now)
        self.ole = Mock()
        self.ole.CoInitializeEx.return_value = 0
        self.ole.CoCreateInstance.side_effect = lambda *args: self.assign(args[-1], 1)
        self.kernel = Mock()
        self.kernel.CreateEventW.side_effect = self.new_event
        self.kernel.CloseHandle.side_effect = self.close_event
        self.kernel.SetEvent.return_value = 1
        self.kernel.WaitForMultipleObjects.side_effect = lambda count, handles, all_, ms: self.wait(ms)
        self.kernel.WaitForSingleObject.side_effect = lambda handle, ms: self.wait(ms)

    @staticmethod
    def assign(pointer, value):
        pointer._obj.value = value
        return 0

    def new_event(self, *args):
        handle = len(self.handles) + 100
        self.handles.add(handle)
        return handle

    def close_event(self, handle):
        self.handles.remove(handle)
        return 1

    def wait(self, milliseconds):
        # Audio-ready events arrive every 10 ms in the original implementation.
        self.now += min(milliseconds / 1000, 0.01) if self.flags & 0x00040000 else milliseconds / 1000
        if self.read_frame and not self.delayed:
            self.delayed = True
            self.now += 0.15
        if self.now >= 0.4:
            self.capture.finish()
        return 0x102

    def initialize(self, this, share, flags, duration, period, wave, session):
        assert share == 0 and period == 0, "shared input required"
        assert (wave._obj.nSamplesPerSec, wave._obj.nChannels, wave._obj.wBitsPerSample) == (16000, 1, 16)
        self.flags = flags
        if flags & 0x00040000:
            assert duration == 0, "shared event-driven capture requires zero duration"
            self.capacity = 356
        else:
            self.capacity = max(356, duration * 16000 // 10_000_000)
        return 0

    def update(self):
        if not self.stopped:
            self.produced = min(6400, round(self.now * 16000) // 160 * 160)
        if self.produced - self.read_frame > self.capacity:
            self.read_frame = self.produced - self.capacity
            self.gap = True

    def stop(self, this):
        self.update()
        self.stopped = True
        return 0

    def packet_size(self, this, output):
        self.update()
        return self.assign(output, min(160, self.produced - self.read_frame))

    def get_buffer(self, this, data, frames, flags, position, timestamp):
        self.frames = min(160, self.produced - self.read_frame)
        chunk = self.expected[self.read_frame * 2:(self.read_frame + self.frames) * 2]
        self.storage = ctypes.create_string_buffer(chunk)
        self.assign(data, ctypes.addressof(self.storage))
        self.assign(frames, self.frames)
        self.assign(flags, int(self.gap))
        self.gap = False
        return 0

    def release_buffer(self, this, frames):
        assert frames == self.frames
        self.read_frame += frames
        self.storage = None
        return 0

    def method(self, pointer, index, result, *arguments):
        if index == 2:
            def release(this):
                self.closed.add(this.value)
                return 0
            return release
        return {
            (1, 4): lambda this, flow, role, out: self.assign(out, 2),
            (2, 3): lambda this, iid, context, params, out: self.assign(out, 3),
            (3, 3): self.initialize,
            (3, 13): lambda this, event: 0,
            (3, 14): lambda this, iid, out: self.assign(out, 4),
            (3, 10): lambda this: 0,
            (3, 11): self.stop,
            (4, 5): self.packet_size,
            (4, 3): self.get_buffer,
            (4, 4): self.release_buffer,
        }[(pointer.value, index)]

    def dll(self, name, **kwargs):
        return {"ole32": self.ole, "kernel32": self.kernel}[name]


endpoint = Endpoint()
with patch.object(audio.sys, "platform", "win32"), \
        patch.object(audio.ctypes, "WinDLL", endpoint.dll, create=True), \
        patch.object(audio, "_method", endpoint.method):
    endpoint.capture.start()
    assert endpoint.capture.done.wait(3), "capture did not retire"

assert endpoint.capture.error is None, endpoint.capture.error
recording = endpoint.capture.recording
assert recording is not None and recording.pcm == endpoint.expected, "question lost or reordered samples"
assert endpoint.delayed, "scheduler pause was not exercised"
assert endpoint.closed == {1, 2, 3, 4} and not endpoint.handles, "capture resources leaked"
assert endpoint.stopped
print("WASAPI capture preserves the complete question across a 150 ms reader pause OK")
