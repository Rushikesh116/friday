"""One live request through the agent, with confirmations auto-declined."""
import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
import asyncio, sys
from friday.brain import Brain
from friday.gate import Gate

async def deny(spoken, detail):
    print("CONFIRM ASKED:", spoken); return False, "no"

async def main(prompt):
    b = Brain(Gate(deny))
    await b.start()
    try:
        async for ev in b.ask(prompt):
            print(f"[{ev.kind}] {ev.text}")
    finally:
        await b.stop()

asyncio.run(main(sys.argv[1]))
