#!/usr/bin/env python3
"""Capture contract at the approved controlled-native-device boundary; no microphone."""
import os
import sys
import threading
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
from ptt_capture import Capture, _Packet


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Device:
    """Borrowed packet storage is poisoned on release, like driver-owned memory."""
    def __init__(self, packets=(), tail=(), clock=None):
        self.packets = deque(packets)
        self.tail = list(tail)
        self.clock = clock or Clock()
        self.signal = threading.Event()
        self.waiting = threading.Event()
        self.closed = False
        self.borrowed = None
        self.owner = None
        self.fail = {}
        self.open_gate = None
        self.start_gate = None
        self.close_gate = None
        self.in_open = threading.Event()
        self.in_start = threading.Event()
        self.in_close = threading.Event()
        self.advance = 0.0
        self.repeat = None
        self.max_borrowed = 0

    def owned(self):
        assert self.owner == threading.get_ident(), "native operation left capture owner"

    def error(self, operation):
        if operation in self.fail:
            raise RuntimeError(self.fail[operation])

    def open(self):
        self.owner = threading.get_ident()
        self.in_open.set()
        if self.open_gate is not None:
            assert self.open_gate.wait(3), "open gate timed out"
        self.error("open")

    def start(self):
        self.owned()
        self.in_start.set()
        if self.start_gate is not None:
            assert self.start_gate.wait(3), "start gate timed out"
        self.error("start")

    def stop(self):
        self.owned()
        self.error("stop")
        self.packets.extend(self.tail)
        self.tail.clear()

    def next_packet(self):
        self.owned()
        assert self.borrowed is None, "packet not released"
        self.error("next_packet")
        self.clock.now += self.advance
        item = self.packets.popleft() if self.packets else self.repeat
        if item is None:
            return None
        data, frames, flags = item
        storage = bytearray(data) if data is not None else None
        self.borrowed = _Packet(frames, memoryview(storage) if storage is not None else None, flags)
        self.max_borrowed = max(self.max_borrowed, frames)
        return self.borrowed

    def release_packet(self, packet):
        self.owned()
        assert packet is self.borrowed
        if packet.data is not None:
            packet.data[:] = b"\x7f" * len(packet.data)
        self.borrowed = None
        self.error("release_packet")

    def wait(self, timeout):
        self.owned()
        self.waiting.set()
        if self.advance:
            self.clock.now += timeout
        else:
            assert self.signal.wait(3), "capture did not wake"
            self.signal.clear()
        self.error("wait")

    def wake(self):
        self.signal.set()

    def close(self):
        self.owned()
        self.in_close.set()
        if self.close_gate is not None:
            assert self.close_gate.wait(3), "close gate timed out"
        assert self.borrowed is None, "packet leaked"
        self.closed = True
        self.error("close")


def packet(data, flags=0):
    return (data, len(data) // 2, flags)


def begin(device):
    capture = Capture(_native_factory=lambda: device, _clock=device.clock)
    capture.start()
    return capture


def completed(capture, device):
    assert capture.done.wait(3), "capture did not retire"
    assert device.closed, "done published before native resources released"
    assert device.owner != threading.get_ident(), "capture ran on caller thread"
    return capture.recording


# Ordered, variable-size packets, including Stop's pending tail, survive driver release.
d = Device([packet(b"\x00\x80\xff\x7f"), packet(b"\x01\x00")], [packet(b"\xfe\xff\x02\x00")])
c = begin(d)
assert c.ready.wait(3)
assert d.waiting.wait(3)
c.finish()
r = completed(c, d)
assert c.error is None and r.pcm == b"\x00\x80\xff\x7f\x01\x00\xfe\xff\x02\x00"
print("capture ordered PCM and final tail OK")

# Silence is real PCM, and startup discontinuity / timing uncertainty are not loss.
d = Device([packet(b"\x01\x00", 0x1), (None, 3, 0x2 | 0x4), packet(b"\xff\xff", 0x4)])
c = begin(d)
assert c.ready.wait(3) and d.waiting.wait(3)
c.finish()
r = completed(c, d)
assert c.error is None and r.pcm == b"\x01\x00\x00\x00\x00\x00\x00\x00\xff\xff"

d = Device()
c = begin(d)
assert c.ready.wait(3) and d.waiting.wait(3)
c.finish()
assert completed(c, d) is None and c.error is None
print("capture silence, startup flags and empty input OK")

# finish and cancel do not wait on an in-flight open or Start.
for operation in ("open", "start"):
    for discard in (False, True):
        d = Device()
        gate = threading.Event()
        setattr(d, operation + "_gate", gate)
        c = begin(d)
        assert getattr(d, "in_" + operation).wait(3)
        assert not c.ready.is_set()
        if discard:
            c.cancel()
            c.finish()  # normal finish must never undo discard
        else:
            c.finish()
        assert not c.done.is_set(), "finish/cancel blocked instead of requesting retirement"
        gate.set()
        assert completed(c, d) is None and c.error is None

d = Device()
c = Capture(_native_factory=lambda: d, _clock=d.clock)
c.cancel()
c.start()
assert c.done.wait(3) and not c.ready.is_set() and c.recording is None and c.error is None
assert d.owner is None, "cancelled attempt opened a microphone"
print("capture release and cancellation during startup OK")

# Cancellation after known samples, and during cleanup after a successful finish,
# cannot publish an already-built recording. Once done, terminal results are stable.
for during_cleanup in (False, True):
    d = Device([packet(b"\x01\x00\x02\x00")])
    if during_cleanup:
        d.close_gate = threading.Event()
    c = begin(d)
    assert c.ready.wait(3) and d.waiting.wait(3)
    if during_cleanup:
        c.finish()
        assert d.in_close.wait(3)
        assert not c.done.is_set()
    c.cancel()
    c.finish()
    if during_cleanup:
        d.close_gate.set()
    assert completed(c, d) is None and c.error is None
    c.cancel()
    c.finish()
    assert c.recording is None and c.error is None
    try:
        c.start()
        raise AssertionError("attempt was restarted")
    except RuntimeError:
        pass
print("capture cancellation dominates finish and late completion OK")

# Each native failure is terminal and releases this attempt before done is visible.
# A format/open failure must not acknowledge readiness.
for operation, message in (
        ("open", "WASAPI initialize failed: unsupported PCM conversion"),
        ("start", "WASAPI Start failed: microphone access denied"),
        ("next_packet", "WASAPI GetBuffer failed: audio device invalidated"),
        ("release_packet", "WASAPI ReleaseBuffer failed"),
        ("wait", "WASAPI event wait failed"),
        ("stop", "WASAPI Stop failed"),
        ("close", "WASAPI close capture event failed")):
    d = Device([packet(b"\x01\x00")])
    d.fail[operation] = message
    if operation == "wait":
        d.signal.set()
    c = begin(d)
    if operation in ("stop", "close"):
        assert c.ready.wait(3) and d.waiting.wait(3)
        c.finish()
    assert completed(c, d) is None
    assert message in c.error, c.error
    if operation in ("open", "start"):
        assert not c.ready.is_set()

# Cleanup failures cannot leave a successful/partial recording usable or erase the
# original failure. Open-failure cleanup still belongs to the same capture owner.
d = Device()
d.fail = {"open": "default microphone absent", "close": "event cleanup failed"}
c = begin(d)
assert completed(c, d) is None
assert "default microphone absent" in c.error and "event cleanup failed" in c.error
print("capture native failures and cleanup ownership OK")

# A later discontinuity is actual known loss, including in pending tail packets.
for in_tail in (False, True):
    lost = packet(b"\x02\x00", 0x1 | 0x4)
    d = Device([packet(b"\x01\x00")] + ([] if in_tail else [lost]),
               [lost] if in_tail else [])
    c = begin(d)
    if in_tail:
        assert c.ready.wait(3) and d.waiting.wait(3)
        c.finish()
    assert completed(c, d) is None and "discontinuity" in c.error

# Invalid packet descriptions are rejected before exposing a Recording; the
# borrowed storage is still released even when validation or the cap rejects it.
for invalid in ((None, 0, 0x2), (b"\x01", 2, 0), (None, 2, 0),
                (None, 1_920_001, 0x2)):
    d = Device([invalid])
    c = begin(d)
    assert completed(c, d) is None and c.error
print("capture integrity rejection and borrowed-packet cleanup OK")

# Reaching (not just exceeding) the frame ceiling discards the whole recording.
# A continuously full endpoint still has a bounded drain and checks monotonic time.
d = Device()
d.repeat = (None, 480_000, 0x2)
c = begin(d)
assert completed(c, d) is None and "frame limit" in c.error

d = Device()
d.repeat = packet(b"\x01\x00")
d.advance = 30.0
c = begin(d)
assert completed(c, d) is None and "duration limit" in c.error
assert d.clock.now <= 120.0, "full endpoint starved the deadline"

# A lost/missing event cannot leave the attempt listening forever.
d = Device()
d.advance = 0.01
c = begin(d)
assert completed(c, d) is None and "stopped delivering audio" in c.error
assert 5.0 <= d.clock.now < 6.0

# The private shorter ceiling is useful for a separately authorized hardware
# proof; production still defaults to the fixed 120-second ceiling.
d = Device()
d.repeat = packet(b"\x01\x00")
d.advance = 0.5
c = Capture(_native_factory=lambda: d, _clock=d.clock, _max_seconds=2.0)
c.start()
assert completed(c, d) is None and "duration limit" in c.error
assert d.clock.now == 2.0
for invalid_limit in (0, -1, 121, float("nan")):
    try:
        Capture(_max_seconds=invalid_limit)
        raise AssertionError("invalid safety ceiling accepted")
    except ValueError:
        pass
print("capture frame/time bounds and no-progress retirement OK")

# A failed attempt cannot poison the next independent microphone lifetime.
failed = Device([packet(b"\x01\x00"), packet(b"\x02\x00", 0x1)])
old = begin(failed)
assert completed(old, failed) is None and old.error
d = Device([packet(b"\x11\x00")], [packet(b"\x22\x00")])
c = begin(d)
assert c.ready.wait(3) and d.waiting.wait(3)
old.finish()
old.cancel()
c.finish()
r = completed(c, d)
assert c.error is None and r.pcm == b"\x11\x00\x22\x00"
assert old.recording is None and old.error
c.cancel()
c.finish()
assert c.recording is r and c.error is None, "done result mutated"
print("capture fresh-attempt recovery and immutable terminal outcomes OK")

# Cancellation is observed even inside an always-ready drain, before the borrowed
# packet is copied; native ownership still requires releasing that packet.
class PendingPacketDevice(Device):
    def __init__(self):
        super().__init__()
        self.repeat = packet(b"\x01\x00")
        self.in_packet = threading.Event()
        self.packet_gate = threading.Event()

    def next_packet(self):
        value = super().next_packet()
        self.in_packet.set()
        assert self.packet_gate.wait(3), "packet gate timed out"
        return value


d = PendingPacketDevice()
c = begin(d)
assert c.ready.wait(3) and d.in_packet.wait(3)
c.cancel()
c.finish()
assert not c.done.is_set()
d.packet_gate.set()
assert completed(c, d) is None and c.error is None

# Release during native Start still retains whatever complete tail was actually
# captured, rather than treating the lack of a prior ready poll as empty audio.
d = Device(tail=[packet(b"\x33\x00")])
d.start_gate = threading.Event()
c = begin(d)
assert d.in_start.wait(3)
c.finish()
d.start_gate.set()
r = completed(c, d)
assert c.ready.is_set() and c.error is None and r.pcm == b"\x33\x00"
print("capture in-drain cancellation and release-during-Start tail OK")

# Start may have already enabled the stream before its call returns. Time spent
# in that call cannot grant extra recording time beyond the hard safety ceiling.
d = Device([packet(b"\x01\x00")])
d.start_gate = threading.Event()
c = begin(d)
assert d.in_start.wait(3)
d.clock.now = 120.0
d.start_gate.set()
assert completed(c, d) is None and "duration limit" in c.error
print("capture Start acknowledgement cannot extend the recording limit OK")
