"""Hearing: split the (echo-cancelled) mic into utterances, transcribe them, and route them.

Everything you say is transcribed locally by whisper.cpp; nothing leaves the Mac unless
it's addressed to her. Each utterance is then:
  - handed to whoever is waiting for an answer (a command, a follow-up, a yes/no), or
  - a command if it starts or ends with her name ("Friday, open Safari"), or
  - "stop" / "quiet" while she's talking or working, or
  - ignored (other people, the TV, or the remains of her own voice).
"""

import asyncio
import atexit
import contextlib
import io
import json
import os
import re
import shutil
import signal
import time
import urllib.request
import uuid
import wave
from collections import deque
from collections.abc import Callable

import numpy as np

from . import config
from .mouth import Mouth

NAME = re.escape(config.NAME.lower())
CALL_START = re.compile(rf"^\W*(?:(?:hey|hi|hello|ok|okay|yo|so)\W+)?{NAME}\b\W*(.*)$", re.I | re.S)
CALL_END = re.compile(rf"^(.+?)\W+{NAME}\W*$", re.I | re.S)
# "Quiet" stops her talking but lets the task finish; "stop" ends the task too.
HUSH = re.compile(r"\b(quiet|shh+|hush|shut up|stop talking|stop speaking|silence|mute|that'?s enough talking)\b", re.I)
STOP = re.compile(r"\b(stop\w*|cancel|enough|never ?mind|forget it|abort|pause|hold on|wait)\b", re.I)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


class Ears:
    def __init__(
        self,
        transcriber: "Transcriber",
        mouth: Mouth,
        is_busy: Callable[[], bool],
        on_command: Callable[[str], None],
        on_stop: Callable[[bool], None],
        log: Callable[[str], None] = print,
    ):
        self.transcriber, self.mouth, self.is_busy = transcriber, mouth, is_busy
        self.on_command, self.on_stop, self.log = on_command, on_stop, log
        # "always": her name wakes her. "ptt" (push to talk): she only takes requests after
        # you press Talk; "stop" still works while she's busy.
        self.mode = "always"
        # Told about every transcribed utterance: (text, outcome) where outcome is one of
        # command / stop / quiet / answer / echo / ignored. Used by the dashboard.
        self.on_heard: Callable[[str, str], None] | None = None
        self.level = 0.0  # loudness of the latest mic frame (int16 RMS), for the level meter
        self._frames: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._segments: asyncio.Queue[tuple[np.ndarray, bool]] = asyncio.Queue()  # (audio, early)
        self._listener: asyncio.Queue[str] | None = None
        self._in_speech = False
        self._transcribing = 0

    # -- plumbing -------------------------------------------------------------------
    def feed(self, frame: np.ndarray) -> None:
        """Called by AudioIO with each 80 ms frame of cleaned 16 kHz mic audio."""
        self._frames.put_nowait(frame)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._segment_loop()), asyncio.create_task(self._transcribe_loop())]

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _segment_loop(self) -> None:
        frame_s = config.FRAME / config.SAMPLE_RATE
        settle_frames = int(1.5 / frame_s)  # echo canceller is still adapting this long into her speech
        noise, loud_run, quiet = 150.0, 0, 0.0
        preroll: deque[np.ndarray] = deque(maxlen=4)  # ~0.3 s before speech is detected
        seg: list[np.ndarray] | None = None
        early = False
        speak_frames = 0  # frames since she started talking (counted in audio time, not wall time)
        while True:
            frame = await self._frames.get()
            speak_frames = speak_frames + 1 if self.mouth.speaking else 0
            r = rms(frame)
            self.level = r
            threshold = max(noise * 3.5, config.MIN_SPEECH_RMS)
            if seg is None:
                preroll.append(frame)
                if r < noise * 2:  # only quiet frames move the noise floor
                    noise = 0.95 * noise + 0.05 * max(r, 20.0)
                loud_run = loud_run + 1 if r > threshold else 0
                if loud_run >= 2:
                    seg, quiet = list(preroll), 0.0
                    early = self.mouth.speaking and speak_frames < settle_frames
                    self._in_speech = True
                    if self.mouth.speaking and self._listener is None:
                        self.mouth.audio.ducked = True  # you're talking over her: lower her voice
                continue
            seg.append(frame)
            quiet = 0.0 if r > threshold else quiet + frame_s
            if quiet >= config.SILENCE_END_S or len(seg) * frame_s >= config.MAX_UTTERANCE_S:
                self._segments.put_nowait((np.concatenate(seg), early))
                seg, loud_run = None, 0
                preroll.clear()
                self._in_speech = False

    async def _transcribe_loop(self) -> None:
        while True:
            audio, early = await self._segments.get()
            self._transcribing += 1
            try:
                text = await self.transcriber.transcribe(audio)
            except Exception as e:
                self.log(f"(speech recognition failed: {e})")
                text = ""
            finally:
                self._transcribing -= 1
            self._route(text, early)

    # -- routing --------------------------------------------------------------------
    def _route(self, text: str, early: bool = False) -> None:
        """early: the utterance began in the first moments of her talking, when leftover echo
        of her own voice is most likely, so only her name or "stop" count then."""
        outcome = "ignored"
        try:
            if not text:
                return
            if self._listener is not None:
                self._listener.put_nowait(text)
                outcome = "answer"
                return
            recent = self.mouth.recent_text()
            if recent and is_echo(text, recent):
                outcome = "echo"
                return
            call = match_call(text)
            rest = call if call is not None else text
            busy = self.mouth.speaking or self.is_busy()
            if busy and len(words(rest)) <= 6:
                said = set(words(recent))
                hush = HUSH.search(rest)
                stop = STOP.search(rest)
                if hush and not set(words(hush.group(0))) <= said:
                    self.log(f"\n(heard: {text})")
                    outcome = "quiet"
                    self.on_stop(False)
                    return
                if stop and stop.group(0).lower() not in said:
                    self.log(f"\n(heard: {text})")
                    outcome = "stop"
                    self.on_stop(True)
                    return
            if self.mode == "ptt":
                return  # push to talk: requests only count after pressing Talk
            # Talking over her without her name: needs a real sentence (3+ words) and not right as
            # she starts speaking, so fragments of her own echo can't cut her off.
            talk_over = config.BARGE_IN and len(words(text)) >= 3 and not early
            if self.mouth.speaking and (call is not None or talk_over):
                # You talked over her. Words spoken over her voice come through clipped (often
                # the first one, her name), so rather than act on a garbled request she stops,
                # chimes, and listens for it again in a quiet room.
                self.log(f"\n(heard over her voice: {text})")
                outcome = "command"
                self.on_command("")
            elif call is not None:
                outcome = "command"
                self.on_command(call)
        finally:
            if outcome not in ("stop", "quiet", "command"):
                self.mouth.audio.ducked = False  # it wasn't for her: back to normal volume
            if text and self.on_heard:
                self.on_heard(text, outcome)

    async def listen(self, wait_for_speech: float = config.WAIT_FOR_SPEECH_S) -> str | None:
        """Wait for your next utterance, whatever it is (no name needed). None if you stay silent."""
        q: asyncio.Queue[str] = asyncio.Queue()
        self._listener = q
        deadline = time.monotonic() + wait_for_speech
        try:
            while True:
                try:
                    return await asyncio.wait_for(q.get(), timeout=0.2)
                except TimeoutError:
                    busy = self._in_speech or self._transcribing or not self._segments.empty()
                    if not busy and time.monotonic() > deadline:
                        return None
        finally:
            self._listener = None


def match_call(text: str) -> str | None:
    """If the utterance is addressed to her, return the request without her name."""
    if m := CALL_START.match(text):
        return m.group(1).strip()
    if m := CALL_END.match(text):
        return m.group(1).strip()
    return None


def is_echo(text: str, recent: str) -> bool:
    """True if most of what was heard is what she just said (echo that leaked through)."""
    heard, said = words(text), set(words(recent))
    if len(heard) < 2:
        return bool(heard) and heard[0] in said and not STOP.search(text) and not HUSH.search(text)
    return sum(w in said for w in heard) / len(heard) >= 0.6


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class Transcriber:
    """Keeps whisper.cpp's server running so the model stays loaded between utterances."""

    def __init__(self):
        self.url = f"http://127.0.0.1:{config.WHISPER_PORT}/inference"
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        exe = shutil.which("whisper-server")
        if not exe:
            raise RuntimeError("whisper-server not found. Run: brew install whisper-cpp")
        if not config.WHISPER_MODEL.exists():
            raise RuntimeError(f"Whisper model missing: {config.WHISPER_MODEL}")
        await asyncio.to_thread(_stop_leftover_servers)
        self._proc = await asyncio.create_subprocess_exec(
            exe, "-m", str(config.WHISPER_MODEL), "--host", "127.0.0.1",
            "--port", str(config.WHISPER_PORT), "-l", "en", "-nt",
            # Nudges recognition toward her name instead of "Fri day" / "Frieda".
            "--prompt", f"{config.NAME}, a voice assistant.",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        pid = self._proc.pid
        atexit.register(_terminate_pid, pid)  # never leave the speech server behind
        for _ in range(100):  # up to ~20 s for the model to load
            await asyncio.sleep(0.2)
            if self._proc.returncode is not None:
                raise RuntimeError(f"the speech server couldn't start; is another program using port "
                                   f"{config.WHISPER_PORT}? (Set FRIDAY_WHISPER_PORT to use a different one.)")
            try:
                await asyncio.to_thread(urllib.request.urlopen, f"http://127.0.0.1:{config.WHISPER_PORT}/", timeout=1)
            except Exception:
                continue
            await asyncio.sleep(0.3)  # make sure it's *our* server answering, not something else on the port
            if self._proc.returncode is None:
                return
        raise RuntimeError("whisper-server did not start in time")

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()

    async def transcribe(self, audio: np.ndarray) -> str:
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 0:  # speech softened by echo cancellation gets brought back up to a normal level
            audio = audio.astype(np.float32) * min(16000.0 / peak, 30.0)
        return clean(await asyncio.to_thread(self._post, to_wav(audio)))

    def _post(self, wav: bytes) -> str:
        boundary = uuid.uuid4().hex
        parts = [
            _field(boundary, "response_format", "json"),
            _field(boundary, "temperature", "0.0"),
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        req = urllib.request.Request(
            self.url, data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()).get("text", "")


def _terminate_pid(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)


def _stop_leftover_servers() -> None:
    """If an earlier Friday crashed, its speech server may still hold our port: stop it.
    Only touches orphaned whisper-server processes (their Friday is gone) started with
    Friday's own model and port, so a Friday that's still running is never disturbed."""
    import psutil

    for p in psutil.process_iter(["name", "cmdline", "ppid"]):
        try:
            cmd = p.info["cmdline"] or []
            if (p.info["name"] == "whisper-server" and p.info["ppid"] == 1
                    and any(c.endswith(config.WHISPER_MODEL.name) for c in cmd)
                    and str(config.WHISPER_PORT) in cmd):
                p.terminate()
                p.wait(3)
        except (psutil.Error, OSError):
            continue


def _field(boundary: str, name: str, value: str) -> bytes:
    return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()


def to_wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


# Whisper invents these on silence or noise.
_NOISE = {"", "you", "thank you.", "thanks for watching!", "bye.", "."}


def clean(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text).strip()  # [BLANK_AUDIO], (wind blowing)
    return "" if text.lower() in _NOISE else text
