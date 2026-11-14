"""Canonical, memory-owned audio for one spoken question recording."""
from dataclasses import dataclass, field
import struct

SAMPLE_RATE = 16000
FRAME_BYTES = 2
MAX_SECONDS = 120
MAX_FRAMES = SAMPLE_RATE * MAX_SECONDS
MAX_PCM_BYTES = MAX_FRAMES * FRAME_BYTES
WAV_HEADER_BYTES = 44


@dataclass(frozen=True, slots=True)
class Recording:
    """Nonempty 16 kHz mono PCM16; derive consumer formats only on demand."""

    pcm: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.pcm, bytes):
            raise TypeError("Recording PCM must be immutable bytes")
        if not self.pcm or len(self.pcm) % FRAME_BYTES:
            raise ValueError("Recording PCM must contain complete, nonempty PCM16 frames")
        if len(self.pcm) > MAX_PCM_BYTES:
            raise ValueError("Recording exceeds the 120-second audio limit")

    def wav_bytes(self) -> bytes:
        """Return a canonical PCM WAV without a file or shared stream cursor."""
        size = len(self.pcm)
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", size + 36, b"WAVE",
                             b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * FRAME_BYTES,
                             FRAME_BYTES, 16, b"data", size)
        return header + self.pcm

    def waveform(self):
        """Return an independent float32 waveform without a normalization copy."""
        import numpy as np
        audio = np.frombuffer(self.pcm, dtype="<i2").astype(np.float32)
        audio *= np.float32(1.0 / 32768.0)
        return audio
