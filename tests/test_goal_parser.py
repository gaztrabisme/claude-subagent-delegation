"""The request-side goal parser is strict and workspace aware."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from subagent.goal import Goal, GoalRow, parse_goal

HEADER = 'Execute plan "Ship the parser" (docs/plan.md). Goal rows:'
BLOCK = (
    f"{HEADER}\n"
    "1 parser: `pytest tests/test_goal_parser.py -q`\n"
    "\n"
    "2 docs: parser output is easy to understand"
)


def test_goal_parser_inline_section_and_command_rows(tmp_path: Path) -> None:
    value = f"Some plan context.\n## Goal block\n{BLOCK}\n## Notes\nignored text"

    goal = parse_goal(value, tmp_path)

    assert isinstance(goal, Goal)
    assert goal.path is None
    assert isinstance(goal.rows, tuple)
    assert goal.rows == (
        GoalRow(
            row=1,
            label="parser",
            check="`pytest tests/test_goal_parser.py -q`",
            kind="command",
            command="pytest tests/test_goal_parser.py -q",
        ),
        GoalRow(
            row=2,
            label="docs",
            check="parser output is easy to understand",
            kind="prose",
            command=None,
        ),
    )


def test_goal_parser_path_and_inline_use_the_same_validator(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "GOAL.md"
    path.parent.mkdir()
    path.write_text(f"# Notes\n## Goal block\n{BLOCK}\n## End\n", encoding="utf-8")

    inline = parse_goal(BLOCK, tmp_path)
    from_path = parse_goal("docs/GOAL.md", tmp_path)

    assert from_path.rows == inline.rows
    assert from_path.path == "docs/GOAL.md"


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        (
            "not a header\nmore inline text",
            'missing header line: expected Execute plan "<plan name>" '
            '(<plan file path or reference>). Goal rows:',
        ),
        (
            'Execute plan "" (docs/plan.md). Goal rows:\n1 first: check',
            'missing header line: expected Execute plan "<plan name>" '
            '(<plan file path or reference>). Goal rows:',
        ),
        (
            'Execute plan "Plan" (). Goal rows:\n1 first: check',
            'missing header line: expected Execute plan "<plan name>" '
            '(<plan file path or reference>). Goal rows:',
        ),
        (
            f"{HEADER}\nintroductory prose\n1 first: check",
            "free text before row 1",
        ),
        (
            f"{HEADER}\n1 first: check\n3 third: check",
            "rows not consecutive: expected 2 got 3",
        ),
        (
            f"{HEADER}\n1malformed: check",
            'row 1: expected "<n> <label>: <check>"',
        ),
        (
            f"{HEADER}\n1 first:",
            "row 1: missing check after ':'",
        ),
        (
            f"{HEADER}\n1 first: check\ntrailing prose\n2 second: check",
            "row 2: row after trailing free text",
        ),
        (f"{HEADER}\nno rows here", "no goal rows"),
        (
            f"{HEADER}\n1 first: `pytest -q` and extra prose",
            "row 1: mixed command and prose check",
        ),
    ],
)
def test_goal_parser_reports_block_problems(
    tmp_path: Path, value: str, problem: str
) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_goal(value, tmp_path)

    assert problem in str(exc_info.value)


def test_goal_parser_rejects_blocks_over_4000_characters(tmp_path: Path) -> None:
    block = f"{HEADER}\n1 first: " + "x" * 4_100

    with pytest.raises(ValueError) as exc_info:
        parse_goal(block, tmp_path)

    assert f"too long: {len(block)}/4000 characters" in str(exc_info.value)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("missing/GOAL.md", "goal file not found: missing/GOAL.md"),
        ("../outside/GOAL.md", "goal file must be inside workspace"),
    ],
)
def test_goal_parser_rejects_missing_and_outside_paths(
    tmp_path: Path, value: str, problem: str
) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_goal(value, tmp_path)

    assert problem in str(exc_info.value)


def test_goal_parser_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.md"
    workspace.mkdir()
    outside.write_text(BLOCK, encoding="utf-8")
    (workspace / "GOAL.md").symlink_to(outside)

    with pytest.raises(ValueError, match="goal file must be inside workspace"):
        parse_goal("GOAL.md", workspace)


def test_goal_parser_rejects_non_utf8_goal_file(tmp_path: Path) -> None:
    path = tmp_path / "GOAL.md"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(ValueError, match="input is not valid UTF-8"):
        parse_goal("GOAL.md", tmp_path)


def test_goal_parser_goal_and_rows_are_immutable_records(tmp_path: Path) -> None:
    goal = parse_goal(BLOCK, tmp_path)
    row = goal.rows[0]

    assert {field.name for field in fields(GoalRow)} == {
        "row", "label", "check", "kind", "command"
    }
    with pytest.raises(FrozenInstanceError):
        goal.path = "changed.md"
    with pytest.raises(FrozenInstanceError):
        row.command = "changed"
