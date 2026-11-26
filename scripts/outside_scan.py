#!/usr/bin/env python3
"""G2 offline scan: harness version against verified version, and fixture age.

Reads the JSON list printed by `subagent harness status --json` and a fixtures
directory (`DIR/<driver>/<name>.jsonl`), and prints one JSON report of flagged
lanes and fixtures older than a refresh date. Never touches the network; the
only subprocess is git. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_MAX_AGE_DAYS = 30


def _parse_ts(raw: str) -> dt.datetime:
    """Parse an ISO 8601 timestamp; naive values are taken as UTC."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = dt.datetime.fromisoformat(text)
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def _lane_state(version: Any, verified: Any) -> str:
    if not version:
        return "unknown"
    if not verified:
        return "unverified"
    return "match" if version == verified else "drift"


def _last_change(path: Path) -> str | None:
    """Committer date of the last commit touching the file; None when untracked."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(path.parent), "log", "-1", "--format=%cI", "--", path.name],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    out = cp.stdout.strip()
    if cp.returncode != 0 or not out:
        return None
    return out


def _load_status(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read status file {path}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError(f"status file {path} is not a JSON list of objects")
    return data


def build_report(rows: list[dict], fixtures_dir: Path, now: dt.datetime, max_age: int) -> dict:
    lanes = []
    flags: list[dict] = []
    for row in rows:
        state = _lane_state(row.get("harness_version"), row.get("verified_version"))
        lanes.append({"lane": row.get("lane"), "harness_version": row.get("harness_version"),
                      "verified_version": row.get("verified_version"), "state": state})
        if state != "match":
            flags.append({"kind": "lane", "id": row.get("lane"), "reason": state})
    lanes_flagged = len(flags)

    fixtures = []
    for driver_dir in sorted(p for p in fixtures_dir.iterdir() if p.is_dir()):
        for f in sorted(driver_dir.glob("*.jsonl")):
            if not f.is_file():
                continue
            changed = _last_change(f)
            age = None
            stale = False
            if changed is not None:
                try:
                    delta = now - _parse_ts(changed)
                    age = int(delta.total_seconds() // 86400)
                    stale = age > max_age
                except ValueError:
                    changed = None
            fixtures.append({"driver": driver_dir.name, "file": f"{driver_dir.name}/{f.name}",
                             "last_change": changed, "age_days": age, "stale": stale})
    for fx in fixtures:
        if fx["last_change"] is None:
            flags.append({"kind": "fixture", "id": fx["file"], "reason": "undated"})
        elif fx["stale"]:
            flags.append({"kind": "fixture", "id": fx["file"], "reason": "stale"})
    return {
        "now": now.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "max_age_days": max_age,
        "lanes": lanes,
        "fixtures": fixtures,
        "flags": flags,
        "summary": {"lanes": len(lanes), "lanes_flagged": lanes_flagged,
                    "fixtures": len(fixtures), "fixtures_flagged": len(flags) - lanes_flagged},
        "network": False,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", required=True, type=Path)
    ap.add_argument("--fixtures", required=True, type=Path)
    ap.add_argument("--now", default=None)
    ap.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    args = ap.parse_args(argv)
    try:
        rows = _load_status(args.status)
        if not args.fixtures.is_dir():
            raise ValueError(f"fixtures directory {args.fixtures} is missing")
        now = _parse_ts(args.now) if args.now else dt.datetime.now(dt.UTC)
    except ValueError as exc:
        print(f"outside_scan: {exc}", file=sys.stderr)
        return 2
    report = build_report(rows, args.fixtures, now, args.max_age_days)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
