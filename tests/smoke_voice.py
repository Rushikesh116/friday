"""End-to-end hearing test without a microphone or speaker.

Synthesizes Friday talking (as if from the laptop speakers, with room echo) and a person
talking over her, runs it through the real echo canceller, segmenter, whisper and router,
and checks what she does. "echo" is how loud her voice is at the mic relative to yours
(1.0 = as loud as you, roughly laptop speakers at high volume).

  - "Friday, open Safari…" while she's idle          -> command "open Safari…"
  - "Friday, stop" over her voice, even when loud    -> stop (task cancelled)
  - "Friday, stop talking" over her voice            -> quiet (task keeps going)
  - "Friday, what time is it" over her voice         -> she stops and listens for the request
  - "what's the weather" over her voice (no name)    -> she stops and listens (talking over her counts)
  - bare "Stop." over her voice at low volume        -> stop
  - nobody interrupts her                            -> nothing (her own voice is ignored)
  - "see you on Friday" in passing                   -> nothing
"""

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))

import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import lfilter

from friday import config
from friday.audio import BLOCK, EchoCanceller
from friday.ears import Ears, Transcriber

SR = config.IO_RATE
FRIDAY_SAYS = ("Here is your inbox. You have four unread messages. The first is from your advisor "
               "about the project meeting on Thursday, and the second is a newsletter about laptops.")


def synth(text: str, voice: str, rate: int = 175) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "a.wav")
        subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(path), f"--data-format=LEI16@{SR}", text], check=True)
        with wave.open(str(path)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)


class FakeMouth:
    """Stands in for Mouth: says whether she's talking and what she just said."""

    def __init__(self, speaking: bool):
        self.speaking = speaking
        self.audio = type("A", (), {"ducked": False})()

    def recent_text(self) -> str:
        return FRIDAY_SAYS if self.speaking else ""


async def run_case(tr: Transcriber, user_text: str | None, friday_talking: bool, echo_gain: float = 0.35):
    rng = np.random.default_rng(0)
    far = synth(FRIDAY_SAYS, config.VOICE) if friday_talking else np.zeros(int(9 * SR), np.float32)
    far = np.concatenate([far, np.zeros(int(1.5 * SR), np.float32)])  # a little silence at the end
    near = rng.normal(0, 50, len(far))  # room noise + the person talking
    if user_text:
        user = synth(user_text, "Daniel", 165)
        at = int(4.0 * SR)
        near[at: at + len(user)] += 0.6 * user[: len(near) - at]
    # Room echo of whatever the speaker actually plays, computed block by block so that
    # when she lowers her voice (ducking) the echo drops too, just like in a real room.
    ir = np.zeros(int(0.1 * SR)); ir[int(0.025 * SR)] = 1.0; ir[int(0.04 * SR)] = 0.4; ir[int(0.07 * SR)] = 0.15
    zi = np.zeros(len(ir) - 1)

    events: list[tuple[str, object]] = []
    mouth = FakeMouth(friday_talking)
    ears = Ears(tr, mouth, is_busy=lambda: friday_talking,
                on_command=lambda t: events.append(("command", t)),
                on_stop=lambda cancel: events.append(("stop" if cancel else "quiet", None)),
                log=lambda s: None)
    ears.start()
    aec = EchoCanceller(enabled=True, delay_ms=25)
    acc: list[np.ndarray] = []
    for i in range(0, len(far) - BLOCK, BLOCK):
        played = far[i:i + BLOCK] * (config.DUCK_VOLUME if mouth.audio.ducked else 1.0)
        echo, zi = lfilter(ir, 1.0, played, zi=zi)
        m = np.clip(echo * echo_gain + near[i:i + BLOCK], -32768, 32767).astype(np.int16)
        f = np.clip(played, -32768, 32767).astype(np.int16)
        acc.append(aec.process(m, f))
        if len(acc) == 8:
            ears.feed(np.concatenate(acc)); acc = []
            await asyncio.sleep(0)
    for _ in range(100):  # let segmentation and transcription finish
        await asyncio.sleep(0.1)
        if ears._frames.empty() and ears._segments.empty() and not ears._transcribing and not ears._in_speech:
            break
    await ears.close()
    return events


async def main() -> int:
    tr = Transcriber()
    await tr.start()
    stopped = lambda ev: ev in ([("stop", None)],)  # noqa: E731
    # At full echo "Friday, stop" can come through as "Friday, st…" → she stops and listens,
    # which also silences her; either outcome is a stop. Nothing else may fire.
    stopped_loud = lambda ev: ev in ([("stop", None)], [("command", "")])  # noqa: E731
    cases = [  # (name, what the person says, is Friday talking, echo level, check)
        ("name + command, idle", "Friday, open Safari and look up the weather in Boston.", False, 0,
         lambda ev: ev and ev[0][0] == "command" and "safari" in ev[0][1].lower() and "boston" in ev[0][1].lower()),
        ("'Friday, stop' over her, echo 0.35", "Friday, stop.", True, 0.35, stopped),
        ("'Friday, stop' over her, echo 0.6", "Friday, stop.", True, 0.6, stopped),
        ("'Friday, stop' over her, echo 1.0", "Friday, stop.", True, 1.0, stopped_loud),
        ("'Friday, stop talking' over her, echo 0.6", "Friday, stop talking.", True, 0.6,
         lambda ev: ev == [("quiet", None)]),
        ("'Friday, what time is it' over her, 0.6", "Friday, what time is it?", True, 0.6,
         lambda ev: ev == [("command", "")]),
        ("'what's the weather' over her, echo 0.35", "What's the weather tomorrow?", True, 0.35,
         lambda ev: ev == [("command", "")]),
        ("bare 'Stop.' over her, echo 0.15", "Stop.", True, 0.15, stopped),
        ("she talks, nobody interrupts, echo 1.0", None, True, 1.0, lambda ev: ev == []),
        ("'see you on Friday at noon', idle", "I'll see you on Friday at noon.", False, 0, lambda ev: ev == []),
    ]
    failed = 0
    try:
        for name, said, talking, echo, ok in cases:
            ev = await run_case(tr, said, talking, echo_gain=echo)
            passed = bool(ok(ev))
            failed += not passed
            print(f"{'PASS' if passed else 'FAIL'}  {name:42s} -> {ev}")
    finally:
        await tr.stop()
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
