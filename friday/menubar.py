"""Menu-bar icon: shows what Friday is doing and gives you a few controls.

AppKit must own the main thread, so the asyncio side runs in a background thread and
the menu polls its state a few times a second (UI calls stay on the main thread).
"""

import asyncio
import threading

import rumps

from . import config
from .core import Friday

ICONS = {
    "starting": "◌", "idle": "◉", "listening": "●", "thinking": "◐", "speaking": "◑",
}
LABELS = {
    "starting": "Starting…", "idle": f"Say “{config.NAME}, …”", "listening": "Listening…",
    "thinking": "Working…", "speaking": "Speaking…",
}


def run_menubar(muted: bool = False) -> None:
    loop = asyncio.new_event_loop()
    holder: dict[str, Friday] = {}

    def background() -> None:
        asyncio.set_event_loop(loop)
        holder["j"] = Friday(voice_input=True, muted=muted)
        try:
            loop.run_until_complete(holder["j"].run_voice())
        except Exception as e:  # surface startup failures in the menu
            holder["error"] = str(e)  # type: ignore[assignment]

    threading.Thread(target=background, daemon=True).start()

    class App(rumps.App):
        def __init__(self):
            super().__init__(config.NAME, title=ICONS["starting"], quit_button=None)
            self.status = rumps.MenuItem(LABELS["starting"])
            self.mute = rumps.MenuItem("Mute voice", callback=self.toggle_mute)
            self.mute.state = muted
            self.menu = [
                self.status, None,
                rumps.MenuItem("Talk now", callback=self.talk),
                rumps.MenuItem("Stop", callback=self.stop),
                rumps.MenuItem("New conversation", callback=self.new_convo),
                self.mute, None,
                rumps.MenuItem(f"Quit {config.NAME}", callback=self.quit),
            ]

        @rumps.timer(0.25)
        def refresh(self, _):
            if "error" in holder:
                self.title, self.status.title = "⚠︎", f"Error: {holder['error']}"[:80]
                return
            j = holder.get("j")
            state = j.state if j else "starting"
            self.title = ICONS.get(state, "◉")
            self.status.title = LABELS.get(state, state)

        def _call(self, coro_fn):
            j = holder.get("j")
            if j:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coro_fn(j)))

        def talk(self, _):
            j = holder.get("j")
            if j:
                loop.call_soon_threadsafe(j.talk_now)

        def stop(self, _):
            self._call(lambda j: j._take_over())

        def new_convo(self, _):
            self._call(lambda j: j.brain.reset())

        def toggle_mute(self, item):
            item.state = not item.state
            j = holder.get("j")
            if j:
                j.mouth.muted = bool(item.state)
                if item.state:
                    loop.call_soon_threadsafe(j.mouth.stop)

        def quit(self, _):
            j = holder.get("j")
            if j:
                fut = asyncio.run_coroutine_threadsafe(j.shutdown(), loop)
                try:
                    fut.result(timeout=5)
                except Exception:
                    pass
            rumps.quit_application()

    App().run()
