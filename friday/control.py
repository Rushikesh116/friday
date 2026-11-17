"""Tools that let the conversation agent use the dashboard's features by voice:
start research or background tasks, check on them, manage schedules and memory."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .tasks import first_sentence

if TYPE_CHECKING:
    from .core import Friday


def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def fail(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {msg}"}], "is_error": True}


SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short name, e.g. 'Morning email summary'"},
        "prompt": {"type": "string", "description": "What to do each time, written as a full instruction"},
        "kind": {"type": "string", "enum": ["daily", "weekly", "interval", "once"]},
        "time": {"type": "string", "description": "24-hour HH:MM, for daily and weekly"},
        "days": {"type": "array", "items": {"type": "integer"}, "description": "weekly: 0=Monday … 6=Sunday"},
        "every_minutes": {"type": "integer", "description": "interval: minutes between runs (min 5)"},
        "at": {"type": "string", "description": "once: local date and time, YYYY-MM-DDTHH:MM"},
        "speak": {"type": "boolean", "description": "Read the result aloud when it finishes"},
    },
    "required": ["name", "prompt", "kind"],
}


def make_server(f: "Friday"):
    @tool("research_topic",
          "Start the researcher on a topic. It investigates in the background and pops an illustrated "
          "brief (story, timeline, figures, photos, stock chart) onto the dashboard, then the summary "
          "is read aloud. Returns immediately.",
          {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]})
    async def research_topic(args: dict[str, Any]) -> dict[str, Any]:
        try:
            run = f.tasks.start_research(str(args.get("topic", "")), speak=True)
        except ValueError as e:
            return fail(str(e))
        return ok(f"Researcher started (run {run.id}). The brief will appear on the dashboard when ready.")

    @tool("start_task",
          "Hand a longer job to a background agent so you stay free to talk. Returns immediately; "
          "progress shows on the dashboard and the user is told when it's done.",
          {"type": "object", "properties": {
              "title": {"type": "string", "description": "A few words naming the job"},
              "prompt": {"type": "string", "description": "Complete instructions for the agent"},
              "speak_result": {"type": "boolean", "description": "Read the outcome aloud when done"}},
           "required": ["title", "prompt"]})
    async def start_task(args: dict[str, Any]) -> dict[str, Any]:
        try:
            run = f.tasks.start(str(args.get("prompt", "")), kind="task", title=str(args.get("title", "")),
                                speak=bool(args.get("speak_result", True)))
        except ValueError as e:
            return fail(str(e))
        return ok(f"Background agent started (run {run.id}).")

    @tool("task_status", "What background agents are doing now, and how recent ones ended.",
          {"type": "object", "properties": {}})
    async def task_status(args: dict[str, Any]) -> dict[str, Any]:
        runs = [r for r in f.hub.runs.values() if r.kind != "conversation"]
        active = [r for r in runs if r.active]
        recent = sorted((r for r in runs if not r.active), key=lambda r: r.ended or 0, reverse=True)[:5]
        lines = [f"Working: {r.title} [{r.status}] - {r.current or 'starting'}"
                 f" ({len(r.subs)} subagents)" for r in active]
        lines += [f"Finished: {r.title} [{r.status}] - {first_sentence(r.result) if r.result else r.error}"
                  for r in recent]
        return ok("\n".join(lines) if lines else "No background agents have run yet.")

    @tool("schedule_add",
          "Create a scheduled job that runs as a background agent: daily (time), weekly (days + time), "
          "interval (every_minutes) or once (at). The user is asked to confirm.",
          SCHEDULE_SCHEMA)
    async def schedule_add(args: dict[str, Any]) -> dict[str, Any]:
        try:
            s = f.scheduler.add(args)
        except (ValueError, KeyError) as e:
            return fail(str(e))
        nxt = datetime.fromtimestamp(s["next_run"]).strftime("%a %-d %b at %-I:%M %p") if s["next_run"] else "never"
        return ok(f"Scheduled '{s['name']}' {s['when']} (id {s['id']}). Next run: {nxt}.")

    @tool("schedule_list", "List scheduled jobs with their timing, next run and last result.",
          {"type": "object", "properties": {}})
    async def schedule_list(args: dict[str, Any]) -> dict[str, Any]:
        if not f.scheduler.items:
            return ok("No schedules yet.")
        rows = []
        for s in f.scheduler.items:
            nxt = datetime.fromtimestamp(s["next_run"]).strftime("%a %-d %b %-I:%M %p") if s.get("next_run") else "-"
            rows.append(f"{s['id']}: {s['name']} - {s['when']} - {'on' if s.get('enabled', True) else 'paused'}"
                        f" - next {nxt} - last {s.get('last_status') or 'never run'}")
        return ok("\n".join(rows))

    @tool("schedule_remove", "Delete a scheduled job by id (see schedule_list).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
    async def schedule_remove(args: dict[str, Any]) -> dict[str, Any]:
        try:
            s = f.scheduler.remove(str(args.get("id", "")))
        except KeyError as e:
            return fail(str(e))
        return ok(f"Removed '{s['name']}'.")

    @tool("remember", "Save a note about the user (a preference, a person, a routine) for future conversations.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        try:
            m = f.memory.add(str(args.get("text", "")), source="Friday")
        except ValueError as e:
            return fail(str(e))
        return ok(f"Noted (id {m['id']}).")

    @tool("forget", "Delete a saved note about the user, by id or by matching text.",
          {"type": "object", "properties": {"id_or_text": {"type": "string"}}, "required": ["id_or_text"]})
    async def forget(args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("id_or_text", "")).strip().lower()
        for m in list(f.memory.items):
            if m["id"] == key or (key and key in m["text"].lower()):
                f.memory.remove(m["id"])
                return ok(f"Forgot: {m['text']}")
        notes = "; ".join(f"{m['id']}: {m['text']}" for m in f.memory.items) or "none"
        return fail(f"no matching note. Saved notes: {notes}")

    return create_sdk_mcp_server("friday", tools=[
        research_topic, start_task, task_status, schedule_add, schedule_list, schedule_remove, remember, forget])


# Pre-approved (schedule_add goes through the confirmation gate instead).
ALLOWED = [f"mcp__friday__{n}" for n in
           ("research_topic", "start_task", "task_status", "schedule_list", "schedule_remove", "remember", "forget")]
