"""All tunable settings in one place. Override any of them with env vars (FRIDAY_*)."""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
WEB = Path(__file__).resolve().parent / "web"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FRIDAY_{name}", default)


# Settings, schedules, memory, task history and research briefs live here.
DATA = Path(_env("DATA", str(ROOT / "data"))).expanduser()

# --- Identity --------------------------------------------------------------
# Her name is also the wake word ("Friday, open Safari"). Any name works, since it's
# recognized by speech-to-text rather than a fixed wake-word model.
NAME = _env("NAME", "Friday")

# --- Brain -----------------------------------------------------------------
MODEL = _env("MODEL", "claude-opus-5-5")
# low = fastest replies, high = more careful on big tasks (coding, research)
EFFORT = _env("EFFORT", "medium")
# Where the agent starts. It can still reach any path you name.
WORKDIR = Path(_env("WORKDIR", str(Path.home()))).expanduser()
CODEX_WORKDIR = Path(_env("CODEX_WORKDIR", str(Path.home() / "Projects"))).expanduser()
# Writes/edits inside these folders run without asking; anywhere else asks first.
SAFE_WRITE_DIRS = [
    Path(p).expanduser()
    for p in _env("SAFE_WRITE_DIRS", "~/Projects:~/Desktop:~/Documents/Friday").split(":")
]
PLAYWRIGHT_MCP = _env("PLAYWRIGHT_MCP", "@playwright/mcp@0.0.85")
# Background tasks (from the dashboard, schedules, research) that may run at once.
MAX_PARALLEL = int(_env("MAX_PARALLEL", "3"))
# Subagents (Task/Agent tool) one request may spawn, counting nested ones. Past runs without a
# cap fanned out to 150-290 subagents and used up the whole Claude plan limit.
MAX_SUBAGENTS = int(_env("MAX_SUBAGENTS", "10"))
# Spending cap for one background task, in USD of model usage. 0 = no cap.
TASK_BUDGET_USD = float(_env("TASK_BUDGET_USD", "10"))
# Largest single message read from the Claude CLI. The SDK default (1 MB) failed on big tool results.
MAX_BUFFER_BYTES = int(_env("MAX_BUFFER_MB", "32")) * 1024 * 1024

# --- Dashboard -------------------------------------------------------------
PORT = int(_env("PORT", "8765"))  # served on 127.0.0.1 only
# Remote access over Tailscale (your own devices only). When on, the dashboard also listens on
# this Mac's Tailscale IP, and accepts these names for it (MagicDNS short and full names, plus
# the https name `tailscale serve` uses). FRIDAY_TAILSCALE=0 turns it off.
TAILSCALE = _env("TAILSCALE", "1") == "1"
REMOTE_HOSTS = tuple(h.strip().lower() for h in _env(
    # Default: this Mac's own short hostname. Set FRIDAY_REMOTE_HOSTS to add the full MagicDNS name.
    "REMOTE_HOSTS", __import__("socket").gethostname().split(".")[0]).split(",") if h.strip())


def tailscale_ip() -> str | None:
    """This Mac's Tailscale IPv4 (100.x.y.z), or None if Tailscale isn't installed or running."""
    if not TAILSCALE:
        return None
    if ip := _env("TAILNET_IP", ""):
        return ip
    for exe in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if not os.path.exists(exe):
            continue
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4).stdout.strip()
        except Exception:
            continue
        if out.startswith("100."):
            return out.splitlines()[0]
    return None

# --- Audio -----------------------------------------------------------------
IO_RATE = 48_000  # speaker + mic stream (echo cancellation runs at this rate)
SAMPLE_RATE = 16_000  # what speech recognition gets
FRAME = 1280  # 80 ms at 16 kHz
# Echo cancellation lets her hear "stop" over her own voice on the laptop speakers.
ECHO_CANCEL = _env("ECHO_CANCEL", "1") == "1"
# While she's talking and hears you start to speak, she drops to this volume.
DUCK_VOLUME = float(_env("DUCK_VOLUME", "0.3"))
# Talking over her makes her stop and listen, even without her name. Set to 0 if other
# people or a TV in the room keep cutting her off (then only "Friday…" interrupts her).
BARGE_IN = _env("BARGE_IN", "1") == "1"

# --- Ears ------------------------------------------------------------------
WHISPER_MODEL = Path(_env("WHISPER_MODEL", str(MODELS / "ggml-small.en.bin")))
WHISPER_PORT = int(_env("WHISPER_PORT", "8178"))
# Endpointing: how long a pause ends your sentence, and safety caps.
SILENCE_END_S = float(_env("SILENCE_END_S", "0.9"))
WAIT_FOR_SPEECH_S = float(_env("WAIT_FOR_SPEECH_S", "6"))
MAX_UTTERANCE_S = float(_env("MAX_UTTERANCE_S", "40"))
# After she answers, keep listening this long for a follow-up without saying her name.
FOLLOW_UP_S = float(_env("FOLLOW_UP_S", "5"))
# Minimum loudness (int16 RMS) that counts as speech, on top of the adaptive noise floor.
# Echo cancellation leaves a very quiet signal (~25) and also softens your voice while she
# talks, so this sits just above that floor. Raise it if a noisy room keeps waking her.
MIN_SPEECH_RMS = float(_env("MIN_SPEECH_RMS", "80"))

# --- Voice -----------------------------------------------------------------
# Her voice: Tara (Indian English), chosen by the user. A higher-quality Tara is used if you
# download one (System Settings > Accessibility > Spoken Content > System Voice > Manage
# Voices); the others are fallbacks for a Mac without Tara. FRIDAY_VOICE overrides all of it.
VOICE_PREFERENCE = [
    "Tara (Premium)", "Tara (Enhanced)", "Tara",
    "Ava (Premium)", "Zoe (Premium)", "Samantha",
]
RATE = int(_env("RATE", "172"))  # words per minute; a touch slower sounds calmer
VOLUME = float(_env("VOLUME", "0.75"))  # 0-1, relative to the system volume
CHIME = "/System/Library/Sounds/Tink.aiff"
STOP_CHIME = "/System/Library/Sounds/Pop.aiff"


def list_voices() -> list[tuple[str, str]]:
    """Installed `say` voices as (name, locale), e.g. ("Tara", "en_IN")."""
    try:
        listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    # Lines look like "Samantha (English (US))   en_US  # Hello…"; the name is before the locale.
    out, seen = [], set()
    for line in listing.splitlines():
        if (m := re.match(r"^(.*?)\s+([a-z]{2,3}_[A-Za-z0-9]+)\s+#", line)) and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), m.group(2)))
    return out


def resolve_voice(name: str) -> str | None:
    """The installed voice matching `name`. macOS renames voices between releases
    ("Tara" vs "Tara (English (India))"), so a saved name also matches by its base name."""
    installed = [n for n, _ in list_voices()]
    if name in installed:
        return name
    base = str(name).split(" (")[0].strip().lower()
    wanted_quality = next((q for q in ("Premium", "Enhanced") if q in str(name)), None)
    matches = [n for n in installed if n.split(" (")[0].strip().lower() == base]
    for n in matches:
        if (wanted_quality or "") in n:
            return n
    return matches[0] if matches else None


def _pick_voice() -> str:
    if v := os.environ.get("FRIDAY_VOICE"):
        return v
    installed = [name for name, _ in list_voices()]
    if not installed:
        return "Samantha"
    for want in VOICE_PREFERENCE:
        base, _, quality = want.partition(" (")
        quality = quality.rstrip(")")
        for name in installed:
            if (name == base or name.startswith(base + " (")) and (not quality or quality in name):
                return name
    return "Samantha"


VOICE = _pick_voice()
