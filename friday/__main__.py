"""Run Friday.

    python -m friday              # the dashboard app (voice on)
    python -m friday --browser    # the dashboard in your web browser instead of its own window
    python -m friday --no-voice   # the dashboard without microphone or speech
    python -m friday --server     # run in the background without a window (open the printed URL)
    python -m friday --terminal   # voice only, in this terminal (no dashboard)
    python -m friday --menubar    # voice with a menu-bar icon (no dashboard)
    python -m friday --text       # type in this terminal instead of talking
"""

import argparse
import asyncio
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path

from . import config


def _log_to_file() -> Path:
    """Opened as Friday.app there's no terminal, so everything Friday (and the tools she
    starts) prints goes to ~/Library/Logs/Friday/friday.log instead."""
    log_dir = Path.home() / "Library" / "Logs" / "Friday"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "friday.log"
    if log.exists() and log.stat().st_size > 5_000_000:
        log.replace(log_dir / "friday.log.1")  # keep one older log
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = open(1, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    sys.stderr = open(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    print(f"\n===== {config.NAME} starting {datetime.now():%Y-%m-%d %H:%M:%S} =====")
    import faulthandler

    faulthandler.enable(sys.stderr)
    return log


def main() -> None:
    if os.environ.get("FRIDAY_APP") == "1" and not sys.stdout.isatty():
        _log_to_file()
        try:
            _main()
        except SystemExit:
            raise
        except BaseException as e:  # never vanish silently: tell the user and leave a trace in the log
            import traceback

            traceback.print_exc()
            from .app import LOG_FILE, alert

            alert(f"{config.NAME} hit a problem", f"{type(e).__name__}: {e}\n\nDetails are in {LOG_FILE}.")
            raise SystemExit(1)
        return
    _main()


def _main() -> None:
    p = argparse.ArgumentParser(prog="friday", description="Voice assistant for your Mac, powered by Claude.")
    p.add_argument("--browser", action="store_true", help="open the dashboard in your browser")
    p.add_argument("--no-voice", action="store_true", help="dashboard without microphone or speech")
    p.add_argument("--server", action="store_true", help="run without opening a window (open the printed URL)")
    p.add_argument("--terminal", action="store_true", help="voice mode in this terminal, no dashboard")
    p.add_argument("--menubar", action="store_true", help="voice mode with a menu-bar icon, no dashboard")
    p.add_argument("--text", action="store_true", help="type requests in this terminal instead of speaking")
    p.add_argument("--mute", action="store_true", help="don't speak replies out loud")
    args = p.parse_args()

    if args.menubar:
        from .menubar import run_menubar

        run_menubar(muted=args.mute)
        return

    if args.terminal or args.text:
        from .core import Friday

        friday = Friday(voice_input=not args.text, muted=args.mute)

        async def go() -> None:
            try:
                await (friday.run_text() if args.text else friday.run_voice())
            finally:
                with contextlib.suppress(Exception):
                    await friday.shutdown()

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(go())
        print(f"\n{config.NAME} offline.")
        return

    from .app import run_dashboard

    run_dashboard(voice=not args.no_voice, muted=args.mute, browser=args.browser, window=not args.server)
    print(f"\n{config.NAME} offline.")


if __name__ == "__main__":
    main()
