"""The shared state behind the dashboard, and the event stream that keeps it live.

Everything the UI shows lives here: agent runs (and the subagents inside them), the chat,
the activity feed, pending approvals and research briefs. Every change is pushed to all
connected dashboards as a small JSON event; a newly opened dashboard gets a snapshot.
"""

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import config

MAX_STEPS = 400
HISTORY = 60  # finished runs kept (and saved)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def short(text: Any, n: int = 160) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class Sub:
    """A subagent spawned inside a run (Claude's Task/Agent tool)."""

    id: str
    description: str
    kind: str = "subagent"
    status: str = "working"  # working | done | failed | stopped
    last_tool: str = ""
    tool_uses: int = 0
    tokens: int = 0
    started: float = field(default_factory=time.time)
    ended: float | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Run:
    """One agent at work: the conversation, a background task, a research job, a schedule."""

    id: str
    kind: str  # conversation | task | research | schedule | briefing
    title: str
    prompt: str = ""
    status: str = "queued"  # queued | working | waiting | idle | done | failed | cancelled
    started: float = field(default_factory=time.time)
    ended: float | None = None
    steps: list[dict] = field(default_factory=list)
    subs: dict[str, Sub] = field(default_factory=dict)
    tool_calls: int = 0
    cost_usd: float = 0.0
    result: str = ""
    error: str = ""
    current: str = ""  # what it's doing right now, one line
    meta: dict = field(default_factory=dict)

    def to_dict(self, steps: bool = True) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("steps", "subs")}
        d["subs"] = [s.to_dict() for s in self.subs.values()]
        d["step_count"] = len(self.steps)
        if steps:
            d["steps"] = self.steps[-MAX_STEPS:]
        return d

    @property
    def active(self) -> bool:
        return self.status in ("queued", "working", "waiting")


class Tracker:
    """Receives what an agent is doing (from brain.py) and records it on its Run."""

    def __init__(self, hub: "Hub", run: Run):
        self.hub, self.run = hub, run

    def _step(self, kind: str, text: str, sub: str | None = None, **extra) -> None:
        step = {"t": time.time(), "kind": kind, "text": text, "sub": sub, **extra}
        self.run.steps.append(step)
        if len(self.run.steps) > MAX_STEPS:
            del self.run.steps[: len(self.run.steps) - MAX_STEPS]
        self.hub.emit("step", {"run": self.run.id, "step": step})

    def text(self, text: str, parent: str | None) -> None:
        self._step("say", text, parent)
        if parent is None:
            self.run.current = short(text, 120)
            self.hub.run_changed(self.run)

    def tool(self, name: str, summary: str, tool_id: str, parent: str | None) -> None:
        self.run.tool_calls += 1
        self._step("tool", summary, parent, tool=name, id=tool_id)
        if parent and parent in self.run.subs:
            sub = self.run.subs[parent]
            sub.last_tool, sub.tool_uses = name, sub.tool_uses + 1
        else:
            self.run.current = f"{name}: {summary}" if summary else name
        self.hub.run_changed(self.run)

    def sub_start(self, tool_id: str, description: str, kind: str) -> None:
        if tool_id not in self.run.subs:
            self.run.subs[tool_id] = Sub(tool_id, description or "subagent", kind)
            self._step("sub", f"Started {kind}: {description}", tool_id)
            self.hub.run_changed(self.run)

    def sub_progress(self, tool_id: str | None, last_tool: str | None, usage: dict | None) -> None:
        sub = self.run.subs.get(tool_id or "")
        if not sub:
            return
        if last_tool:
            sub.last_tool = last_tool
        if usage:
            sub.tool_uses = max(sub.tool_uses, int(usage.get("tool_uses", 0)))
            sub.tokens = int(usage.get("total_tokens", sub.tokens))
        self.hub.run_changed(self.run)

    def sub_end(self, tool_id: str, status: str, summary: str = "") -> None:
        sub = self.run.subs.get(tool_id)
        if not sub or sub.ended:
            return
        sub.status, sub.ended = status, time.time()
        self._step("sub", f"{sub.kind} {status}: {short(summary or sub.description, 120)}", tool_id)
        self.hub.run_changed(self.run)

    def result(self, cost: float | None, is_error: bool, detail: str = "") -> None:
        if cost:
            self.run.cost_usd += cost
            self.hub.session["cost_usd"] += cost
        self.hub.session["turns"] += 1
        for sub in self.run.subs.values():  # anything still "working" when the turn ends is over
            if not sub.ended:
                sub.status, sub.ended = "done", time.time()
        if is_error and detail:
            self._step("error", detail)
        self.hub.run_changed(self.run)
        self.hub.emit("session", self.hub.session)


@dataclass
class Approval:
    id: str
    run_id: str
    agent: str
    spoken: str
    detail: str
    created: float
    future: asyncio.Future

    def to_dict(self) -> dict:
        return {"id": self.id, "run_id": self.run_id, "agent": self.agent, "spoken": self.spoken,
                "detail": self.detail, "created": self.created}


class Hub:
    def __init__(self):
        self.clients: set = set()
        self.runs: dict[str, Run] = {}
        self.chat: list[dict] = []
        self.feed: deque[dict] = deque(maxlen=80)
        self.approvals: dict[str, Approval] = {}
        self.briefs: list[dict] = []
        self.state: dict[str, Any] = {"status": "starting", "heard": "", "heard_outcome": "", "error": ""}
        self.session: dict[str, Any] = {"cost_usd": 0.0, "turns": 0, "started": time.time()}
        self._load()

    # -- events ---------------------------------------------------------------------
    def emit(self, type_: str, data: Any) -> None:
        if not self.clients:
            return
        msg = json.dumps({"type": type_, "data": data}, default=str)
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            asyncio.ensure_future(self._send(ws, msg))

    async def _send(self, ws, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except Exception:
            self.clients.discard(ws)

    def set_state(self, **changes) -> None:
        changed = {k: v for k, v in changes.items() if self.state.get(k) != v}
        if changed:
            self.state.update(changed)
            self.emit("state", changed)

    # -- runs -----------------------------------------------------------------------
    def new_run(self, kind: str, title: str, prompt: str = "", run_id: str | None = None, **meta) -> Run:
        run = Run(run_id or new_id(kind[:4]), kind, short(title, 90), prompt, meta=meta)
        self.runs[run.id] = run
        self._trim()
        self.emit("run", run.to_dict())
        return run

    def tracker(self, run: Run) -> Tracker:
        return Tracker(self, run)

    def run_changed(self, run: Run) -> None:
        self.emit("run", run.to_dict(steps=False))

    def finish_run(self, run: Run, status: str, result: str = "", error: str = "") -> None:
        run.status, run.ended = status, time.time()
        run.result = result or run.result
        run.error = error
        run.current = ""
        self.emit("run", run.to_dict())
        self.save_runs()

    def _trim(self) -> None:
        done = [r for r in self.runs.values() if not r.active and r.kind != "conversation"]
        for r in done[: max(0, len(done) - HISTORY)]:
            self.runs.pop(r.id, None)

    # -- chat threads -------------------------------------------------------------------
    # Each thread is its own conversation with its own Claude session (resumed on switch),
    # like an inbox. Every chat message is tagged with the thread it belongs to.
    @property
    def thread(self) -> dict:
        return self.threads[self.thread_id]

    def new_thread(self, title: str = "") -> dict:
        t = {"id": new_id("thr"), "title": short(title, 60) if title else "New chat", "auto_title": not title,
             "created": time.time(), "updated": time.time(), "session_id": None}
        self.threads[t["id"]] = t
        self._save_threads()
        self.emit("threads", self.thread_list())
        return t

    def touch_thread(self, tid: str, **changes) -> None:
        t = self.threads.get(tid)
        if not t:
            return
        t.update(changes)
        t["updated"] = time.time()
        self._save_threads()
        self.emit("threads", self.thread_list())

    def delete_thread(self, tid: str) -> None:
        self.threads.pop(tid, None)
        self.chat = [m for m in self.chat if m.get("thread") != tid]
        self._save("chat.json", self.chat)
        self._save_threads()
        self.emit("threads", self.thread_list())

    def set_thread(self, tid: str) -> None:
        self.thread_id = tid
        self._save_threads()
        self.set_state(thread=tid)
        self.emit("thread_chat", {"thread": tid, "chat": self.thread_chat(tid)})

    def thread_chat(self, tid: str, n: int = 150) -> list[dict]:
        return [m for m in self.chat if m.get("thread") == tid][-n:]

    def thread_list(self) -> list[dict]:
        out = []
        for t in self.threads.values():
            msgs = self.thread_chat(t["id"], 1_000_000)
            last = next((m for m in reversed(msgs) if m["role"] in ("user", "friday")), None)
            out.append({**t, "count": len(msgs), "preview": short(last["text"], 90) if last else ""})
        return sorted(out, key=lambda t: t["updated"], reverse=True)

    def _save_threads(self) -> None:
        self._save("threads.json", {"active": self.thread_id, "threads": list(self.threads.values())})

    def _trim_chat(self) -> None:
        """Keep the newest 200 messages of each thread."""
        counts: dict[str, int] = {}
        keep = []
        for m in reversed(self.chat):
            tid = m.get("thread", "")
            counts[tid] = counts.get(tid, 0) + 1
            if counts[tid] <= 200:
                keep.append(m)
        self.chat = keep[::-1]

    # -- chat, feed -------------------------------------------------------------------
    def add_chat(self, role: str, text: str, source: str = "chat", thread: str | None = None, **extra) -> dict:
        tid = thread if thread in self.threads else self.thread_id
        msg = {"id": new_id("msg"), "t": time.time(), "role": role, "text": text, "source": source,
               "thread": tid, **extra}
        self.chat.append(msg)
        if len(self.chat) > 2000:
            self._trim_chat()
        self.emit("chat", msg)
        self._save("chat.json", self.chat)
        t = self.threads.get(tid)
        if t and role in ("user", "friday"):
            if role == "user" and t.get("auto_title") and t["title"] == "New chat":
                t["title"] = short(text, 48)
            self.touch_thread(tid)
        return msg

    def add_feed(self, level: str, title: str, detail: str = "", run_id: str | None = None) -> None:
        item = {"id": new_id("feed"), "t": time.time(), "level": level, "title": short(title, 120),
                "detail": short(detail, 200), "run_id": run_id}
        self.feed.appendleft(item)
        self.emit("feed", item)

    # -- approvals --------------------------------------------------------------------
    def new_approval(self, run: Run, spoken: str, detail: str) -> Approval:
        fut = asyncio.get_running_loop().create_future()
        a = Approval(new_id("ok"), run.id, run.title, spoken, detail, time.time(), fut)
        self.approvals[a.id] = a
        self.emit("approval", {"action": "new", **a.to_dict()})
        return a

    def resolve_approval(self, approval_id: str, approved: bool, how: str) -> bool:
        a = self.approvals.pop(approval_id, None)
        if not a:
            return False
        if not a.future.done():
            a.future.set_result((approved, how))
        self.emit("approval", {"action": "resolved", "id": approval_id, "approved": approved})
        return True

    # -- research briefs --------------------------------------------------------------
    def add_brief(self, brief: dict) -> None:
        self.briefs.insert(0, brief)
        del self.briefs[30:]
        self._save("briefs.json", self.briefs)
        self.emit("brief", brief)

    # -- snapshot & persistence ------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "session": self.session,
            "runs": [r.to_dict(steps=r.active or r.kind == "conversation") for r in self.runs.values()],
            "chat": self.thread_chat(self.thread_id),
            "threads": self.thread_list(),
            "feed": list(self.feed),
            "approvals": [a.to_dict() for a in self.approvals.values()],
            "briefs": self.briefs,
        }

    def save_runs(self) -> None:
        done = [r.to_dict() for r in self.runs.values() if not r.active and r.kind != "conversation"]
        for d in done:
            d["steps"] = d["steps"][-120:]
        self._save("runs.json", done[-HISTORY:])

    def _save(self, name: str, data: Any) -> None:
        try:
            config.DATA.mkdir(parents=True, exist_ok=True)
            path = config.DATA / name
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, default=str))
            tmp.replace(path)
        except OSError as e:
            print(f"(couldn't save {name}: {e})")

    def _read(self, name: str, default: Any) -> Any:
        try:
            return json.loads((config.DATA / name).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _load(self) -> None:
        self.chat = self._read("chat.json", [])
        saved = self._read("threads.json", {})
        self.threads: dict[str, dict] = {t["id"]: t for t in saved.get("threads", []) if t.get("id")}
        if not self.threads:  # first run with threads: the existing history becomes the first chat
            first = {"id": new_id("thr"), "title": "Main chat", "auto_title": False,
                     "created": self.chat[0]["t"] if self.chat else time.time(),
                     "updated": self.chat[-1]["t"] if self.chat else time.time(), "session_id": None}
            self.threads[first["id"]] = first
        self.thread_id = saved.get("active") if saved.get("active") in self.threads else \
            max(self.threads.values(), key=lambda t: t["updated"])["id"]
        for m in self.chat:
            if m.get("thread") not in self.threads:
                m["thread"] = self.thread_id
        self.state["thread"] = self.thread_id
        self._save_threads()
        self.briefs = self._read("briefs.json", [])
        for d in self._read("runs.json", []):
            try:
                subs = {s["id"]: Sub(**s) for s in d.pop("subs", [])}
                d.pop("step_count", None)
                run = Run(**d)
                run.subs = subs
                if run.active:  # it was cut off when the app last quit
                    run.status, run.error = "cancelled", "Friday was closed while this was running."
                self.runs[run.id] = run
            except TypeError:
                continue
