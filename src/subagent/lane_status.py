"""Persistent lane health, usage and capacity snapshots.

The status file is deliberately separate from ``lane_state.json``. Lane state
contains authoritative quota closures; this file keeps observations from runs
and probes. Updates are atomic and serialized across threads and processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import health
from .config import log
from .lane_state import LaneState

FILENAME = "lane_status.json"


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


def _refusal_closes(finish_reason: str, code: str | None, detail: str | None) -> bool:
    if finish_reason.lower() == "auth":
        return True
    if finish_reason.lower() != "refused":
        return False
    value = f"{code or ''} {detail or ''}".lower()
    return any(term in value for term in (
        "auth", "401", "403", "api key", "unauthorized", "usage limit", "quota",
        "balance", "1308", "402", "codex_usage_limit", "deepseek_balance",
    ))


class LaneStatusStore:
    """The ``lane_status.json`` store for one shared session root."""

    def __init__(self, session_root: Path):
        self.path = Path(session_root) / FILENAME
        self._thread_lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            log.warning("lane status unreadable, treated as empty: %s", self.path)
            return {}
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
        with self._thread_lock:
            handle = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.path.with_name(FILENAME + ".lock"), "a")  # noqa: SIM115
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                log.warning("could not lock lane status %s", self.path, exc_info=True)
            try:
                yield
            finally:
                if handle is not None:
                    handle.close()

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_name(
            f".{FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError):
            log.warning("could not write lane status %s", self.path, exc_info=True)
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _update(self, lane: str, update: Callable[[dict[str, Any]], None]) -> None:
        with self._locked():
            data = self._load()
            entry = data.get(lane)
            entry = {} if not isinstance(entry, dict) else dict(entry)
            update(entry)
            data[lane] = entry
            self._write(data)

    def record_run(
        self,
        lane: str,
        finish_reason: str,
        *,
        code: str | None = None,
        detail: str | None = None,
        at: datetime | str | float | int | None = None,
    ) -> None:
        """Record a completed run or its latest non-successful observation."""
        observed = _datetime(at)
        reason_text = _reason(code, detail, finish_reason)
        success = finish_reason in ("completed", "completed_unverified")
        closed = _refusal_closes(finish_reason, code, detail)

        def update(entry: dict[str, Any]) -> None:
            previous = _parse_time(entry.get("checked_at"))
            if previous is not None and observed < previous:
                return
            entry["checked_at"] = observed.isoformat()
            if success:
                entry.update(state="open", source="run", reason=None)
            elif closed:
                entry.update(state="closed", source="run", reason=reason_text)
            else:
                entry.setdefault("state", "unknown")
                entry["reason"] = reason_text

        self._update(lane, update)

    def record_probe(
        self,
        lane: str,
        ok: bool,
        *,
        detail: str | None = None,
        at: datetime | str | float | int | None = None,
    ) -> None:
        """Record the outcome of a health or prompt probe."""
        observed = _datetime(at)

        def update(entry: dict[str, Any]) -> None:
            previous = _parse_time(entry.get("checked_at"))
            if previous is not None and observed < previous:
                return
            entry.update(
                checked_at=observed.isoformat(),
                state="open" if ok else "closed",
                source="probe",
                reason=None if ok else (detail or "probe failed"),
            )

        self._update(lane, update)

    def record_usage(
        self,
        lane: str,
        usage: dict[str, Any],
        *,
        at: datetime | str | float | int | None = None,
    ) -> None:
        """Attach usage data without changing the lane's health state."""
        observed = _datetime(at)

        def update(entry: dict[str, Any]) -> None:
            previous = _parse_time(entry.get("usage_at"))
            if previous is not None and observed < previous:
                return
            entry["usage"] = usage
            entry["usage_at"] = observed.isoformat()

        self._update(lane, update)

    def snapshot(
        self,
        settings: Any,
        *,
        now: datetime | str | float | int | None = None,
        live_by_lane: Mapping[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return one stored status view for every configured provider."""
        current = _datetime(now)
        records = self._load()
        live = live_by_lane or {}
        ttl = max(0.0, float(getattr(settings, "lane_status_ttl_seconds", 1800)))
        closures = LaneState(settings.session_root)
        spend = _month_spend(settings, current)
        result: dict[str, dict[str, Any]] = {}
        for name, cfg in settings.providers.items():
            entry = records.get(name)
            if not isinstance(entry, dict):
                entry = {}
            checked = _parse_time(entry.get("checked_at"))
            state = entry.get("state")
            source = entry.get("source")
            reason = entry.get("reason")
            closed_until = None
            closure = closures.closed(name, now=current)
            if closure is not None:
                state = "closed"
                source = "closure"
                checked = _parse_time(closure.get("set_at")) or checked
                until = closure.get("closed_until")
                closed_until = until.isoformat() if isinstance(until, datetime) else None
                reason = _reason(closure.get("code"), closure.get("message"), "lane is closed")
            if state not in ("open", "closed", "unknown"):
                state = "unknown"
            if source not in ("run", "probe", "closure"):
                source = None
            reason = reason if isinstance(reason, str) else None
            count = live.get(name, 0)
            count = max(0, int(count)) if isinstance(count, (int, float)) else 0
            limit = getattr(cfg, "max_agents", None) or settings.max_agents
            cap = getattr(cfg, "extra", {}).get("monthly_usd_cap")
            cap = (
                float(cap)
                if isinstance(cap, (int, float)) and not isinstance(cap, bool)
                else None
            )
            result[name] = {
                "state": state,
                "checked_at": checked.isoformat() if checked else None,
                "source": source,
                "reason": reason,
                "closed_until": closed_until,
                "stale": checked is None or (current - checked).total_seconds() > ttl,
                "usage": entry.get("usage") if isinstance(entry.get("usage"), dict) else None,
                "slots": {"limit": int(limit), "live": count,
                          "free": max(int(limit) - count, 0)},
                "spend": {"month_usd": spend.get(name), "cap_usd": cap},
                "local": bool(getattr(cfg, "local", False)),
                "driver": getattr(cfg, "driver", None),
            }
        return result


def lanes_to_probe(snapshot: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return stale, idle, non-local lanes for the inexpensive prompt probe."""
    return [
        name
        for name, view in snapshot.items()
        if view.get("stale") is True
        and view.get("local") is not True
        and isinstance(view.get("slots"), Mapping)
        and int(view["slots"].get("live", 0)) == 0
    ]


def codex_usage(codex_home: Path) -> dict[str, Any] | None:
    """Read the latest Codex rate-limit event from the newest session JSONL."""
    root = Path(codex_home) / "sessions"
    try:
        files = sorted(root.glob("*/*/*/*.jsonl"), key=lambda path: path.stat().st_mtime_ns,
                       reverse=True)
    except OSError:
        return None
    if not files:
        return None
    try:
        handle = files[0].open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    latest: dict[str, Any] | None = None
    with handle:
        for line in handle:
            # Session text is ignored; only decode the rate-limit event lines.
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
            if not isinstance(primary, dict) or not isinstance(
                primary.get("used_percent"), (int, float)
            ):
                continue
            resets = primary.get("resets_at")
            try:
                resets_at = datetime.fromtimestamp(float(resets), UTC).isoformat()
            except (TypeError, ValueError, OSError, OverflowError):
                resets_at = None
            observed = record.get("timestamp")
            try:
                observed_at = (
                    _iso(observed) if observed is not None else _iso(files[0].stat().st_mtime)
                )
            except (TypeError, ValueError, OSError, OverflowError):
                observed_at = _iso(files[0].stat().st_mtime)
            latest = {
                "kind": "percent",
                "used_percent": float(primary["used_percent"]),
                "resets_at": resets_at,
                "observed_at": observed_at,
            }
    return latest


def deepseek_balance(payload: Any) -> dict[str, Any] | None:
    """Reduce the DeepSeek balance response to stable usage fields."""
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get("balance_infos")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    try:
        balance = float(row["total_balance"])
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
) -> dict[str, dict[str, Any]]:
    """Refresh stale idle lanes, prompting remote providers and health-checking local ones."""
    before = store.snapshot(settings, live_by_lane=live_by_lane)
    remote = set(lanes_to_probe(before))
    local = {
        name for name, view in before.items()
        if view.get("local") is True and view.get("stale") is True
        and view.get("slots", {}).get("live", 0) == 0
    }
    for name, cfg in settings.providers.items():
        if name not in remote and name not in local:
            continue
        if name in local:
            ok, detail = _probe_local(cfg, store, name)
        else:
            try:
                outcome = prompt_turn(settings, cfg)
                ok = bool(outcome.get("ok"))
                detail = outcome.get("error") or outcome.get("state")
            except Exception as exc:  # noqa: BLE001 - a probe failure is a lane observation
                ok, detail = False, str(exc)
            store.record_probe(name, ok, detail=str(detail) if detail else None)
            if cfg.driver == "codex":
                from .providers.codex import source_codex_home

                usage = codex_usage(source_codex_home())
                if usage is not None:
                    store.record_usage(name, usage)
    return store.snapshot(settings, live_by_lane=live_by_lane)


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


def _month_spend(settings: Any, now: datetime) -> dict[str, float | None]:
    """Read lane costs in the current calendar month from the configured trace."""
    providers = settings.providers
    totals: dict[str, float] = {}
    priced: dict[str, bool] = {
        name: _has_price(cfg) for name, cfg in providers.items()
    }
    raw_path = settings.trace
    if raw_path is None:
        path = Path(settings.session_root) / "trace.jsonl"
    elif str(raw_path).strip().lower() in ("off", "0", "false", ""):
        path = None
    else:
        path = Path(raw_path).expanduser()
    if path is not None:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"kind"' not in line or '"run"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or record.get("kind") != "run":
                        continue
                    lane = record.get("lane")
                    if lane not in providers:
                        continue
                    try:
                        event_at = datetime.fromtimestamp(float(record.get("ts")), now.tzinfo)
                    except (TypeError, ValueError, OSError, OverflowError):
                        continue
                    if (event_at.year, event_at.month) != (now.year, now.month):
                        continue
                    cost = record.get("cost")
                    amount = cost.get("provider_usd") if isinstance(cost, dict) else None
                    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                        priced[lane] = True
                        totals[lane] = totals.get(lane, 0.0) + float(amount)
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
        "flat_plan": ("monthly_usd",),
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
