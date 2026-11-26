"""Shared pieces of the scale-gate harness: audit log writer, run config, plan builder.

The audit log contract is LOG_SCHEMA.md. Every record here is a counted id, a number or a
fixed word; nothing reads or prints a key or an environment value.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

CONTROL_QUEUE = "gate-control"
ROOTS = ("root-1", "root-2")
REQUESTS_PER_ROOT = 100
L2_PER_ROOT = 10
PAUSED_WORKFLOW = "root-1/l2-00/l3-0"
PAUSE_AFTER = 6  # requests k < 6 in the paused level-3 workflow run before the pause


def lane_queue(lane: str) -> str:
    return f"lane-{lane}"


def audit(kind: str, **fields: Any) -> None:
    """Append one JSONL record. One O_APPEND write per line, safe across processes."""
    path = os.environ["GATE_AUDIT_LOG"]
    line = json.dumps({"ts": time.time(), "type": kind, **fields}, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def load_config() -> dict[str, Any]:
    """The run config the orchestrator wrote; its path is in GATE_CONFIG."""
    return json.loads(Path(os.environ["GATE_CONFIG"]).read_text(encoding="utf-8"))


def build_plan(deepseek_total: int = 20) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Plan entries for every request, and the workflow spec per root.

    Shape per root: root workflow -> 10 level-2 child workflows -> one level-3 child
    workflow each, holding 10 role requests. The level-3 workflow ``root-1/l2-00/l3-0``
    holds the owner decision: requests 0-5 run first, the rest wait for the owner.
    """
    deepseek_idx: dict[str, set[int]] = {}
    for n, root in enumerate(ROOTS):
        share = deepseek_total // len(ROOTS) + (1 if n < deepseek_total % len(ROOTS) else 0)
        deepseek_idx[root] = {
            min(REQUESTS_PER_ROOT - 1, (j * REQUESTS_PER_ROOT) // max(share, 1) + 5)
            for j in range(share)
        }
    entries: list[dict[str, Any]] = []
    specs: dict[str, dict[str, Any]] = {}
    for root in ROOTS:
        l2_nodes = []
        for a in range(L2_PER_ROOT):
            l2_name = f"l2-{a:02d}"
            wf3 = f"{root}/{l2_name}/l3-0"
            ungated: list[dict[str, Any]] = []
            gated: list[dict[str, Any]] = []
            for k in range(REQUESTS_PER_ROOT // L2_PER_ROOT):
                i = a * 10 + k
                is_gated = wf3 == PAUSED_WORKFLOW and k >= PAUSE_AFTER
                entry = {
                    "request_id": f"{root}-r{i:03d}",
                    "lane": "deepseek" if i in deepseek_idx[root] else "omlx",
                    "root_id": root, "workflow_id": wf3, "depth": 3,
                    "gated_by_owner": is_gated,
                }
                entries.append(entry)
                # Mixed priorities as in the spike: root-2's first 30 requests are urgent.
                priority = 1 if root == "root-2" and i < 30 else 4
                (gated if is_gated else ungated).append({**entry, "priority": priority})
            steps: list[dict[str, Any]] = [{"type": "requests", "items": ungated}]
            if gated:
                steps += [{"type": "owner_decision"}, {"type": "requests", "items": gated}]
            l3 = {"name": "l3-0", "steps": steps}
            l2_nodes.append({"name": l2_name, "steps": [{"type": "children", "items": [l3]}]})
        specs[root] = {"root_id": root, "is_root": True,
                       "steps": [{"type": "children", "items": l2_nodes}]}
    return entries, specs
