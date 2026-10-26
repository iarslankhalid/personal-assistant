"""Small persistent state in data/state.json: things the assistant learns while running.

Right now that's one thing: your WhatsApp id (the agent's "creator"). The bot learns it the first
time you message the agent, and `assistant notify` needs it to know where to send.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self._data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    @property
    def creator(self) -> str | None:
        value = self._data.get("creator")
        return value if isinstance(value, str) and value else None

    @creator.setter
    def creator(self, value: str | None) -> None:
        if value != self.creator:
            self._data["creator"] = value
            self.save()

    def save(self) -> None:
        """Write to a temp file, then swap it in, so a crash can't leave half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
