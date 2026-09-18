"""Lane memory: which lanes are closed, and until when.

A lane that refused with a known reset time (a spent plan quota, a Codex usage
limit) or an empty balance is closed on disk, so the next delegation skips it
without spending a call to hear the same refusal. The file is
<session_root>/lane_state.json:

    {"glm": {"closed_until": "2026-09-18T15:00:00+07:00", "code": "zai_1308",
             "message": "...", "set_at": "2026-09-18T10:12:03+07:00"}}

Entries whose closed_until has passed are ignored. A file that cannot be read
or parsed is treated as empty and logged; lane memory never fails a run.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import log

FILENAME = "lane_state.json"


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


class LaneState:
    """The lane_state.json file under one session root. Thread-safe."""

    def __init__(self, session_root: Path):
        self.path = Path(session_root) / FILENAME
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            log.warning("lane state unreadable, treated as empty: %s", self.path, exc_info=True)
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("lane state is not JSON, treated as empty: %s", self.path)
            return {}
        if not isinstance(data, dict):
            log.warning("lane state is not a JSON object, treated as empty: %s", self.path)
            return {}
        return data

    def closed(self, lane: str, now: datetime | None = None) -> dict[str, Any] | None:
        """The entry closing `lane`, or None when it is open.

        The returned dict carries closed_until as a datetime.
        """
        with self._lock:
            entry = self._load().get(lane)
        if not isinstance(entry, dict):
            return None
        until = _parse(entry.get("closed_until"))
        if until is None or until <= (now or _now()):
            return None
        return {**entry, "closed_until": until}

    def close(self, lane: str, until: datetime, code: str, message: str) -> None:
        """Close `lane` until `until`. Written atomically; errors are logged."""
        with self._lock:
            data = self._load()
            data[lane] = {
                "closed_until": until.isoformat(),
                "code": code,
                "message": message,
                "set_at": _now().isoformat(),
            }
            tmp = self.path.with_name(f".{FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                log.warning("could not write lane state %s", self.path, exc_info=True)
