"""A11 acceptance tests: codex danger-full-access only when wrapped with A10 network deny."""

from __future__ import annotations

from pathlib import Path

from subagent import sandbox
from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import Registry
from tests.conftest import make_settings


def _codex_setup(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake_bin = tmp_path / "bin" / "codex"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    codex_home = tmp_path / "user-codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers={
            "codex": {
                "driver": "codex",
                "binary": str(fake_bin),
                "network_allow": ["api.openai.com"],
            }
        },
        supervisor="off",
    )
    return ws, fake_bin, settings


def test_a11_codex_uses_danger_full_access_only_when_wrapped_with_network_deny(
    tmp_path: Path, monkeypatch
) -> None:
    """A11: on darwin wrapped with A10 network deny, codex uses danger-full-access; without wrap it keeps workspace-write."""
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    ws, fake_bin, settings = _codex_setup(tmp_path, monkeypatch)

    captured: list[list[str]] = []

    def fake_start(self, *, stdin=False, closed_stdin=False):
        captured.append(list(self.argv))
        raise RuntimeError("captured")

    monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("c-darwin", ws, provider="codex")
        run = agent.submit("task", verification="true")
        run.done.wait(timeout=5)
    finally:
        reg.shutdown()

    assert captured
    argv = captured[0]
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]
    assert "network-outbound" in profile
    inner = argv[3:]
    assert inner[inner.index("-s") + 1] == "danger-full-access"

    # Unwrapped argv() or off-macOS spawn without a profile wrap must keep workspace-write.
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    captured.clear()
    reg2 = Registry(settings, start_reaper=False)
    try:
        agent2 = reg2.create_agent("c-linux", ws, provider="codex")
        run2 = agent2.submit("task", verification="true")
        run2.done.wait(timeout=5)
    finally:
        reg2.shutdown()

    assert captured
    linux_argv = captured[0]
    assert linux_argv[0] != "/usr/bin/sandbox-exec"
    assert linux_argv[linux_argv.index("-s") + 1] == "workspace-write"
    assert "danger-full-access" not in linux_argv


def test_a11_codex_argv_method_keeps_workspace_write_even_with_deny_read_off_macos(
    tmp_path: Path, monkeypatch
) -> None:
    """A11: CodexProvider.argv does not emit danger-full-access unless wrapped with A10 network deny."""
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    ws, fake_bin, settings = _codex_setup(tmp_path, monkeypatch)
    driver = for_driver("codex")
    cfg = ProviderConfig(
        name="codex",
        driver="codex",
        vendor="codex",
        binary=str(fake_bin),
        deny_read=(str(tmp_path / "secret"),),
    )
    argv = driver.argv(cfg, settings, "a11-linux", "task", ws, Session(provider="codex"), None)
    if argv[:2] != ["/usr/bin/sandbox-exec", "-p"]:
        assert argv[argv.index("-s") + 1] == "workspace-write"
        assert "danger-full-access" not in argv
