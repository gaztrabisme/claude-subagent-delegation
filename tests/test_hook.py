from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from subagent.config import APPROVAL_HOOK
from subagent.guard.approval_hook import requests
from subagent.guard.classify import DENY, classify

from .signed_supervisor import private_socket_path, signed_supervisor_socket, verify, write_key_file

HOOK = APPROVAL_HOOK


def test_codex_exec_command_autoresearch_read_is_denied(tmp_path: Path):
    mapped = requests(
        "exec_command", {"cmd": "cat ~/.autoresearch/x/measure.sh"}, codex=True
    )
    assert mapped == [("Bash", {"command": "cat ~/.autoresearch/x/measure.sh"})]
    verdict = classify(*mapped[0], tmp_path)
    assert verdict.action == DENY
    assert ".autoresearch" in verdict.reason


def _run_hook(env: dict, stdin: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), *(extra_args or [])],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def _decision(stdout: str) -> dict:
    payload = json.loads(stdout.strip().splitlines()[-1])
    return payload["hookSpecificOutput"]


def test_missing_socket_denies_with_json(monkeypatch, tmp_path: Path):
    env = {**os.environ}
    env.pop("SUBAGENT_APPROVAL_SOCKET", None)
    result = _run_hook(env, json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert result.returncode == 2
    out = _decision(result.stdout)
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"


def test_allow_and_deny_from_unix_socket(tmp_path: Path):
    key, _key_path, key_args = write_key_file(tmp_path / "approval.key")
    planned = [{"action": "allow", "reason": "ok"}, {"action": "deny", "reason": "nope"}]
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})

    with (
        private_socket_path("hook.sock") as sock_path,
        signed_supervisor_socket(
            sock_path, key, lambda _req: planned.pop(0), backlog=2
        ) as seen,
    ):
        env = {**os.environ, "SUBAGENT_APPROVAL_SOCKET": str(sock_path)}
        allowed = _run_hook(env, payload, ["--agent", "a1", *key_args])
        denied = _run_hook(env, payload, ["--agent", "a1", *key_args])

    assert len(seen) == 2
    assert all(verify(key, req) for req in seen)
    assert allowed.returncode == 0
    assert _decision(allowed.stdout)["permissionDecision"] == "allow"
    assert denied.returncode == 2
    assert _decision(denied.stdout)["permissionDecision"] == "deny"
    assert _decision(denied.stdout)["permissionDecisionReason"] == "nope"


def test_unreachable_socket_denies(tmp_path: Path):
    env = {**__import__("os").environ, "SUBAGENT_APPROVAL_SOCKET": str(tmp_path / "missing.sock")}
    result = _run_hook(env, "{}")
    assert result.returncode == 2
    assert _decision(result.stdout)["permissionDecision"] == "deny"
