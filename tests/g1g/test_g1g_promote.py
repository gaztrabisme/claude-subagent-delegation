"""Acceptance tests for G1g: promote's static route check and lane probes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from subagent import cli, config
from subagent.promote import PromoteError, UnknownCommit, promote

from ..r6.test_d_promote import _patch_promote_runner
from ._helpers import (
    DOCTOR_HEAD,
    FakeRunner,
    make_repo,
    providers_table,
    routed_table,
    settings_for,
)

HINT = "; fix the lane, or pass --allow-failed-lane NAME or --skip-probe NAME"


def _missing_routes(settings):
    from subagent import promote as mod

    return mod.missing_routes(settings)


def _served(settings) -> Path:
    return Path(settings.session_root) / "served.json"


def _write_prior(settings, sha: str) -> dict:
    prior = {"commit": sha, "install_path": "/old", "promoted_at": "2026-11-25T00:00:00+00:00"}
    _served(settings).write_text(json.dumps(prior), encoding="utf-8")
    return prior


# --- 1. static route check ------------------------------------------------------


def test_missing_routes_names_in_config_order(tmp_path):
    table = providers_table()
    table["zeta"] = dict(table["nolane"])
    table["hostless"] = {**table["nolane"], "base_url": "not-a-url"}
    settings = settings_for(tmp_path, table)
    assert _missing_routes(settings) == ["nolane", "zeta", "hostless"]


def test_missing_routes_ignores_local_network_allow_and_base_url(tmp_path):
    settings = settings_for(tmp_path, routed_table())
    assert _missing_routes(settings) == []


def test_missing_routes_local_lane_without_route_is_fine(tmp_path):
    table = {"loc": providers_table()["loc"]}
    assert _missing_routes(settings_for(tmp_path, table)) == []


def test_require_routes_refuses_with_message_before_install(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=runner, require_routes=True)
    assert type(exc.value) is PromoteError and exc.value.code == 1
    assert str(exc.value) == (
        "no network route for nolane; add network_allow or set local = true"
    )
    assert runner.installs() == []
    assert not (Path(settings.session_root) / "installs").exists()
    assert not _served(settings).exists()


def test_require_routes_message_joins_several_names(tmp_path):
    table = providers_table()
    table["zeta"] = dict(table["nolane"])
    settings = settings_for(tmp_path, table)
    repo, sha = make_repo(tmp_path)
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=FakeRunner(settings), require_routes=True)
    assert str(exc.value) == (
        "no network route for nolane, zeta; add network_allow or set local = true"
    )


def test_require_routes_leaves_prior_served_json_unchanged(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    repo, sha = make_repo(tmp_path)
    prior = _write_prior(settings, "a" * 40)
    with pytest.raises(PromoteError):
        promote(repo, sha, settings, runner=FakeRunner(settings), require_routes=True)
    assert json.loads(_served(settings).read_text(encoding="utf-8")) == prior


def test_force_does_not_bypass_static_check(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    with pytest.raises(PromoteError, match="no network route"):
        promote(repo, sha, settings, runner=runner, force=True, require_routes=True)
    assert runner.installs() == []


def test_unknown_commit_wins_over_static_check(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    repo, _ = make_repo(tmp_path)
    with pytest.raises(UnknownCommit) as exc:
        promote(repo, "deadbeef00000000", settings, runner=FakeRunner(settings),
                require_routes=True)
    assert exc.value.code == 2


def test_static_check_off_by_default(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    repo, sha = make_repo(tmp_path)
    result = promote(repo, sha, settings, runner=FakeRunner(settings))
    assert result["commit"] == sha
    assert _served(settings).is_file()


def test_already_served_still_runs_static_check(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    repo, sha = make_repo(tmp_path)
    root = Path(settings.session_root) / "installs" / sha
    (root / "venv").mkdir(parents=True)
    _served(settings).write_text(
        json.dumps({"commit": sha, "install_path": str(root), "promoted_at": "x"}),
        encoding="utf-8",
    )
    runner = FakeRunner(settings)
    with pytest.raises(PromoteError, match="no network route for nolane"):
        promote(repo, sha, settings, runner=runner, require_routes=True, probe=True)
    assert runner.doctor_calls() == []


def test_already_served_runs_no_probes(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    root = Path(settings.session_root) / "installs" / sha
    (root / "venv").mkdir(parents=True)
    existing = {"commit": sha, "install_path": str(root), "promoted_at": "x"}
    _served(settings).write_text(json.dumps(existing), encoding="utf-8")
    runner = FakeRunner(settings)
    result = promote(repo, sha, settings, runner=runner, require_routes=True, probe=True)
    assert result == existing
    assert runner.doctor_calls() == []
    assert runner.installs() == []


# --- 2. lane probes -------------------------------------------------------------


def test_probe_argv_and_kwargs_exact(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    promote(repo, sha, settings, runner=runner, probe=True)
    calls = runner.doctor_calls()
    assert calls[0] == [sys.executable, "-m", "subagent", "doctor", "--provider", "alpha",
                        "--prompt"]
    doctor_kwargs = [kw for c, kw in zip(runner.calls, runner.kwargs, strict=True)
                     if c in calls]
    for kw in doctor_kwargs:
        assert kw == {"capture_output": True, "text": True, "timeout": 400}


def test_probes_only_non_local_lanes_in_config_order(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    promote(repo, sha, settings, runner=runner, probe=True)
    assert runner.probed() == ["alpha", "beta", "gamma"]


def test_probes_run_after_install_and_before_served_json(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    promote(repo, sha, settings, runner=runner, probe=True)
    assert runner.served_at_probe == [False, False, False]
    assert runner.installed_before_probe == [True, True, True]
    assert _served(settings).is_file()
    last_install = max(i for i, c in enumerate(runner.calls) if "pip" in c)
    first_probe = min(i for i, c in enumerate(runner.calls) if c in runner.doctor_calls())
    assert last_install < first_probe


def test_probe_off_by_default(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    promote(repo, sha, settings, runner=runner)
    assert runner.doctor_calls() == []


def test_failed_install_runs_no_probe(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, fail_uv=True)
    with pytest.raises(PromoteError, match="failed"):
        promote(repo, sha, settings, runner=runner, probe=True)
    assert runner.doctor_calls() == []
    assert not _served(settings).exists()


def test_probe_failure_message_uses_stderr_first_nonempty_line(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, doctor={"beta": (3, "out line\n", "\n  \nboom here\nsecond\n")})
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=runner, probe=True)
    assert exc.value.code == 1
    assert str(exc.value) == "lane probe failed: beta (boom here)" + HINT


def test_probe_failure_falls_back_to_stdout_then_exit_code(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, doctor={
        "alpha": (2, "\nfrom stdout\nmore\n", " \n"),
        "gamma": (7, "", ""),
    })
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=runner, probe=True)
    assert str(exc.value) == (
        "lane probe failed: alpha (from stdout); lane probe failed: gamma (exit 7)" + HINT
    )
    assert runner.probed() == ["alpha", "beta", "gamma"]


def test_probe_timeout_is_a_failure(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    timeout = subprocess.TimeoutExpired(DOCTOR_HEAD, 400)
    runner = FakeRunner(settings, doctor={"beta": timeout})
    prior = _write_prior(settings, "a" * 40)
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=runner, probe=True)
    message = str(exc.value)
    assert message.startswith("lane probe failed: beta (")
    assert message.endswith(HINT)
    assert json.loads(_served(settings).read_text(encoding="utf-8")) == prior


def test_probe_failure_leaves_served_json_unchanged_and_force_does_not_bypass(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    prior = _write_prior(settings, "a" * 40)
    runner = FakeRunner(settings, doctor={"alpha": (1, "", "bad")})
    with pytest.raises(PromoteError, match="lane probe failed: alpha"):
        promote(repo, sha, settings, runner=runner, force=True, probe=True)
    assert json.loads(_served(settings).read_text(encoding="utf-8")) == prior


def test_probe_failure_without_prior_served_writes_nothing(tmp_path):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, doctor={"gamma": (1, "", "bad")})
    with pytest.raises(PromoteError):
        promote(repo, sha, settings, runner=runner, probe=True)
    assert not _served(settings).exists()


def test_allow_failed_lane_passes_with_warning(tmp_path, capsys):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, doctor={"beta": (1, "", "bad")})
    result = promote(repo, sha, settings, runner=runner, probe=True, allow_failed=["beta"])
    out = capsys.readouterr().out.splitlines()
    assert "warning: lane probe failed for beta, allowed" in out
    assert result["commit"] == sha
    assert _served(settings).is_file()


def test_allow_failed_covers_timeout_and_other_failures_still_raise(tmp_path, capsys):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    timeout = subprocess.TimeoutExpired(DOCTOR_HEAD, 400)
    runner = FakeRunner(settings, doctor={"beta": timeout, "gamma": (1, "", "bad")})
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=runner, probe=True, allow_failed=("beta",))
    assert str(exc.value) == "lane probe failed: gamma (bad)" + HINT
    assert "warning: lane probe failed for beta, allowed" in capsys.readouterr().out
    assert not _served(settings).exists()


def test_allowed_lane_that_passes_prints_no_warning(tmp_path, capsys):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    promote(repo, sha, settings, runner=FakeRunner(settings), probe=True, allow_failed=["beta"])
    assert "warning: lane probe failed" not in capsys.readouterr().out


def test_skip_probe_skips_lane_with_warning(tmp_path, capsys):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, doctor={"beta": (1, "", "would fail")})
    promote(repo, sha, settings, runner=runner, probe=True, skip_probe=["beta"])
    assert runner.probed() == ["alpha", "gamma"]
    assert "warning: lane probe skipped for beta" in capsys.readouterr().out.splitlines()
    assert _served(settings).is_file()


@pytest.mark.parametrize("kwargs", [
    {"skip_probe": ["nope"]},
    {"allow_failed": ["nope"]},
    {"skip_probe": ["alpha"], "allow_failed": ["nope"]},
])
def test_unknown_names_raise_before_install(tmp_path, kwargs):
    settings = settings_for(tmp_path)
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    with pytest.raises(PromoteError) as exc:
        promote(repo, sha, settings, runner=runner, probe=True, **kwargs)
    assert exc.value.code == 1
    assert str(exc.value) == "unknown provider nope"
    assert runner.installs() == []
    assert runner.doctor_calls() == []
    assert not _served(settings).exists()


# --- 3. CLI ---------------------------------------------------------------------


def _cli_setup(tmp_path, monkeypatch, table=None, doctor=None, fail_uv=False):
    from subagent import promote as mod

    settings = settings_for(tmp_path, table)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings, doctor=doctor, fail_uv=fail_uv)
    _patch_promote_runner(monkeypatch, mod, runner)
    return settings, repo, sha, runner


def _run(repo, *args):
    return cli.main(["--root", str(repo), "promote", *args])


def test_cli_defaults_probe_every_cloud_lane(tmp_path, monkeypatch, capsys):
    settings, repo, sha, runner = _cli_setup(tmp_path, monkeypatch)
    assert _run(repo, sha) == 0
    assert runner.probed() == ["alpha", "beta", "gamma"]
    assert sha[:7] in capsys.readouterr().out
    assert _served(settings).is_file()


def test_cli_default_requires_routes(tmp_path, monkeypatch, capsys):
    settings, repo, sha, runner = _cli_setup(tmp_path, monkeypatch, table=providers_table())
    assert _run(repo, sha) == 1
    err = capsys.readouterr().err
    assert "error: no network route for nolane; add network_allow or set local = true" in err
    assert runner.installs() == []
    assert not _served(settings).exists()


def test_cli_unknown_commit_exits_2_before_route_check(tmp_path, monkeypatch, capsys):
    _, repo, _, _ = _cli_setup(tmp_path, monkeypatch, table=providers_table())
    assert _run(repo, "deadbeef00000000") == 2


def test_cli_force_does_not_bypass_route_check(tmp_path, monkeypatch):
    _, repo, sha, runner = _cli_setup(tmp_path, monkeypatch, table=providers_table())
    assert _run(repo, sha, "--force") == 1
    assert runner.installs() == []


def test_cli_no_probe_warns_and_runs_no_doctor(tmp_path, monkeypatch, capsys):
    settings, repo, sha, runner = _cli_setup(
        tmp_path, monkeypatch, doctor={"alpha": (1, "", "bad")}
    )
    assert _run(repo, sha, "--no-probe") == 0
    out = capsys.readouterr().out
    assert "warning: lane probes skipped (--no-probe)" in out.splitlines()
    assert runner.doctor_calls() == []
    assert _served(settings).is_file()


def test_cli_no_probe_still_checks_routes(tmp_path, monkeypatch, capsys):
    _, repo, sha, _ = _cli_setup(tmp_path, monkeypatch, table=providers_table())
    assert _run(repo, sha, "--no-probe") == 1
    assert "no network route" in capsys.readouterr().err


def test_cli_probe_failure_exits_1_with_stderr_message(tmp_path, monkeypatch, capsys):
    settings, repo, sha, _ = _cli_setup(
        tmp_path, monkeypatch, doctor={"beta": (4, "", "no host\n")}
    )
    assert _run(repo, sha) == 1
    assert capsys.readouterr().err.strip() == (
        "error: lane probe failed: beta (no host)" + HINT
    )
    assert not _served(settings).exists()


def test_cli_skip_probe_is_repeatable(tmp_path, monkeypatch, capsys):
    _, repo, sha, runner = _cli_setup(tmp_path, monkeypatch)
    assert _run(repo, sha, "--skip-probe", "alpha", "--skip-probe", "gamma") == 0
    assert runner.probed() == ["beta"]
    out = capsys.readouterr().out.splitlines()
    assert "warning: lane probe skipped for alpha" in out
    assert "warning: lane probe skipped for gamma" in out


def test_cli_allow_failed_lane_is_repeatable(tmp_path, monkeypatch, capsys):
    settings, repo, sha, _ = _cli_setup(
        tmp_path, monkeypatch, doctor={"alpha": (1, "", "x"), "gamma": (1, "", "y")}
    )
    rc = _run(repo, sha, "--allow-failed-lane", "alpha", "--allow-failed-lane", "gamma")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert "warning: lane probe failed for alpha, allowed" in out
    assert "warning: lane probe failed for gamma, allowed" in out
    assert _served(settings).is_file()


def test_cli_unknown_lane_name_exits_1_before_install(tmp_path, monkeypatch, capsys):
    _, repo, sha, runner = _cli_setup(tmp_path, monkeypatch)
    assert _run(repo, sha, "--skip-probe", "nope") == 1
    assert "error: unknown provider nope" in capsys.readouterr().err
    assert runner.installs() == []
    assert _run(repo, sha, "--allow-failed-lane", "nope") == 1
    assert runner.installs() == []


def test_cli_install_failure_runs_no_probe(tmp_path, monkeypatch):
    settings, repo, sha, runner = _cli_setup(tmp_path, monkeypatch, fail_uv=True)
    assert _run(repo, sha) == 1
    assert runner.doctor_calls() == []
    assert not _served(settings).exists()
