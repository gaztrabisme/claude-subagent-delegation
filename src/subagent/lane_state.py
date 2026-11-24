"""Lane memory: which lanes are closed, and until when.

A lane that refused with a known reset time (a spent plan quota, a Codex usage
limit) or an empty balance is closed on disk, so the next delegation skips it
without spending a call to hear the same refusal. The file is
<session_root>/lane_state.json:

    {"glm": {"closed_until": "2026-09-18T15:00:00+07:00", "code": "zai_1308",
             "message": "...", "set_at": "2026-09-18T10:12:03+07:00"}}

Entries whose closed_until has passed are ignored, and no entry keeps a lane
closed more than 7 days past its set_at, whatever it says. reopen() clears a
lane's entry (scripts/reopen_lane.py is its command line). A file that cannot
be read or parsed is treated as empty and logged; lane memory never fails a
run. Read-modify-write holds an fcntl lock on <file>.lock, so two server
processes on one session root cannot lose each other's closures.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import log

FILENAME = "lane_state.json"
# The longest an entry keeps a lane closed (router.MAX_CLOSE).
MAX_CLOSE = timedelta(days=7)


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

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """This process's thread lock plus an flock shared with other processes."""
        with self._lock:
            handle = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.path.with_name(FILENAME + ".lock"), "a")  # noqa: SIM115
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                log.warning("could not lock lane state %s", self.path, exc_info=True)
            try:
                yield
            finally:
                if handle is not None:
                    handle.close()  # releases the flock

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not write lane state %s", self.path, exc_info=True)

    def closed(self, lane: str, now: datetime | None = None) -> dict[str, Any] | None:
        """The entry closing `lane`, or None when it is open.

        The returned dict carries closed_until as a datetime, capped at
        MAX_CLOSE after the entry's set_at.
        """
        with self._lock:
            entry = self._load().get(lane)
        if not isinstance(entry, dict):
            return None
        until = _parse(entry.get("closed_until"))
        if until is None:
            return None
        set_at = _parse(entry.get("set_at"))
        cap = (set_at or (now or _now())) + MAX_CLOSE
        until = min(until, cap)
        if until <= (now or _now()):
            return None
        return {**entry, "closed_until": until}

    def closed_entries(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Return all active closures from one read of the shared state file."""
        current = now or _now()
        with self._lock:
            data = self._load()
        result: dict[str, dict[str, Any]] = {}
        for lane, entry in data.items():
            if not isinstance(entry, dict):
                continue
            until = _parse(entry.get("closed_until"))
            if until is None:
                continue
            set_at = _parse(entry.get("set_at"))
            until = min(until, (set_at or current) + MAX_CLOSE)
            if until > current:
                result[lane] = {**entry, "closed_until": until}
        return result

    def status_entries(
        self, now: datetime | None = None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Return active closures and verified versions from one state read."""
        current = now or _now()
        with self._lock:
            data = self._load()
        closures: dict[str, dict[str, Any]] = {}
        verified: dict[str, str] = {}
        for lane, entry in data.items():
            if not isinstance(entry, dict):
                continue
            version = entry.get("verified_version")
            if isinstance(version, str) and version:
                verified[lane] = version
            until = _parse(entry.get("closed_until"))
            if until is None:
                continue
            set_at = _parse(entry.get("set_at"))
            until = min(until, (set_at or current) + MAX_CLOSE)
            if until > current:
                closures[lane] = {**entry, "closed_until": until}
        return closures, verified

    def close(self, lane: str, until: datetime, code: str, message: str) -> None:
        """Close `lane` until `until` (at most MAX_CLOSE). Written atomically;
        errors are logged."""
        now = _now()
        until = min(until, now + MAX_CLOSE)
        with self._locked():
            data = self._load()
            entry = data.get(lane)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry.update({
                "closed_until": until.isoformat(),
                "code": code,
                "message": message,
                "set_at": now.isoformat(),
            })
            data[lane] = entry
            self._write(data)

    def record_verified(self, lane: str, version: str) -> None:
        """Remember the harness version that passed conformance checks."""
        if not isinstance(version, str) or not version:
            return
        with self._locked():
            data = self._load()
            entry = data.get(lane)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry["verified_version"] = version
            data[lane] = entry
            self._write(data)

    def verified_version(self, lane: str) -> str | None:
        """The last verified harness version for `lane`, if recorded."""
        with self._lock:
            entry = self._load().get(lane)
        if not isinstance(entry, dict):
            return None
        version = entry.get("verified_version")
        return version if isinstance(version, str) and version else None

    def reopen(self, lane: str) -> dict[str, Any] | None:
        """Clear `lane`'s closure while preserving its other lane memory."""
        with self._locked():
            data = self._load()
            entry = data.get(lane)
            if not isinstance(entry, dict):
                return None
            previous = dict(entry)
            closure = {key: entry[key] for key in (
                "closed_until", "code", "message", "set_at"
            ) if key in entry}
            if not closure:
                return None
            updated = dict(entry)
            for key in closure:
                updated.pop(key, None)
            if updated:
                data[lane] = updated
            else:
                data.pop(lane, None)
            self._write(data)
        return previous

    def entries(self) -> dict[str, Any]:
        """Every entry on disk, open or not, as stored."""
        with self._lock:
            return self._load()
