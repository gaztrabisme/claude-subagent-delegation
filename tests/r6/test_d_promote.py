"""Acceptance tests for R6(d): `subagent promote` and `served_commit` in MCP `list`."""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import pytest

from subagent import cli, config
from subagent.lane_status import LaneStatusStore
from subagent.runs import Registry

from ..conftest import make_settings
from ._helpers import commit_files, init_git_repo


def _promote_mod():
    """Import `subagent.promote` lazily so collection succeeds on the base commit."""
    return importlib.import_module("subagent.promote")


def _recording_runner(recorded: list[list[str]], *, fail_install: bool = False):
    def runner(argv: list[str], **kwargs: Any):
        cmd = [str(part) for part in argv]
        recorded.append(cmd)
        if Path(cmd[0]).name in ("git", "tar"):
            return subprocess.run(cmd, **kwargs)
        rc = 1 if fail_install else 0
        stderr = "install failed" if fail_install else ""
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)

    return runner


def _patch_promote_runner(monkeypatch, promote_mod, runner) -> None:
    kwdefaults = getattr(promote_mod.promote, "__kwdefaults__", None)
    if isinstance(kwdefaults, dict) and "runner" in kwdefaults:
        monkeypatch.setitem(kwdefaults, "runner", runner)
    if hasattr(promote_mod, "subprocess"):
        monkeypatch.setattr(promote_mod.subprocess, "run", runner)


def test_d_promote_cli_and_module_exist(capsys):
    mod = _promote_mod()
    assert callable(getattr(mod, "promote", None))
    assert callable(getattr(mod, "served", None))
    with pytest.raises(SystemExit) as exc:
        cli.main(["promote", "--help"])
    captured = capsys.readouterr()
    assert exc.value.code == 0, captured.err


def test_d_promote_exports_exact_commit_non_editable_and_writes_served_json(
    tmp_path: Path, monkeypatch
):
    mod = _promote_mod()
    repo = tmp_path / "repo"
    sha1 = init_git_repo(repo, {"pkg.txt": "v1\n"})
    commit_files(repo, {"pkg.txt": "v2\n"}, "second")
    # Dirty the working tree so an install from `.` instead of `sha1` would be wrong.
    (repo / "pkg.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    settings = make_settings(tmp_path)
    recorded: list[list[str]] = []
    result = mod.promote(repo, sha1[:7], settings, runner=_recording_runner(recorded))

    expected_install = settings.session_root / "installs" / sha1
    assert recorded, "expected runner to receive export/install commands"
    for argv in recorded:
        assert "-e" not in argv
        assert "--editable" not in argv
        assert not any(arg.startswith("--editable=") for arg in argv)
    assert any(
        str(expected_install) in arg for argv in recorded for arg in argv
    ), f"expected install path {expected_install} in recorded argv: {recorded}"
    assert any(
        sha1 in arg for argv in recorded for arg in argv
    ), f"expected exact sha {sha1} in export argv: {recorded}"

    served_path = settings.session_root / "served.json"
    assert served_path.is_file()
    assert not list(settings.session_root.glob("*.tmp"))
    on_disk = json.loads(served_path.read_text(encoding="utf-8"))
    assert on_disk["commit"] == sha1
    assert Path(on_disk["install_path"]) == expected_install
    datetime.fromisoformat(on_disk["promoted_at"].replace("Z", "+00:00"))
    assert result == on_disk
    assert mod.served(settings) == on_disk


def test_d_promote_unknown_commit_exits_2_and_writes_nothing(tmp_path: Path, monkeypatch):
    _promote_mod()
    repo = tmp_path / "repo"
    init_git_repo(repo, {"pkg.txt": "v1\n"})
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))

    rc = cli.main(["--root", str(repo), "promote", "deadbeef00000000"])
    assert rc == 2
    assert not (settings.session_root / "served.json").exists()
    assert not (settings.session_root / "installs").exists()


def test_d_promote_cli_prints_short_sha_and_exits_0(tmp_path: Path, monkeypatch, capsys):
    mod = _promote_mod()
    repo = tmp_path / "repo"
    sha = init_git_repo(repo, {"pkg.txt": "v1\n"})
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))

    recorded: list[list[str]] = []
    _patch_promote_runner(monkeypatch, mod, _recording_runner(recorded))

    rc = cli.main(["--root", str(repo), "promote", sha[:7]])
    out = capsys.readouterr().out
    assert rc == 0
    assert sha[:7] in out
    assert (settings.session_root / "served.json").is_file()


def test_d_promote_runner_failure_exits_1_and_leaves_served_json_unchanged(
    tmp_path: Path, monkeypatch
):
    mod = _promote_mod()
    repo = tmp_path / "repo"
    sha1 = init_git_repo(repo, {"pkg.txt": "v1\n"})
    sha2 = commit_files(repo, {"pkg.txt": "v2\n"}, "second")
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))

    prior = {
        "commit": sha1,
        "install_path": str(settings.session_root / "installs" / sha1),
        "promoted_at": "2026-11-25T00:00:00+00:00",
    }
    served_path = settings.session_root / "served.json"
    served_path.write_text(json.dumps(prior), encoding="utf-8")

    recorded: list[list[str]] = []
    _patch_promote_runner(monkeypatch, mod, _recording_runner(recorded, fail_install=True))

    rc = cli.main(["--root", str(repo), "promote", sha2])
    assert rc == 1
    assert json.loads(served_path.read_text(encoding="utf-8")) == prior


def test_d_promote_refuses_live_runs_unless_force(tmp_path: Path, monkeypatch, capsys):
    mod = _promote_mod()
    repo = tmp_path / "repo"
    sha = init_git_repo(repo, {"pkg.txt": "v1\n"})
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))

    recorded: list[list[str]] = []
    _patch_promote_runner(monkeypatch, mod, _recording_runner(recorded))

    store = LaneStatusStore(settings.session_root)
    with store.active_run("glm"):
        rc_refused = cli.main(["--root", str(repo), "promote", sha])
        captured = capsys.readouterr()
        assert rc_refused == 1
        assert "glm" in captured.out + captured.err
        assert not (settings.session_root / "served.json").exists()

        rc_forced = cli.main(["--root", str(repo), "promote", sha, "--force"])
        forced_out = capsys.readouterr()
        assert rc_forced == 0
        assert "live" in (forced_out.out + forced_out.err).lower()
        assert (settings.session_root / "served.json").is_file()


def test_d_served_returns_none_when_missing(tmp_path: Path):
    mod = _promote_mod()
    settings = make_settings(tmp_path)
    assert mod.served(settings) is None


def test_d_mcp_list_agents_reports_served_commit(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    reg = Registry(settings, start_reaper=False)
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", reg)
    try:
        without_served = anyio.run(module.list_agents)
        assert "served_commit" in without_served
        assert without_served["served_commit"] is None

        sha = "b" * 40
        (settings.session_root / "served.json").write_text(
            json.dumps(
                {
                    "commit": sha,
                    "install_path": str(settings.session_root / "installs" / sha),
                    "promoted_at": "2026-11-25T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with_served = anyio.run(module.list_agents)
        assert with_served["served_commit"] == sha
    finally:
        reg.shutdown()
