"""T12 trip-wire: no outside project listed means no reading, shown visibly (decision S129)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.t12._helpers import CUR, PREV, commit, entry, make_repo, out, pad, repos_file

REASON = "no reading: no outside project listed"
ROOT = Path(__file__).resolve().parents[2]
DASH = ROOT / "scripts" / "owner_dashboard.py"
FIX = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-11-26T09:00:00Z"


# ---- overhead_meter ------------------------------------------------------------------


def _machinery_only(tmp_path: Path) -> Path:
    a = make_repo(tmp_path, "a")
    b = make_repo(tmp_path, "b")
    for r in (a, b):
        pad(r, PREV, 3)
        pad(r, CUR, 3)
    return repos_file(tmp_path, [entry("a", a), entry("b", b)])


def test_no_outside_repo_gives_no_reading(tmp_path: Path) -> None:
    d = out(tmp_path, repos=_machinery_only(tmp_path))
    t = d["tripwire"]
    assert t["tripped"] is None
    assert t["cut_list"] == []
    assert t["reason"] == REASON
    assert REASON in d["warnings"]
    assert d["machinery"]["machinery_share"] == 100.0


def test_no_outside_repo_does_not_trip_even_at_100_percent_both_windows(tmp_path: Path) -> None:
    t = out(tmp_path, "--threshold", "10", repos=_machinery_only(tmp_path))["tripwire"]
    assert t["tripped"] is None and t["cut_list"] == []


def test_outside_repo_listed_behaves_as_before(tmp_path: Path) -> None:
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    for date in (PREV, CUR):
        pad(m, date, 9)
        commit(o, {"x.txt": date}, "outside work", date)
    rf = repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")])
    d = out(tmp_path, repos=rf)
    t = d["tripwire"]
    assert isinstance(t["tripped"], bool) and t["tripped"] is True
    assert t["reason"] is None
    assert REASON not in d["warnings"]


def test_outside_repo_listed_not_tripped_has_null_reason(tmp_path: Path) -> None:
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    for date in (PREV, CUR):
        pad(m, date, 1)
        pad(o, date, 1)
    d = out(tmp_path, repos=repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")]))
    assert d["tripwire"]["tripped"] is False
    assert d["tripwire"]["reason"] is None


# ---- owner_dashboard -----------------------------------------------------------------

OVERHEAD: dict[str, Any] = {
    "window": {"start": "2026-11-19T00:00:00Z", "end": "2026-11-26T00:00:00Z"},
    "commits": {"records_only_share": 62.5},
    "machinery": {"machinery_share": 100.0},
    "rulebook": {"words": 12000, "methods": 40},
    "owner_questions": {"total": 7},
    "tripwire": {
        "threshold": 80.0,
        "current": 100.0,
        "previous": 100.0,
        "tripped": None,
        "cut_list": [],
        "reason": REASON,
    },
}


def _dash(tmp_path: Path, tripwire_changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    ov = json.loads(json.dumps(OVERHEAD))
    ov["tripwire"].update(tripwire_changes)
    ov_path = tmp_path / "ov.json"
    ov_path.write_text(json.dumps(ov), encoding="utf-8")
    outdir = tmp_path / "out"
    argv = [
        sys.executable, "-I", str(DASH), str(outdir),
        "--registers", str(FIX / "registers.md"),
        "--risks", str(FIX / "risk-register.md"),
        "--lane-plan", str(FIX / "lane-plan.md"),
        "--lanes-json", str(FIX / "lanes.json"),
        "--meter", str(FIX / "meter.json"),
        "--overhead", str(ov_path),
        "--now", NOW,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ), timeout=120)
    assert proc.returncode == 0, proc.stderr
    data = json.loads((outdir / "scorecard.json").read_text(encoding="utf-8"))
    warns = [m["value"] for m in data["meta"] if m["label"] == "warning"]
    return data, warns


def _row(data: dict[str, Any], id_: str) -> dict[str, Any]:
    return next(r for r in data["rows"] if r["id"] == id_)


def test_dashboard_shows_reason_visibly_when_no_reading(tmp_path: Path) -> None:
    data, warns = _dash(tmp_path, {})
    r = _row(data, "t12trip")
    assert r["value"] is None
    assert r["status"] == "no reading yet"
    assert r["display"] == REASON
    assert REASON in r["note"]
    assert len([w for w in warns if REASON in w]) == 1
    assert len(warns) == 1


def test_dashboard_tripped_true_as_before(tmp_path: Path) -> None:
    data, warns = _dash(tmp_path, {"tripped": True, "reason": None})
    r = _row(data, "t12trip")
    assert r["value"] is True
    assert r["display"] == "tripped"
    assert r["status"] != "no reading yet"
    assert r["note"].startswith("current 100.0%, previous 100.0%, threshold 80.0%; cut list:")
    assert warns == []


def test_dashboard_tripped_false_as_before(tmp_path: Path) -> None:
    data, warns = _dash(tmp_path, {"tripped": False, "reason": None})
    r = _row(data, "t12trip")
    assert r["value"] is False
    assert r["display"] == "not tripped"
    assert REASON not in r["note"]
    assert warns == []
