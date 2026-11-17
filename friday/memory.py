"""Things Friday remembers about you ("I prefer short emails", "my advisor is Dr. Lee").

Saved in data/memory.json, editable from the dashboard, and given to the conversation agent
at the start of every session as information about you (never as instructions).
"""

import json
import time

from . import config
from .hub import Hub, new_id

MAX_NOTES = 200


class Memory:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.path = config.DATA / "memory.json"
        try:
            self.items: list[dict] = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.items = []

    def texts(self) -> list[str]:
        return [m["text"] for m in self.items]

    def add(self, text: str, source: str = "dashboard") -> dict:
        text = " ".join(str(text).split())[:300]
        if not text:
            raise ValueError("nothing to remember")
        for m in self.items:
            if m["text"].lower() == text.lower():
                return m
        item = {"id": new_id("mem"), "text": text, "created": time.time(), "source": source}
        self.items.append(item)
        del self.items[:-MAX_NOTES]
        self._changed()
        return item

    def remove(self, mid: str) -> dict:
        for m in self.items:
            if m["id"] == mid:
                self.items.remove(m)
                self._changed()
                return m
        raise KeyError(f"no note with id {mid}")

    def _changed(self) -> None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=2))
        tmp.replace(self.path)
        self.hub.emit("memory", self.items)
