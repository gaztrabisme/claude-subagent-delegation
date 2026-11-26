"""Helpers for the G1f tests: run the owner dashboard on clean fixtures (offline)."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "owner_dashboard.py"
FIX = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-11-26T09:00:00Z"
OLD_IDS = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "t8", "t2", "spend"]
NEW_IDS = ["t12", "t12trip", "t3"]
KEYS = ["id", "measure", "value", "unit", "display", "baseline", "target", "status", "note"]
DATASETS = ["waiting", "packages", "envelopes", "holds", "risks", "failures", "scorecard"]

OVERHEAD: dict[str, Any] = {
    "window": {"start": "2026-11-19T00:00:00Z", "end": "2026-11-26T00:00:00Z"},
    "commits": {"records_only_share": 62.5},
    "machinery": {"machinery_share": 71.4},
    "rulebook": {"words": 12000, "methods": 40},
    "owner_questions": {"total": 7},
    "tripwire": {
        "threshold": 50.0,
        "current": 71.4,
        "previous": 66.0,
        "tripped": True,
        "cut_list": [
            {"kind": "method", "name": "Method A", "file": "a.md", "cost": 10, "effect": 0},
            {"kind": "record", "name": "repo:b.md", "file": "b.md", "cost": 9, "effect": 1},
        ],
    },
}

REPORT: dict[str, Any] = {
    "run_roles": {
        "builder": {
            "runs": 7,
            "verified_passed": 3,
            "pass_rate": 0.429,
            "by_provider": {
                "zeta": {"runs": 2, "verified_passed": 1},
                "alpha": {"runs": 5, "verified_passed": 2},
            },
        },
        "review": {"runs": 2, "verified_passed": 2},
        "setup": {"runs": 1},
        "unclassified": {"runs": 4},
    }
}


def overhead(**changes: Any) -> dict[str, Any]:
    """A deep copy of the sample overhead report; a value of `...` deletes a dotted key."""
    data = copy.deepcopy(OVERHEAD)
    for dotted, val in changes.items():
        parts = dotted.split("__")
        cur = data
        for p in parts[:-1]:
            cur = cur[p]
        if val is ...:
            del cur[parts[-1]]
        else:
            cur[parts[-1]] = val
    return data


def report(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(REPORT)
    for dotted, val in changes.items():
        parts = dotted.split("__")
        cur = data
        for p in parts[:-1]:
            cur = cur[p]
        if val is ...:
            del cur[parts[-1]]
        else:
            cur[parts[-1]] = val
    return data


def write_json(path: Path, obj: Any) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def run(outdir: Path, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    argv = [
        sys.executable, "-I", str(SCRIPT), str(outdir),
        "--registers", str(FIX / "registers.md"),
        "--risks", str(FIX / "risk-register.md"),
        "--lane-plan", str(FIX / "lane-plan.md"),
        "--lanes-json", str(FIX / "lanes.json"),
        "--meter", str(FIX / "meter.json"),
        "--now", NOW,
    ] + (extra or [])
    return subprocess.run(
        argv, capture_output=True, text=True, env=dict(os.environ), timeout=120
    )


def scorecard(
    outdir: Path, extra: list[str] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Run, require exit 0 and all seven datasets; return (scorecard data, warnings)."""
    proc = run(outdir, extra)
    assert proc.returncode == 0, proc.stderr
    for name in DATASETS:
        assert (outdir / f"{name}.json").is_file(), f"{name}.json not written"
    data = json.loads((outdir / "scorecard.json").read_text(encoding="utf-8"))
    warns = [m["value"] for m in data["meta"] if m["label"] == "warning"]
    return data, warns


def row(data: dict[str, Any], id_: str) -> dict[str, Any]:
    rows = {r["id"]: r for r in data["rows"]}
    assert id_ in rows, f"scorecard has no {id_} row (ids: {list(rows)})"
    return rows[id_]
