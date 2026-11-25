"""Acceptance tests for R6(c): `Settings.workspace_refusal`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from subagent import config
from subagent.runs import Registry, RegistryError

from ..conftest import make_settings
from ._helpers import git, init_git_repo


def _make_outside_server_paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    pinned = tmp_path / "outside-pinned"
    hook = pinned / "subagent" / "guard" / "approval_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook\n", encoding="utf-8")
    prefix = pinned / "venv"
    prefix.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "APPROVAL_HOOK", hook)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    return hook, prefix


def test_c_worktree_with_repo_layout_outside_pinned_install_is_accepted(tmp_path: Path, monkeypatch):
    _make_outside_server_paths(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    init_git_repo(
        repo,
        {
            "src/subagent/guard/approval_hook.py": "# repo copy\n",
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
        },
    )
    wt = tmp_path / "repo-wt"
    git(repo, "worktree", "add", "-b", "wt-branch", str(wt))
    (wt / ".venv").mkdir(exist_ok=True)
    (wt / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    settings = make_settings(tmp_path)
    assert settings.workspace_refusal(wt) is None


def test_c_workspace_containing_approval_hook_is_refused(tmp_path: Path, monkeypatch):
    _, prefix = _make_outside_server_paths(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    hook = ws / "src" / "subagent" / "guard" / "approval_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVAL_HOOK", hook)
    monkeypatch.setattr(sys, "prefix", str(prefix))

    settings = make_settings(tmp_path)
    reason = settings.workspace_refusal(ws)
    assert reason is not None
    assert "guard hook" in reason.lower()


def test_c_workspace_containing_sys_prefix_is_refused(tmp_path: Path, monkeypatch):
    _make_outside_server_paths(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    prefix = ws / ".venv"
    prefix.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))

    settings = make_settings(tmp_path)
    reason = settings.workspace_refusal(ws)
    assert reason is not None
    assert "server's python" in reason.lower()


def test_c_served_json_install_path_refuses_containing_workspace(tmp_path: Path, monkeypatch):
    _make_outside_server_paths(tmp_path, monkeypatch)
    settings = make_settings(tmp_path)

    ws = tmp_path / "ws-with-install"
    install_dir = ws / "installs" / ("a" * 40)
    install_dir.mkdir(parents=True, exist_ok=True)
    sibling_ws = tmp_path / "sibling-ws"
    sibling_ws.mkdir(parents=True, exist_ok=True)

    (settings.session_root / "served.json").write_text(
        json.dumps(
            {
                "commit": "a" * 40,
                "install_path": str(install_dir),
                "promoted_at": "2026-11-25T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    reason = settings.workspace_refusal(ws)
    assert reason is not None
    assert "pinned install" in reason.lower()
    assert settings.workspace_refusal(sibling_ws) is None


def test_c_registry_create_agent_refuses_disallowed_workspace(tmp_path: Path, monkeypatch):
    _make_outside_server_paths(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    hook = ws / "guard" / "approval_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVAL_HOOK", hook)

    settings = make_settings(tmp_path)
    reg = Registry(settings, start_reaper=False)
    try:
        with pytest.raises(RegistryError, match="guard hook"):
            reg.create_agent("t", ws, "glm")
    finally:
        reg.shutdown()
