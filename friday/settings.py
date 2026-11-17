"""Settings you change from the dashboard, saved to data/settings.json.

Values start from config.py (and FRIDAY_* env vars); anything you set in the dashboard
is saved and wins from then on. Voice, speed, volume and barge-in apply immediately;
model effort applies from the next conversation.
"""

import json
import re
import shutil
from typing import Any

from . import config

EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULTS: dict[str, Any] = {
    "power": True,              # mic + speech on
    "listen_mode": "always",    # "always" (her name wakes her) or "ptt" (push to talk)
    "voice_replies": True,      # speak replies (off = text only)
    "voice": config.VOICE,
    "rate": config.RATE,
    "volume": config.VOLUME,
    "effort": config.EFFORT,
    "provider": "claude",
    "model_mode": "auto",
    "codex_model": "gpt-5.6-sol",
    "barge_in": config.BARGE_IN,
    "schedules_enabled": True,
    "max_parallel": config.MAX_PARALLEL,
    "city": "",                 # for the weather widget; empty = no weather requests
    "units": "celsius",
}


def _validate(key: str, value: Any) -> Any:
    if key in ("power", "voice_replies", "barge_in", "schedules_enabled"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if key == "listen_mode":
        if value not in ("always", "ptt"):
            raise ValueError("listen_mode must be 'always' or 'ptt'")
        return value
    if key == "voice":
        resolved = config.resolve_voice(str(value))
        if not resolved:
            raise ValueError(f"voice '{value}' is not installed")
        return resolved
    if key == "rate":
        v = int(value)
        if not 120 <= v <= 260:
            raise ValueError("rate must be 120-260 words per minute")
        return v
    if key == "volume":
        v = float(value)
        if not 0.0 <= v <= 1.0:
            raise ValueError("volume must be between 0 and 1")
        return round(v, 2)
    if key == "effort":
        if value not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
        return value
    if key == "provider":
        if value not in ("claude", "codex"):
            raise ValueError("provider must be claude or codex")
        if value == "codex" and not shutil.which("codex"):
            raise ValueError("Codex CLI is not installed. Install it and run codex login first.")
        return value
    if key == "model_mode":
        if value not in ("auto", "manual"):
            raise ValueError("model mode must be auto or manual")
        return value
    if key == "codex_model":
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,79}", value):
            raise ValueError("enter a valid Codex model ID")
        return value
    if key == "max_parallel":
        v = int(value)
        if not 1 <= v <= 6:
            raise ValueError("max_parallel must be 1-6")
        return v
    if key == "city":
        v = str(value).strip()
        if len(v) > 80:
            raise ValueError("city name is too long")
        return v
    if key == "units":
        if value not in ("celsius", "fahrenheit"):
            raise ValueError("units must be celsius or fahrenheit")
        return value
    raise KeyError(f"unknown setting '{key}'")


class Settings:
    def __init__(self):
        self.path = config.DATA / "settings.json"
        self.values = dict(DEFAULTS)
        try:
            saved = json.loads(self.path.read_text())
            for k, v in saved.items():
                try:
                    self.values[k] = _validate(k, v)
                except (KeyError, ValueError, TypeError):
                    pass  # ignore stale or invalid entries
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.apply()

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def set(self, key: str, value: Any) -> Any:
        v = _validate(key, value)
        self.values[key] = v
        self.save()
        self.apply()
        return v

    def save(self) -> None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, indent=2))
        tmp.replace(self.path)

    def apply(self) -> None:
        """Push the live-tunable values into config, which the rest of the app reads."""
        config.VOICE = self.values["voice"]
        config.RATE = self.values["rate"]
        config.VOLUME = self.values["volume"]
        config.EFFORT = self.values["effort"]
        config.BARGE_IN = self.values["barge_in"]
        config.MAX_PARALLEL = self.values["max_parallel"]
