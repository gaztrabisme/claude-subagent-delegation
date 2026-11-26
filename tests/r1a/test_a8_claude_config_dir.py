"""A8 acceptance tests: CLAUDE_CONFIG_DIR protection across drivers, case, search, and overlap."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from subagent.guard.classify import DENY, classify
from subagent.runs import COMPLETED, Registry
from tests.conftest import make_settings
from tests.test_runs import FakeProcess, _result


def _setup_all_drivers(tmp_path: Path, monkeypatch, claude_dir: Path | None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "credentials.json").write_text('{"tok": "x"}\n', encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "README.md").write_text("# ws\n", encoding="utf-8")

    fake_bin = tmp_path / "bin" / "fake"
    fake_bin.parent.mkdir(exist_ok=True)
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    codex_home = tmp_path / "user-codex"
    codex_home.mkdir(exist_ok=True)
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GLM_API_KEY", "test-glm")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh")

    if claude_dir is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        target_dir = home / ".claude"
    else:
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "credentials.json").write_text('{"tok": "d"}\n', encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
        target_dir = claude_dir

    providers = {
        "claude_api": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake_bin),
        },
        "claude_login": {
            "driver": "claude",
            "auth": "login",
            "binary": str(fake_bin),
        },
        "codex_p": {
            "driver": "codex",
            "binary": str(fake_bin),
        },
        "omp_p": {
            "driver": "omp",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen-test",
            "binary": str(fake_bin),
            "experimental": True,
        },
        "dsh_p": {
            "driver": "dsh",
            "vendor": "deepseek",
            "api_key_env": "DEEPSEEK_API_KEY",
            "binary": str(fake_bin),
            "experimental": True,
        },
    }
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers=providers,
        max_agents=8,
        supervisor="off",
        trace="off",
    )
    return home, ws, target_dir, settings


@pytest.mark.parametrize("use_custom_dir", [True, False])
@pytest.mark.parametrize("pname", ["claude_api", "claude_login", "codex_p", "omp_p", "dsh_p"])
def test_a8_1_claude_config_dir_denied_for_all_hook_drivers(
    tmp_path: Path, monkeypatch, use_custom_dir: bool, pname: str
) -> None:
    """A8.1: reads and writes of CLAUDE_CONFIG_DIR (or ~/.claude) are denied for every hook driver."""
    custom_dir = tmp_path / "CustomClaude" if use_custom_dir else None
    _, ws, target_dir, settings = _setup_all_drivers(tmp_path, monkeypatch, custom_dir)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent(pname, ws, provider=pname)
        cred_file = str(target_dir / "credentials.json")
        assert classify("Read", {"file_path": cred_file}, ws, context=agent.guard_context).action == DENY
        assert classify("Write", {"file_path": cred_file}, ws, context=agent.guard_context).action == DENY
        assert classify("Edit", {"file_path": cred_file}, ws, context=agent.guard_context).action == DENY
        assert classify(
            "Bash", {"command": f"cat {cred_file}"}, ws, context=agent.guard_context
        ).action == DENY
    finally:
        reg.shutdown()


def test_a8_2_case_variants_of_claude_config_dir_and_dot_claude_denied(
    tmp_path: Path, monkeypatch
) -> None:
    """A8.2: case variants of CLAUDE_CONFIG_DIR and ~/.claude are denied as the same path."""
    custom_dir = tmp_path / "CustomClaude"
    home, ws, _, settings = _setup_all_drivers(tmp_path, monkeypatch, custom_dir)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("a8-case", ws, provider="claude_api")
        variants = [
            tmp_path / "CUSTOMCLAUDE" / "credentials.json",
            tmp_path / "customclaude" / "credentials.json",
            home / ".CLAUDE" / "credentials.json",
            home / ".Claude" / "credentials.json",
        ]
        for variant in variants:
            assert classify(
                "Read", {"file_path": str(variant)}, ws, context=agent.guard_context
            ).action == DENY, str(variant)
            assert classify(
                "Write", {"file_path": str(variant)}, ws, context=agent.guard_context
            ).action == DENY, str(variant)
            assert classify(
                "Bash", {"command": f"cat {variant}"}, ws, context=agent.guard_context
            ).action == DENY, str(variant)
    finally:
        reg.shutdown()


def test_a8_3_recursive_search_and_glob_reaching_claude_config_dir_denied(
    tmp_path: Path, monkeypatch
) -> None:
    """A8.3: recursive search at an ancestor of CLAUDE_CONFIG_DIR and globs matching it are denied."""
    custom_dir = tmp_path / "owner_cfg" / "CustomClaude"
    _, ws, target_dir, settings = _setup_all_drivers(tmp_path, monkeypatch, custom_dir)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("a8-search", ws, provider="codex_p")
        ancestor = target_dir.parent
        assert classify(
            "Bash", {"command": f"grep -r tok {ancestor}"}, ws, context=agent.guard_context
        ).action == DENY
        assert classify(
            "Bash", {"command": f"rg tok {ancestor}"}, ws, context=agent.guard_context
        ).action == DENY
        assert classify(
            "Grep", {"path": str(ancestor), "pattern": "tok"}, ws, context=agent.guard_context
        ).action == DENY
        assert classify(
            "Bash", {"command": f"cat {ancestor}/Custom*/*"}, ws, context=agent.guard_context
        ).action == DENY
    finally:
        reg.shutdown()


def test_a8_4_overlapping_login_runs_keep_protection_when_first_ends(
    tmp_path: Path, monkeypatch
) -> None:
    """A8.4: when two login runs overlap, ending the first does not lift CLAUDE_CONFIG_DIR protection."""
    custom_dir = tmp_path / "LoginClaude"
    _, ws, target_dir, settings = _setup_all_drivers(tmp_path, monkeypatch, custom_dir)
    # Override claude_login max_agents to 2 for overlapping runs.
    providers = {
        "claude_login": {
            "driver": "claude",
            "auth": "login",
            "max_agents": 2,
            "binary": str(tmp_path / "bin" / "fake"),
        }
    }
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers=providers,
        max_agents=4,
        supervisor="off",
        trace="off",
    )

    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    counter_lock = threading.Lock()
    spawn_idx = 0

    class BlockingProcess(FakeProcess):
        def __init__(self, idx: int, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._idx = idx

        def events(self):
            if self._idx == 1:
                first_started.set()
                release_first.wait(timeout=5)
            else:
                second_started.set()
                release_second.wait(timeout=5)
            yield from super().events()

    def fake_spawn(argv, env, cwd):
        nonlocal spawn_idx
        with counter_lock:
            spawn_idx += 1
            idx = spawn_idx
        return BlockingProcess(idx, _result("ok"), argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", fake_spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        a1 = reg.create_agent("login-1", ws, provider="claude_login")
        a2 = reg.create_agent("login-2", ws, provider="claude_login")
        r1 = a1.submit("first", verification="true")
        assert first_started.wait(timeout=5)
        r2 = a2.submit("second", verification="true")
        assert second_started.wait(timeout=5)

        release_first.set()
        assert r1.done.wait(timeout=5)
        assert r1.state == COMPLETED

        # While r2 is still active (and even after), target_dir remains denied.
        cred_file = str(target_dir / "credentials.json")
        assert classify("Read", {"file_path": cred_file}, ws, context=a2.guard_context).action == DENY
        assert classify("Write", {"file_path": cred_file}, ws, context=a2.guard_context).action == DENY

        release_second.set()
        assert r2.done.wait(timeout=5)
        assert r2.state == COMPLETED
        assert classify("Read", {"file_path": cred_file}, ws, context=a2.guard_context).action == DENY
    finally:
        release_first.set()
        release_second.set()
        reg.shutdown()
