"""Decides which tool calls run freely and which need a spoken "yes" first.

Read-only tools are pre-approved in brain.py (allowed_tools) and never reach this file.
Everything else lands in `check`, which allows ordinary actions and asks you before
anything that is hard to undo: sending mail, deleting or overwriting outside your work
folders, destructive shell commands, and clicks on buttons like Send / Buy / Delete.
"""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from . import config

# (spoken summary, on-screen detail) -> (approved?, what the user said)
Confirm = Callable[[str, str], Awaitable[tuple[bool, str]]]

DANGEROUS_SHELL = re.compile(
    r"""(?x)
    \b(rm|rmdir|srm|shred|unlink|mv|dd|mkfs|diskutil|sudo|su|kill|killall|pkill
       |shutdown|reboot|halt|launchctl|crontab|chmod|chown|trash)\b
    | \bdefaults\s+(write|delete)\b
    | \bgit\s+(push|reset\s+--hard|clean|checkout\s+--|branch\s+-D|rebase|stash\s+drop)\b
    | \bgh\s+\S+\s+(create|merge|close|delete|edit|comment|review)\b
    | \b(brew|pip3?|npm|pnpm|yarn|uv\s+pip)\s+(uninstall|remove|publish|unpublish)\b
    | \b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b
    | \bosascript\b.*\b(send|delete|empty\s+trash|quit|restart|shut\s+down|log\s+out)\b
    | \bsqlite3\b.*\b(delete|drop|update)\b
    """,
    re.I | re.S,
)

IRREVERSIBLE_UI = re.compile(
    r"\b(send|submit|buy|purchase|pay|order|checkout|check out|place|delete|remove|post|publish|"
    r"confirm|sign up|register|transfer|book|reserve|subscribe|unsubscribe|accept|agree|share|tweet|reply)\b",
    re.I,
)

# Page JavaScript that does more than read: clicks, submits, sends requests, sets values.
PAGE_SCRIPT_ACTS = re.compile(
    r"\.click\(|\.submit\(|requestSubmit|dispatchEvent|fetch\(|XMLHttpRequest|sendBeacon|\.value\s*=|location\s*=",
)

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _in_safe_dir(path: str) -> bool:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        return False
    return any(p.is_relative_to(d.resolve()) for d in config.SAFE_WRITE_DIRS)


def _short(text: str, n: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def needs_confirmation(name: str, inp: dict[str, Any]) -> tuple[str, str] | None:
    """Return (spoken summary, on-screen detail) if this call needs a yes, else None."""
    if name == "mcp__mac__mail_send":
        cc = f", copying {inp['cc']}" if inp.get("cc") else ""
        return (
            f"I'm about to send an email to {inp.get('to')}{cc}, subject: {inp.get('subject')}. "
            f"It says: {_short(inp.get('body', ''), 300)}",
            f"To: {inp.get('to')}\nCc: {inp.get('cc', '')}\nSubject: {inp.get('subject')}\n\n{inp.get('body')}",
        )
    if name == "mcp__mac__mail_reply" and inp.get("send"):
        who = "everyone on the thread" if inp.get("reply_all") else "the sender"
        return (f"I'm about to send a reply to {who}. It says: {_short(inp.get('body', ''), 300)}",
                inp.get("body", ""))

    if name == "Bash":
        cmd = inp.get("command", "")
        if DANGEROUS_SHELL.search(cmd):
            what = inp.get("description") or "run a command that changes or removes things"
            return f"I want to {what[0].lower() + what[1:]}.", f"$ {cmd}"
        return None

    if name in WRITE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and not _in_safe_dir(path):
            return f"I want to change the file {Path(path).name}, in {Path(path).parent}.", path
        return None

    if name == "mcp__friday__schedule_add":
        # A schedule runs by itself later, so it's confirmed like any other standing change.
        from .scheduler import describe_when

        when = describe_when(inp)
        return (f"I'll schedule \"{inp.get('name', 'a task')}\" {when}.",
                f"Name: {inp.get('name')}\nWhen: {when}\nTask: {inp.get('prompt')}")

    if name.startswith("mcp__playwright__"):
        action = name.removeprefix("mcp__playwright__")
        label = " ".join(str(inp.get(k, "")) for k in ("element", "text", "key", "values"))
        if action == "browser_click" and IRREVERSIBLE_UI.search(label):
            return f"I'm about to click {_short(inp.get('element', 'a button'), 80)} on the web page.", label
        if action == "browser_press_key" and inp.get("key") == "Enter" and IRREVERSIBLE_UI.search(label):
            return "I'm about to submit the form on the page.", label
        if action in ("browser_type",) and inp.get("submit") and IRREVERSIBLE_UI.search(str(inp.get("element", ""))):
            return f"I'm about to type into {inp.get('element')} and submit it.", label
        if action == "browser_evaluate" and PAGE_SCRIPT_ACTS.search(str(inp.get("function", ""))):
            return "I want to run a script on the page that clicks or submits something.", str(inp.get("function"))
        if action in ("browser_file_upload", "browser_run_code_unsafe"):
            return f"I want to {action.removeprefix('browser_').replace('_', ' ')} in the browser.", str(inp)
        return None

    return None


class Gate:
    def __init__(self, confirm: Confirm, log: Callable[[str], None] = print):
        self.confirm = confirm
        self.log = log

    async def check(
        self, name: str, inp: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        ask = needs_confirmation(name, inp)
        if ask is None:
            return PermissionResultAllow()
        spoken, detail = ask
        approved, heard = await self.confirm(spoken, detail)
        if approved:
            self.log(f"  ✔ approved ({heard})")
            return PermissionResultAllow()
        self.log(f"  ✘ declined ({heard or 'no answer'})")
        reply = f'The user declined. They said: "{heard}".' if heard else "The user didn't answer, so it was not done."
        return PermissionResultDeny(message=reply + " Don't retry the same action; ask what they want instead.")


YES = re.compile(r"\b(yes|yeah|yep|yup|sure|go ahead|do it|send it|confirm(ed)?|okay|ok|affirmative|please do|correct|go for it)\b", re.I)
NO = re.compile(r"\b(no|nope|don'?t|do not|stop|cancel|wait|hold on|never ?mind|not yet|change)\b", re.I)


def is_yes(text: str) -> bool:
    return bool(YES.search(text)) and not NO.search(text)
