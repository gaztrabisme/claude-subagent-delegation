"""A3 acceptance tests: approval_hook.py catch-all denies with exit status 2."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from subagent.guard import approval_hook

from ._helpers import APPROVAL_HOOK


def _parse_decision(stdout: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, "expected JSON hookSpecificOutput on stdout, got empty output"
    return json.loads(lines[-1])["hookSpecificOutput"]


def test_a3_hook_os_getcwd_failure_before_socket_denies_exit_2(tmp_path: Path) -> None:
    """A3: when os.getcwd() fails before the socket opens, approval_hook exits 2 with deny JSON."""
    key_file = tmp_path / "approval.key"
    key_file.write_text(secrets.token_bytes(32).hex() + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    dead_dir = tmp_path / "vanishing-cwd"
    dead_dir.mkdir()
    sock_path = tmp_path / "unused.sock"

    runner = (
        "import os, runpy, sys\n"
        f"os.chdir({str(dead_dir)!r})\n"
        f"os.rmdir({str(dead_dir)!r})\n"
        f"sys.argv = [{str(APPROVAL_HOOK)!r}, '--agent', 'a3', '--key-file', {str(key_file)!r}]\n"
        f"runpy.run_path({str(APPROVAL_HOOK)!r}, run_name='__main__')\n"
    )
    env = {**os.environ, "SUBAGENT_APPROVAL_SOCKET": str(sock_path)}
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "a.txt"}}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}; stderr={proc.stderr!r}"
    decision = _parse_decision(proc.stdout)
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize("dialect_args", [[], ["--dialect", "codex"]])
def test_a3_hook_unexpected_exception_in_main_denies_exit_2(
    tmp_path: Path, monkeypatch, capsys, dialect_args: list[str]
) -> None:
    """A3: any unexpected exception in approval_hook.main() turns into deny with exit status 2."""
    key_file = tmp_path / "approval.key"
    key_file.write_text(secrets.token_bytes(32).hex() + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    monkeypatch.setenv("SUBAGENT_APPROVAL_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setattr(
        sys, "argv", [str(APPROVAL_HOOK), "--agent", "a3", "--key-file", str(key_file), *dialect_args]
    )
    import io

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "a.txt"}})),
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(approval_hook, "requests", boom)

    with pytest.raises(SystemExit) as exc_info:
        approval_hook.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    decision = _parse_decision(captured.out)
    assert decision["permissionDecision"] == "deny"
