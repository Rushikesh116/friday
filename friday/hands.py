"""Mac-specific tools the agent can call: Apple Mail, Calendar, notifications.

Everything goes through AppleScript (`osascript`), with values passed as argv so
quotes in an email body can never break out of the script.
"""

import asyncio
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from . import config

SEP = "␞"  # record separator that won't appear in mail text


async def osa(script: str, *args: str, timeout: float = 60) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError("AppleScript timed out (is Mail waiting on a dialog?)")
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip() or "AppleScript failed")
    return out.decode().strip()


def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def fail(e: Exception) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


READ_ONLY = ToolAnnotations(readOnlyHint=True)

# ---------------------------------------------------------------- Mail: read

LIST_SCRIPT = f"""
on run argv
  set lim to (item 1 of argv) as integer
  set onlyUnread to (item 2 of argv) is "true"
  set out to ""
  tell application "Mail"
    if onlyUnread then
      set msgs to (messages of inbox whose read status is false)
    else
      set msgs to messages of inbox
    end if
    set n to count of msgs
    if n > lim then set n to lim
    repeat with i from 1 to n
      set m to item i of msgs
      set out to out & (id of m) & "{SEP}" & (sender of m) & "{SEP}" & (subject of m) & "{SEP}" & ((date received of m) as string) & "{SEP}" & (read status of m) & linefeed
    end repeat
  end tell
  return out
end run
"""


@tool(
    "mail_list",
    "List messages in the Apple Mail inbox (all accounts), newest first. Returns id, sender, "
    "subject, date and read state for each. Use the id with mail_read or mail_reply.",
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many messages, default 10"},
            "unread_only": {"type": "boolean", "description": "Only unread messages, default true"},
        },
    },
    annotations=READ_ONLY,
)
async def mail_list(args: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = await osa(LIST_SCRIPT, str(args.get("limit", 10)), str(args.get("unread_only", True)).lower())
    except Exception as e:
        return fail(e)
    if not raw:
        return ok("No messages found.")
    rows = []
    for line in raw.splitlines():
        mid, sender, subject, date, read = (line.split(SEP) + [""] * 5)[:5]
        rows.append(f"id={mid} | from: {sender} | subject: {subject} | {date} | {'read' if read == 'true' else 'unread'}")
    return ok("\n".join(rows))


READ_SCRIPT = f"""
on run argv
  set mid to (item 1 of argv) as integer
  tell application "Mail"
    set m to first message of inbox whose id is mid
    set toList to ""
    repeat with r in (to recipients of m)
      set toList to toList & (address of r) & ", "
    end repeat
    set bodyText to content of m
    set read status of m to true
    return (sender of m) & "{SEP}" & toList & "{SEP}" & (subject of m) & "{SEP}" & ((date received of m) as string) & "{SEP}" & bodyText
  end tell
end run
"""


@tool(
    "mail_read",
    "Read one inbox message in full (sender, recipients, subject, date, body) and mark it read.",
    {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
)
async def mail_read(args: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = await osa(READ_SCRIPT, str(args["id"]))
    except Exception as e:
        return fail(e)
    sender, to, subject, date, body = (raw.split(SEP, 4) + [""] * 5)[:5]
    if len(body) > 12000:
        body = body[:12000] + "\n[...truncated]"
    return ok(f"From: {sender}\nTo: {to.rstrip(', ')}\nSubject: {subject}\nDate: {date}\n\n{body}")


# ---------------------------------------------------------- Mail: write/send

COMPOSE_SCRIPT = """
on run argv
  set toAddrs to item 1 of argv
  set ccAddrs to item 2 of argv
  set subj to item 3 of argv
  set bodyText to item 4 of argv
  set doSend to (item 5 of argv) is "true"
  tell application "Mail"
    set m to make new outgoing message with properties {subject:subj, content:bodyText, visible:(not doSend)}
    tell m
      repeat with a in my splitText(toAddrs)
        make new to recipient at end of to recipients with properties {address:a}
      end repeat
      repeat with a in my splitText(ccAddrs)
        make new cc recipient at end of cc recipients with properties {address:a}
      end repeat
    end tell
    if doSend then
      send m
      return "sent"
    else
      activate
      return "draft open"
    end if
  end tell
end run

on splitText(t)
  if t is "" then return {}
  set AppleScript's text item delimiters to ","
  set parts to text items of t
  set AppleScript's text item delimiters to ""
  set out to {}
  repeat with p in parts
    set s to (do shell script "echo " & quoted form of p & " | xargs")
    if s is not "" then set end of out to s
  end repeat
  return out
end splitText
"""

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Comma-separated email addresses"},
        "cc": {"type": "string", "description": "Comma-separated, optional"},
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Plain-text body, including greeting and sign-off"},
    },
    "required": ["to", "subject", "body"],
}


@tool(
    "mail_draft",
    "Open a new email in Apple Mail as a visible draft for the user to review. Nothing is sent.",
    EMAIL_SCHEMA,
)
async def mail_draft(args: dict[str, Any]) -> dict[str, Any]:
    try:
        await osa(COMPOSE_SCRIPT, args["to"], args.get("cc", ""), args["subject"], args["body"], "false")
    except Exception as e:
        return fail(e)
    return ok("Draft is open in Mail for review.")


@tool(
    "mail_send",
    "Send a new email from Apple Mail's default account. The user is asked to confirm out loud first.",
    EMAIL_SCHEMA,
)
async def mail_send(args: dict[str, Any]) -> dict[str, Any]:
    try:
        await osa(COMPOSE_SCRIPT, args["to"], args.get("cc", ""), args["subject"], args["body"], "true")
    except Exception as e:
        return fail(e)
    return ok(f"Sent to {args['to']}.")


REPLY_SCRIPT = """
on run argv
  set mid to (item 1 of argv) as integer
  set bodyText to item 2 of argv
  set allFlag to (item 3 of argv) is "true"
  set doSend to (item 4 of argv) is "true"
  tell application "Mail"
    set m to first message of inbox whose id is mid
    if allFlag then
      set r to reply m with reply to all without opening window
    else
      set r to reply m without opening window
    end if
    set content of r to bodyText & return & return & content of r
    if doSend then
      send r
      return "sent"
    else
      set visible of r to true
      activate
      return "draft open"
    end if
  end tell
end run
"""


@tool(
    "mail_reply",
    "Reply to an inbox message by id. send=false opens the reply as a draft to review; "
    "send=true sends it (the user is asked to confirm out loud first).",
    {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "body": {"type": "string", "description": "Your reply text; the quoted original is kept below it"},
            "reply_all": {"type": "boolean"},
            "send": {"type": "boolean"},
        },
        "required": ["id", "body", "send"],
    },
)
async def mail_reply(args: dict[str, Any]) -> dict[str, Any]:
    try:
        res = await osa(
            REPLY_SCRIPT, str(args["id"]), args["body"],
            str(args.get("reply_all", False)).lower(), str(args["send"]).lower(),
        )
    except Exception as e:
        return fail(e)
    return ok("Reply sent." if res == "sent" else "Reply draft is open in Mail.")


# -------------------------------------------------------------- Calendar

CAL_SCRIPT = f"""
on run argv
  set dayOffset to (item 1 of argv) as integer
  set nDays to (item 2 of argv) as integer
  set startD to (current date) + dayOffset * days
  set time of startD to 0
  set endD to startD + nDays * days
  set out to ""
  tell application "Calendar"
    repeat with c in calendars
      repeat with e in (every event of c whose start date >= startD and start date < endD)
        set out to out & ((start date of e) as string) & "{SEP}" & (summary of e) & "{SEP}" & (name of c) & linefeed
      end repeat
    end repeat
  end tell
  return out
end run
"""


@tool(
    "calendar_events",
    "List Apple Calendar events. day_offset 0 = today, 1 = tomorrow; days = how many days to cover.",
    {
        "type": "object",
        "properties": {"day_offset": {"type": "integer"}, "days": {"type": "integer"}},
    },
    annotations=READ_ONLY,
)
async def calendar_events(args: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = await osa(CAL_SCRIPT, str(args.get("day_offset", 0)), str(args.get("days", 1)), timeout=90)
    except Exception as e:
        return fail(e)
    rows = [line.replace(SEP, " | ") for line in raw.splitlines() if line.strip()]
    return ok("\n".join(sorted(rows)) if rows else "No events in that range.")


# -------------------------------------------------------------- Misc

@tool(
    "notify",
    "Show a macOS notification banner (for results the user should see, like a link or a path).",
    {"type": "object", "properties": {"title": {"type": "string"}, "message": {"type": "string"}},
     "required": ["message"]},
)
async def notify(args: dict[str, Any]) -> dict[str, Any]:
    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    try:
        await osa(script, args.get("title", config.NAME), args["message"])
    except Exception as e:
        return fail(e)
    return ok("Shown.")


server = create_sdk_mcp_server(
    "mac", tools=[mail_list, mail_read, mail_draft, mail_send, mail_reply, calendar_events, notify]
)
