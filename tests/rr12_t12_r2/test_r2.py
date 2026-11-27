"""T12-fix2 round 2: generic display without --overhead; unreadable repo named (S129)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.t12._helpers import CUR, PREV, entry, make_repo, out, pad, repos_file

ROOT = Path(__file__).resolve().parents[2]
DASH = ROOT / "scripts" / "owner_dashboard.py"
FIX = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-11-26T09:00:00Z"


def _dash(tmp_path: Path, overhead: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    outdir = tmp_path / "out"
    argv = [
        sys.executable, "-I", str(DASH), str(outdir),
        "--registers", str(FIX / "registers.md"),
        "--risks", str(FIX / "risk-register.md"),
        "--lane-plan", str(FIX / "lane-plan.md"),
        "--lanes-json", str(FIX / "lanes.json"),
        "--meter", str(FIX / "meter.json"),
        "--now", NOW,
    ]
    if overhead is not None:
        p = tmp_path / "ov.json"
        p.write_text(json.dumps(overhead), encoding="utf-8")
        argv += ["--overhead", str(p)]
    proc = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ), timeout=120)
    assert proc.returncode == 0, proc.stderr
    data = json.loads((outdir / "scorecard.json").read_text(encoding="utf-8"))
    warns = [m["value"] for m in data["meta"] if m["label"] == "warning"]
    return data, warns


def _row(data: dict[str, Any], id_: str) -> dict[str, Any]:
    return next(r for r in data["rows"] if r["id"] == id_)


def _overhead(reason: str | None, tripped: Any = None) -> dict[str, Any]:
    return {
        "window": {"start": "2026-11-19T00:00:00Z", "end": "2026-11-26T00:00:00Z"},
        "commits": {"records_only_share": 62.5},
        "machinery": {"machinery_share": None},
        "rulebook": {"words": 12000, "methods": 40},
        "owner_questions": {"total": 7},
        "tripwire": {"threshold": 80.0, "current": None, "previous": None,
                     "tripped": tripped, "cut_list": [], "reason": reason},
    }


# ---- 1. dashboard without --overhead -------------------------------------------------


def test_no_overhead_flag_displays_generic_no_reading_yet(tmp_path: Path) -> None:
    data, _ = _dash(tmp_path, None)
    r = _row(data, "t12trip")
    assert r["value"] is None
    assert r["status"] == "no reading yet"
    assert r["display"] == "no reading yet"
    assert r["note"] == "no --overhead given"


def test_reason_is_display_only_when_report_carries_it(tmp_path: Path) -> None:
    reason = "no reading: repo zed unreadable"
    data, warns = _dash(tmp_path, _overhead(reason))
    r = _row(data, "t12trip")
    assert r["display"] == reason
    assert reason in r["note"]
    assert len([w for w in warns if reason in w]) == 1


# ---- 2. unreadable repo named in the reading -----------------------------------------


def _reason(name: str) -> str:
    return f"no reading: repo {name} unreadable"


def _assert_unreadable(d: dict[str, Any], name: str) -> None:
    t = d["tripwire"]
    assert t["reason"] == _reason(name)
    assert t["tripped"] is None
    assert t["cut_list"] == []


def test_unreadable_machinery_repo_named(tmp_path: Path) -> None:
    good = make_repo(tmp_path, "good")
    pad(good, PREV, 2)
    pad(good, CUR, 2)
    o = make_repo(tmp_path, "o")
    pad(o, CUR, 1)
    rf = repos_file(tmp_path, [entry("good", good), entry("gone", tmp_path / "missing"),
                               entry("o", o, "outside")])
    _assert_unreadable(out(tmp_path, repos=rf), "gone")


def test_unreadable_outside_repo_named(tmp_path: Path) -> None:
    m = make_repo(tmp_path, "m")
    pad(m, PREV, 2)
    pad(m, CUR, 2)
    rf = repos_file(tmp_path, [entry("m", m), entry("far", tmp_path / "missing", "outside")])
    _assert_unreadable(out(tmp_path, repos=rf), "far")


def test_unreadable_repo_wins_over_no_outside_listed(tmp_path: Path) -> None:
    m = make_repo(tmp_path, "m")
    pad(m, PREV, 2)
    pad(m, CUR, 2)
    rf = repos_file(tmp_path, [entry("m", m), entry("broken", tmp_path / "missing")])
    d = out(tmp_path, repos=rf)
    _assert_unreadable(d, "broken")
    assert d["tripwire"]["reason"] != "no reading: no outside project listed"
