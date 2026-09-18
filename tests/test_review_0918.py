"""Regression tests for wiki/review.md (the 2026-09-18 adversarial review of
750218f), one block per finding ID, using the review's own cases."""

from __future__ import annotations

import importlib.util
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from subagent_mcp.config import APPROVAL_HOOK
from subagent_mcp.guard import ALLOW, DENY, classify

HOME = Path.home()


def _hook_module():
    spec = importlib.util.spec_from_file_location("sam_hook_under_test", APPROVAL_HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HOOK = _hook_module()


@pytest.fixture
def ws() -> Iterator[Path]:
    """A workspace under $HOME, like a real one, with a subdirectory."""
    root = Path(tempfile.mkdtemp(dir=HOME, prefix=".sam-review-0918-")).resolve()
    (root / "sub").mkdir()
    yield root
    (root / "sub").rmdir()
    root.rmdir()


def _codex(tool_name: str, tool_input: dict, ws: Path, cwd: str | None = None) -> list[str]:
    """Every verdict action for a Codex call, as the hook asks them."""
    return [classify(name, args, ws, cwd if cwd is not None else str(ws)).action
            for name, args in HOOK.requests(tool_name, tool_input, True)]


# --- H2: ~/.codex and ~/.git-credentials are sensitive -------------------------------


@pytest.mark.parametrize("path", ["~/.codex/auth.json", "~/.codex/sessions",
                                  "~/.codex/sessions/2026/09/18/rollout.jsonl",
                                  "~/.git-credentials"])
def test_h2_cat_of_codex_login_and_git_credentials_is_denied(ws, path):
    verdict = classify("Bash", {"command": f"cat {path}"}, ws)
    assert verdict.action == DENY, verdict.reason


@pytest.mark.parametrize("path", [f"{HOME}/.codex/auth.json", "~/.codex/auth.json",
                                  f"{HOME}/.git-credentials", f"{HOME}/.codex/sessions"])
def test_h2_read_of_codex_login_and_git_credentials_is_denied(ws, path):
    assert classify("Read", {"file_path": path}, ws).action == DENY


def test_h2_controls_still_denied(ws):
    for path in ("~/.claude.json", "~/.omlx", "~/.aws/credentials", "~/.netrc", "~/.kube"):
        assert classify("Bash", {"command": f"cat {path}"}, ws).action == DENY, path


# --- H1: Codex workdir and the payload cwd reach the classifier -----------------------


def test_h1_exec_command_workdir_ssh_is_denied(ws):
    assert _codex("exec_command", {"cmd": "cat id_ed25519", "workdir": "~/.ssh"}, ws) == [DENY]


def test_h1_shell_workdir_codex_is_denied(ws):
    assert _codex("shell", {"command": ["cat", "auth.json"], "workdir": "~/.codex"}, ws) == [DENY]


def test_h1_control_absolute_ssh_path_is_denied(ws):
    assert _codex("exec_command", {"cmd": "cat ~/.ssh/id_ed25519"}, ws) == [DENY]


def test_h1_workdir_outside_the_workspace_is_denied(ws):
    assert _codex("exec_command", {"cmd": "ls", "workdir": "/etc"}, ws) == [DENY]
    assert _codex("exec_command", {"cmd": "ls", "workdir": "../"}, ws) == [DENY]
    assert _codex("exec_command", {"cmd": "ls", "workdir": "$HOME"}, ws) == [DENY]


def test_h1_workdir_inside_the_workspace_is_allowed_and_used(ws):
    assert _codex("exec_command", {"cmd": "ls", "workdir": "sub"}, ws) == [ALLOW]
    assert _codex("exec_command", {"cmd": "ls", "workdir": str(ws / "sub")}, ws) == [ALLOW]
    # Relative paths resolve against the workdir: `..` from sub is the workspace.
    verdict = classify("Bash", {"command": "cat ../x.txt", "workdir": "sub"}, ws)
    assert verdict.action == ALLOW, verdict.reason


def test_h1_payload_cwd_outside_the_workspace_is_denied(ws):
    ssh = str(HOME / ".ssh")
    assert classify("Bash", {"command": "cat id_ed25519"}, ws, ssh).action == DENY
    assert classify("Read", {"file_path": "id_ed25519"}, ws, ssh).action == DENY
    assert classify("Bash", {"command": "ls"}, ws, "/tmp").action == DENY
    assert classify("Bash", {"command": "ls"}, ws, str(ws)).action == ALLOW


def test_h1_hook_forwards_workdir():
    pairs = HOOK.requests("exec_command", {"cmd": "ls", "workdir": "/w/sub"}, True)
    assert pairs == [("Bash", {"command": "ls", "workdir": "/w/sub"})]


# --- H3: agent ids never repeat across server processes on one session root ---------


def test_h3_two_registries_on_one_session_root_never_share_an_agent_home(tmp_path):
    from dataclasses import replace

    from subagent_mcp.runs import Registry

    from .conftest import make_settings

    base = make_settings(tmp_path, trace="off")
    first = Registry(base, start_reaper=False)
    second = Registry(replace(base), start_reaper=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        a = first.create_agent("a", ws, lane="glm", fallback="none")
        b = second.create_agent("b", ws, lane="glm", fallback="none")
        assert a.agent_id != b.agent_id
        glm, deepseek = base.lanes["glm"], base.lanes["deepseek"]
        path_a = base.hooks_config(a.agent_id, glm)
        path_b = base.hooks_config(b.agent_id, deepseek)
        assert path_a != path_b
        assert glm.base_url in path_a.read_text()
        assert deepseek.base_url not in path_a.read_text()
        # Ids claimed by one process are skipped by the other even with equal tags.
        import itertools

        second._tag, second._counter = first._tag, itertools.count(1)
        c = second.create_agent("c", ws, lane="omlx", fallback="none")
        assert c.agent_id not in (a.agent_id, b.agent_id)
    finally:
        first.shutdown()
        second.shutdown()
