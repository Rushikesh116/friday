"""Codex CLI adapter for Friday's event and session interface."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Callable
from typing import Any

from . import config
from .brain import Event, VOICE_PERSONA, MEMORY_HEADER


class CodexBrain:
    def __init__(self, persona: str, tracker: Any = None,
                 memory: Callable[[], list[str]] | None = None):
        self.persona, self.tracker, self.memory = persona, tracker, memory
        self.model, self.effort = "gpt-5.6-sol", "medium"
        self.session_id: str | None = None
        self.on_session: Callable[[str], None] | None = None
        self.busy = False
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if not shutil.which("codex"):
            raise RuntimeError("Codex CLI is not installed. Install it and run codex login first.")

    async def stop(self) -> None:
        await self.interrupt()

    async def reset(self) -> None:
        await self.switch(None)

    async def switch(self, session_id: str | None) -> None:
        await self.interrupt()
        self.session_id = session_id

    async def configure(self, model: str, effort: str) -> None:
        self.model, self.effort = model, effort

    async def interrupt(self) -> None:
        proc = self.proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        self.proc = None
        self.busy = False

    async def ask(self, text: str) -> AsyncIterator[Event]:
        if self.busy:
            await self.interrupt()
        resumed = bool(self.session_id)
        base = self.persona.split("## The dashboard and your helpers")[0] if self.persona == VOICE_PERSONA else self.persona
        base = base.replace("confirmations for risky actions are requested automatically.",
                            "Do not take irreversible actions without asking the user first.")
        notes = self.memory() if self.memory else []
        prompt = text if resumed else (
            base.replace("{name}", config.NAME)
            + (MEMORY_HEADER + "\n".join(f"- {m}" for m in notes) if notes else "")
            + "\n## Tools and safety\nUse your built-in shell and web tools. "
              "Work inside the configured Codex workspace. Do not send mail, change calendars, "
              "create schedules, or claim to use Friday dashboard tools: those requests are "
              "routed to Claude. Ask before destructive or irreversible actions.\n\n"
            + "## User request\n" + text)
        args = ["codex", "exec"]
        if resumed:
            args += ["resume", self.session_id]
        args += ["--json", "--skip-git-repo-check", "--model", self.model,
                 "--config", f"model_reasoning_effort={self.effort}"]
        if not resumed:
            args += ["--sandbox", "workspace-write"]
        args += ["-"]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(config.CODEX_WORKDIR if config.CODEX_WORKDIR.is_dir() else config.ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.proc, self.busy = proc, True
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(proc.stderr.read())
        last_message = ""
        failed = ""
        completed = False
        try:
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = event.get("type")
                if typ == "thread.started" and event.get("thread_id"):
                    self.session_id = event["thread_id"]
                    if self.on_session:
                        self.on_session(self.session_id)
                elif typ in ("item.started", "item.completed"):
                    item = event.get("item") or {}
                    item_type = item.get("type")
                    if item_type == "agent_message" and typ == "item.completed":
                        last_message = item.get("text", "")
                        if last_message:
                            if self.tracker:
                                self.tracker.text(last_message, None)
                            yield Event("say", last_message)
                    elif item_type in ("command_execution", "mcp_tool_call", "web_search") and typ == "item.started":
                        summary = item.get("command") or item.get("name") or item.get("query") or item_type
                        if self.tracker:
                            self.tracker.tool(item_type, str(summary)[:200], item.get("id", ""), None)
                        yield Event("tool", str(summary))
                elif typ in ("turn.failed", "error"):
                    detail = event.get("error") or event.get("message") or "Codex failed"
                    failed = str(detail.get("message", detail) if isinstance(detail, dict) else detail)
                elif typ == "turn.completed":
                    completed = True
            await proc.wait()
            if proc.returncode != 0:
                stderr = (await stderr_task).decode(errors="replace").strip()
                failed = failed or stderr[-1000:] or f"Codex exited with status {proc.returncode}"
            if not failed and (not completed or not last_message):
                failed = "Codex finished without a complete reply."
            if failed:
                if self.tracker:
                    self.tracker.result(None, True, failed)
                yield Event("error", failed)
            else:
                if self.tracker:
                    self.tracker.result(None, False)
                yield Event("done", "")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            self.proc, self.busy = None, False
