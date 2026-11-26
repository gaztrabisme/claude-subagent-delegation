"""Shared fixtures for tests/graph (spec: wiki/briefs-2026-11-26/graph/SPEC.md section 12.2).

Rules every graph test follows: no network, no real provider, no download. A
Temporal test imports `temporalio` with `importorskip`, finds the CLI through
TEMPORAL_BIN or PATH, and starts the dev server with `dev_server_existing_path`
(never without it, which would download) and never with time skipping.

The package under test is imported lazily here so a missing package fails the
tests that need it, not every test in the folder.
"""

from __future__ import annotations

import contextlib
import copy
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from ..conftest import default_providers, make_settings

WORKED_EXAMPLE: dict[str, Any] = {
    "schema": "subagent.graph/1",
    "id": "feature-x",
    "version": 1,
    "workspace": "worktrees/feature-x",
    "inputs": {"ticket": "Add the export command"},
    "budget_usd": 6.0,
    "defaults": {"provider": "codex", "reserve_usd": 0.8, "timeout_s": 2400},
    "body": [
        {"id": "plan", "kind": "task", "role": "planner",
         "prompt": "Write PLAN.md for: {{ inputs.ticket }}. Put the two package names in "
                   ".subagent/result.json as {\"packages\": [..]}.",
         "verification": "test -s PLAN.md"},
        {"id": "builds", "kind": "parallel", "join": "allSettled", "branches": [
            {"id": "build_a", "kind": "task", "role": "builder",
             "workspace": "worktrees/feature-x-a",
             "prompt": "Build {{ nodes.plan.output.result_json.packages.0 }} per PLAN.md.",
             "verification": "uv run pytest -q tests/graph_example/a"},
            {"id": "build_b", "kind": "task", "role": "builder", "provider": "deepseek",
             "workspace": "worktrees/feature-x-b",
             "prompt": "Build {{ nodes.plan.output.result_json.packages.1 }} per PLAN.md.",
             "verification": "uv run pytest -q tests/graph_example/b"},
        ]},
        {"id": "both_built", "kind": "gate",
         "condition": {"path": "nodes.builds.output.failed", "op": "eq", "value": 0},
         "on_failure": "exception"},
        {"id": "review_loop", "kind": "loop", "max_iterations": 2,
         "until": {"path": "nodes.review.output.verdict", "op": "eq", "value": "PASS"},
         "body": [
             {"id": "review", "kind": "task", "role": "reviewer", "provider": "gemini",
              "reserve_usd": 0.3,
              "prompt": "Review the diff of both worktrees; write {\"verdict\": "
                        "\"PASS\"|\"FAIL\", \"findings\": [..]} to .subagent/result.json.",
              "verification": "true"},
             {"id": "fix", "kind": "task", "role": "builder",
              "when": {"path": "nodes.review.output.verdict", "op": "eq", "value": "FAIL"},
              "prompt": "Fix these findings: {{ nodes.review.output.result_json.findings }}",
              "verification": "uv run pytest -q tests/graph_example"},
         ]},
        {"id": "merge_ok", "kind": "owner_decision",
         "question": "Review passed for {{ inputs.ticket }}. Merge into fuse?",
         "options": ["merge", "hold"], "default": "hold", "timeout_s": 86400},
    ],
    "output": {"verdict": "nodes.review.output.verdict",
               "decision": "nodes.merge_ok.output.answer"},
}


def worked_example() -> dict[str, Any]:
    """A fresh copy of the section 2.6 document."""
    return copy.deepcopy(WORKED_EXAMPLE)


def minimal_graph(**body_overrides: Any) -> dict[str, Any]:
    """The smallest valid document: one task."""
    task = {"id": "t1", "kind": "task", "provider": "codex", "prompt": "do it",
            "verification": "true"}
    task.update(body_overrides)
    return {"schema": "subagent.graph/1", "id": "g1", "version": 1, "body": [task]}


def providers_with_gemini() -> dict[str, dict[str, Any]]:
    """default_providers() plus a `gemini` entry, so the worked example's providers exist."""
    table = default_providers()
    table["gemini"] = dict(table["glm"])
    return table


def temporal_binary() -> Path | None:
    """The Temporal CLI from TEMPORAL_BIN or PATH; None when there is none."""
    explicit = os.environ.get("TEMPORAL_BIN")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    found = shutil.which("temporal")
    return Path(found) if found else None


def require_temporal() -> Path:
    """importorskip temporalio, then the CLI; skips the test when either is absent."""
    pytest.importorskip("temporalio")
    binary = temporal_binary()
    if binary is None:
        pytest.skip("Temporal CLI not found")
    return binary


@contextlib.asynccontextmanager
async def start_local(tmp_path: Path, binary: Path | None = None):
    """A local Temporal dev server, exactly as spec 12.2 says: existing binary, file db."""
    from temporalio.testing import WorkflowEnvironment

    binary = binary or require_temporal()
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=str(binary),
        dev_server_database_filename=str(tmp_path / "t.db"),
    )
    try:
        yield env
    finally:
        await env.shutdown()


@pytest.fixture
def temporal_bin() -> Path:
    return require_temporal()


@pytest.fixture
def graph_settings(tmp_path):
    """Settings with the default providers plus gemini, workspace tmp_path."""
    (tmp_path / "worktrees").mkdir(exist_ok=True)
    for name in ("feature-x", "feature-x-a", "feature-x-b"):
        (tmp_path / "worktrees" / name).mkdir(exist_ok=True)
    return make_settings(tmp_path, providers=providers_with_gemini())


@pytest.fixture
def state_dir(tmp_path) -> Path:
    path = tmp_path / "graph-state"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def fake_dispatcher(tmp_path):
    """Factory: fake_dispatcher(script=None, default=None, audit=False) -> FakeDispatcher."""

    def make(script: dict[str, list[dict[str, Any]]] | None = None,
             default: dict[str, Any] | None = None, *, audit: bool = False):
        from subagent.graph.dispatch import FakeDispatcher, FakeStep

        steps = {key: [FakeStep(**s) for s in value] for key, value in (script or {}).items()}
        return FakeDispatcher(
            steps,
            default=FakeStep(**(default or {})),
            audit_log=(tmp_path / "audit.jsonl") if audit else None,
        )

    return make


@pytest.fixture(autouse=True)
def _graph_trace_kinds_are_not_run_records(trace_records):
    """graph_* trace kinds are additive (spec 10) and not in the locked run schema.

    The suite-wide trace_records fixture checks every record against
    tests/test_trace_schema.py, which does not know graph kinds. Drop them
    before that check; every other record still goes through it.
    """
    yield trace_records
    trace_records[:] = [r for r in trace_records if not str(r.get("kind", "")).startswith("graph_")]
