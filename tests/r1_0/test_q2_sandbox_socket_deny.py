"""Q2 acceptance tests: approval socket directory write-denied in every Seatbelt profile."""

from __future__ import annotations

from pathlib import Path

from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import COMPLETED, Registry
from tests.conftest import make_settings
from tests.test_runs import FakeProcess, _result

from ._helpers import sbpl_allows_write


def test_q2_dsh_argv_profile_denies_write_on_socket_directory(tmp_path: Path, monkeypatch) -> None:
    """Q2: dsh Seatbelt profile explicitly denies file-write* on the socket directory."""
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-q2")

    sock_dir = (tmp_path / "sockdir").resolve()
    sock_dir.mkdir(mode=0o700)
    private = tmp_path / "private"
    private.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    settings = make_settings(
        tmp_path,
        approval_socket=str(sock_dir / "approval.sock"),
    )
    driver = for_driver("dsh")

    for deny_read in ((), (str(private),)):
        cfg = ProviderConfig(
            name="deepseek",
            driver="dsh",
            vendor="deepseek",
            binary="/fake/bin/dsh",
            api_key_envs=("DEEPSEEK_API_KEY",),
            deny_read=deny_read,
            experimental=True,
        )
        argv = driver.argv(cfg, settings, "q2-dsh", "task", ws, Session(provider="deepseek"), None)
        assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
        profile = argv[2]
        assert f'(deny file-write* (subpath "{sock_dir}"))' in profile
        assert not sbpl_allows_write(profile, sock_dir)
        assert not sbpl_allows_write(profile, sock_dir / "approval.sock")


def test_q2_runs_deny_read_wrap_denies_write_on_socket_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """Q2: runs.py deny_read Seatbelt wrap denies file-write* on the socket directory."""
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")

    sock_dir = (tmp_path / "sockdir").resolve()
    sock_dir.mkdir(mode=0o700)
    private = tmp_path / "private"
    private.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    settings = make_settings(
        tmp_path,
        approval_socket=str(sock_dir / "approval.sock"),
    )
    captured: list[FakeProcess] = []

    def fake_spawn(argv: list[str], env: dict[str, str], cwd: str) -> FakeProcess:
        proc = FakeProcess(_result("done"), argv, env)
        captured.append(proc)
        return proc

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", fake_spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("q2-runs", ws, provider="glm", deny_read=[str(private)])
        run = agent.submit("do work", verification="true")
        assert run.done.wait(5), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED
    finally:
        reg.shutdown()

    assert len(captured) == 1
    argv = captured[0].argv
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]
    assert f'(deny file-write* (subpath "{sock_dir}"))' in profile
    assert not sbpl_allows_write(profile, sock_dir)
    assert not sbpl_allows_write(profile, sock_dir / "approval.sock")
