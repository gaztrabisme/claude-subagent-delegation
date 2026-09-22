from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "subagent" / "guard" / "approval_hook.py"


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
    sock_path = Path("/tmp") / f"sam-hook-test-{tmp_path.name}.sock"
    sock_path.unlink(missing_ok=True)
    replies = {"allow": {"action": "allow", "reason": "ok"}, "deny": {"action": "deny", "reason": "nope"}}
    planned = ["allow", "deny"]

    def serve() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(2)
        try:
            for key in planned:
                conn, _ = server.accept()
                conn.recv(65536)
                conn.sendall((json.dumps(replies[key]) + "\n").encode())
                conn.close()
        finally:
            server.close()
            if sock_path.exists():
                sock_path.unlink()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    for _ in range(50):
        if sock_path.exists():
            break
        __import__("time").sleep(0.02)
    env = {**__import__("os").environ, "SUBAGENT_APPROVAL_SOCKET": str(sock_path)}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    allowed = _run_hook(env, payload, ["--agent", "a1"])
    denied = _run_hook(env, payload, ["--agent", "a1"])
    thread.join(timeout=2)
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
