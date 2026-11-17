"""Launch Friday with her dashboard: a native macOS window (or your browser).

AppKit needs the main thread for the window, so Friday, the system monitor and the web
server run on an asyncio loop in a background thread.

Opened as Friday.app (FRIDAY_APP=1) she behaves like a normal Mac app: closing the window
just hides it while she keeps listening, clicking her Dock icon brings it back, and Quit
(⌘Q or the Dock menu) shuts everything down. Started from Terminal, closing the window quits.
"""

import asyncio
import atexit
import os
import signal
import subprocess
import sys
import threading
import webbrowser

from aiohttp import web

from . import config, server
from .core import BOLD, DIM, END, RED, Friday
from .server import make_app
from .sysmon import SysMon

APP_MODE = os.environ.get("FRIDAY_APP") == "1"
LOG_FILE = "~/Library/Logs/Friday/friday.log"


def alert(title: str, message: str) -> None:
    """Show a dialog (the app has no terminal for error messages). Blocks until dismissed."""
    script = "on run argv\n display alert (item 1 of argv) message (item 2 of argv) as critical\nend run"
    try:
        subprocess.run(["osascript", "-e", script, title, message], timeout=600, check=False)
    except Exception:
        pass


def run_dashboard(voice: bool = True, muted: bool = False, browser: bool = False, window: bool = True) -> None:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box: dict = {}
    url = f"http://127.0.0.1:{config.PORT}/"

    async def main() -> None:
        stop = asyncio.Event()
        box["stop"] = stop
        f = Friday(voice_input=voice, muted=muted)
        mon = SysMon(f)
        runner = web.AppRunner(make_app(f, mon), access_log=None)
        await runner.setup()
        try:
            # Short shutdown timeout so quitting never hangs on an open dashboard connection.
            await web.TCPSite(runner, "127.0.0.1", config.PORT, shutdown_timeout=2.0).start()
        except OSError:
            where = "from Terminal (./friday.sh)" if APP_MODE else "in another window or as the app"
            box["error"] = (f"{config.NAME} seems to be running already {where}: port {config.PORT} is in use. "
                            f"Quit that copy first, then open {config.NAME} again.")
            ready.set()
            return
        # Also listen on the Tailscale IP so your phone (on the same tailnet) can open the dashboard.
        if ts_ip := await asyncio.to_thread(config.tailscale_ip):
            try:
                await web.TCPSite(runner, ts_ip, config.PORT, shutdown_timeout=2.0).start()
                server.REMOTE_NAMES.add(ts_ip)
                print(f"{DIM}Also on your tailnet: http://{config.REMOTE_HOSTS[0] if config.REMOTE_HOSTS else ts_ip}"
                      f":{config.PORT}/  (or http://{ts_ip}:{config.PORT}/){END}")
            except OSError as e:
                print(f"{DIM}Couldn't listen on Tailscale address {ts_ip}: {e}{END}")
        box["friday"] = f
        ready.set()
        mon_task = asyncio.create_task(mon.run())
        try:
            await f.start()
            if f.voice_on:
                f.mouth.say(f"Hi, I'm {config.NAME}. I'm here whenever you need me.")
            print(f"{BOLD}{config.NAME} is running.{END} Dashboard: {url}")
            await stop.wait()
        finally:
            mon_task.cancel()
            await f.shutdown()
            for ws in list(f.hub.clients):  # say goodbye to open dashboards so cleanup is quick
                try:
                    await asyncio.wait_for(ws.close(), 1)
                except Exception:
                    pass
            await runner.cleanup()

    def background() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(main())
        except Exception as e:
            box["error"] = f"{type(e).__name__}: {e}"
            first_failure = not ready.is_set()
            ready.set()
            print(f"{RED}{config.NAME} stopped:{END} {e}")
            if APP_MODE and not first_failure:
                alert(f"{config.NAME} stopped unexpectedly", f"{e}\n\nDetails are in {LOG_FILE}.")

    thread = threading.Thread(target=background, name="friday-core", daemon=True)
    thread.start()
    ready.wait(30)
    if "error" in box:
        print(f"{RED}{box['error']}{END}")
        if APP_MODE:
            alert(f"{config.NAME} can't start", box["error"])
        sys.exit(1)

    finished = threading.Event()

    def shut_down(*_args) -> None:
        """Stop Friday cleanly (agents, speech server, audio). Safe to call more than once."""
        if finished.is_set():
            return
        finished.set()
        if "stop" in box and not loop.is_closed():
            loop.call_soon_threadsafe(box["stop"].set)
        thread.join(timeout=12)
        print(f"{config.NAME} stopped{'' if not thread.is_alive() else ' (forced: shutdown took too long)'}.")

    atexit.register(shut_down)  # also covers exits that skip the normal path

    if browser or not window:
        if browser:
            webbrowser.open(url)
        print(DIM + f"Dashboard at {url} — press Ctrl+C to quit." + END)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        shut_down()
        return

    try:
        import webview
    except ImportError:
        print(DIM + "pywebview isn't installed; opening the dashboard in your browser." + END)
        return _browser_fallback(url, thread, shut_down)

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    win = webview.create_window(config.NAME, url, width=1480, height=930, min_size=(1100, 720),
                                background_color="#040a14", text_select=True)
    _install_app_delegate(win, shut_down, hide_on_close=APP_MODE)
    win.events.closed += shut_down  # the window really closing (Terminal mode) stops Friday
    webview.start()
    shut_down()


def _install_app_delegate(win, shut_down, hide_on_close: bool) -> None:
    """Make every way of quitting (⌘Q, the Dock's Quit, Ctrl+C in Terminal, logout) run Friday's
    shutdown first: AppKit ends the process the moment quitting is approved. As the Mac app,
    closing the window only hides it and clicking the Dock icon shows it again."""
    import objc
    from AppKit import NSApp
    from PyObjCTools import MachSignals
    from webview.platforms import cocoa

    quitting = threading.Event()
    base = cocoa.BrowserView.AppDelegate

    class FridayAppDelegate(base):
        def applicationShouldTerminate_(self, app):
            quitting.set()
            reply = objc.super(FridayAppDelegate, self).applicationShouldTerminate_(app)
            if reply:
                # AppKit exits the process as soon as this returns, so stop everything now.
                shut_down()
            else:
                quitting.clear()
            return reply

        @objc.typedSelector(b"Z@:@Z")
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
            if not has_visible:
                win.show()
            return True

    cocoa.BrowserView.AppDelegate = FridayAppDelegate  # pywebview creates the delegate from this class
    # `kill <pid>` (used by make-app.sh to update a running copy) quits through the same path.
    # (pywebview routes Ctrl+C to the same "terminate" itself.)
    MachSignals.signal(signal.SIGTERM, lambda _signum: NSApp().terminate_(None))

    if hide_on_close:
        def on_closing():
            if quitting.is_set():
                return True  # quitting: let the window close
            win.hide()
            return False  # just closing the window: keep Friday running in the background

        win.events.closing += on_closing


def _browser_fallback(url: str, thread: threading.Thread, shut_down) -> None:
    webbrowser.open(url)
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    shut_down()
