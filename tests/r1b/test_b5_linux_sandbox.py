"""B5 acceptance tests: required sandbox refuses off macOS; claude/codex/omp run and doctor reports os_boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from subagent import config, sandbox
from subagent.cli import main
from subagent.providers import claude as claude_driver
from subagent.providers import codex as codex_driver
from subagent.providers import dsh as dsh_driver
from subagent.providers import omp as omp_driver
from subagent.providers.base import Session
from subagent.runs import Registry
from tests.conftest import make_settings


def _make_fake_bin(tmp_path: Path, name: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_b5_deny_read_refuses_off_macos(tmp_path: Path, monkeypatch) -> None:
    """B5.1: with sys.platform patched to linux, deny_read refuses to start naming the missing sandbox."""
    monkeypatch.setattr(sys, "platform", "linux")
    secret = str(tmp_path / "secret")

    with pytest.raises(RuntimeError, match=r"macOS|sandbox-exec"):
        sandbox.check_platform([secret])

    settings = make_settings(tmp_path)
    reg = Registry(settings, start_reaper=False)
    try:
        with pytest.raises(RuntimeError, match=r"macOS|sandbox-exec"):
            reg.create_agent("a1", tmp_path, provider="glm", deny_read=[secret])
    finally:
        reg.shutdown()


def test_b5_dsh_refuses_to_start_off_macos(tmp_path: Path, monkeypatch) -> None:
    """B5.2: with sys.platform patched to linux, the dsh driver refuses to start and never runs unsandboxed DSH."""
    monkeypatch.setattr(sys, "platform", "linux")

    ran_marker = tmp_path / "dsh-ran"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_dsh = bindir / "dsh"
    fake_dsh.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(ran_marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    fake_dsh.chmod(0o755)

    settings = make_settings(
        tmp_path,
        providers={
            "deepseek": {
                "driver": "dsh",
                "vendor": "deepseek",
                "binary": str(fake_dsh),
                "api_key_env": "DEEPSEEK_API_KEY",
                "experimental": True,
            },
        },
    )
    cfg = settings.providers["deepseek"]
    ws = tmp_path / "ws"
    ws.mkdir()

    error, _ = dsh_driver.DSH_PROVIDER.boot(settings, "a1", cfg)
    assert error is not None, "dsh driver boot() must refuse off macOS"
    assert any(tok in error.lower() for tok in ("macos", "sandbox", "darwin")), error

    with pytest.raises(RuntimeError, match=r"macOS|sandbox-exec|sandbox"):
        dsh_driver.DSH_PROVIDER.spawn(cfg, settings, "a1", "hi", ws, Session(provider="deepseek"), None)

    assert not ran_marker.exists(), "unsandboxed DSH process must never run"


def test_b5_claude_codex_omp_start_off_macos(tmp_path: Path, monkeypatch) -> None:
    """B5.3: with sys.platform patched to linux, claude, codex and omp drivers without deny_read still start."""
    monkeypatch.setattr(sys, "platform", "linux")

    claude_bin = _make_fake_bin(tmp_path, "claude")
    codex_bin = _make_fake_bin(tmp_path, "codex")
    omp_bin = _make_fake_bin(tmp_path, "omp")

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    ws = tmp_path / "ws"
    ws.mkdir()

    settings = make_settings(
        tmp_path,
        providers={
            "claude_p": {
                "driver": "claude",
                "vendor": "zai",
                "base_url": "http://127.0.0.1:1",
                "api_key": "k",
                "binary": str(claude_bin),
            },
            "codex_p": {
                "driver": "codex",
                "vendor": "codex",
                "binary": str(codex_bin),
            },
            "omp_p": {
                "driver": "omp",
                "vendor": "omlx",
                "base_url": "http://127.0.0.1:1",
                "api_key": "k",
                "model": "m",
                "binary": str(omp_bin),
            },
        },
    )

    monkeypatch.setattr(claude_driver, "_spawn_claude", lambda argv, env, cwd: ("claude", argv, env))
    monkeypatch.setattr(codex_driver, "_spawn_codex", lambda argv, env, cwd, prompt: ("codex", argv, env))
    monkeypatch.setattr(omp_driver, "_spawn_omp", lambda argv, env, cwd, session: ("omp", argv, env))

    for name, driver in (
        ("claude_p", claude_driver.CLAUDE_PROVIDER),
        ("codex_p", codex_driver.CODEX_PROVIDER),
        ("omp_p", omp_driver.OMP_PROVIDER),
    ):
        cfg = settings.providers[name]
        err, session = driver.boot(settings, f"agent-{name}", cfg)
        assert err is None, f"{name} failed to boot off macOS: {err}"
        proc = driver.spawn(cfg, settings, f"agent-{name}", "hi", ws, session, None)
        assert proc is not None


def test_b5_doctor_json_reports_os_boundary_by_platform(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """B5.4: subagent doctor --json reports os_boundary=false on linux and os_boundary=true on macOS for claude, codex, and omp."""
    claude_bin = _make_fake_bin(tmp_path, "claude")
    codex_bin = _make_fake_bin(tmp_path, "codex")
    omp_bin = _make_fake_bin(tmp_path, "omp")

    cfg_path = tmp_path / "doctor.toml"
    cfg_path.write_text(
        "[core]\n"
        'default_provider = "c"\n'
        f'session_root = "{tmp_path / "sessions"}"\n\n'
        "[providers.c]\n"
        'driver = "claude"\n'
        f'binary = "{claude_bin}"\n'
        'base_url = "http://127.0.0.1:1"\n'
        'api_key = "k"\n\n'
        "[providers.x]\n"
        'driver = "codex"\n'
        f'binary = "{codex_bin}"\n\n'
        "[providers.o]\n"
        'driver = "omp"\n'
        f'binary = "{omp_bin}"\n'
        'base_url = "http://127.0.0.1:1"\n'
        'api_key = "k"\n'
        'model = "m"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg_path))

    for plat, expected in (("linux", False), ("darwin", True)):
        monkeypatch.setattr(sys, "platform", plat)
        assert main(["doctor", "--no-probe", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        by_name = {row["name"]: row for row in payload["providers"]}
        for name in ("c", "x", "o"):
            assert "os_boundary" in by_name[name], f"missing os_boundary on {plat} for {name}"
            assert by_name[name]["os_boundary"] is expected, (
                f"expected os_boundary={expected} on {plat} for {name}, got {by_name[name]['os_boundary']!r}"
            )
