"""Persistent lane health, usage and capacity snapshots.

The status file is deliberately separate from ``lane_state.json``. Lane state
contains authoritative quota closures; this file keeps observations from runs
and probes. Updates are atomic and serialized across threads and processes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import inspect
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import health
from .config import log
from .lane_state import LaneState
from .router import clip
from .secrets import redact_secrets

FILENAME = "lane_status.json"
DETAIL_LIMIT = 300
LOCK_TIMEOUT_SECONDS = 0.75
FUTURE_SKEW_SECONDS = 3600


def _datetime(value: datetime | str | float | int | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _iso(value: datetime | str | float | int | None = None) -> str:
    return _datetime(value).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _datetime(value)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _reason(code: str | None, detail: str | None, fallback: str) -> str:
    parts = [part.strip() for part in (code, detail) if isinstance(part, str) and part.strip()]
    return ": ".join(parts) if parts else fallback


def _safe_detail(value: Any, redact_keys: tuple[str, ...] = ()) -> str | None:
    if value is None:
        return None
    text = redact_secrets(str(value), redact_keys)
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    return clip(text, DETAIL_LIMIT)


def _safe_json(value: Any, redact_keys: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        return _safe_detail(value, redact_keys)
    if isinstance(value, list):
        return [_safe_json(item, redact_keys) for item in value]
    if isinstance(value, dict):
        return {
            _safe_detail(str(key), redact_keys): _safe_json(item, redact_keys)
            for key, item in value.items()
        }
    return value


def _refusal_closes(finish_reason: str, code: str | None, detail: str | None) -> bool:
    if finish_reason.lower() == "auth":
        return True
    if finish_reason.lower() != "refused":
        return False
    value = f"{code or ''} {detail or ''}".lower()
    return any(term in value for term in (
        "auth", "401", "403", "api key", "unauthorized", "usage limit", "quota",
        "balance", "1308", "1310", "1313_exhausted", "402", "codex_usage_limit",
        "deepseek_balance",
    ))


class LaneStatusStore:
    """The ``lane_status.json`` store for one shared session root."""

    def __init__(self, session_root: Path, *, redact_keys: tuple[str, ...] = ()):
        self.path = Path(session_root) / FILENAME
        self._thread_lock = threading.Lock()
        self.redact_keys = tuple(redact_keys)
        self.lock_timeout = LOCK_TIMEOUT_SECONDS
        self.probe_timeout = 30.0

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            log.warning("lane status unreadable; update skipped: %s", self.path, exc_info=True)
            raise
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("lane status is not JSON, treated as empty: %s", self.path)
            return {}
        if not isinstance(value, dict):
            log.warning("lane status is not a JSON object, treated as empty: %s", self.path)
            return {}
        return value

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        acquired = self._thread_lock.acquire(timeout=self.lock_timeout)
        if not acquired:
            raise TimeoutError("timed out acquiring lane status thread lock")
        handle = None
        deadline = time.monotonic() + self.lock_timeout
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path.with_name(FILENAME + ".lock"), "a")  # noqa: SIM115
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "timed out acquiring lane status file lock"
                        ) from exc
                    time.sleep(0.02)
            yield
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
            self._thread_lock.release()

    @staticmethod
    def _lease_path(path: Path, kind: str, lane: str) -> Path:
        token = hashlib.sha256(lane.encode("utf-8")).hexdigest()[:24]
        return path.with_name(f".{FILENAME}.{kind}.{token}.lock")

    @contextlib.contextmanager
    def probe_lease(self, lane: str) -> Iterator[bool]:
        """Claim one stale lane across processes, unless a run currently owns it."""
        probe_handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            probe_handle = open(  # noqa: SIM115 - held for the cross-process lease
                self._lease_path(self.path, "probe", lane), "a"
            )
            fcntl.flock(probe_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if probe_handle is not None:
                probe_handle.close()
            yield False
            return
        busy = self._active_count_locked(lane) > 0
        if busy:
            probe_handle.close()
            yield False
            return
        try:
            yield True
        finally:
            probe_handle.close()

    def _active_count_locked(self, lane: str) -> int:
        """Count live shared active leases; caller holds this lane's probe lock."""
        token = hashlib.sha256(lane.encode("utf-8")).hexdigest()[:24]
        pattern = f".{FILENAME}.active.{token}.*.lock"
        count = 0
        for path in self.path.parent.glob(pattern):
            try:
                handle = open(path, "a")  # noqa: SIM115 - probe active lock state
            except OSError:
                continue
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                count += 1
            finally:
                handle.close()
        return count

    def active_count(self, lane: str) -> int:
        """Best-effort non-blocking count of this lane's runs in all processes."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return self._active_count_locked(lane)
        except OSError:
            return 0

    @contextlib.contextmanager
    def active_run(self, lane: str) -> Iterator[None]:
        """Hold a shared lane lease so another process cannot prompt-probe it."""
        probe_handle = active_handle = None
        active_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            probe_handle = open(  # noqa: SIM115 - held for the cross-process lease
                self._lease_path(self.path, "probe", lane), "a"
            )
            deadline = time.monotonic() + self.probe_timeout + 5.0
            while True:
                try:
                    fcntl.flock(probe_handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "timed out waiting for lane probe to finish"
                        ) from exc
                    time.sleep(0.05)
            active_path = self._lease_path(self.path, "active", lane)
            active_path = active_path.with_name(
                f".{FILENAME}.active.{hashlib.sha256(lane.encode('utf-8')).hexdigest()[:24]}"
                f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:12]}.lock"
            )
            active_handle = open(active_path, "a")  # noqa: SIM115 - held for the lane run
            fcntl.flock(active_handle, fcntl.LOCK_SH)
            yield
        finally:
            if active_handle is not None:
                active_handle.close()
                if active_path is not None:
                    with contextlib.suppress(OSError):
                        active_path.unlink(missing_ok=True)
            if probe_handle is not None:
                probe_handle.close()

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_name(
            f".{FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        fd = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump(value, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError):
            log.warning("could not write lane status %s", self.path, exc_info=True)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _update(self, lane: str, update: Callable[[dict[str, Any]], None]) -> None:
        self._update_many({lane: update})

    def _update_many(self, updates: Mapping[str, Callable[[dict[str, Any]], None]]) -> None:
        try:
            with self._locked():
                data = self._load()
                for lane, update in updates.items():
                    entry = data.get(lane)
                    entry = {} if not isinstance(entry, dict) else dict(entry)
                    update(entry)
                    data[lane] = entry
                self._write(_safe_json(data, self.redact_keys))
        except (OSError, TimeoutError):
            log.warning("lane status update skipped after lock/read failure: %s", self.path)

    def harness_record(self, lane: str) -> dict[str, Any]:
        """Return stored harness observations without probing the provider."""
        try:
            record = self._load().get(lane)
        except OSError:
            return {}
        return dict(record) if isinstance(record, dict) else {}

    def record_harness_version(self, lane: str, version: str | None) -> None:
        """Persist the last detected harness version for read-only status views."""
        self._update(lane, lambda entry: entry.update(harness_version=version))

    def record_harness_previous(self, lane: str, version: str | None) -> None:
        """Remember the version present before an operator-triggered update."""
        self._update(lane, lambda entry: entry.update(previous_harness_version=version))

    def record_harness_check(
        self, lane: str, result: Mapping[str, Any], *, version: str | None
    ) -> None:
        """Persist one update check and the version observed alongside it."""
        safe_result = _safe_json(dict(result), self.redact_keys)

        def update(entry: dict[str, Any]) -> None:
            entry["harness_version"] = version
            entry["harness_check"] = safe_result

        self._update(lane, update)

    @staticmethod
    def _observed_time(previous: Any, observed: datetime) -> datetime | None:
        checked = _parse_time(previous)
        if checked is not None and (checked - observed).total_seconds() > FUTURE_SKEW_SECONDS:
            return None
        return checked

    def _run_update(
        self, finish_reason: str, code: str | None, detail: str | None, observed: datetime
    ) -> Callable[[dict[str, Any]], None]:
        safe_detail = _safe_detail(detail, self.redact_keys)
        safe_code = _safe_detail(code, self.redact_keys)
        reason_text = _safe_detail(_reason(safe_code, safe_detail, finish_reason), self.redact_keys)
        success = finish_reason in ("completed", "completed_unverified")
        closed = _refusal_closes(finish_reason, code, detail)

        def update(entry: dict[str, Any]) -> None:
            previous = self._observed_time(entry.get("checked_at"), observed)
            if previous is not None and observed < previous:
                return
            if success or closed:
                entry["checked_at"] = observed.isoformat()
            if success:
                entry.update(state="open", source="run", reason=None)
            elif closed:
                entry.update(state="closed", source="run", reason=reason_text)
            else:
                entry.setdefault("state", "unknown")
                entry["reason"] = reason_text

        return update

    def record_observations(self, observations: list[Mapping[str, Any]]) -> None:
        """Apply several run/probe/usage observations with one locked rewrite."""
        if not observations:
            return
        first = observations[0]
        kind = first.get("kind")
        lane = first.get("lane")
        if not isinstance(lane, str):
            self._apply_observations(observations)
            return
        if kind == "run":
            self.record_run(
                lane, str(first.get("finish_reason") or "error"),
                code=first.get("code"), detail=first.get("detail"), at=first.get("at"),
                _observations=observations,
            )
        elif kind == "probe":
            self.record_probe(
                lane, bool(first.get("ok")), detail=first.get("detail"), at=first.get("at"),
                _observations=observations,
            )
        elif kind == "usage":
            usage = first.get("usage")
            if isinstance(usage, dict):
                self.record_usage(lane, usage, at=first.get("at"), _observations=observations)
            else:
                self._apply_observations(observations)
        else:
            self._apply_observations(observations)

    def _apply_observations(self, observations: list[Mapping[str, Any]]) -> None:
        if not observations:
            return
        by_lane: dict[str, list[Mapping[str, Any]]] = {}
        for observation in observations:
            lane = observation.get("lane")
            if isinstance(lane, str) and lane:
                by_lane.setdefault(lane, []).append(observation)
        updates: dict[str, Callable[[dict[str, Any]], None]] = {}
        for lane, lane_observations in by_lane.items():
            def apply(entry: dict[str, Any], rows=lane_observations) -> None:
                for row in rows:
                    observed = _datetime(row.get("at"))
                    kind = row.get("kind")
                    if kind == "run":
                        self._run_update(
                            str(row.get("finish_reason") or "error"),
                            row.get("code") if isinstance(row.get("code"), str) else None,
                            row.get("detail") if isinstance(row.get("detail"), str) else None,
                            observed,
                        )(entry)
                    elif kind == "probe":
                        previous = self._observed_time(entry.get("checked_at"), observed)
                        if previous is not None and observed < previous:
                            continue
                        ok = bool(row.get("ok"))
                        detail = _safe_detail(row.get("detail"), self.redact_keys)
                        entry.update(
                            checked_at=observed.isoformat(), state="open" if ok else "closed",
                            source="probe", reason=None if ok else (detail or "probe failed"),
                        )
                    elif kind == "usage":
                        previous = self._observed_time(entry.get("usage_at"), observed)
                        if previous is not None and observed < previous:
                            continue
                        usage = row.get("usage")
                        if isinstance(usage, dict):
                            entry["usage"] = _safe_json(usage, self.redact_keys)
                            entry["usage_at"] = observed.isoformat()
            updates[lane] = apply
        self._update_many(updates)

    def record_run(
        self,
        lane: str,
        finish_reason: str,
        *,
        code: str | None = None,
        detail: str | None = None,
        at: datetime | str | float | int | None = None,
        _observations: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """Record a completed run or its latest non-successful observation."""
        observed = _datetime(at)
        self._apply_observations(_observations or [{
            "lane": lane, "kind": "run", "finish_reason": finish_reason,
            "code": code, "detail": detail, "at": observed,
        }])

    def record_probe(
        self,
        lane: str,
        ok: bool,
        *,
        detail: str | None = None,
        at: datetime | str | float | int | None = None,
        _observations: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """Record the outcome of a health or prompt probe."""
        observed = _datetime(at)

        self._apply_observations(_observations or [{
            "lane": lane, "kind": "probe", "ok": ok, "detail": detail, "at": observed,
        }])

    def record_usage(
        self,
        lane: str,
        usage: dict[str, Any],
        *,
        at: datetime | str | float | int | None = None,
        _observations: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """Attach usage data without changing the lane's health state."""
        observed = _datetime(at)

        self._apply_observations(_observations or [{
            "lane": lane, "kind": "usage", "usage": usage, "at": observed,
        }])

    def snapshot(
        self,
        settings: Any,
        *,
        now: datetime | str | float | int | None = None,
        live_by_lane: Mapping[str, int] | None = None,
        count_runs: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Return one stored status view for every configured provider.

        ``count_runs`` is for callers that do not need the pre-dispatch
        envelope readings: plain lane listings and harness views pass False
        when ``[envelope] lane_plan`` is unset, so they never scan the trace
        just to fill a field nothing reads.
        """
        current = _datetime(now)
        try:
            records = self._load()
        except OSError:
            records = {}
        live = live_by_lane or {}
        ttl = max(0.0, float(getattr(settings, "lane_status_ttl_seconds", 1800)))
        lane_state = LaneState(settings.session_root)
        closures, verified_versions = lane_state.status_entries(now=current)
        spend = _month_spend(settings, current)
        runs = _run_counts(settings, current) if count_runs else None
        result: dict[str, dict[str, Any]] = {}
        for name, cfg in settings.providers.items():
            entry = records.get(name)
            if not isinstance(entry, dict):
                entry = {}
            checked = self._observed_time(entry.get("checked_at"), current)
            state = entry.get("state")
            source = entry.get("source")
            reason = entry.get("reason")
            closed_until = None
            closure = closures.get(name)
            if closure is not None:
                state = "closed"
                source = "closure"
                checked = checked or _parse_time(closure.get("set_at"))
                until = closure.get("closed_until")
                closed_until = until.isoformat() if isinstance(until, datetime) else None
                reason = _reason(closure.get("code"), closure.get("message"), "lane is closed")
            if state not in ("open", "closed", "unknown"):
                state = "unknown"
            if source not in ("run", "probe", "closure"):
                source = None
            reason = _safe_detail(reason, self.redact_keys) if isinstance(reason, str) else None
            count = live.get(name, 0)
            count = max(0, int(count)) if isinstance(count, (int, float)) else 0
            count = max(count, self.active_count(name))
            limit = getattr(cfg, "max_agents", None) or settings.max_agents
            cap = getattr(cfg, "extra", {}).get("monthly_usd_cap")
            cap = (
                float(cap)
                if isinstance(cap, (int, float)) and not isinstance(cap, bool)
                else None
            )
            harness_version = entry.get("harness_version")
            if not isinstance(harness_version, str) or not harness_version:
                harness_version = None
            verified_version = verified_versions.get(name)
            harness_state = (
                "unknown" if harness_version is None else
                "verified" if harness_version == verified_version else "unverified"
            )
            result[name] = {
                "state": state,
                "checked_at": checked.isoformat() if checked else None,
                "source": source,
                "reason": reason,
                "closed_until": closed_until,
                "stale": False if closure is not None else (
                    checked is None
                    or (checked - current).total_seconds() > FUTURE_SKEW_SECONDS
                    or (current - checked).total_seconds() > ttl
                ),
                "usage": _safe_json(entry.get("usage"), self.redact_keys)
                if isinstance(entry.get("usage"), dict) else None,
                "slots": {"limit": int(limit), "live": count,
                          "free": max(int(limit) - count, 0)},
                "spend": {"month_usd": spend.get(name), "cap_usd": cap},
                "runs": (
                    runs.get(name, {"day": 0, "week": 0}) if runs is not None
                    else {"day": None, "week": None}
                ),
                "local": bool(getattr(cfg, "local", False)),
                "driver": getattr(cfg, "driver", None),
                "harness_version": harness_version,
                "harness_state": harness_state,
            }
        return result


def lanes_to_probe(snapshot: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return stale, idle, non-local lanes for the inexpensive prompt probe."""
    return [
        name
        for name, view in snapshot.items()
        if view.get("stale") is True
        and view.get("source") != "closure"
        and view.get("closed_until") is None
        and view.get("local") is not True
        and isinstance(view.get("slots"), Mapping)
        and int(view["slots"].get("live", 0)) == 0
    ]


def codex_usage(codex_home: Path) -> dict[str, Any] | None:
    """Read a recent Codex rate-limit event, preferring newer session days."""
    root = Path(codex_home) / "sessions"
    try:
        days = sorted((path for path in root.glob("*/*/*") if path.is_dir()), reverse=True)
    except OSError:
        return None
    for day in days:
        try:
            files = sorted(
                day.glob("*.jsonl"), key=lambda path: path.stat().st_mtime_ns, reverse=True
            )
        except OSError:
            continue
        for path in files:
            try:
                size = path.stat().st_size
                with path.open("rb") as handle:
                    handle.seek(max(0, size - 256 * 1024))
                    lines = handle.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if '"rate_limits"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                primary = limits.get("primary") if isinstance(limits, dict) else None
                percent = primary.get("used_percent") if isinstance(primary, dict) else None
                if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                    continue
                resets = primary.get("resets_at")
                try:
                    resets_at = datetime.fromtimestamp(float(resets), UTC).isoformat()
                except (TypeError, ValueError, OSError, OverflowError):
                    resets_at = None
                observed = record.get("timestamp")
                try:
                    observed_at = (
                        _iso(observed) if observed is not None else _iso(path.stat().st_mtime)
                    )
                except (TypeError, ValueError, OSError, OverflowError):
                    observed_at = _iso(path.stat().st_mtime)
                return {
                    "kind": "percent",
                    "used_percent": float(percent),
                    "resets_at": resets_at,
                    "observed_at": observed_at,
                }
    return None


def deepseek_balance(payload: Any) -> dict[str, Any] | None:
    """Reduce the DeepSeek balance response to stable usage fields."""
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get("balance_infos")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    try:
        raw_balance = row["total_balance"]
        if isinstance(raw_balance, bool):
            return None
        balance = float(raw_balance)
    except (KeyError, TypeError, ValueError):
        return None
    currency = row.get("currency")
    if not isinstance(currency, str):
        return None
    return {
        "kind": "balance",
        "balance": balance,
        "currency": currency,
        "available": bool(payload.get("is_available")) and balance > 0,
    }


def omlx_usage(payload: Any) -> dict[str, Any] | None:
    """Reduce an oMLX ``/api/status`` object to loaded-model information."""
    if not isinstance(payload, Mapping) or payload.get("status") != "ok":
        return None
    loaded = payload.get("loaded_models")
    loaded_models = [str(model) for model in loaded] if isinstance(loaded, list) else []
    count = payload.get("models_loaded")
    if isinstance(count, bool) or not isinstance(count, int):
        count = len(loaded_models)
    return {"kind": "local", "models_loaded": count, "loaded_models": loaded_models}


def probe_stale_lanes(
    settings: Any,
    store: LaneStatusStore,
    prompt_turn: Callable[[Any, Any], Mapping[str, Any]],
    *,
    live_by_lane: Mapping[str, int] | None = None,
    stop: threading.Event | None = None,
) -> dict[str, dict[str, Any]]:
    """Refresh stale idle lanes, prompting remote providers and health-checking local ones."""
    store.redact_keys = tuple(dict.fromkeys((
        *store.redact_keys, *getattr(settings, "leaked_keys", ()),
    )))
    # Envelope run counts are only consumed by the pre-dispatch check. Keep
    # plain lane probing from scanning the trace when the check is off.
    count_runs = getattr(settings, "envelope_lane_plan", None) is not None
    before = store.snapshot(settings, live_by_lane=live_by_lane, count_runs=count_runs)
    remote = set(lanes_to_probe(before))
    local = {
        name for name, view in before.items()
        if view.get("local") is True and view.get("stale") is True
        and view.get("slots", {}).get("live", 0) == 0
    }
    for name, cfg in settings.providers.items():
        if stop is not None and stop.is_set():
            break
        if name not in remote and name not in local:
            continue
        if name in local and cfg.health.kind == "none":
            continue
        if not _probe_allowed(cfg):
            continue
        with store.probe_lease(name) as leased:
            if not leased or (stop is not None and stop.is_set()):
                continue
            latest = store.snapshot(
                settings, live_by_lane=live_by_lane, count_runs=count_runs,
            ).get(name, {})
            slots = latest.get("slots", {})
            if (latest.get("stale") is not True or latest.get("source") == "closure"
                    or latest.get("closed_until") is not None
                    or int(slots.get("live", 0)) > 0):
                continue
            if name in local:
                _probe_local(cfg, store, name)
                continue
            timeout = min(float(cfg.run_timeout), store.probe_timeout)
            probe_cfg = dataclasses.replace(cfg, run_timeout=timeout)
            probe_providers = dict(settings.providers)
            probe_providers[name] = probe_cfg
            probe_settings = dataclasses.replace(
                settings, trace="off", providers=probe_providers,
            )
            try:
                params = inspect.signature(prompt_turn).parameters
                if stop is not None and "stop" in params:
                    outcome = prompt_turn(probe_settings, probe_cfg, stop=stop)
                else:
                    outcome = prompt_turn(probe_settings, probe_cfg)
                if stop is not None and stop.is_set():
                    continue
                ok = bool(outcome.get("ok"))
                detail = outcome.get("error") or outcome.get("state")
            except Exception as exc:  # noqa: BLE001 - a probe failure is a lane observation
                if stop is not None and stop.is_set():
                    continue
                ok, detail = False, str(exc)
                outcome = {}
            store.record_probe(name, ok, detail=str(detail) if detail else None)
            usage = outcome.get("usage") if isinstance(outcome, Mapping) else None
            if cfg.driver == "codex" and not isinstance(usage, dict):
                from .providers.codex import source_codex_home

                usage = codex_usage(source_codex_home())
            if isinstance(usage, dict):
                store.record_usage(name, usage)
    return store.snapshot(settings, live_by_lane=live_by_lane, count_runs=count_runs)


def _probe_allowed(cfg: Any) -> bool:
    """Keep automatic probes away from unavailable, login, or unknown-driver lanes."""
    if cfg.driver == "epi" or cfg.extra.get("auth") == "login":
        return False
    if cfg.unavailable() is not None:
        return False
    try:
        from .providers import for_driver

        for_driver(cfg.driver)
    except (KeyError, ImportError):
        return False
    return True


def _probe_local(cfg: Any, store: LaneStatusStore, name: str) -> tuple[bool, str | None]:
    """Check a local endpoint without ever starting a model prompt."""
    detail = None
    try:
        if cfg.health.kind == "omlx" and cfg.health.url:
            key = cfg.api_key()
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            found = health._http_get(cfg.health.url, headers)
            if found is None:
                ok = False
                detail = f"{name}: status endpoint did not answer"
            elif found[0] != 200:
                ok = False
                detail = f"{name}: status endpoint answered HTTP {found[0]}"
            else:
                try:
                    payload = json.loads(found[1].decode("utf-8", "replace"))
                except (TypeError, ValueError):
                    payload = None
                usage = omlx_usage(payload)
                ok = usage is not None
                detail = None if ok else f"{name}: status is not ok"
                if usage is not None:
                    store.record_usage(name, usage)
        else:
            status = health.check(cfg)
            ok, detail = status.ok, status.message or None
    except Exception as exc:  # noqa: BLE001 - record a health failure, do not stop other lanes
        ok, detail = False, str(exc)
    store.record_probe(name, ok, detail=detail)
    return ok, detail


_SPEND_CACHE_LOCK = threading.Lock()
_SPEND_CACHE: dict[str, dict[str, Any]] = {}


def _spend_record(line: bytes, providers: Mapping[str, Any], now: datetime,
                  totals: dict[str, float]) -> None:
    if b'"kind"' not in line or b'"run"' not in line:
        return
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return
    if not isinstance(record, dict) or record.get("kind") != "run":
        return
    lane = record.get("lane")
    if lane not in providers:
        return
    try:
        event_at = datetime.fromtimestamp(float(record.get("ts")), now.tzinfo)
    except (TypeError, ValueError, OSError, OverflowError):
        return
    if (event_at.year, event_at.month) != (now.year, now.month):
        return
    cost = record.get("cost")
    amount = cost.get("provider_usd") if isinstance(cost, dict) else None
    if amount is None and record.get("native_usage") is True:
        amount = record.get("cash_usd")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        totals[lane] = totals.get(lane, 0.0) + float(amount)


_RUN_CACHE_LOCK = threading.Lock()
_RUN_CACHE: dict[str, dict[str, Any]] = {}
_RUN_CHUNK_BYTES = 256 * 1024
_RUN_DAY_SECONDS = 86400.0
_RUN_WEEK_SECONDS = 7 * 86400.0


def _run_event(line: bytes) -> tuple[float, str] | None:
    """(timestamp, lane) for one run record, or None for anything else."""
    if b'"kind"' not in line or b'"run"' not in line:
        return None
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("kind") != "run":
        return None
    if record.get("refusal_code") == "envelope":
        # Refused at admission by the envelope: it never ran, so it does not
        # use up the lane's run caps.
        return None
    lane = record.get("lane")
    if not isinstance(lane, str):
        return None
    try:
        return float(record.get("ts")), lane
    except (TypeError, ValueError):
        return None


def _run_age(ts: float, now: datetime) -> float | None:
    """Seconds since a run record, or None when its timestamp is unusable."""
    try:
        event_at = datetime.fromtimestamp(ts, now.tzinfo)
        return (now - event_at).total_seconds()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _run_is_recent(ts: float, now: datetime) -> bool:
    """Whether a run event belongs in the seven-day cache window."""
    age = _run_age(ts, now)
    return age is not None and 0 <= age <= _RUN_WEEK_SECONDS


def _run_counts(settings: Any, now: datetime) -> dict[str, dict[str, int]] | None:
    """Run records per lane in the last day and week, from a cached trace tail.

    Only the last seven days of ``run`` events are kept, so the cache and the
    per-snapshot loop stay bounded no matter how long the trace grows. The
    trace is tailed in bounded chunks; an incomplete last line is carried to
    the next read instead of re-reading the whole file. None means the trace is
    off or unreadable; an empty mapping means it was read and holds no recent
    run records.
    """
    raw_path = settings.trace
    if raw_path is None:
        path: Path | None = Path(settings.session_root) / "trace.jsonl"
    elif str(raw_path).strip().lower() in ("off", "0", "false", ""):
        path = None
    else:
        path = Path(raw_path).expanduser()
    if path is None:
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    key = str(path.resolve())
    try:
        with _RUN_CACHE_LOCK:
            cached = _RUN_CACHE.get(key)
            reset = (
                cached is None
                or cached["device"] != info.st_dev
                or cached["inode"] != info.st_ino
                or info.st_size < cached["seen_size"]
                or (info.st_size == cached["seen_size"]
                    and info.st_mtime_ns != cached["mtime_ns"])
            )
            if reset:
                cached = {
                    "device": info.st_dev, "inode": info.st_ino, "offset": 0,
                    "seen_size": 0, "mtime_ns": 0, "pending": b"", "events": [],
                }
            with path.open("rb") as handle:
                # ``offset`` is the start of the incomplete line carried from
                # the previous read; seek there and rebuild it from disk
                # rather than prepending the cached copy as well.
                handle.seek(cached["offset"])
                pending = b""
                while True:
                    chunk = handle.read(_RUN_CHUNK_BYTES)
                    if not chunk:
                        break
                    lines = (pending + chunk).splitlines(keepends=True)
                    pending = b""
                    if lines and not lines[-1].endswith((b"\n", b"\r")):
                        pending = lines.pop()
                    for line in lines:
                        event = _run_event(line)
                        if event is not None and _run_is_recent(event[0], now):
                            cached["events"].append(event)
                    cached["offset"] = handle.tell() - len(pending)
                cached["seen_size"] = handle.tell()
                cached["pending"] = pending
                cached["mtime_ns"] = os.fstat(handle.fileno()).st_mtime_ns
            # Prune on read as well as on append: a snapshot later than the
            # one that filled the cache must not keep older events around.
            cached["events"] = [
                event for event in cached["events"] if _run_is_recent(event[0], now)
            ]
            _RUN_CACHE[key] = cached
            events = list(cached["events"])
    except OSError:
        return None
    counts: dict[str, dict[str, int]] = {}
    for ts, lane in events:
        age = _run_age(ts, now)
        if age is None or age < 0 or age > _RUN_WEEK_SECONDS:
            continue
        entry = counts.setdefault(lane, {"day": 0, "week": 0})
        if age <= _RUN_DAY_SECONDS:
            entry["day"] += 1
        if age <= _RUN_WEEK_SECONDS:
            entry["week"] += 1
    return counts


def _month_spend(settings: Any, now: datetime) -> dict[str, float | None]:
    """Read current-month lane costs, tailing only bytes added since last snapshot."""
    providers = settings.providers
    priced: dict[str, bool] = {name: _has_price(cfg) for name, cfg in providers.items()}
    raw_path = settings.trace
    if raw_path is None:
        path = Path(settings.session_root) / "trace.jsonl"
    elif str(raw_path).strip().lower() in ("off", "0", "false", ""):
        path = None
    else:
        path = Path(raw_path).expanduser()
    totals: dict[str, float] = {}
    if path is not None:
        key = str(path.resolve())
        month = (now.year, now.month)
        try:
            info = path.stat()
            with _SPEND_CACHE_LOCK:
                cached = _SPEND_CACHE.get(key)
                reset = (
                    cached is None
                    or cached["month"] != month
                    or cached["device"] != info.st_dev
                    or cached["inode"] != info.st_ino
                    or info.st_size < cached["offset"]
                    or (info.st_size == cached["seen_size"]
                        and info.st_mtime_ns != cached["mtime_ns"])
                )
                if reset:
                    cached = {
                        "month": month, "device": info.st_dev, "inode": info.st_ino,
                        "offset": 0, "seen_size": 0, "mtime_ns": 0,
                        "pending": b"", "totals": {},
                    }
                with path.open("rb") as handle:
                    handle.seek(cached["offset"])
                    chunk = handle.read()
                raw = cached["pending"] + chunk
                lines = raw.splitlines(keepends=True)
                pending = b""
                if lines and not lines[-1].endswith((b"\n", b"\r")):
                    pending = lines.pop()
                for line in lines:
                    _spend_record(line, providers, now, cached["totals"])
                cached["offset"] = info.st_size - len(pending)
                cached["seen_size"] = info.st_size
                cached["mtime_ns"] = info.st_mtime_ns
                cached["pending"] = pending
                _SPEND_CACHE[key] = cached
                totals = dict(cached["totals"])
        except OSError:
            pass
    return {
        name: totals[name] if name in totals else (0.0 if priced[name] else None)
        for name in providers
    }


def _has_price(cfg: Any) -> bool:
    """Whether configured pricing contains enough information to price a run."""
    kind = cfg.pricing.kind
    values = cfg.pricing.values
    fields = {
        "per_token": ("input", "output", "cache_read"),
        "credits": ("usd_per_credit",),
        "local": ("usd",),
    }.get(kind)
    if fields is None:
        return False
    for field in fields:
        value = values.get(field)
        try:
            if value is None or float(value) < 0:
                return False
        except (TypeError, ValueError):
            return False
    return True
