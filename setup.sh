#!/bin/zsh
# Rebuild Friday's environment from scratch (safe to run again).
set -e
cd "$(dirname "$0")"
command -v whisper-server >/dev/null || brew install whisper-cpp
command -v uv >/dev/null || brew install uv
[ -d .venv ] || uv venv -p 3.12 .venv
uv pip install -q -p .venv/bin/python -r requirements.txt
mkdir -p models
[ -f models/ggml-small.en.bin ] || curl -L --fail -o models/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
echo "Ready. Sign in with 'claude auth login' if you haven't, then run ./friday.sh"
