"""AGENTS.md rule 2: no machine-specific values under src/subagent/graph (SPEC 12.2 f)."""

from __future__ import annotations

import re
from pathlib import Path

GRAPH_SRC = Path(__file__).resolve().parents[3] / "src" / "subagent" / "graph"
MACHINE_VALUES = re.compile(r"100\.106|192\.168|Tailscale\.app|\.omlx/settings")


def _files() -> list[Path]:
    assert GRAPH_SRC.is_dir(), f"{GRAPH_SRC} does not exist"
    files = [p for p in GRAPH_SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files, "no files under src/subagent/graph"
    return files


def test_no_machine_addresses_or_vendor_key_files():
    hits = [
        f"{p.relative_to(GRAPH_SRC)}:{n}: {line.strip()}"
        for p in _files()
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if MACHINE_VALUES.search(line)
    ]
    assert hits == []


def test_no_home_directory_paths():
    hits = [
        f"{p.relative_to(GRAPH_SRC)}:{n}: {line.strip()}"
        for p in _files()
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if "/Users/" in line or "/home/" in line
    ]
    assert hits == []


def test_every_module_in_the_layout_exists():
    expected = ["__init__", "schema", "expr", "ids", "interpreter", "local", "store", "ledger",
                "journal", "admission", "exceptions", "dispatch", "registry_dispatcher",
                "server", "workflow", "activities", "worker", "service", "mcp_tools", "cli",
                "trace"]
    missing = [m for m in expected if not (GRAPH_SRC / f"{m}.py").is_file()]
    assert missing == []
