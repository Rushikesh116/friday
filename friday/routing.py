"""Choose a model before a turn starts. Keep this deterministic and easy to inspect."""

import re
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    effort: str
    level: str


HARD = re.compile(
    r"\b(debug|refactor|architect|implement|build|migrate|investigate|prove|"
    r"security|audit|multi[- ]?step|entire|complex|thorough|deep|codebase|"
    r"compare .{0,45} (options|approaches)|analyse|analyze)\b", re.I,
)
QUICK = re.compile(
    r"\b(what time|what date|define|spell|translate|convert|open|launch|"
    r"remind me|say hello|hello|hi|thanks|thank you|weather)\b", re.I,
)
NATIVE_REQUEST = re.compile(
    r"\b(mail|email|inbox|calendar|schedule|remind|remember|forget|briefing|research|"
    r"browser|website|webpage|open|launch|background task)\b", re.I,
)


def requires_claude(prompt: str) -> bool:
    """Friday's in-process Mac and dashboard tools currently live in Claude SDK."""
    return bool(NATIVE_REQUEST.search(prompt))


def choose(prompt: str, settings: dict, kind: str = "conversation") -> Route:
    provider = settings.get("provider", "claude")
    if settings.get("model_mode", "auto") == "manual":
        return Route(provider, settings.get("codex_model", "gpt-5.6-sol") if provider == "codex"
                     else config.MODEL, settings.get("effort", config.EFFORT), "manual")

    words = len(prompt.split())
    if kind == "research" or HARD.search(prompt) or words > 90:
        level, effort = "hard", "max"
    elif words <= 15 and QUICK.search(prompt):
        level, effort = "quick", "low"
    else:
        level, effort = "standard", "medium"
    if provider == "codex":
        model = "gpt-5.6-luna" if level == "quick" else settings.get("codex_model", "gpt-5.6-sol")
    else:
        model = {"quick": "claude-haiku-5-5", "standard": "claude-sonnet-5-5",
                 "hard": config.MODEL}[level]
    return Route(provider, model, effort, level)
