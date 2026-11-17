"""Friday's centre: the conversation loop plus everything the dashboard controls.

Conversation: hear her name (or a chat message) -> act -> speak -> listen for a follow-up.
You can talk over her at any time:
  "Friday, stop"            -> she stops talking and abandons the request
  "Friday, stop talking"    -> she stops talking but finishes silently
  "Friday" / anything else  -> she stops and listens for your new request

Alongside the conversation run background agents (tasks, research, schedules), all of
them recorded in the Hub so the dashboard can show and control them.
"""

import asyncio
import contextlib
import re
import sys
import time

from . import config, control, hands, net
from .audio import AudioIO
from .brain import VOICE_PERSONA, Brain
from .codex_brain import CodexBrain
from .gate import Gate, is_yes
from .hub import Hub, Run
from .memory import Memory
from .mouth import Mouth
from .scheduler import Scheduler
from .settings import Settings
from .tasks import TaskManager
from .routing import choose, requires_claude

STOP = re.compile(r"^\W*(stop|cancel|never ?mind|that'?s all|nothing|shut up|be quiet|quiet)\W*$", re.I)
RESET = re.compile(r"\b(new conversation|start over|start fresh|forget (that|everything)|clear (the )?context)\b", re.I)

# Terminal colours (left out when output goes to the app's log file).
DIM, BOLD, CYAN, PINK, YELLOW, RED, END = (
    ("\033[2m", "\033[1m", "\033[36m", "\033[35m", "\033[33m", "\033[31m", "\033[0m")
    if sys.stdout.isatty() else ("",) * 7)
APPROVAL_TIMEOUT_S = 30 * 60


class Friday:
    def __init__(self, voice_input: bool = True, muted: bool = False):
        self.voice_input = voice_input
        self.cli_muted = muted
        self.settings = Settings()
        self.hub = Hub()
        self.memory = Memory(self.hub)
        self.tasks = TaskManager(self)
        self.scheduler = Scheduler(self.hub, self.tasks.start, enabled=lambda: self.settings["schedules_enabled"])
        self.audio: AudioIO | None = None
        self.mouth: Mouth | None = None
        self.ears = None
        self.transcriber = None
        self.conv = self.hub.new_run("conversation", config.NAME, run_id="conversation")
        self.conv.status = "idle"
        self._provider = self.settings["provider"]
        self.brain = self._make_brain(self._provider)
        self._brain_thread = self.hub.thread_id  # the chat thread the brain's session belongs to
        self._bind_session()
        self.brain_ready = False
        self._switching = asyncio.Lock()
        self._convo: asyncio.Task | None = None
        self._hushed = False  # "stop talking" was said: finish the current request silently
        self._bg: list[asyncio.Task] = []

    # -- state ------------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self.hub.state["status"]

    @state.setter
    def state(self, value: str) -> None:
        if not self.settings["power"] and value in ("idle", "listening", "speaking"):
            value = "off"
        self.hub.set_state(status=value)
        conv_status = "working" if value in ("thinking", "speaking") else "idle"
        if self.conv.status not in ("waiting",) and self.conv.status != conv_status:
            self.conv.status = conv_status
            self.hub.run_changed(self.conv)

    @property
    def voice_on(self) -> bool:
        """Mic and speech are live (power on, and voice mode was available)."""
        return bool(self.audio and self.audio.running and self.voice_input and self.ears)

    def _publish_controls(self) -> None:
        s = self.settings
        self.hub.set_state(
            power=s["power"], listen_mode=s["listen_mode"], voice_replies=s["voice_replies"],
            schedules_enabled=s["schedules_enabled"], voice_available=bool(self.ears),
            name=config.NAME, voice=config.VOICE, provider=self._provider,
            model=self.brain.model, effort=self.brain.effort)

    def _session_key(self, provider: str) -> str:
        return "session_id" if provider == "claude" else "codex_session_id"

    def _make_brain(self, provider: str) -> Brain | CodexBrain:
        if provider == "codex":
            return CodexBrain(VOICE_PERSONA, tracker=self.hub.tracker(self.conv), memory=self.memory.texts)
        return Brain(
            Gate(lambda s, d: self.confirm(s, d, self.conv), log=lambda s: print(DIM + s + END)),
            persona=VOICE_PERSONA, servers={"friday": control.make_server(self)}, allowed=control.ALLOWED,
            tracker=self.hub.tracker(self.conv), memory=self.memory.texts)

    def _bind_session(self) -> None:
        self.brain.session_id = self.hub.thread.get(self._session_key(self._provider))
        key = self._session_key(self._provider)
        self.brain.on_session = lambda sid: self.hub.touch_thread(self._brain_thread, **{key: sid})

    async def _ensure_provider(self, provider: str) -> None:
        if self._provider == provider:
            return
        await self.brain.stop()
        self._provider = provider
        self.brain = self._make_brain(provider)
        self._bind_session()
        self.brain_ready = False
        await self._connect_brain()

    # -- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        print(DIM + f"Voice: {config.VOICE}" + END)
        if self.voice_input:
            from .ears import Ears, Transcriber

            print(DIM + "Loading speech recognition…" + END)
            try:
                self.transcriber = Transcriber()
                await self.transcriber.start()
            except Exception as e:
                print(f"{RED}Speech recognition unavailable:{END} {e}")
                self.hub.set_state(error=f"Speech recognition unavailable: {e}")
                self.voice_input, self.transcriber = False, None
        if self.voice_input:
            self.audio = AudioIO(on_frame=lambda fr: self.ears.feed(fr))
            self.mouth = Mouth(self.audio)
            self.ears = Ears(
                self.transcriber, self.mouth,
                is_busy=lambda: self.brain.busy,
                on_command=self.heard_command, on_stop=self.heard_stop,
                log=lambda s: print(DIM + s + END),
            )
            self.ears.on_heard = lambda text, outcome: self.hub.set_state(heard=text, heard_outcome=outcome,
                                                                          heard_at=time.time())
            self.ears.mode = self.settings["listen_mode"]
            self.ears.start()
        else:
            self.audio = AudioIO(on_frame=None)
            self.mouth = Mouth(self.audio)
        self.mouth.muted = self.cli_muted or not self.settings["voice_replies"]
        self.mouth.start()
        if self.settings["power"]:
            self._open_audio()
        self._publish_controls()
        print(DIM + f"Connecting to {self._provider.title()}…" + END)
        await self._connect_brain()
        self._bg.append(asyncio.create_task(self.scheduler.loop()))
        self._bg.append(asyncio.create_task(self._level_loop()))
        self.state = "idle"

    def _open_audio(self) -> None:
        try:
            self.audio.start()
        except Exception as e:
            print(f"{RED}Audio device problem:{END} {e}")
            self.hub.set_state(error=f"Audio device problem: {e}")

    async def _connect_brain(self) -> bool:
        try:
            await self.brain.start()
            self.brain_ready = True
            self.hub.set_state(claude="connected", error="", provider=self._provider)
        except Exception as e:
            self.brain_ready = False
            print(f"{RED}Couldn't connect to {self._provider.title()}:{END} {e}")
            self.hub.set_state(claude="error", error=f"Couldn't connect to {self._provider.title()}: {e}")
        return self.brain_ready

    async def shutdown(self) -> None:
        for t in self._bg:
            t.cancel()
        if self.mouth:
            self.mouth.stop()
        await self.tasks.cancel_all()
        await self.brain.stop()
        if self.audio:
            self.audio.stop()
        if self.transcriber:
            await self.transcriber.stop()
        await net.close()

    async def _level_loop(self) -> None:
        """Mic and speaker levels for the dashboard's meters and orb (~12 times a second)."""
        last = (-1.0, -1.0)
        while True:
            await asyncio.sleep(0.08)
            if not self.hub.clients:
                continue
            mic = min(1.0, (self.ears.level / 2500) ** 0.5) if (self.ears and self.voice_on) else 0.0
            out = min(1.0, (self.audio.out_level * 4) ** 0.7) if self.audio else 0.0
            cur = (round(mic, 2), round(out, 2))
            if cur != last:
                last = cur
                self.hub.emit("level", {"mic": cur[0], "out": cur[1]})

    # -- dashboard controls -----------------------------------------------------------
    def set_power(self, on: bool) -> None:
        self.settings.set("power", on)
        if on:
            if self.audio and not self.audio.running:
                self._open_audio()
            self.state = "idle"
        else:
            if self.mouth:
                self.mouth.stop()
            if self._convo and not self._convo.done():
                self._convo.cancel()
            if self.audio:
                self.audio.stop()
            self.hub.set_state(status="off")
        self._publish_controls()

    def set_listen_mode(self, mode: str) -> None:
        self.settings.set("listen_mode", mode)
        if self.ears:
            self.ears.mode = mode
        self._publish_controls()

    def set_voice_replies(self, on: bool) -> None:
        self.settings.set("voice_replies", on)
        if self.mouth:
            self.mouth.muted = self.cli_muted or not on
            if not on:
                self.mouth.stop()
        self._publish_controls()

    async def set_setting(self, key: str, value) -> None:
        self.settings.set(key, value)
        if key == "volume" and self.audio:
            self.audio.volume = config.VOLUME
        if key == "provider":
            await self._take_over()
            await self._ensure_provider(value)
        self._publish_controls()

    def talk_now(self) -> None:
        """Start listening without her name (Talk button / push to talk)."""
        if self.voice_on:
            self.heard_command("")

    def stop_all(self) -> None:
        """Stop talking and drop the current conversation request (background tasks keep going)."""
        if self.mouth:
            self.mouth.stop()
        asyncio.create_task(self._take_over())
        self.state = "idle"

    async def new_conversation(self, title: str = "") -> dict:
        """Start a new chat thread (the old one is kept and can be reopened)."""
        t = self.hub.new_thread(title)
        await self.switch_thread(t["id"])
        return t

    async def switch_thread(self, tid: str) -> None:
        """Make another chat thread the active one: what you say or type next goes to it."""
        if tid not in self.hub.threads:
            raise ValueError("that chat doesn't exist any more")
        async with self._switching:
            if tid == self._brain_thread and self.brain_ready:
                self.hub.set_thread(tid)
                return
            await self._take_over()
            self.hub.set_thread(tid)
            self._brain_thread = tid
            self.brain.session_id = self.hub.threads[tid].get(self._session_key(self._provider))
            try:
                await self.brain.switch(self.brain.session_id)
                self.brain_ready = True
                self.hub.set_state(claude="connected", error="")
            except Exception as e:
                self.brain_ready = False
                self.hub.set_state(claude="error", error=f"Couldn't reconnect to {self._provider.title()}: {e}")
            self.conv.steps.clear()
            self.hub.run_changed(self.conv)

    async def delete_thread(self, tid: str) -> None:
        if tid not in self.hub.threads:
            return
        if tid == self.hub.thread_id:
            others = [t for t in self.hub.thread_list() if t["id"] != tid]
            if others:
                await self.switch_thread(others[0]["id"])
            else:
                await self.new_conversation()
        self.hub.delete_thread(tid)

    def rename_thread(self, tid: str, title: str) -> None:
        title = " ".join(title.split())[:60]
        if not title:
            raise ValueError("give the chat a name")
        self.hub.touch_thread(tid, title=title, auto_title=False)

    def notify(self, title: str, message: str) -> None:
        script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
        asyncio.create_task(self._quiet(hands.osa(script, f"{config.NAME} · {title}"[:120], message[:240] or " ")))

    @staticmethod
    async def _quiet(coro) -> None:
        with contextlib.suppress(Exception):
            await coro

    def announce(self, text: str) -> None:
        """Say something unprompted (a finished task, a research summary) if voice is on."""
        if self.voice_on and self.mouth and not self.mouth.muted and text:
            self.mouth.say(text)

    def on_brief(self, brief: dict, speak: bool) -> None:
        self.hub.add_chat("friday", f"Research ready: {brief['title']}", "research", brief_id=brief["id"])
        if speak:
            self.announce(brief["summary"])

    # -- conversation -----------------------------------------------------------------
    def chat(self, text: str) -> None:
        """A message typed in the dashboard. Like speaking: the newest request wins."""
        text = text.strip()
        if text:
            asyncio.create_task(self._start_conversation(text, "chat"))

    def heard_command(self, text: str) -> None:
        """Her name was said (with or without a request): drop everything and handle it."""
        asyncio.create_task(self._start_conversation(text, "voice"))

    def heard_stop(self, cancel_task: bool) -> None:
        """You said stop/cancel (cancel_task=True) or stop talking/shh (False) while she was busy."""
        self.mouth.stop()
        asyncio.create_task(self.mouth.chime(config.STOP_CHIME))
        if cancel_task:
            print(f"{DIM}  ■ stopped{END}")
            asyncio.create_task(self._take_over())
            self.state = "idle"
            self.hub.add_chat("system", "Stopped.", "system")
        else:
            print(f"{DIM}  ■ quiet (still working){END}")
            self._hushed = self.brain.busy

    async def _take_over(self) -> None:
        """Silence her and stop whatever the conversation was doing."""
        if self.mouth:
            self.mouth.stop()
        self._hushed = False
        if self._convo and not self._convo.done() and self._convo is not asyncio.current_task():
            self._convo.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._convo
        await self.brain.interrupt()

    async def _start_conversation(self, text: str, source: str) -> None:
        await self._take_over()
        self._convo = asyncio.create_task(self._conversation(text, source))

    async def _conversation(self, text: str, source: str) -> None:
        try:
            if not text and source == "voice":  # just her name: chime and wait for the request
                asyncio.create_task(self.mouth.chime())
                self.state = "listening"
                text = await self.ears.listen(config.WAIT_FOR_SPEECH_S) or ""
            while text:
                print(f"\n{CYAN}You:{END} {text}")
                self.hub.add_chat("user", text, source)
                if STOP.match(text):
                    break
                await self._handle(text)
                await self.mouth.wait_idle()
                if source != "voice" or not self.voice_on:
                    break
                self.state = "listening"
                text = await self.ears.listen(config.FOLLOW_UP_S) or ""  # follow-up, no name needed
        finally:
            self.state = "idle"

    async def _handle(self, text: str) -> None:
        if RESET.search(text):
            await self.new_conversation()
            self.mouth.say("Okay, I've started a new chat. The old one is still in your chat list.")
            return
        provider = "claude" if requires_claude(text) else self.settings["provider"]
        await self._ensure_provider(provider)
        if not self.brain_ready and not await self._connect_brain():
            self.hub.add_chat("error", self.hub.state.get("error") or f"Couldn't connect to {provider.title()}.", "system")
            self.mouth.say(f"I can't reach {provider.title()} right now. The details are on the dashboard.")
            return
        route = choose(text, {**self.settings.values, "provider": provider})
        try:
            await self.brain.configure(route.model, route.effort)
        except Exception as e:
            self.brain_ready = False
            self.hub.add_chat("error", f"Couldn't select {route.model}: {e}", "system")
            self.mouth.say("I couldn't select that model. The details are on the dashboard.")
            return
        self.conv.meta.update(provider=provider, model=route.model, effort=route.effort, route=route.level)
        self.hub.set_state(provider=provider, model=route.model, effort=route.effort)
        self.state = "thinking"
        self.conv.current = text[:120]
        self.hub.tracker(self.conv)._step("user", text)
        self._hushed = False
        started = time.monotonic()
        try:
            async with contextlib.aclosing(self.brain.ask(text)) as events:  # closed promptly if cancelled
                async for ev in events:
                    if ev.kind == "say":
                        print(f"{PINK}{config.NAME}:{END} {ev.text}")
                        self.hub.add_chat("friday", ev.text, "voice" if self.voice_on else "chat")
                        if not self._hushed:
                            self.mouth.say(ev.text)
                            if self.mouth.speaking:
                                self.state = "speaking"
                    elif ev.kind == "tool":
                        print(f"{DIM}  → {ev.text}{END}")
                    elif ev.kind == "error":
                        print(f"{RED}Error:{END} {ev.text}")
                        self.hub.add_chat("error", ev.text, "system")
                        if not self._hushed:
                            self.mouth.say("Sorry, something went wrong. The details are on the dashboard.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the agent process died or similar: reconnect next time
            print(f"{RED}Error:{END} {e}")
            self.brain_ready = False
            self.hub.add_chat("error", f"{type(e).__name__}: {e}", "system")
        finally:
            self.conv.current = ""
            self.hub.run_changed(self.conv)
        print(f"{DIM}  ({time.monotonic() - started:.1f}s){END}")
        self._hushed = False

    # -- confirmations (called by the gate in the middle of any agent's work) ---------------
    async def confirm(self, spoken: str, detail: str, run: Run) -> tuple[bool, str]:
        print(f"\n{YELLOW}{BOLD}Needs your OK ({run.title}):{END} {spoken}\n{DIM}{detail}{END}")
        approval = self.hub.new_approval(run, spoken, detail)
        prev = run.status
        run.status = "waiting"
        self.hub.run_changed(run)
        self.hub.add_feed("warn", f"Needs your OK: {run.title}", spoken, run.id)
        waiters: set[asyncio.Future] = {approval.future}
        voice_task = None
        if run is self.conv and self.voice_on:
            voice_task = asyncio.create_task(self._voice_confirm(spoken))
            waiters.add(voice_task)
        elif run is self.conv and not self.hub.clients and not self.voice_on:
            voice_task = asyncio.create_task(self._terminal_confirm())
            waiters.add(voice_task)
        else:
            self.notify(f"Needs your OK: {run.title}", spoken)
            if run is not self.conv:
                self.announce(f"{run.title} needs your approval on the dashboard.")
        approved, how = False, ""
        try:
            done, _ = await asyncio.wait(waiters, timeout=APPROVAL_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
            if approval.future in done:
                approved, how = approval.future.result()
                if how == "dashboard":
                    how = f"{'approved' if approved else 'declined'} in the dashboard"
            elif voice_task in done:
                approved, how = voice_task.result()
        finally:
            if voice_task and not voice_task.done():
                voice_task.cancel()
                if self.mouth:
                    self.mouth.stop()
            # Close the dashboard dialog with the real answer (no-op if it was answered there).
            self.hub.resolve_approval(approval.id, approved, how or "no answer")
            run.status = "working" if prev in ("waiting", "idle", "queued") else prev
            if run is self.conv:
                run.status = "working" if self.brain.busy else "idle"
            self.hub.run_changed(run)
        print(f"{CYAN}Answer:{END} {how or '(no answer)'}")
        return approved, how

    async def _voice_confirm(self, spoken: str) -> tuple[bool, str]:
        await self.mouth.wait_idle()
        self._hushed = False  # a question always gets asked out loud
        prev = self.state
        await self.mouth.say_and_wait(f"{spoken} Shall I go ahead?")
        self.state = "listening"
        heard = ""
        for _ in range(2):  # one retry if it hears nothing
            heard = await self.ears.listen(8) or ""
            if heard:
                break
            await self.mouth.say_and_wait("Sorry, was that a yes?")
        self.state = prev
        if not heard and self.hub.clients:
            await asyncio.Event().wait()  # no spoken answer: leave it to the dashboard button
        return is_yes(heard), heard

    async def _terminal_confirm(self) -> tuple[bool, str]:
        answer = await asyncio.to_thread(input, f"{YELLOW}Go ahead? [y/N] {END}")
        return answer.strip().lower() in ("y", "yes"), answer.strip()

    # -- terminal modes -------------------------------------------------------------------
    async def run_voice(self) -> None:
        await self.start()
        print(f"{BOLD}{config.NAME} is listening.{END} Start with her name: “{config.NAME}, open Safari.” "
              f"Say “{config.NAME}, stop” to interrupt her. Ctrl+C to quit.")
        self.mouth.say(f"Hi, I'm {config.NAME}. I'm here whenever you need me.")
        await asyncio.Event().wait()  # everything else is driven by what the ears hear

    async def run_text(self) -> None:
        await self.start()
        print(f"{BOLD}{config.NAME} (text mode).{END} Type a request; 'exit' to quit, 'new' for a fresh conversation.")
        while True:
            try:
                text = (await asyncio.to_thread(input, f"\n{CYAN}You:{END} ")).strip()
            except EOFError:
                break
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                break
            if text.lower() == "new":
                text = "new conversation"
            self.hub.add_chat("user", text, "chat")
            await self._handle(text)
            await self.mouth.wait_idle()
        await self.shutdown()
