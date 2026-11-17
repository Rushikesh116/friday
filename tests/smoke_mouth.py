"""Checks her speech output path silently (volume 0): render, queue, and instant cut-off."""

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))

import asyncio
import sys
import time

from friday import config
from friday.audio import AudioIO
from friday.mouth import Mouth


async def main() -> int:
    audio = AudioIO(on_frame=None)
    audio.start()
    audio.volume = 0.0 if "--loud" not in sys.argv else config.VOLUME
    mouth = Mouth(audio)
    mouth.start()
    print(f"voice: {config.VOICE}")

    t = time.monotonic()
    await mouth.say_and_wait("Hi, I'm Friday. This is a short test of my voice.")
    full = time.monotonic() - t
    print(f"two sentences played in {full:.1f}s")

    mouth.say("This is a much longer answer that keeps going. " * 6)
    await asyncio.sleep(1.5)
    assert mouth.speaking, "should still be talking"
    t = time.monotonic()
    mouth.stop()
    await asyncio.wait_for(mouth.wait_idle(), 1)
    cut = (time.monotonic() - t) * 1000
    await asyncio.sleep(0.3)
    print(f"cut off in {cut:.0f} ms, still playing afterwards: {audio.playing}")
    audio.stop()
    ok = 1.5 < full < 10 and cut < 50 and not audio.playing
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
