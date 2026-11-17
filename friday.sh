#!/bin/zsh
# Start Friday (opens the dashboard). Options: --browser, --no-voice, --server, --terminal, --menubar, --text (see README).
cd "$(dirname "$0")" && exec .venv/bin/python -m friday "$@"
