from __future__ import annotations

from pathlib import Path

from subagent_mcp.guard import ALLOW, DENY, ESCALATE, classify_verification
from subagent_mcp.verify import run_verification

from .conftest import make_settings


def test_ordinary_acceptance_commands_are_allowed(tmp_path: Path):
    assert classify_verification("true", tmp_path).action == ALLOW
    assert classify_verification("pytest -q && ls", tmp_path).action == ALLOW


def test_dangerous_acceptance_commands_never_reach_execution(tmp_path: Path):
    assert classify_verification("sudo pytest", tmp_path).action == DENY
    assert classify_verification("python3 -c 'import os'", tmp_path).action == ESCALATE
    assert classify_verification("", tmp_path).action == ESCALATE


def test_a_blocked_command_is_reported_not_run(tmp_path: Path):
    settings = make_settings(tmp_path)
    marker = tmp_path / "should-not-exist"
    result = run_verification(f"sudo touch {marker}", tmp_path, settings)
    assert result.passed is False
    assert "blocked before execution" in result.reason
    assert not marker.exists()


def test_exit_zero_passes(tmp_path: Path):
    settings = make_settings(tmp_path)
    result = run_verification("true", tmp_path, settings)
    assert result.passed is True
    assert result.exit_code == 0


def test_false_fails(tmp_path: Path):
    settings = make_settings(tmp_path)
    result = run_verification("false", tmp_path, settings)
    assert result.passed is False


def test_empty_command_is_reported_not_executed(tmp_path: Path):
    settings = make_settings(tmp_path)
    result = run_verification("   ", tmp_path, settings)
    assert result.passed is False
    assert "no verification command" in result.reason
