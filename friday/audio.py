"""One full-duplex audio stream: plays Friday's voice and records the mic.

Doing both in the same callback means we know exactly what went to the speaker on
every 10 ms tick, so WebRTC's echo canceller can subtract her voice from the mic.
That's what lets her hear you say "stop" while she's still talking.

Callers get 16 kHz int16 frames (80 ms) of cleaned mic audio, and can queue speech
clips (48 kHz float32) that play back-to-back, be ducked, or be cut off instantly.
"""

import asyncio
import threading
from collections import deque
from collections.abc import Callable

import numpy as np
import sounddevice as sd
from scipy.signal import firwin, lfilter

from . import config

BLOCK = config.IO_RATE // 100  # 10 ms, the unit the echo canceller works in
DECIM = config.IO_RATE // config.SAMPLE_RATE  # 48 kHz -> 16 kHz


class EchoCanceller:
    """WebRTC AEC3 plus 48->16 kHz downsampling. Pure processing, so it's testable offline."""

    def __init__(self, enabled: bool = True, delay_ms: int = 30):
        self.enabled = enabled
        if enabled:
            import livekit.rtc as rtc

            self._rtc = rtc
            # Noise suppression is off on purpose: in testing it ate the start of "stop".
            self.apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
            self.apm.set_stream_delay_ms(delay_ms)
        self._taps = firwin(63, 7000, fs=config.IO_RATE)  # anti-alias before dropping to 16 kHz
        self._zi = np.zeros(len(self._taps) - 1)

    def process(self, mic: np.ndarray, speaker: np.ndarray) -> np.ndarray:
        """mic and speaker: one 10 ms block of int16 at 48 kHz. Returns 16 kHz int16."""
        if self.enabled:
            ref = self._rtc.AudioFrame(speaker.tobytes(), config.IO_RATE, 1, BLOCK)
            self.apm.process_reverse_stream(ref)
            frame = self._rtc.AudioFrame(mic.tobytes(), config.IO_RATE, 1, BLOCK)
            self.apm.process_stream(frame)
            mic = np.frombuffer(frame.data, dtype=np.int16)
        y, self._zi = lfilter(self._taps, 1.0, mic.astype(np.float64), zi=self._zi)
        return np.clip(y[::DECIM], -32768, 32767).astype(np.int16)


class _Clip:
    def __init__(self, pcm: np.ndarray, done: Callable[[], None]):
        self.pcm, self.pos, self.done = pcm, 0, done


class AudioIO:
    def __init__(self, on_frame: Callable[[np.ndarray], None] | None):
        """on_frame gets each 80 ms mic frame on the asyncio loop; None = playback only."""
        self.on_frame = on_frame
        self.volume = config.VOLUME
        self.ducked = False
        self.out_level = 0.0  # loudness of what's playing right now (0-1), for the UI
        self._clips: deque[_Clip] = deque()
        self._lock = threading.Lock()
        self._acc: list[np.ndarray] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._aec: EchoCanceller | None = None
        self._stream: sd.Stream | sd.OutputStream | None = None

    # -- stream -------------------------------------------------------------------
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.on_frame is None:
            self._stream = sd.OutputStream(
                samplerate=config.IO_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                callback=lambda out, n, t, s: self._fill(out[:, 0]),
            )
        else:
            self._stream = sd.Stream(
                samplerate=config.IO_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                callback=self._duplex,
            )
        self._stream.start()
        if self.on_frame is not None:
            in_lat, out_lat = self._stream.latency
            self._aec = EchoCanceller(config.ECHO_CANCEL, delay_ms=int((in_lat + out_lat) * 1000) + 10)

    def stop(self) -> None:
        """Close the stream (mic off, nothing plays). start() opens it again."""
        self.cut()
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._acc = []
        self.out_level = 0.0

    @property
    def running(self) -> bool:
        return self._stream is not None

    def _duplex(self, indata, outdata, frames, t, status) -> None:
        spk = self._fill(outdata[:, 0])
        if self._aec is None or frames != BLOCK:
            return
        mic16 = (np.clip(indata[:, 0], -1, 1) * 32767).astype(np.int16)
        spk16 = (np.clip(spk, -1, 1) * 32767).astype(np.int16)
        self._acc.append(self._aec.process(mic16, spk16))
        if len(self._acc) * (BLOCK // DECIM) >= config.FRAME:
            frame = np.concatenate(self._acc)
            self._acc = []
            self._loop.call_soon_threadsafe(self.on_frame, frame)

    def _fill(self, out: np.ndarray) -> np.ndarray:
        """Write the next block of speech (or silence) into out; return what was played."""
        out[:] = 0
        gain = self.volume * (config.DUCK_VOLUME if self.ducked else 1.0)
        n, filled = len(out), 0
        with self._lock:
            while filled < n and self._clips:
                clip = self._clips[0]
                take = min(n - filled, len(clip.pcm) - clip.pos)
                out[filled:filled + take] = clip.pcm[clip.pos:clip.pos + take] * gain
                clip.pos += take
                filled += take
                if clip.pos >= len(clip.pcm):
                    self._clips.popleft()
                    self._loop.call_soon_threadsafe(clip.done)
        self.out_level = float(np.sqrt(np.mean(out * out))) if filled else 0.0
        return out

    # -- playback -----------------------------------------------------------------
    def play(self, pcm: np.ndarray) -> asyncio.Future:
        """Queue a 48 kHz float32 clip. The future resolves when it finishes or is cut off
        (immediately if the stream is closed, e.g. Friday is switched off)."""
        loop = self._loop or asyncio.get_running_loop()
        fut = loop.create_future()
        if self._stream is None:
            fut.set_result(None)
            return fut
        done = lambda: fut.done() or fut.set_result(None)  # noqa: E731
        with self._lock:
            self._clips.append(_Clip(pcm.astype(np.float32), done))
        return fut

    @property
    def playing(self) -> bool:
        return bool(self._clips)

    def cut(self) -> None:
        """Stop all speech right now."""
        with self._lock:
            clips = list(self._clips)
            self._clips.clear()
        for c in clips:
            c.done()
        self.ducked = False
