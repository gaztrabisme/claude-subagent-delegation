"""Q5 acceptance tests: approval_hook.py fails closed and drivers pass --key-file."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from subagent.providers import for_driver
from subagent.providers.base import Session
from subagent.providers.codex import prepare_home
from subagent.providers.omp_config import OMP_HOOK, write_agent_config
from subagent.providers.omp_config import child_env as omp_child_env
from subagent.runs import Registry
from subagent.telemetry.trace import Trace
from tests.conftest import make_settings

from ._helpers import APPROVAL_HOOK, wire_sign, wire_verify


def _run_hook(
    socket_path: Path,
    extra_args: list[str],
    payload: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SUBAGENT_APPROVAL_SOCKET": str(socket_path)}
    stdin_data = json.dumps(payload or {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    return subprocess.run(
        [sys.executable, str(APPROVAL_HOOK), *extra_args],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def _decision(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])["hookSpecificOutput"]


@contextmanager
def _fake_listener(sock_path: Path, responder):
    """Listen on `sock_path` and call `responder(raw_bytes) -> bytes` per request."""
    sock_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(8)
    server.settimeout(0.1)
    stop = threading.Event()
    requests_seen: list[dict[str, Any]] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                requests_seen.append(req)
                conn.sendall(responder(req))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield requests_seen
    finally:
        stop.set()
        server.close()
        thread.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_q5_hook_denies_when_key_file_missing_unreadable_or_malformed(
    tmp_path: Path, tmp_path_factory
) -> None:
    """Q5: approval_hook.py denies when --key-file is omitted, missing, unreadable, or malformed."""
    sock_path = tmp_path_factory.mktemp("q5a") / "h.sock"
    dummy_key = secrets.token_bytes(32)

    def reply_allow(req: dict[str, Any]) -> bytes:
        resp = wire_sign(
            dummy_key,
            {"action": "allow", "reason": "ok", "nonce": req.get("nonce", "")},
        )
        return (json.dumps(resp) + "\n").encode("utf-8")

    missing_key = tmp_path / "does-not-exist.key"
    unreadable_key = tmp_path / "unreadable.key"
    unreadable_key.write_text(dummy_key.hex(), encoding="utf-8")
    unreadable_key.chmod(0o000)

    bad_hex = tmp_path / "bad-hex.key"
    bad_hex.write_text("not-valid-hex\n", encoding="utf-8")
    short_hex = tmp_path / "short.key"
    short_hex.write_text("abcd\n", encoding="utf-8")
    empty_key = tmp_path / "empty.key"
    empty_key.write_text("", encoding="utf-8")

    try:
        with _fake_listener(sock_path, reply_allow):
            cases = [
                ["--agent", "a1"],
                ["--agent", "a1", "--key-file", str(missing_key)],
                ["--agent", "a1", "--key-file", str(unreadable_key)],
                ["--agent", "a1", "--key-file", str(bad_hex)],
                ["--agent", "a1", "--key-file", str(short_hex)],
                ["--agent", "a1", "--key-file", str(empty_key)],
            ]
            for args in cases:
                res = _run_hook(sock_path, args)
                assert res.returncode == 2, f"expected deny exit 2 for {args}, got {res.returncode}"
                out = _decision(res.stdout)
                assert out["permissionDecision"] == "deny"
                assert "supervisor unreachable" not in out.get("permissionDecisionReason", "")
    finally:
        unreadable_key.chmod(0o600)


def test_q5_hook_denies_unsigned_wrong_key_wrong_nonce_and_non_object_replies(
    tmp_path: Path, tmp_path_factory
) -> None:
    """Q5: approval_hook.py denies unsigned allow, wrong-key mac, wrong nonce, or non-object."""
    sock_path = tmp_path_factory.mktemp("q5b") / "h.sock"
    key = secrets.token_bytes(32)
    other_key = secrets.token_bytes(32)
    key_file = tmp_path / "approval.key"
    key_file.write_text(key.hex() + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    responses: list[bytes] = []

    def next_response(req: dict[str, Any]) -> bytes:
        builder = responses.pop(0)
        return builder(req)

    responses.extend([
        # 1. unsigned allow
        lambda req: (
            json.dumps({"action": "allow", "reason": "ok", "nonce": req.get("nonce", "")}) + "\n"
        ).encode("utf-8"),
        # 2. allow signed with different key
        lambda req: (
            json.dumps(
                wire_sign(
                    other_key,
                    {"action": "allow", "reason": "ok", "nonce": req.get("nonce", "")},
                )
            )
            + "\n"
        ).encode("utf-8"),
        # 3. correctly signed allow with wrong nonce
        lambda _req: (
            json.dumps(
                wire_sign(
                    key,
                    {"action": "allow", "reason": "ok", "nonce": "f" * 64},
                )
            )
            + "\n"
        ).encode("utf-8"),
        # 4. non-object JSON
        lambda _req: b"[1, 2, 3]\n",
    ])

    with _fake_listener(sock_path, next_response) as seen:
        for label in ("unsigned", "wrong-key", "wrong-nonce", "non-object"):
            res = _run_hook(sock_path, ["--agent", "a1", "--key-file", str(key_file)])
            assert res.returncode == 2, f"{label}: expected exit 2, got {res.returncode}"
            assert _decision(res.stdout)["permissionDecision"] == "deny"
    assert len(seen) == 4


def test_q5_hook_allows_verified_allow_and_denies_with_reason_on_signed_deny(
    tmp_path: Path, tmp_path_factory
) -> None:
    """Q5: hook sends nonce+mac, allows verified allow with matching nonce, denies signed deny."""
    sock_path = tmp_path_factory.mktemp("q5c") / "h.sock"
    key = secrets.token_bytes(32)
    key_file = tmp_path / "approval.key"
    key_file.write_text(key.hex() + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    planned = [
        ("allow", "verified-ok"),
        ("deny", "signed-deny-reason"),
    ]

    def respond(req: dict[str, Any]) -> bytes:
        action, reason = planned.pop(0)
        reply = wire_sign(key, {"action": action, "reason": reason, "nonce": req.get("nonce", "")})
        return (json.dumps(reply) + "\n").encode("utf-8")

    with _fake_listener(sock_path, respond) as seen:
        allow_res = _run_hook(sock_path, ["--agent", "a1", "--key-file", str(key_file)])
        deny_res = _run_hook(sock_path, ["--agent", "a1", "--key-file", str(key_file)])

    assert len(seen) == 2
    for req in seen:
        nonce = req.get("nonce")
        assert isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{64}", nonce)
        assert wire_verify(key, req)
    assert seen[0]["nonce"] != seen[1]["nonce"]

    assert allow_res.returncode == 0
    assert _decision(allow_res.stdout)["permissionDecision"] == "allow"
    assert deny_res.returncode == 2
    deny_out = _decision(deny_res.stdout)
    assert deny_out["permissionDecision"] == "deny"
    assert deny_out["permissionDecisionReason"] == "signed-deny-reason"


def _make_driver_settings(
    tmp_path: Path, monkeypatch, *, max_agents: int = 4
) -> tuple[Any, Path]:
    fake_bin = tmp_path / "bin" / "fake-cli"
    fake_bin.parent.mkdir(exist_ok=True)
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    user_codex = tmp_path / "user-codex"
    user_codex.mkdir(exist_ok=True)
    (user_codex / "auth.json").write_text('{"fake": true}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(user_codex))
    monkeypatch.setenv("GLM_API_KEY", "test-glm-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh-key")

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    providers = {
        "claude_p": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake_bin),
        },
        "codex_p": {
            "driver": "codex",
            "binary": str(fake_bin),
        },
        "dsh_p": {
            "driver": "dsh",
            "vendor": "deepseek",
            "binary": str(fake_bin),
            "api_key_env": "DEEPSEEK_API_KEY",
            "experimental": True,
        },
        "omp_p": {
            "driver": "omp",
            "binary": str(fake_bin),
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen-test",
            "experimental": True,
        },
    }
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers=providers,
        max_agents=max_agents,
        supervisor="agent",
        supervisor_cmd="claude -p --model sonnet",
        trace="off",
    )
    return settings, ws


def _assert_cmd_has_key_file(cmd: str, agent_id: str, expected_key_file: Path) -> None:
    tokens = shlex.split(cmd)
    assert "--agent" in tokens and tokens[tokens.index("--agent") + 1] == agent_id
    assert "--key-file" in tokens, f"missing --key-file in hook command: {cmd!r}"
    idx = tokens.index("--key-file")
    assert idx + 1 < len(tokens)
    assert Path(tokens[idx + 1]).resolve() == expected_key_file.resolve()


def test_q5_guard_hook_command_and_all_hook_drivers_carry_key_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Q5: guard_hook_command and claude/codex/dsh/omp hook setups pass --key-file."""
    settings, ws = _make_driver_settings(tmp_path, monkeypatch, max_agents=8)
    reg = Registry(settings, start_reaper=False)
    try:
        # Direct Settings.guard_hook_command check
        a_cmd = reg.create_agent("cmd", ws, provider="claude_p")
        key_cmd_path = settings.session_root / "agents" / a_cmd.agent_id / "approval.key"
        _assert_cmd_has_key_file(settings.guard_hook_command(a_cmd.agent_id), a_cmd.agent_id, key_cmd_path)

        # 1. Claude settings.json
        a_claude = reg.create_agent("c", ws, provider="claude_p")
        claude_key_path = settings.session_root / "agents" / a_claude.agent_id / "approval.key"
        claude_cfg = settings.provider("claude_p")
        settings_json = json.loads(
            settings.hooks_config(a_claude.agent_id, claude_cfg, claude_cfg.model).read_text(
                encoding="utf-8"
            )
        )
        claude_hook_cmd = settings_json["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        _assert_cmd_has_key_file(claude_hook_cmd, a_claude.agent_id, claude_key_path)

        # 2. Codex hooks.json
        a_codex = reg.create_agent("x", ws, provider="codex_p")
        codex_key_path = settings.session_root / "agents" / a_codex.agent_id / "approval.key"
        codex_home = prepare_home(settings, a_codex.agent_id, settings.provider("codex_p"))
        codex_hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        codex_hook_cmd = codex_hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        _assert_cmd_has_key_file(codex_hook_cmd, a_codex.agent_id, codex_key_path)

        # 3. DSH SUBAGENT_GUARD_HOOK
        a_dsh = reg.create_agent("d", ws, provider="dsh_p")
        dsh_key_path = settings.session_root / "agents" / a_dsh.agent_id / "approval.key"
        dsh_env = for_driver("dsh").env(
            settings, a_dsh.agent_id, settings.provider("dsh_p"), Session(provider="dsh_p")
        )
        _assert_cmd_has_key_file(dsh_env["SUBAGENT_GUARD_HOOK"], a_dsh.agent_id, dsh_key_path)

        # 4. OMP env (+ omp_hook.ts forwarding --key-file)
        a_omp = reg.create_agent("o", ws, provider="omp_p")
        omp_key_path = settings.session_root / "agents" / a_omp.agent_id / "approval.key"
        omp_env = omp_child_env(settings, a_omp.agent_id, settings.provider("omp_p"))
        hook_val = omp_env.get("SUBAGENT_GUARD_HOOK", "")
        in_hook_cmd = (
            "--key-file" in hook_val
            and str(omp_key_path.resolve()) in str(Path(shlex.split(hook_val)[-1]).resolve())
            if "--key-file" in hook_val
            else False
        )
        key_file_env = omp_env.get("SUBAGENT_GUARD_KEY_FILE")
        in_env_var = (
            bool(key_file_env)
            and Path(key_file_env).resolve() == omp_key_path.resolve()
            and "--key-file" in OMP_HOOK.read_text(encoding="utf-8")
        )
        assert in_hook_cmd or in_env_var, (
            f"omp env must pass --key-file pointing at {omp_key_path}; got {omp_env!r}"
        )
    finally:
        reg.shutdown()


def test_q5_approval_key_never_leaks_in_argv_env_files_or_trace(
    tmp_path: Path, monkeypatch
) -> None:
    """Q5: approval_key value appears in no argv, env value, trace, or file other than approval.key."""
    settings, ws = _make_driver_settings(tmp_path, monkeypatch)
    trace_path = tmp_path / "trace.jsonl"
    reg = Registry(settings, start_reaper=False, trace=Trace(trace_path))
    keys: list[tuple[str, Path]] = []
    argvs: list[list[str]] = []
    envs: list[dict[str, str]] = []
    try:
        for pname in ("claude_p", "codex_p", "dsh_p", "omp_p"):
            agent = reg.create_agent(pname, ws, provider=pname)
            key_hex = agent.approval_key.hex()
            key_file = (settings.session_root / "agents" / agent.agent_id / "approval.key").resolve()
            keys.append((key_hex, key_file))

            cfg = settings.provider(pname)
            driver = for_driver(cfg.driver)
            session = Session(provider=pname)
            if cfg.driver == "codex":
                session.home = prepare_home(settings, agent.agent_id, cfg)
            elif cfg.driver == "omp":
                write_agent_config(settings, agent.agent_id, cfg, cfg.model)

            argvs.append(driver.argv(cfg, settings, agent.agent_id, "task", ws, session, cfg.model))
            envs.append(driver.env(settings, agent.agent_id, cfg, session))
    finally:
        reg.shutdown()

    trace_text = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
    key_files = {kf for _, kf in keys}

    for key_hex, _ in keys:
        for argv in argvs:
            for item in argv:
                assert key_hex not in item
        for env in envs:
            for k, v in env.items():
                assert key_hex not in k and key_hex not in v
        assert key_hex not in trace_text
        for path in settings.session_root.rglob("*"):
            if not path.is_file() or path.resolve() in key_files:
                continue
            assert key_hex not in path.read_text(encoding="utf-8", errors="replace"), (
                f"approval key leaked into {path}"
            )
