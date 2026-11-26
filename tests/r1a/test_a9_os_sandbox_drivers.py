"""A9 acceptance tests: every hook driver under macOS sandbox-exec with write/read/net rules."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from subagent import sandbox
from subagent.providers import for_driver
from subagent.runs import Registry
from tests.conftest import make_settings

from ._helpers import sbpl_allows

HOOK_DRIVERS = ("claude", "codex", "omp", "dsh")


def _setup_providers(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# ws\n", encoding="utf-8")

    fake_bin = tmp_path / "bin" / "fake"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    codex_home = tmp_path / "user-codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")

    dsh_owner = tmp_path / "owner-dsh"
    dsh_owner.mkdir()

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("DSH_OWNER_HOME", str(dsh_owner))
    monkeypatch.setenv("GLM_API_KEY", "test-glm")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh")

    providers = {
        "claude": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake_bin),
        },
        "codex": {
            "driver": "codex",
            "binary": str(fake_bin),
        },
        "omp": {
            "driver": "omp",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen-test",
            "binary": str(fake_bin),
            "experimental": True,
        },
        "dsh": {
            "driver": "dsh",
            "vendor": "deepseek",
            "api_key_env": "DEEPSEEK_API_KEY",
            "binary": str(fake_bin),
            "experimental": True,
        },
        "antigravity": {
            "driver": "antigravity",
            "binary": str(fake_bin),
            "experimental": True,
        },
    }
    sock_dir = (tmp_path / "sockdir").resolve()
    sock_dir.mkdir(mode=0o700)
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers=providers,
        approval_socket=str(sock_dir / "approval.sock"),
        max_agents=8,
        supervisor="off",
        trace="off",
    )
    return ws, sock_dir, settings


def _capture_spawn_argv(monkeypatch, settings, ws: Path, provider_name: str, agent_id: str) -> list[str]:
    captured: list[list[str]] = []

    def fake_start(self, *, stdin=False, closed_stdin=False):
        captured.append(list(self.argv))
        raise RuntimeError("captured-spawn")

    monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent(provider_name, ws, provider=provider_name, agent_id=agent_id)
        run = agent.submit("do work", verification="true")
        run.done.wait(timeout=5)
    finally:
        reg.shutdown()
    assert captured, f"{provider_name} did not invoke Process._start"
    return captured[0]


@pytest.mark.parametrize("driver_name", HOOK_DRIVERS)
def test_a9_darwin_all_hook_drivers_wrapped_even_without_deny_read(
    tmp_path: Path, monkeypatch, driver_name: str
) -> None:
    """A9: on macOS, claude/codex/omp/dsh spawned argv starts with sandbox-exec even without deny_read."""
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    ws, sock_dir, settings = _setup_providers(tmp_path, monkeypatch)

    argv = _capture_spawn_argv(monkeypatch, settings, ws, driver_name, f"a9-{driver_name}")
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"], f"{driver_name}: {argv[:4]}"
    profile = argv[2]

    # A9.2: approval socket directory write-denied
    assert not sbpl_allows(profile, "file-write*", sock_dir)
    assert not sbpl_allows(profile, "file-write*", sock_dir / "approval.sock")

    # A9.1: writes outside write roots are denied, workspace writes allowed
    outside = tmp_path / "outside-secret.txt"
    assert not sbpl_allows(profile, "file-write*", outside)
    assert sbpl_allows(profile, "file-write*", ws / "allowed.txt")

    # A9.3: other agent's directory under <session_root>/agents/ is read-denied
    other_agent_dir = settings.session_root / "agents" / "other-agent"
    own_agent_dir = settings.session_root / "agents" / f"a9-{driver_name}"
    assert not sbpl_allows(profile, "file-read*", other_agent_dir / "approval.key")
    assert sbpl_allows(profile, "file-read*", own_agent_dir / "approval.key")


@pytest.mark.parametrize("driver_name", HOOK_DRIVERS)
def test_a9_1_each_driver_exposes_sandbox_write_roots(
    tmp_path: Path, monkeypatch, driver_name: str
) -> None:
    """A9.1: each hook driver exposes sandbox_write_roots(settings, agent_id, cfg, cwd) -> list[str]."""
    ws, _, settings = _setup_providers(tmp_path, monkeypatch)
    mod = importlib.import_module(f"subagent.providers.{driver_name}")
    driver = for_driver(driver_name)
    fn = getattr(mod, "sandbox_write_roots", None) or getattr(driver, "sandbox_write_roots", None)
    assert callable(fn), f"{driver_name} must expose sandbox_write_roots(settings, agent_id, cfg, cwd)"
    roots = fn(settings, f"roots-{driver_name}", settings.provider(driver_name), ws)
    assert isinstance(roots, list)
    assert all(isinstance(r, str) and r for r in roots)
    assert str(ws) in roots or str(ws.resolve()) in roots


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt runtime check")
@pytest.mark.parametrize("driver_name", ["claude", "codex", "omp", "dsh"])
def test_a9_3_real_seatbelt_blocks_cross_agent_read_and_outside_write(
    tmp_path: Path, monkeypatch, driver_name: str
) -> None:
    """A9.1/A9.3: live sandbox-exec on macOS blocks reading sibling agent dir and writing outside roots."""
    ws, _, settings = _setup_providers(tmp_path, monkeypatch)
    other_dir = settings.session_root / "agents" / "sibling-agent"
    other_dir.mkdir(parents=True, exist_ok=True)
    secret_file = other_dir / "secret.txt"
    secret_file.write_text("other-agent-secret\n", encoding="utf-8")

    outside_file = tmp_path / "outside.txt"
    argv = _capture_spawn_argv(monkeypatch, settings, ws, driver_name, f"live-{driver_name}")
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]
    assert not sbpl_allows(profile, "file-read*", secret_file)
    assert not sbpl_allows(profile, "file-write*", outside_file)

    can_nest = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        timeout=5,
    ).returncode == 0
    if not can_nest:
        pytest.skip("Seatbelt cannot nest in this runner; the live A9.3 probe did not run")

    probe = (
        "import pathlib, sys\n"
        f"p_other = pathlib.Path({str(secret_file)!r})\n"
        f"p_out = pathlib.Path({str(outside_file)!r})\n"
        "try:\n"
        "    p_other.read_text()\n"
        "    sys.exit(10)\n"
        "except PermissionError:\n"
        "    pass\n"
        "try:\n"
        "    p_out.write_text('x')\n"
        "    sys.exit(11)\n"
        "except PermissionError:\n"
        "    pass\n"
        "sys.exit(0)\n"
    )
    proc = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, f"{driver_name} rc={proc.returncode} stderr={proc.stderr!r}"


def test_a9_antigravity_keeps_sandbox_flag(tmp_path: Path, monkeypatch) -> None:
    """A9: antigravity driver argv keeps --sandbox."""
    ws, _, settings = _setup_providers(tmp_path, monkeypatch)
    driver = for_driver("antigravity")
    cfg = settings.provider("antigravity")
    _, session = driver.boot(settings, "agy-1", cfg)
    argv = driver.argv(cfg, settings, "agy-1", "task", ws, session, cfg.model)
    assert "--sandbox" in argv


@pytest.mark.parametrize("driver_name", ["claude", "codex", "omp"])
def test_a9_off_macos_behaves_as_base_without_deny_read(
    tmp_path: Path, monkeypatch, driver_name: str
) -> None:
    """A9: off macOS without deny_read, claude/codex/omp are not wrapped in sandbox-exec."""
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    ws, _, settings = _setup_providers(tmp_path, monkeypatch)
    argv = _capture_spawn_argv(monkeypatch, settings, ws, driver_name, f"linux-{driver_name}")
    assert argv[0] != "/usr/bin/sandbox-exec"
