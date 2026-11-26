"""Held lanes and lanes under an unexpired closure are not probed by promote."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from subagent import cli, config
from subagent.lane_state import LaneState
from subagent.promote import PromoteError, promote

from ..g1g._helpers import FakeRunner, make_repo, providers_table, routed_table, settings_for
from ..r6.test_d_promote import _patch_promote_runner

REASON = "H7 until 2026-12-24"


def _table(**held):
    """routed_table with `held` values set per lane name."""
    table = routed_table()
    for name, value in held.items():
        table[name] = {**table[name], "held": value}
    return table


def _now() -> datetime:
    return datetime.now().astimezone()


def _close(settings, lane, days=2):
    LaneState(Path(settings.session_root)).close(
        lane, _now() + timedelta(days=days), "quota", "spent"
    )


def _closed_text_forms(settings, lane) -> set[str]:
    until = LaneState(Path(settings.session_root)).status_entries()[0][lane]["closed_until"]
    return {str(until), until.isoformat()}


def _closed_line_ok(lines, settings, lane) -> bool:
    forms = _closed_text_forms(settings, lane)
    return any(
        f"warning: lane probe skipped for {lane} (closed until {form})" in lines
        for form in forms
    )


def _write_state(settings, data):
    path = Path(settings.session_root) / "lane_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _run(settings, tmp_path, **kwargs):
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    promote(repo, sha, settings, runner=runner, probe=True, **kwargs)
    return runner


# --- held ------------------------------------------------------------------------


def test_held_lane_is_not_probed_and_warns(tmp_path, capsys):
    settings = settings_for(tmp_path, _table(beta=REASON))
    runner = _run(settings, tmp_path)
    assert runner.probed() == ["alpha", "gamma"]
    lines = capsys.readouterr().out.splitlines()
    assert f"warning: lane probe skipped for beta (held: {REASON})" in lines


@pytest.mark.parametrize("value", ["", 5, True, ["x"], {"a": "b"}])
def test_held_non_string_or_empty_is_ignored(tmp_path, capsys, value):
    settings = settings_for(tmp_path, _table(beta=value))
    runner = _run(settings, tmp_path)
    assert runner.probed() == ["alpha", "beta", "gamma"]
    assert "lane probe skipped" not in capsys.readouterr().out


def test_config_with_held_loads_and_keeps_it_in_extra(tmp_path):
    settings = settings_for(tmp_path, _table(beta=REASON))
    assert settings.providers["beta"].extra["held"] == REASON


# --- closed until later ----------------------------------------------------------


def test_unexpired_closure_is_not_probed_and_warns(tmp_path, capsys):
    settings = settings_for(tmp_path)
    _close(settings, "gamma")
    runner = _run(settings, tmp_path)
    assert runner.probed() == ["alpha", "beta"]
    lines = capsys.readouterr().out.splitlines()
    assert _closed_line_ok(lines, settings, "gamma"), lines


def test_expired_closure_is_still_probed(tmp_path, capsys):
    settings = settings_for(tmp_path)
    past = _now() - timedelta(days=1)
    _write_state(settings, {"gamma": {
        "closed_until": past.isoformat(), "code": "quota", "message": "m",
        "set_at": (past - timedelta(days=1)).isoformat(),
    }})
    runner = _run(settings, tmp_path)
    assert runner.probed() == ["alpha", "beta", "gamma"]
    assert "lane probe skipped" not in capsys.readouterr().out


def test_closure_without_closed_until_is_still_probed(tmp_path, capsys):
    settings = settings_for(tmp_path)
    _write_state(settings, {"gamma": {"code": "probe_failed", "message": "m"}})
    runner = _run(settings, tmp_path)
    assert runner.probed() == ["alpha", "beta", "gamma"]
    assert "lane probe skipped" not in capsys.readouterr().out


# --- combinations ----------------------------------------------------------------


def test_held_and_closed_prints_held_line_only(tmp_path, capsys):
    settings = settings_for(tmp_path, _table(beta=REASON))
    _close(settings, "beta")
    runner = _run(settings, tmp_path)
    assert runner.probed() == ["alpha", "gamma"]
    out = capsys.readouterr().out
    assert f"warning: lane probe skipped for beta (held: {REASON})" in out.splitlines()
    assert "closed until" not in out
    assert out.count("skipped for beta") == 1


def test_warning_lines_in_config_order(tmp_path, capsys):
    settings = settings_for(tmp_path, _table(gamma=REASON))
    _close(settings, "alpha")
    runner = _run(settings, tmp_path, skip_probe=["beta"])
    assert runner.probed() == []
    lines = [x for x in capsys.readouterr().out.splitlines() if "lane probe skipped" in x]
    assert len(lines) == 3
    assert lines[0].startswith("warning: lane probe skipped for alpha (closed until ")
    assert lines[1] == "warning: lane probe skipped for beta"
    assert lines[2] == f"warning: lane probe skipped for gamma (held: {REASON})"


def test_skip_probe_keeps_its_own_line_even_when_held(tmp_path, capsys):
    settings = settings_for(tmp_path, _table(beta=REASON))
    runner = _run(settings, tmp_path, skip_probe=["beta"])
    assert runner.probed() == ["alpha", "gamma"]
    out = capsys.readouterr().out.splitlines()
    assert "warning: lane probe skipped for beta" in out
    assert not any("held:" in x for x in out)


# --- static route check and probe=false ------------------------------------------


def test_static_route_check_still_covers_held_lane(tmp_path):
    table = providers_table()
    table["nolane"] = {**table["nolane"], "held": REASON}
    settings = settings_for(tmp_path, table)
    repo, sha = make_repo(tmp_path)
    with pytest.raises(PromoteError, match="no network route for nolane"):
        promote(repo, sha, settings, runner=FakeRunner(settings), require_routes=True,
                probe=True)


def test_static_route_check_still_covers_closed_lane(tmp_path):
    settings = settings_for(tmp_path, providers_table())
    _close(settings, "nolane")
    repo, sha = make_repo(tmp_path)
    with pytest.raises(PromoteError, match="no network route for nolane"):
        promote(repo, sha, settings, runner=FakeRunner(settings), require_routes=True,
                probe=True)


def test_probe_false_prints_nothing_for_held_or_closed(tmp_path, capsys):
    settings = settings_for(tmp_path, _table(beta=REASON))
    _close(settings, "gamma")
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    promote(repo, sha, settings, runner=runner, probe=False)
    assert runner.probed() == []
    assert "lane probe skipped" not in capsys.readouterr().out


# --- CLI -------------------------------------------------------------------------


def test_cli_shows_held_and_closed_lines(tmp_path, monkeypatch, capsys):
    from subagent import promote as mod

    settings = settings_for(tmp_path, _table(beta=REASON))
    _close(settings, "gamma")
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    repo, sha = make_repo(tmp_path)
    runner = FakeRunner(settings)
    _patch_promote_runner(monkeypatch, mod, runner)
    assert cli.main(["--root", str(repo), "promote", sha]) == 0
    assert runner.probed() == ["alpha"]
    lines = capsys.readouterr().out.splitlines()
    assert f"warning: lane probe skipped for beta (held: {REASON})" in lines
    assert _closed_line_ok(lines, settings, "gamma"), lines
