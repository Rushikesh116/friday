"""Scheduled jobs: "every weekday at 8:00 summarize my email", "every 2 hours check …".

Each job runs as a background agent at its time, like a task you started yourself, and
shows up in the dashboard's agent list. Saved in data/schedules.json. Jobs missed while
Friday was closed are skipped rather than all fired at once on the next start.
"""

import asyncio
import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from . import config
from .hub import Hub, new_id

KINDS = ("daily", "weekly", "interval", "once")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_time(value: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(value).strip().split(":"))
    except ValueError:
        raise ValueError("time must look like 08:30 (24-hour clock)")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("time must be between 00:00 and 23:59")
    return h, m


def _fmt_time(h: int, m: int) -> str:
    return datetime(2000, 1, 1, h, m).strftime("%-I:%M %p")


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    """Check and normalise a schedule coming from the dashboard or from Friday herself."""
    name = str(raw.get("name", "")).strip()[:80]
    prompt = str(raw.get("prompt", "")).strip()[:4000]
    kind = raw.get("kind", "daily")
    if not name:
        raise ValueError("give the schedule a name")
    if not prompt:
        raise ValueError("say what Friday should do")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    s: dict[str, Any] = {"name": name, "prompt": prompt, "kind": kind,
                         "enabled": bool(raw.get("enabled", True)), "speak": bool(raw.get("speak", False))}
    if kind in ("daily", "weekly"):
        h, m = _parse_time(raw.get("time", ""))
        s["time"] = f"{h:02d}:{m:02d}"
    if kind == "weekly":
        days = sorted({int(d) for d in raw.get("days", []) if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
        if not days:
            raise ValueError("pick at least one day")
        s["days"] = days
    if kind == "interval":
        try:
            every = int(raw.get("every_minutes", 0))
        except (TypeError, ValueError):
            raise ValueError("every_minutes must be a number")
        if not 5 <= every <= 7 * 24 * 60:
            raise ValueError("interval must be between 5 minutes and 7 days")
        s["every_minutes"] = every
    if kind == "once":
        try:
            at = datetime.fromisoformat(str(raw.get("at", "")).strip())
        except ValueError:
            raise ValueError("date and time must look like 2026-11-20T17:00")
        s["at"] = at.strftime("%Y-%m-%dT%H:%M")
    return s


def next_run(s: dict[str, Any], after: datetime, last_run: float | None = None) -> datetime | None:
    kind = s["kind"]
    if kind in ("daily", "weekly"):
        h, m = _parse_time(s["time"])
        for offset in range(0, 8):
            day = (after + timedelta(days=offset)).replace(hour=h, minute=m, second=0, microsecond=0)
            if day <= after:
                continue
            if kind == "daily" or day.weekday() in s["days"]:
                return day
        return None
    if kind == "interval":
        every = timedelta(minutes=s["every_minutes"])
        base = datetime.fromtimestamp(last_run) if last_run else after
        nxt = base + every
        if nxt <= after:
            steps = math.ceil((after - base) / every)
            nxt = base + every * max(steps, 1)
            if nxt <= after:
                nxt += every
        return nxt
    if kind == "once":
        at = datetime.fromisoformat(s["at"])
        return at if at > after and not last_run else None
    return None


def describe_when(s: dict[str, Any]) -> str:
    """Plain-English timing, e.g. 'every weekday at 8:00 AM'. Tolerates incomplete input."""
    kind = s.get("kind", "daily")
    try:
        if kind == "daily":
            return f"every day at {_fmt_time(*_parse_time(s.get('time', '')))}"
        if kind == "weekly":
            days = sorted({int(d) for d in s.get("days", [])})
            t = _fmt_time(*_parse_time(s.get("time", "")))
            if days == [0, 1, 2, 3, 4]:
                return f"every weekday at {t}"
            if days == [5, 6]:
                return f"every weekend at {t}"
            names = [DAY_NAMES[d] for d in days]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            return f"every {joined} at {t}"
        if kind == "interval":
            mins = int(s.get("every_minutes", 0))
            if mins % 1440 == 0:
                n = mins // 1440
                return "every day" if n == 1 else f"every {n} days"
            if mins % 60 == 0:
                n = mins // 60
                return "every hour" if n == 1 else f"every {n} hours"
            return f"every {mins} minutes"
        if kind == "once":
            at = datetime.fromisoformat(str(s.get("at", "")))
            return "once, on " + at.strftime("%a %-d %b at %-I:%M %p")
    except (ValueError, TypeError, IndexError):
        pass
    return "at a time that still needs to be set"


class Scheduler:
    def __init__(self, hub: Hub, start_task: Callable[..., Any], enabled: Callable[[], bool]):
        self.hub = hub
        self.start_task = start_task  # (prompt, kind, title, speak, on_done) -> Run
        self.enabled = enabled
        self.path = config.DATA / "schedules.json"
        self.items: list[dict[str, Any]] = self._load()
        now = datetime.now()
        for s in self.items:  # skip anything missed while Friday was closed
            self._plan(s, now)

    # -- storage --------------------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self) -> None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=2))
        tmp.replace(self.path)

    def _plan(self, s: dict[str, Any], now: datetime) -> None:
        nxt = next_run(s, now, s.get("last_run")) if s.get("enabled", True) else None
        s["next_run"] = nxt.timestamp() if nxt else None
        s["when"] = describe_when(s)

    def _changed(self) -> None:
        self._save()
        self.hub.emit("schedules", self.items)

    # -- editing --------------------------------------------------------------------
    def add(self, raw: dict[str, Any]) -> dict[str, Any]:
        s = validate(raw)
        s.update({"id": new_id("sch"), "created": time.time(), "last_run": None,
                  "last_status": None, "last_run_id": None})
        self._plan(s, datetime.now())
        self.items.append(s)
        self._changed()
        self.hub.add_feed("info", f"Scheduled: {s['name']}", s["when"])
        return s

    def update(self, sid: str, raw: dict[str, Any]) -> dict[str, Any]:
        s = self.get(sid)
        merged = {**s, **raw}
        clean = validate(merged)
        for k in ("time", "days", "every_minutes", "at"):  # drop fields of the old kind
            s.pop(k, None)
        s.update(clean)
        self._plan(s, datetime.now())
        self._changed()
        return s

    def remove(self, sid: str) -> dict[str, Any]:
        s = self.get(sid)
        self.items.remove(s)
        self._changed()
        return s

    def get(self, sid: str) -> dict[str, Any]:
        for s in self.items:
            if s["id"] == sid:
                return s
        raise KeyError(f"no schedule with id {sid}")

    def run_now(self, sid: str) -> None:
        self._fire(self.get(sid), manual=True)

    # -- running --------------------------------------------------------------------
    def _fire(self, s: dict[str, Any], manual: bool = False) -> None:
        running = s.get("last_run_id") and self.hub.runs.get(s["last_run_id"])
        if running and running.active:
            return  # previous run of this job is still going

        def done(run) -> None:
            s["last_status"] = run.status
            self._changed()

        run = self.start_task(s["prompt"], kind="schedule", title=s["name"], speak=s.get("speak", False),
                              on_done=done)
        s["last_run"], s["last_run_id"], s["last_status"] = time.time(), run.id, "working"
        if s["kind"] == "once" and not manual:
            s["enabled"] = False
        self._plan(s, datetime.now())
        self._changed()

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            if not self.enabled():
                continue
            now = time.time()
            for s in list(self.items):
                if s.get("enabled", True) and s.get("next_run") and s["next_run"] <= now:
                    try:
                        self._fire(s)
                    except Exception as e:
                        self.hub.add_feed("error", f"Schedule failed to start: {s['name']}", str(e))

    def today(self) -> list[dict[str, Any]]:
        """Jobs due between now and midnight, for the mission timeline."""
        end = datetime.now().replace(hour=23, minute=59, second=59).timestamp()
        return [{"t": s["next_run"], "title": s["name"], "kind": "schedule", "id": s["id"]}
                for s in self.items if s.get("enabled", True) and s.get("next_run") and s["next_run"] <= end]
