"""Parse request-side GOAL.md blocks into immutable checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_GOAL_CHARS = 4000
HEADER_PROBLEM = (
    'missing header line: expected Execute plan "<plan name>" '
    '(<plan file path or reference>). Goal rows:'
)
_HEADER_START = 'Execute plan "'
_HEADER = re.compile(r'^Execute plan "([^"\r\n]+)" \((.+)\)\. Goal rows:$')
_ROW = re.compile(r"^(\d+)\s+([^:]+):(.*)$")
_ROW_NUMBER = re.compile(r"^(\d+)")


class GoalError(ValueError):
    """A goal input or block that cannot be accepted."""

    def __init__(self, problems: list[str], *, block: bool = False) -> None:
        self.problems = tuple(problems)
        self.block = block
        super().__init__("\n".join(problems))


@dataclass(frozen=True, slots=True)
class GoalRow:
    row: int
    label: str
    check: str
    kind: str
    command: str | None


@dataclass(frozen=True, slots=True)
class Goal:
    rows: tuple[GoalRow, ...]
    path: str | None = None


def _goal_source(value: str, workspace: Path) -> tuple[str, str | None]:
    """Return goal text and its canonical workspace-relative file path."""
    if value.lstrip().startswith(_HEADER_START):
        return value, None

    has_newline = "\n" in value or "\r" in value
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        if has_newline:
            return value, None
        raise GoalError([f"goal file not found: {value}"]) from None

    try:
        relative = resolved.relative_to(workspace)
    except ValueError:
        if has_newline:
            try:
                if not resolved.is_file():
                    return value, None
            except OSError:
                return value, None
        raise GoalError(["goal file must be inside workspace"]) from None

    if resolved.is_file():
        try:
            return resolved.read_text(encoding="utf-8"), relative.as_posix()
        except UnicodeDecodeError:
            raise GoalError(["input is not valid UTF-8"]) from None
        except OSError:
            raise GoalError([f"goal file not found: {value}"]) from None

    if has_newline:
        return value, None
    raise GoalError([f"goal file not found: {value}"])


def _extract_block(text: str) -> str:
    lines = text.splitlines()
    marker = next((i for i, line in enumerate(lines) if line.strip() == "## Goal block"), None)
    if marker is not None:
        end = next(
            (i for i in range(marker + 1, len(lines)) if lines[i].startswith("##")),
            len(lines),
        )
        lines = lines[marker + 1:end]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _row_from_line(line: str, expected: int, trailing: bool) -> tuple[GoalRow | None, list[str]]:
    problems: list[str] = []
    match = _ROW.fullmatch(line)
    number_match = _ROW_NUMBER.match(line)
    if match is None:
        if number_match is None:
            return None, problems
        number = int(number_match.group(1))
        if trailing:
            problems.append(f"row {number}: row after trailing free text")
        else:
            problems.append(f'row {number}: expected "<n> <label>: <check>"')
        return None, problems

    number = int(match.group(1))
    label = match.group(2).strip()
    check = match.group(3).strip()
    if trailing:
        problems.append(f"row {number}: row after trailing free text")
    if number != expected:
        problems.append(f"rows not consecutive: expected {expected} got {number}")
    if not label:
        problems.append(f'row {number}: expected "<n> <label>: <check>"')
        return None, problems
    if not check:
        problems.append(f"row {number}: missing check after ':'")
        return None, problems

    if "`" in check:
        command_match = re.fullmatch(r"`([^`]+)`", check)
        if command_match is None:
            problems.append(f"row {number}: mixed command and prose check")
            return None, problems
        return GoalRow(number, label, check, "command", command_match.group(1).strip()), problems
    return GoalRow(number, label, check, "prose", None), problems


def _parse_block(text: str, path: str | None) -> Goal:
    block = _extract_block(text)
    if len(block) > MAX_GOAL_CHARS:
        raise GoalError([f"too long: {len(block)}/4000 characters"], block=True)

    lines = block.splitlines()
    problems: list[str] = []
    plan_header = _HEADER.fullmatch(lines[0]) if lines else None
    if plan_header is None or not plan_header.group(1).strip() or not plan_header.group(2).strip():
        problems.append(HEADER_PROBLEM)
        body = lines
    else:
        body = lines[1:]

    rows: list[GoalRow] = []
    expected = 1
    trailing = False
    before_row = False
    for raw_line in body:
        line = raw_line.strip()
        if not line:
            continue
        syntactic_row = _ROW.fullmatch(line)
        parsed, row_problems = _row_from_line(line, expected, trailing)
        if syntactic_row is not None:
            expected = int(syntactic_row.group(1)) + 1
        if parsed is not None:
            rows.append(parsed)
            problems.extend(row_problems)
            continue
        if row_problems:
            problems.extend(row_problems)
            continue
        if not rows and not before_row:
            problems.append("free text before row 1")
            before_row = True
        elif rows:
            trailing = True

    if not rows:
        problems.append("no goal rows")
    if problems:
        raise GoalError(list(dict.fromkeys(problems)), block=True)
    return Goal(tuple(rows), path)


def parse_goal(value: str, workspace: str | Path) -> Goal:
    """Parse inline goal text or a workspace-relative GOAL.md path."""
    root = Path(workspace).expanduser().resolve()
    text, path = _goal_source(value, root)
    return _parse_block(text, path)
