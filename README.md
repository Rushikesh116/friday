# Friday

A voice assistant for your Mac. Say **"Friday,"** then what you need: open apps and
websites, research a topic, write and run code, read your email out loud, reply to it,
or write new emails. By default she's powered by Claude through the Claude Agent SDK, so she has a
shell, your files and the web, plus Apple Mail, Calendar and a browser she can click
around in.

Speech recognition (whisper.cpp) and echo cancellation (WebRTC) run **on your Mac**.
Your audio is never uploaded; only requests addressed to her go to the selected AI provider, as text.

## Setup (one time)

Everything is already installed in this folder. To rebuild it on another Mac, run
`./setup.sh` (installs whisper.cpp, a Python 3.12 environment and the speech model), then
`./make-app.sh --dock` to install the Friday app and keep it in the Dock.

1. **Sign in to Claude** (she uses your Claude account through the `claude` CLI):
   ```bash
   claude auth login
   ```
   To use Codex for general chat and tasks, install the Codex CLI and run `codex login`.
2. **Set up Apple Mail** with your email accounts (Mail → Settings → Accounts) if you
   want her to handle email. Any account Mail supports works (Gmail, Outlook/Exchange, iCloud…).
3. **Run her once from Terminal** and allow the permission prompts macOS shows:
   Microphone (to hear you), and Automation for Mail, Calendar and System Events (to act
   for you). If you miss one: System Settings → Privacy & Security.
4. **Optional: a higher-quality Tara.** Her voice is Tara (Indian English). If System
   Settings → Accessibility → Spoken Content → System Voice → Manage Voices → English
   (India) offers an Enhanced or Premium Tara, download it and she'll use it
   automatically the next time she starts.

## The Friday app

Friday is installed as a regular Mac app: **/Applications/Friday.app**. Open it from the
Dock, Launchpad or Spotlight (⌘Space, "Friday").

- **The first time**, macOS asks "Friday would like to access the microphone" (allow it so
  she can hear you), and later asks before she first uses Mail or Calendar.
- **Closing the window** (red button) hides it, but Friday keeps running and listening for
  her name. The dot under her Dock icon shows she's running. **Click the Dock icon** to bring
  the window back.
- **Quit** with ⌘Q, or right-click the Dock icon → Quit. That stops her listening, her agents
  and her speech server.
- **Logs**, if something goes wrong: `~/Library/Logs/Friday/friday.log`. Startup problems
  are shown in a dialog.

The app is a small launcher that runs Friday from this folder, so code changes here apply
the next time you open it. **Rebuild it** with `./make-app.sh` if you move this folder or
change your Python setup. macOS may then ask for the microphone again, because each build
gets a new signature. `./make-app.sh --dock` also puts it back in the Dock. To remove it,
drag it out of the Dock and delete `/Applications/Friday.app`. Your Dock layout from before
the app was added is saved in `build/dock-backup.plist`.

## Run from Terminal

```bash
./friday.sh              # the dashboard app, in its own window (voice on)
./friday.sh --browser    # the dashboard in your web browser instead
./friday.sh --no-voice   # the dashboard without microphone or speech
./friday.sh --server     # run without a window; open http://127.0.0.1:8765 yourself
./friday.sh --terminal   # voice only, in the terminal (the original mode)
./friday.sh --menubar    # voice only, with a menu-bar icon
./friday.sh --text       # type in the terminal instead of talking
```

Started from Terminal, closing the dashboard window (or Ctrl+C) quits Friday. Run only one
copy at a time, either the app or Terminal.

## The dashboard

| Section | What it does |
|---|---|
| **Command Center** | Everything at a glance: the core orb (it glows green while listening, violet while working, and pulses with her voice), her status, live activity, the agents at work, today's calendar and scheduled jobs, quick commands, real CPU/RAM/disk/battery/network, the latest research and your Claude usage. |
| **Voice controls** (left) | **Assistant** on/off (mic and speech), **Always on** (her name wakes her) or **Push to talk** (only after you press Talk), **Voice replies** on/off, the mic button, a live level meter and what she last heard. |
| **Chat** | Type to her. Spoken and typed messages share one conversation. Send a message as a **background task** or a **research** request instead. |
| **Agents & Tasks** | Start background agents that work while you keep talking (up to 3 at once by default). See every agent and the **subagents** each one spawns, its live steps, cost and result, and stop or re-run them. |
| **Research** | Ask about any topic ("What happened to Nvidia's stock this month?"). A researcher agent reads the news and pops up an illustrated brief: photos from the coverage, the story, a dated timeline, key figures, a live share-price chart for public companies, sources, and a "keep in mind" box separating confirmed facts from allegations. She reads the summary aloud. |
| **Schedules** | Jobs that run on their own: every day, chosen weekdays, every N minutes/hours, or once. Each run is a background agent you can watch. You can also say "Friday, every weekday at 8 summarize my email" (she asks you to confirm). |
| **Memory** | Notes Friday keeps about you ("I prefer short emails"). She reads them at the start of each conversation. Say "Friday, remember …" or add them here. |
| **Settings** | Voice (with preview), speed, volume, interrupt-by-talking, Claude or Codex provider, automatic or manual model selection, agents at once, weather city and units, pause all schedules. |

### Models and providers

**Automatic** selection is the default. Friday chooses Claude Haiku 5.5 at low effort for
short routine requests, Sonnet 5.5 at medium effort for normal work, and the model set by
`FRIDAY_MODEL` (Opus 5.5 by default) at max effort for difficult work. This is a local,
rule-based estimate made before each request. **Manual** uses `FRIDAY_MODEL` and the effort
chosen in Settings for every Claude request. The chosen model and effort appear in each
agent's details.

Choose **Codex** in Settings to use the local Codex CLI for general conversations and
background tasks. Set its main model ID there. Automatic mode uses the verified Luna model
for quick requests and the chosen model for other work, with reasoning effort adjusted by
task; manual mode uses the chosen model and effort. Codex and Claude keep separate context
within each chat thread. Mail, Calendar, browser actions, schedules, reminders, saved memory, briefings and illustrated
research still use Claude because their Friday tools are connected to the Claude SDK.
Codex tasks use the Codex CLI's workspace sandbox; they cannot use Friday's Claude-only
Mail, Calendar, browser or dashboard tools. Keep Claude signed in for those features.
The Codex workspace starts in `~/Projects` by default; set `FRIDAY_CODEX_WORKDIR` to a
different folder before starting Friday if needed. Codex shell actions use its workspace
sandbox and do not appear in Friday's Claude approval dialog.

Anything that needs your OK (sending email, deleting files, a new schedule…) pops up as an
**approval dialog** from whichever agent asked. During a voice conversation you can also
just answer out loud.

**Privacy:** the dashboard only listens on your own Mac (127.0.0.1) and every connection
needs a secret created at launch, so websites can't control Friday. Weather is only looked
up once you enter a city. Friday's agents don't load your Claude Code settings or plugins;
they get exactly the tools described here.

## Talking to her

Start with her name. You can say it all in one breath:

- "Friday, open Spotify." / "Friday, open YouTube and search for lo-fi music."
- "Friday, what's unread in my inbox?" → she summarizes each email.
- "Reply to the one from Professor Lee saying I'll be there at 3." → she reads the reply
  back and waits for your **"yes"** before sending.
- "Friday, write an email to sam@example.com about Friday's meeting." → opens a draft for you to review.
- "Friday, research the best laptop stands under $50 and put a summary on my desktop."
- "Friday, make a Python script in ~/Projects that renames my screenshots by date, and run it."

Or just say "Friday" and wait for the chime. After she answers she listens for about 5
seconds, so you can follow up without her name. Mentioning Friday in passing ("see you
on Friday") doesn't wake her; her name has to start or end what you say.

### Interrupting her

| Say | What happens |
|---|---|
| **"Friday, stop"** | She stops talking and abandons the task. Works even while she's loud. |
| **"Friday, stop talking"** | She stops talking but finishes the task silently (results still show in the dashboard). |
| **"Friday"**, or a sentence of 3+ words over her voice | She stops, chimes, and listens for your new request. (Not in the first second of her talking, when leftover echo could fool her; her name always works.) |
| "stop" / "cancel" / "never mind", "quiet" / "shh" | Same as above when her volume is low or you're on headphones. |
| The mic button or Talk pill | Stops her while she's talking or working; otherwise starts listening. |

While she's talking, the speakers are right next to the mic. Echo cancellation removes
her voice, but it also clips the first word you say over her. That's why starting with
"Friday" works best: her name takes the clip and "stop" comes through. On headphones,
none of this matters.

Other commands: **"new conversation"** clears her memory of the chat.

## What she asks before doing with Claude

She asks first, with a spoken read-back and an approval dialog on the dashboard. Answer
either way: say "yes"/"no", or click Approve/Decline. This covers:

- sending an email or reply
- creating a schedule (it will run on its own later)
- deleting, moving or overwriting files outside `~/Projects`, `~/Desktop` and `~/Documents/Friday`
- destructive or far-reaching shell commands (`rm`, `sudo`, `git push`, `kill`, uninstalling…)
- clicking buttons like Send, Buy, Pay, Delete, Post or Submit on web pages, or running
  page scripts that click or submit

She never types passwords, card numbers or one-time codes; she hands those steps to you.
The rules live in `friday/gate.py` if you want to tighten or relax them.

## Settings

Most of these can be changed in the dashboard's **Settings** page; what you set there is
saved in `data/settings.json` and takes priority. The rest are environment variables set
before starting (or edit `friday/config.py`):

| Variable | Default | What it does |
|---|---|---|
| `FRIDAY_PORT` | `8765` | Dashboard port (on 127.0.0.1 only) |
| `FRIDAY_DATA` | `./data` | Where settings, schedules, memory, task history and briefs are saved |
| `FRIDAY_MAX_PARALLEL` | `3` | Background agents allowed at once |
| `FRIDAY_MAX_SUBAGENTS` | `10` | Subagents one request may start (nested ones count); more are refused |
| `FRIDAY_TASK_BUDGET_USD` | `10` | A background task stops once its model usage passes this. `0` = no cap |
| `FRIDAY_MAX_BUFFER_MB` | `32` | Largest single message read from Claude; raise if a task fails with a JSON buffer error |
| `FRIDAY_NAME` | `Friday` | Her name, which is also what wakes her. Any name works. |
| `FRIDAY_VOICE` | `Tara` (best quality installed) | Any voice from `say -v '?'`, e.g. `Samantha`, `Moira`, `Tessa` |
| `FRIDAY_RATE` | `172` | Speaking speed, words per minute |
| `FRIDAY_VOLUME` | `0.75` | Her loudness relative to the system volume |
| `FRIDAY_EFFORT` | `medium` | Effort in Manual mode |
| `FRIDAY_MODEL` | `claude-opus-5-5` | Claude model in Manual mode and for hard tasks in Auto mode |
| `FRIDAY_CODEX_WORKDIR` | `~/Projects` | Writable workspace for Codex tasks |
| `FRIDAY_BARGE_IN` | `1` | `0` = only her name interrupts her (if a TV or other people keep cutting her off) |
| `FRIDAY_MIN_SPEECH_RMS` | `80` | Raise (150–300) if background noise keeps waking her |
| `FRIDAY_SILENCE_END_S` | `0.9` | Pause length that ends your sentence |
| `FRIDAY_SAFE_WRITE_DIRS` | `~/Projects:~/Desktop:~/Documents/Friday` | Folders she can write to without asking |

Try another voice before switching: `say -v Moira -r 172 "Hi, I'm Friday."`

## How it fits together

```
           ┌──────────────────── audio.py (one duplex stream, 48 kHz) ────────────────────┐
speaker ◄──┤ her voice ─────────────────────────┐                                         │
mic ──────►│ your voice + her echo ─► WebRTC echo canceller ─► 16 kHz ─► ears.py          │
           └──────────────────────────────────────────────────────────────────────────────┘
ears.py: split into utterances ─► whisper.cpp (local) ─► "Friday, …"? "stop"? her echo?
                                                            │
mouth.py (macOS `say`) ◄─ spoken reply ◄─ brain.py (Claude Agent SDK) ◄─┘
                                            │  tools: shell, files, web search,
                                            │  Apple Mail/Calendar (hands.py), Playwright browser
                                            └─ gate.py: "Shall I go ahead?" before anything hard to undo
```

Around that conversation:

```
dashboard window (web/: index.html, app.js, viz.js) ◄── live WebSocket ──► server.py (127.0.0.1, token)
                                                                              │
hub.py: shared state — agent runs & subagents, chat, activity, approvals, briefs
   ▲            ▲                 ▲                    ▲                 ▲
core.py     tasks.py          scheduler.py         research.py       sysmon.py
(voice +    (background       (timed jobs →        (researcher tools: (CPU/RAM/disk,
 chat)       agents, queue)    background agents)   photos, stocks,    network, weather,
                                                    publish_brief)     calendar)
control.py lets the conversation start research/tasks/schedules and manage memory (memory.py).
```

`app.py` starts it all and opens the window, and `menubar.py` is the menu-bar-only mode.

## Checking it works

```bash
.venv/bin/python -m unittest tests.test_limits tests.test_routing tests.test_codex_brain   # offline unit tests
.venv/bin/python tests/smoke_voice.py    # name, stop, barge-in and echo handling (no mic needed)
.venv/bin/python tests/smoke_mouth.py    # her voice output, silently
.venv/bin/python tests/smoke_brain.py "What's in ~/Projects?"   # one live request to Claude
```
