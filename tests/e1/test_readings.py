"""E1-3: run counts in lane status, and reading_for."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subagent.lane_status import LaneStatusStore

from ._helpers import DAY, build_settings, member, row, run_line

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
T = NOW.timestamp()


def snapshot(tmp_path: Path, lines: list[str] | None, *, trace: bool = True):
    settings = build_settings(tmp_path, ("codex", "glm", "deepseek"), trace=trace)
    if lines is not None:
        (tmp_path / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return settings, LaneStatusStore(settings.session_root).snapshot(settings, now=NOW)


def test_runs_counted_per_lane_in_day_and_week_windows(tmp_path: Path):
    lines = [
        run_line(T - 3600, "codex", n=1),
        run_line(T - 7200, "codex", n=2),
        run_line(T - 3 * DAY, "codex", n=3),
        run_line(T - 8 * DAY, "codex", n=4),
        run_line(T - 60, "glm", n=5),
        json.dumps({"schema": 3, "ts": T - 60, "kind": "hop", "lane": "codex"}),
    ]
    _, snap = snapshot(tmp_path, lines)
    assert snap["codex"]["runs"] == {"day": 2, "week": 3}
    assert snap["glm"]["runs"] == {"day": 1, "week": 1}
    assert snap["deepseek"]["runs"] == {"day": 0, "week": 0}


def test_runs_are_none_when_trace_is_off(tmp_path: Path):
    _, snap = snapshot(tmp_path, None, trace=False)
    assert snap["codex"]["runs"] == {"day": None, "week": None}


def test_runs_are_none_when_trace_is_unreadable(tmp_path: Path):
    settings = build_settings(tmp_path, ("codex", "glm"))
    (tmp_path / "trace.jsonl").mkdir()
    snap = LaneStatusStore(settings.session_root).snapshot(settings, now=NOW)
    assert snap["codex"]["runs"] == {"day": None, "week": None}


def test_runs_added_to_the_trace_show_in_the_next_snapshot(tmp_path: Path):
    settings, snap = snapshot(tmp_path, [run_line(T - 60, "codex", n=1)])
    store = LaneStatusStore(settings.session_root)
    assert store.snapshot(settings, now=NOW)["codex"]["runs"]["day"] == 1
    with (tmp_path / "trace.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(run_line(T - 30, "codex", n=2) + "\n")
    assert store.snapshot(settings, now=NOW)["codex"]["runs"]["day"] == 2


def test_existing_snapshot_keys_are_untouched(tmp_path: Path):
    _, snap = snapshot(tmp_path, [run_line(T - 60, "deepseek", usd=1.5, n=1)])
    view = snap["deepseek"]
    for key in ("state", "checked_at", "source", "reason", "closed_until", "stale", "usage",
                "slots", "spend"):
        assert key in view
    assert view["spend"]["month_usd"] == pytest.approx(1.5)
    assert "runs" in view


def reading_for(label: str, snap: dict):
    return member("reading_for")(row(label), snap)


def view(**parts):
    return {"usage": None, "spend": {"month_usd": None, "cap_usd": None},
            "runs": {"day": 0, "week": 0}, **parts}


def test_codex_reading_takes_percent_and_runs():
    snap = {"codex": view(usage={"kind": "percent", "used_percent": 35.0},
                          runs={"day": 2, "week": 3})}
    got = reading_for("Codex", snap)
    assert got["used_percent"] == 35.0
    assert (got["runs_day"], got["runs_week"]) == (2, 3)


def test_deepseek_reading_takes_month_spend():
    snap = {"deepseek": view(spend={"month_usd": 0.61, "cap_usd": None})}
    assert reading_for("DeepSeek", snap)["month_usd"] == pytest.approx(0.61)


def test_gemini_row_sums_runs_over_its_providers_only():
    snap = {
        "gemini": view(runs={"day": 7, "week": 9}),
        "gemini-neon": view(runs={"day": 2, "week": 3}),
        "geminix": view(runs={"day": 5, "week": 5}),
        "glm": view(runs={"day": 4, "week": 4}),
    }
    got = reading_for("Gemini", snap)
    assert (got["runs_day"], got["runs_week"]) == (9, 12)


def test_non_percent_usage_gives_no_percent_reading():
    snap = {"deepseek": view(usage={"kind": "balance", "balance": 12.5})}
    got = reading_for("DeepSeek", snap)
    assert got.get("used_percent") is None


def test_missing_counts_are_absent_not_zero():
    snap = {"codex": view(runs={"day": None, "week": None})}
    got = reading_for("Codex", snap)
    assert got.get("runs_day") is None and got.get("runs_week") is None


def test_row_whose_providers_are_not_configured_gives_empty_reading():
    got = reading_for("Codex", {"glm": view()})
    assert got.get("used_percent") is None and got.get("runs_week") is None
    assert isinstance(got, dict)
