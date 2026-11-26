#!/usr/bin/env python3
"""T10: score one planted-defect reviewer calibration (file-and-line match, optional QA).

Reads a key (plants and decoys), one findings file per lane and an optional QA judgement
file. Writes nothing except `--out`. Standard library only; no subprocess.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

WINDOW = 5
Z = 1.96


class InputError(Exception):
    """Invalid or unreadable input: exit 2."""


def _load(path: str, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"cannot read {what} {path}: {exc}") from exc


def _norm(name: Any) -> str:
    text = str(name)
    if text.startswith(("./", "a/", "b/")):
        text = text[2:]
    return text


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def wilson(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def read_key(path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    key = _load(path, "key")
    if not isinstance(key, dict):
        raise InputError("key must be a JSON object")
    plants, decoys = key.get("plants"), key.get("decoys", [])
    if not isinstance(plants, list) or not isinstance(decoys, list):
        raise InputError("key needs lists 'plants' and 'decoys'")
    for p in plants:
        ok = isinstance(p, dict) and {"id", "kind", "file"} <= p.keys()
        if not (ok and _is_int(p.get("line"))):
            raise InputError(f"bad plant entry: {p!r}")
    for d in decoys:
        if not (isinstance(d, dict) and "file" in d and _is_int(d.get("line"))):
            raise InputError(f"bad decoy entry: {d!r}")
    return plants, decoys


def read_findings(spec: str) -> tuple[str, list[dict[str, Any]]]:
    lane, eq, path = spec.partition("=")
    if not eq or not lane or not path:
        raise InputError(f"--findings needs LANE=FILE, got {spec!r}")
    data = _load(path, "findings")
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InputError(f"findings file {path} needs a 'findings' list")
    for f in items:
        if not (isinstance(f, dict) and "file" in f and _is_int(f.get("line"))):
            raise InputError(f"bad finding in {path}: {f!r}")
    return lane, items


def score_lane(
    plants: list[dict[str, Any]], decoys: list[dict[str, Any]],
    findings: list[dict[str, Any]], qa: dict[str, Any] | None,
) -> dict[str, Any]:
    assigned: dict[int, str] = {}
    fp = unmatched = 0
    for idx, f in enumerate(findings):
        fname = _norm(f["file"])
        cands = [
            (abs(f["line"] - p["line"]), str(p["id"]))
            for p in plants
            if _norm(p["file"]) == fname and abs(f["line"] - p["line"]) <= WINDOW
        ]
        if cands:
            assigned[idx] = min(cands)[1]
            continue
        on_decoy = any(
            _norm(d["file"]) == fname and abs(f["line"] - d["line"]) <= WINDOW for d in decoys
        )
        if on_decoy:
            fp += 1
            continue
        unmatched += 1
        if qa is not None:
            entry = qa.get(str(idx))
            if not isinstance(entry, dict) or not entry.get("real_defect"):
                fp += 1
    caught_ids: set[str] = set()
    for idx, pid in assigned.items():
        if qa is None:
            caught_ids.add(pid)
        else:
            entry = qa.get(str(idx))
            if isinstance(entry, dict) and entry.get("plant") == pid:
                caught_ids.add(pid)
    kinds = sorted({str(p["kind"]) for p in plants})
    by_kind = {
        k: {
            "caught": sum(1 for p in plants if str(p["kind"]) == k and str(p["id"]) in caught_ids),
            "planted": sum(1 for p in plants if str(p["kind"]) == k),
        }
        for k in kinds
    }
    n = len(plants)
    caught = len(caught_ids)
    return {
        "qa": qa is not None,
        "caught": caught,
        "planted": n,
        "rate": round(caught / n, 3) if n else None,
        "wilson95": wilson(caught, n),
        "by_kind": by_kind,
        "security": by_kind.get("security", {"caught": 0, "planted": 0}),
        "caught_ids": sorted(caught_ids),
        "false_positives": fp,
        "unmatched": unmatched,
        "findings": len(findings),
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    plants, decoys = read_key(args.key)
    lanes: dict[str, list[dict[str, Any]]] = {}
    for spec in args.findings:
        lane, items = read_findings(spec)
        if lane in lanes:
            raise InputError(f"lane {lane!r} given twice")
        lanes[lane] = items
    qa_all: dict[str, Any] = {}
    warnings: list[str] = []
    if args.qa:
        qa_all = _load(args.qa, "qa")
        if not isinstance(qa_all, dict):
            raise InputError("qa file must be a JSON object")
        for lane in qa_all:
            if lane not in lanes:
                warnings.append(f"qa lane {lane!r} has no findings file")
    out: dict[str, Any] = {}
    for lane in sorted(lanes):
        lane_qa = qa_all.get(lane)
        if lane_qa is not None and not isinstance(lane_qa, dict):
            raise InputError(f"qa for lane {lane!r} must be an object")
        out[lane] = score_lane(plants, decoys, lanes[lane], lane_qa)
    return {"plants": len(plants), "lanes": out, "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key", required=True)
    ap.add_argument("--findings", action="append", default=[], metavar="LANE=FILE")
    ap.add_argument("--qa", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    try:
        report = build(args)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
