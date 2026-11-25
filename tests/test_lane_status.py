"""Acceptance tests for W1: persistent lane status, usage readers, MCP list and CLI."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest

from subagent import cli, config, health
from subagent.lane_state import LaneState
from subagent.runs import COMPLETED, COMPLETED_UNVERIFIED, FAILED, Registry

from .conftest import default_providers, make_settings, write_config
from .test_runs import _wait

FIXTURES = Path(__file__).parent / "fixtures" / "lane_status"
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"
SCENARIOS = Path(__file__).parent / "fakes" / "scenarios"

REQUIRED_VIEW_KEYS = frozenset({
    "state",
    "checked_at",
    "source",
    "reason",
    "closed_until",
    "stale",
    "usage",
    "slots",
    "spend",
})


def _lane_status():
    """Import subagent.lane_status lazily so tests collect before W1 is built."""
    return importlib.import_module("subagent.lane_status")


def _as_datetime(raw: Any) -> datetime:
    assert isinstance(raw, str) and raw, f"expected ISO-8601 string, got {raw!r}"
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _as_epoch(raw: Any) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return _as_datetime(raw).timestamp()


def _assert_lane_view_shape(view: dict[str, Any]) -> None:
    assert isinstance(view, dict)
    assert set(view) >= REQUIRED_VIEW_KEYS
    assert view["state"] in ("open", "closed", "unknown")
    assert view["checked_at"] is None or isinstance(view["checked_at"], str)
    assert view["source"] in ("run", "probe", "closure", None)
    assert view["reason"] is None or isinstance(view["reason"], str)
    assert view["closed_until"] is None or isinstance(view["closed_until"], str)
    assert isinstance(view["stale"], bool)
    assert view["usage"] is None or isinstance(view["usage"], dict)
    slots = view["slots"]
    assert isinstance(slots, dict) and set(slots) == {"limit", "live", "free"}
    assert all(type(slots[k]) is int for k in ("limit", "live", "free"))
    spend = view["spend"]
    assert isinstance(spend, dict) and set(spend) == {"month_usd", "cap_usd"}
    for key in ("month_usd", "cap_usd"):
        val = spend[key]
        assert val is None or (isinstance(val, (int, float)) and not isinstance(val, bool))


def _fake_claude_provider(*, model: str = "glm-test", **extra: Any) -> dict[str, Any]:
    return {
        "driver": "claude",
        "base_url": "https://api.z.ai/api/anthropic",
        "model": model,
        "api_key_env": "GLM_API_KEY",
        "vendor": "zai",
        "binary": str(FAKE_CLAUDE),
        **extra,
    }


def _trace_run_line(
    *,
    ts: float,
    lane: str,
    run_id: str,
    provider_usd: float | None,
    cost_kind: str = "per_token",
) -> str:
    record = {
        "schema": 3,
        "ts": ts,
        "kind": "run",
        "run_id": run_id,
        "agent_id": "a1",
        "session_id": "s1",
        "model": "test-model",
        "lane": lane,
        "provider": lane,
        "driver": "claude",
        "workspace": "/tmp/ws",
        "guard": "hook",
        "state": "completed",
        "finish_reason": "completed",
        "elapsed_seconds": 1.0,
        "wall_seconds": 1.0,
        "ttft_seconds": 0.1,
        "turns": 1,
        "tool_calls": 0,
        "continues": 0,
        "end_state": "completed",
        "verification_passed": True,
        "guard_verdicts": {"allow": 0, "deny": 0, "escalate": 0},
        "refusal_code": None,
        "usage": {
            "input": 100,
            "output": 20,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "total": 120,
            "steps": 0,
            "turns": 1,
        },
        "cost": {
            "provider_usd": provider_usd,
            "counterfactual_usd": 0.05,
            "kind": cost_kind,
            "note": None,
        },
        "prompt_chars": 10,
        "result_chars": 5,
        "distilled": False,
        "truncated": False,
        "verification": {"command": "true", "passed": True, "exit_code": 0, "output_tail": ""},
        "error": None,
    }
    return json.dumps(record)


def _mp_write_worker(
    session_root_str: str, mode: str, lanes: list[str], at_iso: str
) -> None:
    """Top-level picklable worker for multiprocessing concurrency test."""
    from subagent.lane_status import LaneStatusStore

    store = LaneStatusStore(Path(session_root_str))
    when = datetime.fromisoformat(at_iso)
    for lane in lanes:
        if mode == "run":
            store.record_run(lane, "completed", at=when)
        else:
            store.record_probe(lane, False, detail=f"closed-{lane}", at=when)


# --- 1. Store persistence & methods ---------------------------------------------


def test_store_persists_to_lane_status_json_atomically(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("glm", "completed", at=t0)
    store.record_probe("deepseek", False, detail="401 Unauthorized", at=t0)

    status_file = settings.session_root / "lane_status.json"
    assert status_file.is_file()
    raw = json.loads(status_file.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    # Atomic replace leaves no temp files behind.
    assert not list(settings.session_root.glob("*.tmp"))

    reloaded = ls.LaneStatusStore(settings.session_root).snapshot(settings, now=t0)
    assert reloaded["glm"]["state"] == "open"
    assert reloaded["deepseek"]["state"] == "closed"


def test_store_record_usage_preserves_state_and_attaches_payload(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    usage_codex = {
        "kind": "percent",
        "used_percent": 55.0,
        "resets_at": 1796743897,
        "observed_at": t1.isoformat(),
    }
    usage_ds = {
        "kind": "balance",
        "balance": 12.5,
        "currency": "USD",
        "available": True,
    }

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("deepseek", "completed", at=t0)
    store.record_usage("deepseek", usage_ds, at=t1)
    store.record_usage("codex", usage_codex, at=t1)

    snap = store.snapshot(settings, now=t1)
    assert snap["deepseek"]["state"] == "open"
    assert snap["deepseek"]["source"] == "run"
    assert snap["deepseek"]["usage"] == usage_ds

    assert snap["codex"]["state"] == "unknown"
    assert snap["codex"]["source"] is None
    assert snap["codex"]["usage"] == usage_codex


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", ""])
def test_store_missing_or_malformed_file_is_handled_safely(
    tmp_path: Path, content: str
) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    missing_store = ls.LaneStatusStore(tmp_path / "does-not-exist")
    missing_snap = missing_store.snapshot(settings, now=t0)
    assert missing_snap["glm"]["state"] == "unknown"
    assert missing_snap["glm"]["stale"] is True

    (settings.session_root / "lane_status.json").write_text(content, encoding="utf-8")
    store = ls.LaneStatusStore(settings.session_root)
    snap = store.snapshot(settings, now=t0)
    assert snap["glm"]["state"] == "unknown"

    store.record_run("glm", "completed", at=t0)
    assert store.snapshot(settings, now=t0)["glm"]["state"] == "open"


# --- 2. Lane view shape ---------------------------------------------------------


def test_snapshot_returns_full_lane_view_for_each_configured_provider(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("glm", "completed", at=t0)
    snap = store.snapshot(settings, now=t0)

    assert set(snap) == set(settings.providers)
    for view in snap.values():
        _assert_lane_view_shape(view)


# --- 3. Rules -------------------------------------------------------------------


def test_no_record_yields_unknown_and_stale(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    snap = ls.LaneStatusStore(settings.session_root).snapshot(settings, now=t0)
    view = snap["glm"]
    assert view["state"] == "unknown"
    assert view["checked_at"] is None
    assert view["source"] is None
    assert view["closed_until"] is None
    assert view["stale"] is True
    assert view["usage"] is None


@pytest.mark.parametrize("finish_reason", ["completed", "completed_unverified"])
def test_successful_run_marks_lane_open(tmp_path: Path, finish_reason: str) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("glm", finish_reason, at=t0)
    view = store.snapshot(settings, now=t0)["glm"]

    assert view["state"] == "open"
    assert view["source"] == "run"
    assert _as_datetime(view["checked_at"]) == t0
    assert view["closed_until"] is None
    assert view["stale"] is False


def test_passing_probe_marks_lane_open(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_probe("deepseek", True, at=t0)
    view = store.snapshot(settings, now=t0)["deepseek"]

    assert view["state"] == "open"
    assert view["source"] == "probe"
    assert _as_datetime(view["checked_at"]) == t0
    assert view["closed_until"] is None
    assert view["stale"] is False


@pytest.mark.parametrize(
    ("lane", "finish_reason", "code", "detail"),
    [
        ("glm", "auth", None, "401 Unauthorized: invalid api key"),
        ("glm", "refused", "zai_1308", "Usage limit reached for 5 hour"),
        ("deepseek", "refused", "deepseek_balance", "Insufficient Balance"),
        ("codex", "refused", "codex_usage_limit", "You've hit your usage limit"),
    ],
)
def test_refused_run_for_auth_or_quota_marks_lane_closed_with_reason(
    tmp_path: Path,
    lane: str,
    finish_reason: str,
    code: str | None,
    detail: str,
) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run(lane, finish_reason, code=code, detail=detail, at=t0)
    view = store.snapshot(settings, now=t0)[lane]

    assert view["state"] == "closed"
    assert view["source"] == "run"
    assert _as_datetime(view["checked_at"]) == t0
    assert isinstance(view["reason"], str) and view["reason"]
    assert detail in view["reason"] or (code is not None and code in view["reason"])


def test_failing_probe_marks_lane_closed_with_reason(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_probe("glm", False, detail="HTTP 403 forbidden", at=t0)
    view = store.snapshot(settings, now=t0)["glm"]

    assert view["state"] == "closed"
    assert view["source"] == "probe"
    assert _as_datetime(view["checked_at"]) == t0
    assert isinstance(view["reason"], str) and "403" in view["reason"]


def test_unexpired_closure_in_lane_state_wins_over_newer_record(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    now = datetime.now().astimezone()
    until = now + timedelta(hours=2)

    LaneState(settings.session_root).close("glm", until, "zai_1308", "5h quota spent")
    store = ls.LaneStatusStore(settings.session_root)
    # Newer successful run and passing probe must still lose to the unexpired closure.
    store.record_run("glm", "completed", at=now + timedelta(minutes=5))
    store.record_probe("glm", True, at=now + timedelta(minutes=10))

    view = store.snapshot(settings, now=now + timedelta(minutes=15))["glm"]
    assert view["state"] == "closed"
    assert view["source"] == "closure"
    assert _as_datetime(view["closed_until"]) == until
    assert isinstance(view["reason"], str) and view["reason"]


def test_expired_closure_in_lane_state_is_ignored(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    now = datetime.now().astimezone()
    until = now + timedelta(minutes=30)

    LaneState(settings.session_root).close("glm", until, "zai_1308", "5h quota spent")
    LaneState(settings.session_root).close(
        "deepseek", now - timedelta(seconds=5), "deepseek_balance", "expired"
    )
    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("glm", "completed", at=now + timedelta(minutes=35))

    # Past `until`, glm's closure is expired so its newer open run takes effect;
    # deepseek has no other record so it falls back to unknown.
    after = until + timedelta(minutes=10)
    snap = store.snapshot(settings, now=after)

    assert snap["glm"]["state"] == "open"
    assert snap["glm"]["source"] == "run"
    assert snap["glm"]["closed_until"] is None

    assert snap["deepseek"]["state"] == "unknown"
    assert snap["deepseek"]["source"] is None
    assert snap["deepseek"]["closed_until"] is None


def test_newest_record_wins_between_runs_and_probes(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t1 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    t2 = t1 + timedelta(minutes=5)
    t3 = t1 + timedelta(minutes=10)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_probe("glm", False, detail="temporary outage", at=t1)
    assert store.snapshot(settings, now=t1)["glm"]["state"] == "closed"

    # Newer successful run reopens the lane.
    store.record_run("glm", "completed", at=t2)
    v2 = store.snapshot(settings, now=t2)["glm"]
    assert v2["state"] == "open"
    assert v2["source"] == "run"
    assert _as_datetime(v2["checked_at"]) == t2

    # Newer failing probe closes it again.
    store.record_probe("glm", False, detail="key revoked", at=t3)
    v3 = store.snapshot(settings, now=t3)["glm"]
    assert v3["state"] == "closed"
    assert v3["source"] == "probe"
    assert _as_datetime(v3["checked_at"]) == t3

    # An older run written out of order does not beat the newer probe.
    store.record_run("glm", "completed", at=t1)
    v4 = store.snapshot(settings, now=t3)["glm"]
    assert v4["state"] == "closed"
    assert v4["source"] == "probe"
    assert _as_datetime(v4["checked_at"]) == t3


def test_plain_error_run_keeps_previous_state_and_sets_reason(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t1 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    t2 = t1 + timedelta(minutes=2)

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("glm", "completed", at=t1)
    store.record_run("glm", "error", detail="subprocess segfault", at=t2)
    store.record_run("deepseek", "error", detail="unexpected EOF", at=t2)

    snap = store.snapshot(settings, now=t2)
    assert snap["glm"]["state"] == "open"
    assert isinstance(snap["glm"]["reason"], str) and "subprocess segfault" in snap["glm"]["reason"]

    assert snap["deepseek"]["state"] == "unknown"
    assert (
        isinstance(snap["deepseek"]["reason"], str)
        and "unexpected EOF" in snap["deepseek"]["reason"]
    )


def test_config_lane_status_ttl_seconds_default_and_override(tmp_path: Path) -> None:
    default_dir = tmp_path / "default"
    custom_dir = tmp_path / "custom"
    default_dir.mkdir()
    custom_dir.mkdir()

    default_settings = make_settings(default_dir)
    assert default_settings.lane_status_ttl_seconds == 1800

    custom_settings = make_settings(custom_dir, lane_status_ttl_seconds=60)
    assert custom_settings.lane_status_ttl_seconds == 60


def test_staleness_respects_default_and_configured_ttl(tmp_path: Path) -> None:
    ls = _lane_status()
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    default_dir = tmp_path / "default"
    short_dir = tmp_path / "short"
    default_dir.mkdir()
    short_dir.mkdir()

    default_settings = make_settings(default_dir)
    store_default = ls.LaneStatusStore(default_settings.session_root)
    store_default.record_run("glm", "completed", at=t0)
    assert store_default.snapshot(
        default_settings, now=t0 + timedelta(seconds=1799)
    )["glm"]["stale"] is False
    assert store_default.snapshot(
        default_settings, now=t0 + timedelta(seconds=1801)
    )["glm"]["stale"] is True

    short_settings = make_settings(short_dir, lane_status_ttl_seconds=60)
    store_short = ls.LaneStatusStore(short_settings.session_root)
    store_short.record_run("glm", "completed", at=t0)
    assert store_short.snapshot(
        short_settings, now=t0 + timedelta(seconds=59)
    )["glm"]["stale"] is False
    assert store_short.snapshot(
        short_settings, now=t0 + timedelta(seconds=61)
    )["glm"]["stale"] is True


def test_slots_reflect_provider_limit_core_fallback_live_count_and_clamp(
    tmp_path: Path,
) -> None:
    ls = _lane_status()
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "max_agents": 2,
        },
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            # No max_agents -> inherits [core].max_agents = 5
        },
        "omlx": {
            "driver": "claude",
            "base_url": "http://127.0.0.1:8000",
            "model": "qwen-test",
            "api_key_env": "OMLX_API_KEY",
            "local": True,
            "max_agents": 1,
        },
    }
    settings = make_settings(tmp_path, providers=providers, max_agents=5)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    store = ls.LaneStatusStore(settings.session_root)

    snap = store.snapshot(settings, now=t0, live_by_lane={"glm": 1, "omlx": 3})
    assert snap["glm"]["slots"] == {"limit": 2, "live": 1, "free": 1}
    assert snap["deepseek"]["slots"] == {"limit": 5, "live": 0, "free": 5}
    assert snap["omlx"]["slots"] == {"limit": 1, "live": 3, "free": 0}

    snap_no_live = store.snapshot(settings, now=t0)
    assert snap_no_live["glm"]["slots"] == {"limit": 2, "live": 0, "free": 2}


def test_spend_sums_current_month_trace_and_keeps_unpriced_null(tmp_path: Path) -> None:
    ls = _lane_status()
    providers = {
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            "pricing": {"kind": "per_token", "input": 1.0, "output": 2.0, "cache_read": 0.1},
        },
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "pricing": {"kind": "per_token", "input": 0.5, "output": 1.0, "cache_read": 0.1},
        },
        "unpriced_runs": {
            "driver": "claude",
            "base_url": "https://api.example.test",
            "model": "m-test",
            "api_key_env": "GLM_API_KEY",
        },
        "unpriced_idle": {
            "driver": "claude",
            "base_url": "https://api.example.test",
            "model": "m-test",
            "api_key_env": "GLM_API_KEY",
        },
    }
    trace_path = tmp_path / "sessions" / "trace.jsonl"
    # The fixture's session root is no longer tmp_path/sessions (R1a round 4).
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    settings = make_settings(tmp_path, providers=providers, trace=str(trace_path))

    oct_ts = datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp()
    nov_1 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC).timestamp()
    nov_2 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC).timestamp()
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    lines = [
        # Previous calendar month on deepseek: must be excluded.
        _trace_run_line(ts=oct_ts, lane="deepseek", run_id="run-oct", provider_usd=5.0),
        # Two priced runs this calendar month on deepseek: 1.25 + 0.75 = 2.0.
        _trace_run_line(ts=nov_1, lane="deepseek", run_id="run-nov1", provider_usd=1.25),
        _trace_run_line(ts=nov_2, lane="deepseek", run_id="run-nov2", provider_usd=0.75),
        # Another lane this month: must stay isolated to glm.
        _trace_run_line(ts=nov_2, lane="glm", run_id="run-glm", provider_usd=9.0),
        # Unpriced lane with a run whose provider_usd is null: must stay null, never 0.
        _trace_run_line(
            ts=nov_2,
            lane="unpriced_runs",
            run_id="run-unp",
            provider_usd=None,
            cost_kind="none",
        ),
        # Non-run trace event: ignored.
        json.dumps({"schema": 3, "ts": nov_2, "kind": "verdict", "lane": "deepseek"}),
    ]
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    snap = ls.LaneStatusStore(settings.session_root).snapshot(settings, now=now)
    assert snap["deepseek"]["spend"]["month_usd"] == pytest.approx(2.0)
    assert snap["deepseek"]["spend"]["cap_usd"] is None
    assert snap["glm"]["spend"]["month_usd"] == pytest.approx(9.0)
    assert snap["glm"]["spend"]["cap_usd"] is None
    assert snap["unpriced_runs"]["spend"]["month_usd"] is None
    assert snap["unpriced_runs"]["spend"]["cap_usd"] is None
    assert snap["unpriced_idle"]["spend"]["month_usd"] is None
    assert snap["unpriced_idle"]["spend"]["cap_usd"] is None


# --- 4. Probe selection ---------------------------------------------------------


def test_lanes_to_probe_selects_stale_idle_non_local_lanes_only(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store = ls.LaneStatusStore(settings.session_root)
    # glm is fresh -> not stale -> excluded.
    store.record_run("glm", "completed", at=t0)
    # deepseek is stale (older than 1800s) and idle -> included.
    store.record_run("deepseek", "completed", at=t0 - timedelta(hours=2))
    # codex has no record (stale) and has a live agent -> excluded.
    # gpubox and omlx have no record (stale) and 0 live, but local=True -> excluded.

    snap = store.snapshot(settings, now=t0, live_by_lane={"codex": 1})
    selected = ls.lanes_to_probe(snap)

    assert list(selected) == ["deepseek"]
    assert "glm" not in selected
    assert "codex" not in selected
    assert "gpubox" not in selected
    assert "omlx" not in selected


# --- 5. Usage readers -----------------------------------------------------------


def test_codex_usage_reads_last_rate_limit_from_newest_session(tmp_path: Path) -> None:
    ls = _lane_status()
    codex_home = tmp_path / "codex-home"
    shutil.copytree(FIXTURES / "codex_sessions", codex_home)

    old_file = codex_home / "sessions" / "2026" / "11" / "23" / "rollout-2026-11-23T09-00-00.jsonl"
    new_file = codex_home / "sessions" / "2026" / "11" / "24" / "rollout-2026-11-24T10-15-00.jsonl"
    os.utime(old_file, (1_700_000_000, 1_700_000_000))
    os.utime(new_file, (1_700_100_000, 1_700_100_000))

    usage = ls.codex_usage(codex_home)
    assert isinstance(usage, dict)
    assert {"kind", "used_percent", "resets_at", "observed_at"} <= set(usage)
    assert usage["kind"] == "percent"
    assert usage["used_percent"] == pytest.approx(67.5)
    assert _as_epoch(usage["resets_at"]) == pytest.approx(1796743897)
    assert _as_datetime(usage["observed_at"]) == datetime(2026, 11, 24, 10, 15, tzinfo=UTC)


def test_codex_usage_returns_none_when_absent_or_missing(tmp_path: Path) -> None:
    ls = _lane_status()
    assert ls.codex_usage(tmp_path / "nonexistent") is None
    empty_home = tmp_path / "empty-home"
    (empty_home / "sessions").mkdir(parents=True)
    assert ls.codex_usage(empty_home) is None
    assert ls.codex_usage(FIXTURES / "codex_no_limits") is None


def test_deepseek_balance_parses_available_and_empty_payloads() -> None:
    ls = _lane_status()
    ok_payload = json.loads((FIXTURES / "deepseek_balance_ok.json").read_text(encoding="utf-8"))
    empty_payload = json.loads(
        (FIXTURES / "deepseek_balance_empty.json").read_text(encoding="utf-8")
    )

    ok_out = ls.deepseek_balance(ok_payload)
    assert ok_out == {
        "kind": "balance",
        "balance": pytest.approx(14.25),
        "currency": "USD",
        "available": True,
    }

    empty_out = ls.deepseek_balance(empty_payload)
    assert empty_out == {
        "kind": "balance",
        "balance": pytest.approx(0.0),
        "currency": "USD",
        "available": False,
    }

    assert ls.deepseek_balance({}) is None


def test_omlx_usage_parses_loaded_cold_and_minimal_status() -> None:
    ls = _lane_status()
    loaded = json.loads((FIXTURES / "omlx_status_loaded.json").read_text(encoding="utf-8"))
    cold = json.loads((FIXTURES / "omlx_status_cold.json").read_text(encoding="utf-8"))

    assert ls.omlx_usage(loaded) == {
        "kind": "local",
        "models_loaded": 1,
        "loaded_models": ["Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B"],
    }
    assert ls.omlx_usage(cold) == {
        "kind": "local",
        "models_loaded": 0,
        "loaded_models": [],
    }
    assert ls.omlx_usage({"status": "ok", "models_loaded": 2}) == {
        "kind": "local",
        "models_loaded": 2,
        "loaded_models": [],
    }


# --- 6. MCP `list` --------------------------------------------------------------


def test_mcp_list_includes_status_key_on_every_provider(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    mcp_server = importlib.import_module("subagent.mcp_server")
    reg = Registry(settings, start_reaper=False)
    monkeypatch.setattr(mcp_server, "settings", settings)
    monkeypatch.setattr(mcp_server, "registry", reg)
    try:
        out = anyio.run(mcp_server.list_agents)
        assert len(out["providers"]) == len(settings.providers)
        for entry in out["providers"]:
            assert "status" in entry, f"provider {entry['name']!r} missing status"
            _assert_lane_view_shape(entry["status"])
    finally:
        reg.shutdown()


def test_mcp_list_returns_stored_status_and_live_slots(tmp_path: Path, monkeypatch) -> None:
    ls = _lane_status()
    providers = default_providers()
    providers["glm"]["binary"] = str(FAKE_CLAUDE)
    settings = make_settings(tmp_path, providers=providers)
    now = datetime.now().astimezone()

    store = ls.LaneStatusStore(settings.session_root)
    store.record_run("glm", "completed", at=now)
    store.record_probe("deepseek", False, detail="401 Unauthorized", at=now)

    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    mcp_server = importlib.import_module("subagent.mcp_server")
    reg = Registry(settings, start_reaper=False)
    monkeypatch.setattr(mcp_server, "settings", settings)
    monkeypatch.setattr(mcp_server, "registry", reg)
    try:
        agent = reg.create_agent("live-glm", tmp_path, provider="glm", fallback="none")
        assert agent.wait_ready(5) is None

        out = anyio.run(mcp_server.list_agents)
        by_name = {p["name"]: p for p in out["providers"]}

        assert by_name["glm"]["status"]["state"] == "open"
        assert by_name["glm"]["status"]["source"] == "run"
        assert by_name["glm"]["status"]["slots"]["live"] == 1
        assert by_name["glm"]["status"]["slots"]["free"] == (
            by_name["glm"]["status"]["slots"]["limit"] - 1
        )
        assert by_name["deepseek"]["status"]["state"] == "closed"
        assert by_name["deepseek"]["status"]["source"] == "probe"
        assert by_name["omlx"]["status"]["state"] == "unknown"
        assert by_name["omlx"]["status"]["stale"] is True
    finally:
        reg.shutdown()


def test_mcp_list_makes_no_network_or_subprocess_call_on_its_own_path(
    tmp_path: Path, monkeypatch
) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    now = datetime.now().astimezone()
    ls.LaneStatusStore(settings.session_root).record_run("glm", "completed", at=now)

    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    mcp_server = importlib.import_module("subagent.mcp_server")
    reg = Registry(settings, start_reaper=False)
    monkeypatch.setattr(mcp_server, "settings", settings)
    monkeypatch.setattr(mcp_server, "registry", reg)

    stop = threading.Event()

    def slow_or_fail(*args: Any, **kwargs: Any) -> Any:
        stop.wait(5.0)
        raise RuntimeError("probe/reader/network/subprocess called on list path")

    monkeypatch.setattr(health, "check", slow_or_fail)
    monkeypatch.setattr(health, "_http_get", slow_or_fail)
    monkeypatch.setattr(health, "_run", slow_or_fail)
    monkeypatch.setattr(cli, "_prompt_turn", slow_or_fail)
    monkeypatch.setattr(ls, "codex_usage", slow_or_fail)
    monkeypatch.setattr(ls, "deepseek_balance", slow_or_fail)
    monkeypatch.setattr(ls, "omlx_usage", slow_or_fail)
    monkeypatch.setattr(urllib.request, "urlopen", slow_or_fail)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", slow_or_fail)
    monkeypatch.setattr(subprocess, "run", slow_or_fail)
    monkeypatch.setattr(subprocess, "Popen", slow_or_fail)

    try:
        started = time.monotonic()
        out = anyio.run(mcp_server.list_agents)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"list_agents took {elapsed:.2f}s (blocked on probe/I/O)"
        by_name = {p["name"]: p for p in out["providers"]}
        assert by_name["glm"]["status"]["state"] == "open"
        assert by_name["deepseek"]["status"]["state"] == "unknown"
    finally:
        stop.set()
        reg.shutdown()


# --- 7. Run integration (end-to-end with existing fake CLIs) --------------------


def test_completed_fake_claude_run_records_lane_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    settings = make_settings(tmp_path, providers={"glm": _fake_claude_provider()})
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("ok", tmp_path, provider="glm", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("do work", "true"), timeout=15)
        assert run.state == COMPLETED
        assert run.finish_reason == "completed"
    finally:
        reg.shutdown()

    ls = _lane_status()
    view = ls.LaneStatusStore(settings.session_root).snapshot(settings)["glm"]
    assert view["state"] == "open"
    assert view["source"] == "run"
    assert view["checked_at"] is not None
    assert view["stale"] is False


def test_completed_unverified_fake_claude_run_records_lane_open(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    settings = make_settings(tmp_path, providers={"glm": _fake_claude_provider()})
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("unverified", tmp_path, provider="glm", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("do work", "false"), timeout=15)
        assert run.state == COMPLETED_UNVERIFIED
    finally:
        reg.shutdown()

    ls = _lane_status()
    view = ls.LaneStatusStore(settings.session_root).snapshot(settings)["glm"]
    assert view["state"] == "open"
    assert view["source"] == "run"
    assert view["checked_at"] is not None


def test_auth_refusal_fake_claude_run_records_lane_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.setenv("FAKE_FAIL_MODEL", "401")
    settings = make_settings(tmp_path, providers={"glm": _fake_claude_provider(model="401")})
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("auth-fail", tmp_path, provider="glm", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("do work", "true"), timeout=15)
        assert run.state == FAILED
        assert run.finish_reason == "auth"
    finally:
        reg.shutdown()

    ls = _lane_status()
    view = ls.LaneStatusStore(settings.session_root).snapshot(settings)["glm"]
    assert view["state"] == "closed"
    assert view["source"] == "run"
    assert view["checked_at"] is not None
    assert isinstance(view["reason"], str) and view["reason"]


def test_quota_refusal_fake_codex_run_records_lane_closed(
    tmp_path: Path, fake_codex
) -> None:
    fake_codex.use("usage_limit.jsonl")
    settings = make_settings(tmp_path, providers={"codex": default_providers()["codex"]})
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("codex-quota", tmp_path, provider="codex", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("do work", "true"), timeout=15)
        assert run.state == FAILED
        assert run.finish_reason == "refused"
    finally:
        reg.shutdown()

    ls = _lane_status()
    view = ls.LaneStatusStore(settings.session_root).snapshot(settings)["codex"]
    assert view["state"] == "closed"
    assert view["source"] in ("run", "closure")
    assert isinstance(view["reason"], str) and view["reason"]


# --- 8. CLI (`subagent lanes`) --------------------------------------------------


def _write_cli_lanes_config(tmp_path: Path, monkeypatch, mock_endpoint) -> Path:
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True, exist_ok=True)
    omlx_base = mock_endpoint.url("omlx")
    mock_endpoint.routes["/omlx/api/status"] = (
        200,
        {"status": "ok", "models_loaded": 1, "loaded_models": ["qwen-test"]},
    )
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "vendor": "zai",
        },
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            "vendor": "deepseek",
        },
        "codex": {
            "driver": "codex",
            "binary": "/nonexistent/codex",
        },
        "omlx": {
            "driver": "claude",
            "base_url": omlx_base,
            "model": "qwen-test",
            "api_key_env": "OMLX_API_KEY",
            "local": True,
            "max_agents": 1,
            "health": {"kind": "omlx", "url": f"{omlx_base}/api/status"},
        },
    }
    cfg_path = write_config(
        tmp_path / "lanes.toml",
        {
            "core": {
                "workspace": str(tmp_path),
                "session_root": str(session_root),
                "default_provider": "glm",
                "trace": "off",
            },
            "providers": providers,
        },
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg_path))
    return session_root


def test_cli_lanes_json_prints_snapshot_without_probing(
    tmp_path: Path, monkeypatch, mock_endpoint, capsys
) -> None:
    _write_cli_lanes_config(tmp_path, monkeypatch, mock_endpoint)
    monkeypatch.setattr(
        cli, "_prompt_turn", lambda *args, **kwargs: pytest.fail("lanes --json must not probe")
    )

    rc = cli.main(["lanes", "--json"])
    assert rc == 0
    assert mock_endpoint.hits["/omlx/api/status"] == 0

    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"glm", "deepseek", "codex", "omlx"}
    for view in data.values():
        _assert_lane_view_shape(view)
        assert view["state"] == "unknown"
        assert view["stale"] is True


def test_cli_lanes_json_reflects_stored_status_without_probing(
    tmp_path: Path, monkeypatch, mock_endpoint, capsys
) -> None:
    ls = _lane_status()
    session_root = _write_cli_lanes_config(tmp_path, monkeypatch, mock_endpoint)
    now = datetime.now().astimezone()
    ls.LaneStatusStore(session_root).record_run("glm", "completed", at=now)
    monkeypatch.setattr(
        cli, "_prompt_turn", lambda *args, **kwargs: pytest.fail("lanes --json must not probe")
    )

    rc = cli.main(["lanes", "--json"])
    assert rc == 0
    assert mock_endpoint.hits["/omlx/api/status"] == 0

    data = json.loads(capsys.readouterr().out)
    assert data["glm"]["state"] == "open"
    assert data["glm"]["source"] == "run"
    assert data["deepseek"]["state"] == "unknown"
    assert data["omlx"]["state"] == "unknown"


def test_cli_lanes_probe_runs_probes_for_lanes_to_probe_first(
    tmp_path: Path, monkeypatch, mock_endpoint, capsys
) -> None:
    ls = _lane_status()
    session_root = _write_cli_lanes_config(tmp_path, monkeypatch, mock_endpoint)
    now = datetime.now().astimezone()
    store = ls.LaneStatusStore(session_root)
    # glm is fresh -> skipped by lanes_to_probe.
    store.record_run("glm", "completed", at=now)
    # codex is stale (older than default 1800s TTL) -> probed.
    store.record_run("codex", "completed", at=now - timedelta(hours=2))
    # deepseek has no record (stale) -> probed.
    # omlx is local=True -> never prompt-probed.

    prompted: list[str] = []

    def fake_prompt_turn(settings, cfg):
        prompted.append(cfg.name)
        if cfg.name == "deepseek":
            return {
                "ok": True,
                "state": "completed",
                "error": None,
                "output_tokens": 1,
                "ttft_seconds": 0.1,
                "wall_seconds": 0.2,
            }
        return {
            "ok": False,
            "state": "failed",
            "error": "auth: 401 Unauthorized",
            "output_tokens": 0,
            "ttft_seconds": None,
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(cli, "_prompt_turn", fake_prompt_turn)

    rc = cli.main(["lanes", "--probe", "--json"])
    assert rc == 0
    assert set(prompted) == {"deepseek", "codex"}
    assert "glm" not in prompted
    assert "omlx" not in prompted

    data = json.loads(capsys.readouterr().out)
    assert data["glm"]["state"] == "open"
    assert data["glm"]["source"] == "run"
    assert data["deepseek"]["state"] == "open"
    assert data["deepseek"]["source"] == "probe"
    assert data["deepseek"]["stale"] is False
    assert data["codex"]["state"] == "closed"
    assert data["codex"]["source"] == "probe"
    assert "401" in (data["codex"]["reason"] or "")
    # Local lane was never prompt-probed and must not be marked closed against a 200 ok endpoint.
    assert data["omlx"]["state"] in ("open", "unknown")


# --- 9. Two processes -----------------------------------------------------------


def test_two_store_instances_see_each_others_records(tmp_path: Path) -> None:
    ls = _lane_status()
    settings = make_settings(tmp_path)
    t1 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    t2 = t1 + timedelta(seconds=10)

    store_a = ls.LaneStatusStore(settings.session_root)
    store_b = ls.LaneStatusStore(settings.session_root)

    store_a.record_run("glm", "completed", at=t1)
    store_b.record_probe("deepseek", False, detail="402 balance empty", at=t2)

    snap_a = store_a.snapshot(settings, now=t2)
    snap_b = store_b.snapshot(settings, now=t2)

    for snap in (snap_a, snap_b):
        assert snap["glm"]["state"] == "open"
        assert snap["glm"]["source"] == "run"
        assert snap["deepseek"]["state"] == "closed"
        assert snap["deepseek"]["source"] == "probe"


def test_two_processes_concurrent_writes_lose_no_records(tmp_path: Path) -> None:
    ls = _lane_status()
    lanes_a = [f"lane-a-{i}" for i in range(25)]
    lanes_b = [f"lane-b-{i}" for i in range(25)]
    providers = {
        name: {
            "driver": "claude",
            "base_url": "https://api.example.test",
            "model": "m-test",
            "api_key_env": "GLM_API_KEY",
        }
        for name in (*lanes_a, *lanes_b)
    }
    settings = make_settings(tmp_path, providers=providers)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    ctx = multiprocessing.get_context("spawn")
    proc_a = ctx.Process(
        target=_mp_write_worker,
        args=(str(settings.session_root), "run", lanes_a, t0.isoformat()),
    )
    proc_b = ctx.Process(
        target=_mp_write_worker,
        args=(str(settings.session_root), "probe", lanes_b, t0.isoformat()),
    )
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=30)
    proc_b.join(timeout=30)

    assert proc_a.exitcode == 0, f"worker A failed with exitcode {proc_a.exitcode}"
    assert proc_b.exitcode == 0, f"worker B failed with exitcode {proc_b.exitcode}"

    # Persisted file must remain valid JSON with no temp files left behind.
    raw = json.loads((settings.session_root / "lane_status.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert not list(settings.session_root.glob("*.tmp"))

    snap = ls.LaneStatusStore(settings.session_root).snapshot(settings, now=t0)
    for name in lanes_a:
        assert snap[name]["state"] == "open", f"lost record for {name}"
        assert snap[name]["source"] == "run"
    for name in lanes_b:
        assert snap[name]["state"] == "closed", f"lost record for {name}"
        assert snap[name]["source"] == "probe"
