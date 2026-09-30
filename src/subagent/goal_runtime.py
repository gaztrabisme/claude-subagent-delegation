"""Evaluate caller-owned goal rows on the server after implementation."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .config import Settings
from .goal import Goal, GoalError, GoalRow
from .verify import run_verification

GoalReviewer = Callable[[list[GoalRow]], Mapping[int, bool | None]]


def pending_uat(goal: Goal) -> list[dict[str, Any]]:
    """The exact result shape before any row has reached a verdict."""
    return [
        {
            "row": row.row,
            "label": row.label,
            "kind": row.kind,
            "command": row.command,
            "passed": None,
            "output_tail": "",
        }
        for row in goal.rows
    ]


def evaluate_goal(
    goal: Goal,
    workspace: str | Path,
    settings: Settings,
    reviewer: GoalReviewer | None = None,
    budget: float | None = None,
) -> list[dict[str, Any]]:
    """Run command rows with the normal verifier and ask a reviewer about prose rows."""
    result = pending_uat(goal)
    started = time.monotonic()
    prose: list[GoalRow] = []
    for row, record in zip(goal.rows, result, strict=True):
        if row.kind == "prose":
            prose.append(row)
            continue
        remaining = None if budget is None else max(0.0, budget - (time.monotonic() - started))
        checked = run_verification(row.command or "", Path(workspace), settings, budget=remaining)
        record["passed"] = checked.passed
        record["output_tail"] = checked.output_tail

    if prose and reviewer is not None:
        try:
            verdicts = reviewer(prose)
        except Exception:  # noqa: BLE001 - a prose opinion never breaks the run
            verdicts = {}
        if isinstance(verdicts, Mapping):
            by_row = {row.row: row for row in prose}
            for record in result:
                if record["row"] not in by_row:
                    continue
                verdict = verdicts.get(record["row"])
                record["passed"] = verdict if type(verdict) is bool else None
    return result


def goal_error_messages(error: GoalError) -> list[str]:
    """CLI-style messages for callers that report parser errors as exceptions."""
    prefix = "error: goal block is invalid: " if error.block else "error: "
    return [prefix + problem for problem in error.problems]
