"""The codex driver, against a fake `codex` binary that replays real fixtures.

The fake is a small Python script selected by SAM_CODEX_BIN. It prints the
fixture named by FAKE_CODEX_FIXTURE and records its argv, stdin and
environment to FAKE_CODEX_RECORD (one JSON line per call).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

from subagent_mcp import codex_driver
from subagent_mcp.codex_driver import (
    REFUSAL_USAGE_LIMIT,
    codex_refusal,
    parse_reset,
)
from subagent_mcp.runs import COMPLETED, FAILED, Registry
from subagent_mcp.trace import Trace

from .conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
HOOK = Path(__file__).resolve().parents[1] / "src" / "subagent_mcp" / "runtime" / "approval_hook.py"

FAKE = """\
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read()
record = {
    "argv": argv,
    "stdin": stdin,
    "env": {k: os.environ.get(k) for k in (
        "CODEX_HOME", "SAM_APPROVAL_SOCKET", "GLM_API_KEY", "ANTHROPIC_AUTH_TOKEN")},
    "cwd": os.getcwd(),
}
with open(os.environ["FAKE_CODEX_RECORD"], "a") as fh:
    fh.write(json.dumps(record) + "\\n")
fixture = os.environ["FAKE_CODEX_FIXTURE"]
failed = False
with open(fixture) as fh:
    for line in fh:
        if line.strip():
            sys.stdout.write(line if line.endswith("\\n") else line + "\\n")
            failed = failed or json.loads(line).get("type") == "turn.failed"
sys.stdout.flush()
sys.exit(1 if failed else 0)
"""


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "codex"
    binary.write_text(f"#!{sys.executable}\n{FAKE}")
    binary.chmod(0o755)
    record = tmp_path / "codex-calls.jsonl"
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    (user_home / "auth.json").write_text('{"fake": "login"}')
    (user_home / "config.toml").write_text(
        'notify = ["/bin/echo"]\nmodel = "gpt-6-astra"\nmodel_reasoning_effort = "xhigh"\n'
        '[mcp_servers.x]\ncommand = "x"\n'
    )
    (user_home / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "/bin/sh /user/own-hook.sh"}]}]}}))
    monkeypatch.setenv("SAM_CODEX_BIN", str(binary))
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    monkeypatch.setenv("FAKE_CODEX_FIXTURE", str(FIXTURES / "success.jsonl"))

    class Fake:
        home = user_home

        def use(self, fixture: str) -> None:
            monkeypatch.setenv("FAKE_CODEX_FIXTURE", str(FIXTURES / fixture))

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


@pytest.fixture
def registry(tmp_path: Path):
    trace = Trace(tmp_path / "trace.jsonl")
    settings = make_settings(tmp_path, supervisor="auto", idle_timeout=3600, run_timeout=30)
    reg = Registry(settings, start_reaper=False, trace=trace)
    reg.trace_path = tmp_path / "trace.jsonl"  # type: ignore[attr-defined]
    yield reg
    reg.shutdown()


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _run(agent, prompt="do it", verification="true"):
    assert agent.wait_ready(5) is None
    run = agent.submit(prompt, verification=verification)
    assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
    return run


def test_success_maps_usage_text_and_thread(fake_codex, registry, tmp_path: Path):
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    run = _run(agent, prompt="the task")
    assert run.state == COMPLETED, run.error
    assert run.session_id == agent.session_id == "01a0a0de-91f8-7441-a178-a154caba9282"
    usage = run.usage
    assert usage.input == 17182164 - 16873856
    assert usage.cache_read == 16873856
    assert usage.cache_write == 0
    assert usage.output == 117200
    assert usage.reasoning == 89770
    # 9 command_execution items completed in the fixture, no file_change.
    assert usage.steps == 9
    assert run.final_response.startswith("I’ve confirmed the current failing check path")
    assert any(line == "tool_use: Bash" for line in run.transcript)
    call = fake_codex.calls()[0]
    assert call["stdin"] == "the task"
    assert call["argv"][-1] == "-"
    assert "resume" not in call["argv"]


def test_argv_is_sandboxed_and_never_bypasses(fake_codex, registry, tmp_path: Path):
    ws = _workspace(tmp_path)
    agent = registry.create_agent("c", ws, lane="codex", model="gpt-5.3-codex-spark")
    _run(agent)
    argv = fake_codex.calls()[0]["argv"]
    assert argv[:2] == ["exec", "--json"]
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(ws)
    for flag in ("--skip-git-repo-check", "--ignore-rules", "--dangerously-bypass-hook-trust"):
        assert flag in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.3-codex-spark"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--ignore-user-config" not in argv  # it would skip the per-agent config.toml too


def test_no_model_means_no_m_flag(fake_codex, registry, tmp_path: Path):
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    _run(agent)
    assert "-m" not in fake_codex.calls()[0]["argv"]


def test_resume_passes_thread_id(fake_codex, registry, tmp_path: Path):
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    _run(agent)
    second = agent.follow_up("keep going")
    assert second.done.wait(10)
    first_argv, second_argv = (c["argv"] for c in fake_codex.calls()[:2])
    assert "resume" not in first_argv
    at = second_argv.index("resume")
    assert second_argv[at + 1] == "01a0a0de-91f8-7441-a178-a154caba9282"
    assert second_argv[-1] == "-"
    assert second_argv[at - 1] != "-m"
    # Same flags as the first turn.
    assert second_argv[:at] == first_argv[:-1]
    assert fake_codex.calls()[1]["stdin"] == "keep going"


def test_usage_limit_is_a_refusal_with_reset(fake_codex, registry, tmp_path: Path):
    fake_codex.use("usage_limit.jsonl")
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    run = _run(agent)
    assert run.state == FAILED
    assert run.finish_reason == REFUSAL_USAGE_LIMIT
    assert run.usage.steps == 0
    assert "usage limit" in (run.error or "")
    assert len(fake_codex.calls()) == 1  # not retried

    code, message, reset_at = codex_refusal(_load("usage_limit.jsonl"))
    assert code == REFUSAL_USAGE_LIMIT
    assert "try again at Sep 20th, 2026 1:29 PM" in message
    local = datetime(2026, 9, 20, 13, 29).astimezone()
    assert reset_at == local
    assert (reset_at.year, reset_at.month, reset_at.day, reset_at.hour, reset_at.minute) == (
        2026, 9, 20, 13, 29,
    )


def test_context_full_is_failed_not_refused(fake_codex, registry, tmp_path: Path):
    events = _load("context_full.jsonl")
    assert codex_refusal(events) is None
    fake_codex.use("context_full.jsonl")
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    run = _run(agent)
    assert run.state == FAILED
    assert run.finish_reason == "context_full"
    assert "context window" in (run.error or "")
    assert run.usage.steps > 0


def test_usage_limit_after_work_is_not_a_refusal():
    events = _load("context_full.jsonl")[:-2] + [
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit. "
                                          "Try again at 1:01 PM."}},
    ]
    assert codex_refusal(events) is None
    assert codex_driver.failure_kind(events) == "usage_limit_after_work"


def test_bare_time_reset_is_today_or_tomorrow():
    message = "You've hit your usage limit. Upgrade or try again at 1:01 PM."
    morning = datetime(2026, 9, 18, 9, 0).astimezone()
    evening = datetime(2026, 9, 18, 15, 0).astimezone()
    today = parse_reset(message, morning)
    tomorrow = parse_reset(message, evening)
    assert (today.day, today.hour, today.minute) == (18, 13, 1)
    assert (tomorrow.day, tomorrow.hour, tomorrow.minute) == (19, 13, 1)
    assert parse_reset("try again at Sep 18th, 2026 3:22 PM").hour == 15
    assert parse_reset("try again at 12:05 AM", morning).hour == 0
    assert parse_reset("no time here") is None
    refused = codex_refusal([
        {"type": "thread.started", "thread_id": "t"},
        {"type": "error", "message": message},
    ], now=morning)
    assert refused is not None and refused[2] == today


def test_codex_home_is_per_agent_and_isolated(fake_codex, registry, tmp_path: Path):
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    _run(agent)
    home = Path(fake_codex.calls()[0]["env"]["CODEX_HOME"])
    assert home == registry.settings.session_root / "agents" / agent.agent_id / "codex-home"
    auth = home / "auth.json"
    assert auth.is_symlink()
    assert Path(os.readlink(auth)) == fake_codex.home / "auth.json"
    config = (home / "config.toml").read_text()
    assert 'model = "gpt-6-astra"' in config
    assert 'model_reasoning_effort = "xhigh"' in config
    assert "notify" not in config and "mcp_servers" not in config and "hooks" not in config.replace(
        "Hooks live in hooks.json", "")
    hooks = json.loads((home / "hooks.json").read_text())["hooks"]
    assert list(hooks) == ["PreToolUse"]
    commands = [h["command"] for group in hooks["PreToolUse"] for h in group["hooks"]]
    assert commands == [registry.settings.guard_hook_command(agent.agent_id, "--dialect", "codex")]
    assert str(HOOK) in commands[0] and "own-hook.sh" not in json.dumps(hooks)
    env = fake_codex.calls()[0]["env"]
    assert env["SAM_APPROVAL_SOCKET"] == registry.settings.approval_socket
    assert env["GLM_API_KEY"] is None and env["ANTHROPIC_AUTH_TOKEN"] is None


def test_lane_model_skips_user_model(fake_codex, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAM_CODEX_MODEL", "gpt-5.6-luna")
    from subagent_mcp.lanes import load_lanes

    settings = make_settings(tmp_path, lanes=load_lanes(os.environ))
    home = codex_driver.prepare_home(settings, "a9", settings.lanes["codex"])
    config = (home / "config.toml").read_text()
    assert "gpt-6-astra" not in config and "xhigh" not in config


def test_trace_marks_guard_per_driver(fake_codex, registry, tmp_path: Path, monkeypatch):
    from .test_runs import FakeProcess, _result

    monkeypatch.setattr(
        "subagent_mcp.runs._spawn_claude", lambda argv, env, cwd: FakeProcess(_result("ok"), argv, env)
    )
    codex_agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    _run(codex_agent)
    claude_agent = registry.create_agent("g", _workspace(tmp_path), "glm-5.3", lane="glm")
    _run(claude_agent)
    records = [json.loads(line) for line in registry.trace_path.read_text().splitlines()]
    runs = {r["agent_id"]: r for r in records if r["kind"] == "run"}
    assert runs[codex_agent.agent_id]["guard"] == "sandbox+hook"
    assert runs[codex_agent.agent_id]["session_id"] == "01a0a0de-91f8-7441-a178-a154caba9282"
    assert runs[claude_agent.agent_id]["guard"] == "hook"


def test_missing_binary_is_a_start_error(tmp_path: Path, registry):
    agent = registry.create_agent("c", _workspace(tmp_path), lane="codex")
    assert "not on PATH" in (agent.wait_ready(5) or "")


# --- the guard hook, both payload shapes ---------------------------------------


@pytest.fixture
def supervisor_socket(tmp_path: Path):
    """A one-thread fake supervisor: records requests, answers from a list."""
    path = Path("/tmp") / f"sam-codex-hook-{os.getpid()}-{tmp_path.name[-8:]}.sock"
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(8)
    seen: list[dict] = []
    answers: list[dict] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                seen.append(json.loads(data.split(b"\n", 1)[0]))
                reply = answers.pop(0) if answers else {"action": "allow", "reason": "ok"}
                conn.sendall((json.dumps(reply) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    class Sock:
        pass

    sock = Sock()
    sock.path, sock.seen, sock.answers = path, seen, answers
    yield sock
    server.close()
    path.unlink(missing_ok=True)


def _hook(sock, payload: dict, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "SAM_APPROVAL_SOCKET": str(sock.path)}
    return subprocess.run(
        [sys.executable, str(HOOK), "--agent", "a1", *extra],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10,
    )


def test_hook_claude_shape_allows_with_explicit_decision(supervisor_socket):
    out = _hook(supervisor_socket, {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/w",
        "hook_event_name": "PreToolUse",
    })
    assert out.returncode == 0
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"
    assert supervisor_socket.seen[0]["tool_name"] == "Bash"
    assert supervisor_socket.seen[0]["workspace"] == "/w"


# The Codex 0.153.4 pre-tool-use.command.input schema (from the binary):
# cwd, hook_event_name, model, permission_mode, session_id, tool_input,
# tool_name, tool_use_id, transcript_path, turn_id.
CODEX_PAYLOAD = {
    "cwd": "/w", "hook_event_name": "PreToolUse", "model": "gpt-6-astra",
    "permission_mode": "bypassPermissions", "session_id": "s", "tool_use_id": "u",
    "transcript_path": None, "turn_id": "t",
}


def test_hook_codex_shape_allows_silently(supervisor_socket):
    out = _hook(supervisor_socket, {**CODEX_PAYLOAD, "tool_name": "Bash",
                                    "tool_input": {"command": "ls"}}, "--dialect", "codex")
    assert out.returncode == 0
    assert out.stdout.strip() == ""  # Codex rejects permissionDecision: allow
    assert supervisor_socket.seen[0]["tool_name"] == "Bash"


def test_hook_codex_turn_id_detected_without_flag(supervisor_socket):
    out = _hook(supervisor_socket, {**CODEX_PAYLOAD, "tool_name": "exec_command",
                                    "tool_input": {"cmd": ["git", "status"]}})
    assert out.returncode == 0 and out.stdout.strip() == ""
    assert supervisor_socket.seen[0]["tool_name"] == "Bash"
    assert supervisor_socket.seen[0]["tool_input"] == {"command": "git status"}


def test_hook_codex_deny_has_reason(supervisor_socket):
    supervisor_socket.answers.append({"action": "deny", "reason": "nope"})
    out = _hook(supervisor_socket, {**CODEX_PAYLOAD, "tool_name": "shell",
                                    "tool_input": {"command": ["rm", "-rf", "/"]}},
                "--dialect", "codex")
    assert out.returncode == 2
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == "nope"


def test_hook_codex_apply_patch_asks_per_file(supervisor_socket):
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n"
        "*** Add File: ../outside.py\n+z\n*** End Patch\n"
    )
    supervisor_socket.answers.extend([
        {"action": "allow", "reason": "ok"}, {"action": "deny", "reason": "outside"},
    ])
    out = _hook(supervisor_socket, {**CODEX_PAYLOAD, "tool_name": "apply_patch",
                                    "tool_input": {"command": patch}}, "--dialect", "codex")
    assert out.returncode == 2
    assert [r["tool_input"] for r in supervisor_socket.seen] == [
        {"file_path": "a.py"}, {"file_path": "../outside.py"},
    ]
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecisionReason"] == "outside"


def test_no_source_mentions_full_bypass():
    src = Path(codex_driver.__file__).parent
    for path in src.rglob("*.py"):
        assert "dangerously-bypass-approvals-and-sandbox" not in path.read_text(), path


def test_translator_counts_items_once():
    translator = codex_driver.Translator()
    out = []
    for event in _load("success.jsonl"):
        out.extend(translator.feed(event))
    tool_uses = [
        b for e in out if e["type"] == "assistant"
        for b in e["message"]["content"] if b["type"] == "tool_use"
    ]
    assert len(tool_uses) == 9
    assert out[-1]["type"] == "result" and out[-1]["is_error"] is False
