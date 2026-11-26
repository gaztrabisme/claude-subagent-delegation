"""RF2 (F2): verification cannot write under <workspace>/.git."""

from __future__ import annotations

import sys

import pytest

from tests.r1b_fix._helpers import HEADER, run_script

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_SCRIPT = HEADER + """
ws = Path.cwd()

def hook():
    (ws / ".git" / "hooks" / "rf2-probe").write_text("#!/bin/sh\\n")

def config():
    with open(ws / ".git" / "config", "a") as fh:
        fh.write("[core]\\n\\tfsmonitor = x\\n")

for label, action in (("hook", hook), ("config", config)):
    try:
        action()
        R[label] = "written"
    except PermissionError as exc:
        R[label] = f"PermissionError:{exc.errno}"
    except OSError as exc:
        R[label] = f"OSError:{exc.errno}"
done()
"""


def _setup(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    return ws


def test_rf2_git_hook_write_is_denied(tmp_path) -> None:
    """RF2: a verification command cannot create .git/hooks/rf2-probe (permission error, file absent)."""
    ws = _setup(tmp_path)
    _, result, data = run_script(tmp_path, _SCRIPT)
    assert result.sandboxed is True
    assert data.get("hook", "").startswith("PermissionError"), data
    assert not (ws / ".git" / "hooks" / "rf2-probe").exists()


def test_rf2_git_config_append_is_denied(tmp_path) -> None:
    """RF2: a verification command cannot append to .git/config (permission error, config unchanged)."""
    ws = _setup(tmp_path)
    before = (ws / ".git" / "config").read_text(encoding="utf-8")
    _, _, data = run_script(tmp_path, _SCRIPT)
    assert data.get("config", "").startswith("PermissionError"), data
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == before
