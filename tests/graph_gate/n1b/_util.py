"""Shared helpers: load the scripts as modules, list processes with their environment."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEMO = REPO / "scripts" / "graph_live_demo" / "run_demo.py"
README = REPO / "scripts" / "graph_live_demo" / "README.md"
ORCH = REPO / "scripts" / "graph_scale_gate" / "orchestrate.py"


def load(path: Path, name: str):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ps() -> list[tuple[int, int, str]]:
    """(pid, ppid, command + environment) of every process (`ps e` appends the environment)."""
    out = subprocess.run(["ps", "-axeww", "-o", "pid=,ppid=,command="], capture_output=True,
                         text=True, check=False).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def descendants(root: int) -> set[int]:
    rows = ps()
    found: set[int] = set()
    todo = [root]
    while todo:
        cur = todo.pop()
        for pid, ppid, _ in rows:
            if ppid == cur and pid not in found:
                found.add(pid)
                todo.append(pid)
    return found
