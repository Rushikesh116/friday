"""Background agents: tasks from the dashboard, scheduled jobs, research and briefings.

Each one is its own agent (its own session), so several can work at once while the
conversation stays free. Up to config.MAX_PARALLEL run together; the rest wait in a queue.
"""

import asyncio
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import config, research
from .brain import TASK_PERSONA, Brain
from .codex_brain import CodexBrain
from .gate import Gate
from .hub import Run
from .routing import choose, requires_claude

if TYPE_CHECKING:
    from .core import Friday

BRIEFING_PROMPT = """Give me my briefing for today. Cover, briefly:
1. Today's calendar events (calendar_events with day_offset 0).
2. My unread email: who it's from and what needs my attention (mail_list, unread_only true).
3. Anything else worth knowing first thing.
If a source isn't available (for example Mail or Calendar access hasn't been granted), say so
in one line and carry on with the rest."""


def first_sentence(text: str, limit: int = 300) -> str:
    plain = re.sub(r"[#*_`>\[\]]", "", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", plain, re.S)
    s = (m.group(1) if m else plain.split("\n")[0]).strip()
    return s[:limit]


class TaskManager:
    def __init__(self, friday: "Friday"):
        self.f = friday
        self.hub = friday.hub
        self._tasks: dict[str, asyncio.Task] = {}
        self._brains: dict[str, Brain | CodexBrain] = {}
        self._running = 0
        self._cond: asyncio.Condition | None = None

    @property
    def active(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    def start(self, prompt: str, kind: str = "task", title: str | None = None, speak: bool = False,
              on_done: Callable[[Run], None] | None = None, persona: str = TASK_PERSONA,
              servers: dict[str, Any] | None = None, allowed: list[str] | None = None,
              run: Run | None = None, browser: bool = True) -> Run:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("the task is empty")
        run = run or self.hub.new_run(kind, title or prompt, prompt)
        self._tasks[run.id] = asyncio.create_task(
            self._run(run, persona, servers or {}, allowed or [], speak, on_done, browser))
        return run

    def start_research(self, topic: str, speak: bool = True) -> Run:
        topic = " ".join(topic.split())[:300]
        if not topic:
            raise ValueError("what should I research?")
        run = self.hub.new_run("research", f"Research: {topic}", topic)

        def on_brief(brief: dict) -> None:
            self.hub.add_brief(brief)
            self.hub.add_feed("ok", f"Research ready: {brief['title']}", brief["summary"], run.id)
            self.f.on_brief(brief, speak)

        server, published = research.make_server(topic, run.id, on_brief)
        run.meta["published"] = published  # checked when the run ends
        prompt = (f"Research this for the user and publish an illustrated brief: {topic}\n"
                  f"(Today is {time.strftime('%A %d %B %Y')}.)")
        return self.start(prompt, kind="research", persona=research.RESEARCH_PERSONA,
                          servers={"research": server}, allowed=research.TOOL_NAMES, run=run,
                          browser=False)

    def briefing(self) -> Run:
        return self.start(BRIEFING_PROMPT, kind="briefing", title="Daily briefing", speak=True)

    def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def cancel_all(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- running ----------------------------------------------------------------------
    async def _slot(self) -> None:
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            await self._cond.wait_for(lambda: self._running < config.MAX_PARALLEL)
            self._running += 1

    async def _release(self) -> None:
        async with self._cond:
            self._running -= 1
            self._cond.notify_all()

    async def _run(self, run: Run, persona: str, servers: dict[str, Any], allowed: list[str],
                   speak: bool, on_done: Callable[[Run], None] | None, browser: bool = True) -> None:
        got_slot = False
        status, result, error = "failed", "", ""
        brain: Brain | CodexBrain | None = None
        try:
            await self._slot()
            got_slot = True
            run.status, run.started = "working", time.time()
            run.current = "Starting…"
            self.hub.run_changed(run)
            self.hub.add_feed("info", f"Started: {run.title}", run.prompt, run.id)
            route = choose(run.prompt, self.f.settings.values, run.kind)
            # Research and the morning briefing depend on Claude-only in-process tools.
            if run.kind in ("research", "briefing") or requires_claude(run.prompt):
                route = choose(run.prompt, {**self.f.settings.values, "provider": "claude"}, run.kind)
            run.meta.update(provider=route.provider, model=route.model, effort=route.effort,
                            route=route.level)
            self.hub.run_changed(run)
            if route.provider == "codex":
                brain = CodexBrain(persona=persona, tracker=self.hub.tracker(run), memory=self.f.memory.texts)
            else:
                brain = Brain(Gate(lambda s, d: self.f.confirm(s, d, run), log=lambda m: None),
                              persona=persona, servers=servers, allowed=allowed,
                              tracker=self.hub.tracker(run), memory=self.f.memory.texts, browser=browser,
                              budget_usd=config.TASK_BUDGET_USD or None)
            self._brains[run.id] = brain
            await brain.configure(route.model, route.effort)
            await brain.start()
            since_tool: list[str] = []
            texts: list[str] = []
            async for ev in brain.ask(run.prompt):
                if ev.kind == "say":
                    texts.append(ev.text)
                    since_tool.append(ev.text)
                elif ev.kind == "tool":
                    since_tool = []
                elif ev.kind == "error":
                    error = ev.text
            result = "\n\n".join(since_tool) if since_tool else (texts[-1] if texts else "")
            status = "failed" if error else "done"
        except asyncio.CancelledError:
            status, error = "cancelled", "Stopped by you."
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
        finally:
            if brain:
                await brain.stop()
            self._brains.pop(run.id, None)
            for a in [a for a in self.hub.approvals.values() if a.run_id == run.id]:
                self.hub.resolve_approval(a.id, False, "task ended")
            if got_slot:
                await self._release()
            self._finish(run, status, result, error, speak, on_done)

    def _finish(self, run: Run, status: str, result: str, error: str, speak: bool,
                on_done: Callable[[Run], None] | None) -> None:
        if run.kind == "research" and status == "done" and not run.meta.get("published"):
            status, error = "failed", "Finished without publishing a brief."
        run.meta.pop("published", None)  # not JSON
        self.hub.finish_run(run, status, result, error)
        level = {"done": "ok", "failed": "error", "cancelled": "warn"}.get(status, "info")
        verb = {"done": "Done", "failed": "Failed", "cancelled": "Cancelled"}.get(status, status)
        self.hub.add_feed(level, f"{verb}: {run.title}", first_sentence(result) if result else error, run.id)
        if status in ("done", "failed") and run.kind != "research":
            self.f.notify(f"{verb}: {run.title}", first_sentence(result) if result else error)
        if speak and status == "done" and result:
            self.f.announce(first_sentence(result))
        if on_done:
            try:
                on_done(run)
            except Exception as e:
                print(f"(on_done failed: {e})")
