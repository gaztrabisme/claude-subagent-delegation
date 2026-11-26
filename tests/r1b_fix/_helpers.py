"""Shared helpers for the R1b fix-round tests (no assertions here)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from subagent.verify import VerificationResult, run_verification
from tests.conftest import make_settings


def run_script(
    tmp_path: Path,
    script: str,
    settings=None,
    targets: dict[str, Any] | None = None,
    command: str = "python3 probe.py",
) -> tuple[Path, VerificationResult, dict[str, Any]]:
    """Write probe.py (and targets.json) into a workspace, run it under verification."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "probe.py").write_text(script, encoding="utf-8")
    (ws / "targets.json").write_text(json.dumps(targets or {}), encoding="utf-8")
    settings = settings or make_settings(tmp_path, verify_timeout=15.0)
    result = run_verification(command, ws, settings)
    out = ws / "out.json"
    data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    return ws, result, data


HEADER = """\
import json
from pathlib import Path
T = json.loads(Path("targets.json").read_text(encoding="utf-8"))
R = {}
def done():
    Path("out.json").write_text(json.dumps(R), encoding="utf-8")
"""
