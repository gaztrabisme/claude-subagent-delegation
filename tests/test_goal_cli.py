"""CLI flags expose the shared goal parser without starting a worker."""

from __future__ import annotations

import json
import os
from pathlib import Path

from subagent import config
from subagent.cli import main
from tests.conftest import write_config

COMMAND_GOAL = (
    'Execute plan "CLI goal" (plan.md). Goal rows:\n'
    "1 tests: `pytest -q`"
)
PROSE_GOAL = (
    'Execute plan "CLI goal" (plan.md). Goal rows:\n'
    "1 design: interface is clear"
)


def _project_config(root: Path, monkeypatch, fake_codex) -> Path:
    binary = os.environ["SUBAGENT_TEST_CODEX_BIN"]
    (root / ".subagent").mkdir(parents=True, exist_ok=True)
    path = write_config(root / ".subagent" / "config.toml", {
        "core": {"workspace": str(root), "default_provider": "codex"},
        "providers": {"codex": {"driver": "codex", "binary": binary}},
    })
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    return path


def test_goal_cli_run_forwards_inline_goal_without_starting_worker(
    tmp_path: Path, monkeypatch, fake_codex
) -> None:
    _project_config(tmp_path, monkeypatch, fake_codex)
    seen: dict[str, object] = {}

    def capture(command, args, raw, root, settings):
        seen.update(command=command, goal=args.goal, root=root)
        return 0

    monkeypatch.setattr("subagent.cli._run_loop_command", capture)

    assert main(["--root", str(tmp_path), "run", "--goal", COMMAND_GOAL]) == 0
    assert seen == {"command": "run", "goal": COMMAND_GOAL, "root": tmp_path}


def test_goal_cli_doctor_valid_command_text(tmp_path: Path, monkeypatch, fake_codex, capsys) -> None:
    _project_config(tmp_path, monkeypatch, fake_codex)

    assert main(["--root", str(tmp_path), "doctor", "--no-probe", "--goal", COMMAND_GOAL]) == 0
    output = capsys.readouterr().out
    assert "goal block valid: 1 rows" in output
    assert "codex" in output


def test_goal_cli_doctor_invalid_text_reports_parser_problem(
    tmp_path: Path, monkeypatch, fake_codex, capsys
) -> None:
    _project_config(tmp_path, monkeypatch, fake_codex)
    invalid = 'Execute plan "CLI goal" (plan.md). Goal rows:\n2 tests: `pytest -q`'

    assert main(["--root", str(tmp_path), "doctor", "--no-probe", "--goal", invalid]) == 1
    captured = capsys.readouterr()
    assert "error: goal block is invalid: rows not consecutive: expected 1 got 2" in captured.err


def test_goal_cli_doctor_valid_goal_json_keeps_machine_readable_output(
    tmp_path: Path, monkeypatch, fake_codex, capsys
) -> None:
    _project_config(tmp_path, monkeypatch, fake_codex)

    assert main([
        "--root", str(tmp_path), "doctor", "--no-probe", "--json", "--goal", COMMAND_GOAL
    ]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["goal"] == {"rows": 1, "command_rows": 1}
    assert data["providers"][0]["name"] == "codex"
    assert captured.err == ""


def test_goal_cli_doctor_prose_only_json_adds_warning(
    tmp_path: Path, monkeypatch, fake_codex, capsys
) -> None:
    _project_config(tmp_path, monkeypatch, fake_codex)
    warning = (
        "warning: goal has no command rows; completion has no server-side goal check "
        "when it contains only prose"
    )

    assert main([
        "--root", str(tmp_path), "doctor", "--no-probe", "--json", "--goal", PROSE_GOAL
    ]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["goal"] == {"rows": 1, "command_rows": 0}
    assert warning in data["warnings"]


def test_goal_cli_doctor_invalid_json_stays_json_and_exits_one(
    tmp_path: Path, monkeypatch, fake_codex, capsys
) -> None:
    _project_config(tmp_path, monkeypatch, fake_codex)
    invalid = 'Execute plan "CLI goal" (plan.md). Goal rows:\n1 tests:'

    assert main([
        "--root", str(tmp_path), "doctor", "--no-probe", "--json", "--goal", invalid
    ]) == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "row 1: missing check after ':'" in json.dumps(data)
    assert captured.err == ""
