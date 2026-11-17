"""Text-to-speech: macOS `say` renders each sentence, AudioIO plays it.

Sentences are rendered one ahead of playback, so she starts talking quickly and long
answers flow without gaps. Everything can be cut off mid-word.
"""

import asyncio
import re
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from . import config
from .audio import AudioIO


def to_speech(text: str) -> str:
    """Turn markdown-ish model output into something pleasant to hear."""
    text = re.sub(r"```.*?```", " I've put the code on screen. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links -> label
    text = re.sub(r"https?://\S+", "the link on screen", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)  # headings
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)  # bullets
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.M)  # tables
    text = re.sub(r"[*_~]{1,3}", "", text)
    text = re.sub(r"\n{2,}", ". ", text)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    out: list[str] = []
    for p in parts:  # glue very short bits to the previous sentence so it doesn't sound choppy
        if out and len(p) < 25:
            out[-1] += " " + p
        else:
            out.append(p)
    return [s for s in out if s.strip()]


class Mouth:
    def __init__(self, audio: AudioIO, muted: bool = False):
        self.audio = audio
        self.muted = muted
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._pending = 0  # sentences queued or playing
        self._idle = asyncio.Event()
        self._idle.set()
        self._epoch = 0  # bumped on stop(), so renders already in flight are dropped
        self._recent: list[list] = []  # [sentence, finished_at] (inf while still playing)
        self._tmp = Path(tempfile.mkdtemp(prefix="friday-tts-"))

    def start(self) -> None:
        asyncio.create_task(self._render_loop())

    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

    def say(self, text: str) -> None:
        spoken = to_speech(text)
        if not spoken or self.muted or not self.audio.running:
            return
        for s in sentences(spoken):
            self._pending += 1
            self._idle.clear()
            self._q.put_nowait(s)

    async def say_and_wait(self, text: str) -> None:
        self.say(text)
        await self.wait_idle()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def stop(self) -> None:
        """Cut speech off immediately."""
        self._epoch += 1
        while not self._q.empty():
            self._q.get_nowait()
        self.audio.cut()
        self._pending = 0
        self._idle.set()

    def recent_text(self, within: float = 2.5) -> str:
        """What she's saying now or said in the last few seconds (to recognise her own echo)."""
        now = time.monotonic()
        self._recent = [e for e in self._recent if e[1] > now - 15]  # keep the same objects
        return " ".join(s for s, t in self._recent if t > now - within)

    async def chime(self, sound: str = config.CHIME) -> None:
        proc = await asyncio.create_subprocess_exec("afplay", "-v", "0.5", sound)
        await proc.wait()

    # -- internals ----------------------------------------------------------------
    async def _render_loop(self) -> None:
        n = 0
        while True:
            text = await self._q.get()
            epoch = self._epoch
            n += 1
            try:
                pcm = await self._render(text, self._tmp / f"s{n % 4}.wav")
            except Exception as e:
                print(f"(speech failed: {e})")
                pcm = None
            if epoch != self._epoch:  # stop() was called while rendering
                continue
            if pcm is None:
                self._finish_one(epoch)
                continue
            entry = [text, float("inf")]
            self._recent.append(entry)
            fut = self.audio.play(pcm)
            fut.add_done_callback(lambda _f, e=entry, ep=epoch: self._played(e, ep))

    def _played(self, entry: list, epoch: int) -> None:
        entry[1] = time.monotonic()  # was inf while playing
        self._finish_one(epoch)

    def _finish_one(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._idle.set()

    async def _render(self, text: str, path: Path) -> np.ndarray:
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", config.VOICE, "-r", str(config.RATE), "-o", str(path),
            f"--data-format=LEI16@{config.IO_RATE}", text,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode().strip() or "say failed")
        with wave.open(str(path)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        return np.concatenate([pcm, np.zeros(int(0.12 * config.IO_RATE), np.float32)])  # small breath
