"""Q6 acceptance tests: per-agent temp folder and no /private/tmp or server TMPDIR in dsh."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from tests.conftest import make_settings

from ._helpers import sbpl_allows_write


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_q6_dsh_profile_and_env_use_per_agent_tmp_only(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """Q6: dsh profile write-allows <session_root>/agents/<id>/tmp (0700) and denies /tmp & TMPDIR."""
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")
    server_tmp = (tmp_path / "server-tmpdir").resolve()
    server_tmp.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(server_tmp))

    if login:
        owner_home = tmp_path / "owner-dsh"
        owner_home.mkdir()
        (owner_home / ".credentials.yaml").write_text("version: 1\n", encoding="utf-8")
        monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-q6")

    ws = tmp_path / "ws"
    ws.mkdir()
    settings = make_settings(tmp_path)
    cfg = ProviderConfig(
        name="deepseek-login" if login else "deepseek",
        driver="dsh",
        vendor="deepseek",
        binary="/fake/bin/dsh",
        api_key_envs=() if login else ("DEEPSEEK_API_KEY",),
        max_agents=1 if login else 4,
        experimental=True,
        extra={"auth": "login"} if login else {},
    )
    driver = for_driver("dsh")

    old_umask = os.umask(0o022)
    try:
        process = driver.spawn(
            cfg, settings, "agent-q6", "task", ws, Session(provider=cfg.name), None
        )
    finally:
        os.umask(old_umask)

    expected_tmp = (settings.session_root / "agents" / "agent-q6" / "tmp").resolve()
    assert expected_tmp.is_dir(), f"expected per-agent tmp dir at {expected_tmp}"
    assert stat.S_IMODE(expected_tmp.stat().st_mode) == 0o700
    assert Path(process.env["TMPDIR"]).resolve() == expected_tmp

    assert process.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = process.argv[2]

    for forbidden in (Path("/private/tmp"), Path("/tmp"), server_tmp):
        assert not sbpl_allows_write(profile, forbidden), (
            f"dsh profile must not allow writes to {forbidden}"
        )
        assert not sbpl_allows_write(profile, forbidden / "probe.txt"), (
            f"dsh profile must not allow writes under {forbidden}"
        )
    assert '"/private/tmp"' not in profile
    assert '"/tmp"' not in profile
    assert str(server_tmp) not in profile

    assert sbpl_allows_write(profile, expected_tmp)
    assert sbpl_allows_write(profile, expected_tmp / "scratch.txt")
