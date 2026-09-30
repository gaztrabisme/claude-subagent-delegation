"""Goal UAT runtime, MCP result and delegation trace contract."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from subagent.goal import parse_goal
from subagent.goal_runtime import evaluate_goal
from subagent.loop import loop as loop_module
from subagent.runs import COMPLETED, COMPLETED_UNVERIFIED, Registry
from subagent.telemetry.trace import Trace
from tests.conftest import make_settings


def _block(rows: str) -> str:
    return f'Execute plan "Caller plan" (plan.md). Goal rows:\n{rows}'


def test_goal_protected_path_is_denied_and_restored(tmp_path: Path) -> None:
    from subagent.guard.classify import DENY, classify_bash
    from subagent.loop.testguard import TestGuard

    goal_file = tmp_path / "GOAL.md"
    original = 'Execute plan "Protected" (plan.md). Goal rows:\n1 check: `true`\n'
    goal_file.write_text(original, encoding="utf-8")
    guard = TestGuard(tmp_path, (), protected_paths=("GOAL.md",))
    guard.lock()

    verdict = classify_bash(
        "printf changed > GOAL.md",
        tmp_path,
        context={"protected": guard.protected},
    )
    assert verdict.action == DENY

    goal_file.chmod(0o600)
    goal_file.write_text("worker replacement", encoding="utf-8")
    violations, _ = guard.release()

    assert {item["file"] for item in violations} == {"GOAL.md"}
    assert goal_file.read_text(encoding="utf-8") == original

    outside = tmp_path / "sentinel.txt"
    outside.write_text("keep", encoding="utf-8")
    guard = TestGuard(tmp_path, (), protected_paths=("GOAL.md",))
    guard.lock()
    goal_file.unlink()
    goal_file.symlink_to(outside)
    violations, _ = guard.release()

    assert violations[0] == {"file": "GOAL.md", "change": "modified"}
    assert goal_file.read_text(encoding="utf-8") == original
    assert outside.read_text(encoding="utf-8") == "keep"


def test_goal_inline_and_path_use_same_validator(tmp_path: Path) -> None:
    text = _block("1 exists: `test -f marker.txt`\n2 quality: no warnings")
    path = tmp_path / "GOAL.md"
    path.write_text(f"# Context\n## Goal block\n{text}\n## Notes\nignored", encoding="utf-8")

    inline = parse_goal(text, tmp_path)
    file_goal = parse_goal("GOAL.md", tmp_path)

    assert file_goal.rows == inline.rows
    assert file_goal.path == "GOAL.md"


def test_goal_command_rows_run_server_side(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("ready", encoding="utf-8")
    goal = parse_goal(_block("1 exists: `test -f marker.txt`"), tmp_path)
    settings = make_settings(tmp_path)

    result = evaluate_goal(goal, tmp_path, settings)

    assert result == [{
        "row": 1,
        "label": "exists",
        "kind": "command",
        "command": "test -f marker.txt",
        "passed": True,
        "output_tail": "",
    }]


def _fake_agent_registry(tmp_path: Path, monkeypatch, worker_text: str, goal):
    from tests.test_runs import FakeProcess, _result

    def spawn(argv, env, cwd):
        return FakeProcess(_result(worker_text), argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    settings = make_settings(tmp_path, run_timeout=10)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent("goal-test", tmp_path, provider="glm", goal=goal)
    return registry, agent


def test_goal_command_rows_gate_completed(tmp_path: Path, monkeypatch) -> None:
    goal = parse_goal(_block("1 exists: `test -f missing.txt`"), tmp_path)
    registry, agent = _fake_agent_registry(tmp_path, monkeypatch, "worker finished", goal)
    try:
        run = agent.delegate("implement", "true")
        assert run.done.wait(5)

        assert run.state == COMPLETED_UNVERIFIED
        assert run.detail()["uat"][0]["passed"] is False
    finally:
        registry.shutdown()


def test_goal_prose_rows_reach_reviewer(tmp_path: Path) -> None:
    goal = parse_goal(_block("1 clarity: interface is understandable"), tmp_path)
    received = []

    def reviewer(rows):
        received.extend(rows)
        return {1: True}

    result = evaluate_goal(goal, tmp_path, make_settings(tmp_path), reviewer=reviewer)

    assert [(row.row, row.label, row.check) for row in received] == [
        (1, "clarity", "interface is understandable")
    ]
    assert result[0]["passed"] is True


def test_goal_mcp_prose_rows_use_configured_reviewer(tmp_path: Path, monkeypatch) -> None:
    from subagent import mcp_server
    from subagent.config import LoopTarget

    settings = make_settings(tmp_path)
    settings = replace(settings, loop=replace(settings.loop, review=LoopTarget(provider="glm")))

    class FakeRun:
        run_id = "run-goal"
        agent_id = "agent-goal"
        state = COMPLETED
        lane = "glm"

        def detail(self):
            return {"run_id": self.run_id, "agent_id": self.agent_id, "state": self.state}

    class FakeAgent:
        model = "fake-model"
        cfg = SimpleNamespace(name="glm")
        fallback = "none"

        def wait_ready(self, _timeout):
            return None

        def delegate(self, *_args, **_kwargs):
            return FakeRun()

    class FakeRegistry:
        def select_provider(self, _provider, _fallback):
            return settings.providers["glm"]

        def create_agent(self, **kwargs):
            seen.update(kwargs)
            return FakeAgent()

    seen = {}
    monkeypatch.setattr(mcp_server, "settings", settings)
    monkeypatch.setattr(mcp_server, "registry", FakeRegistry())

    result = asyncio.run(mcp_server.delegate(
        "task", "true", workspace=str(tmp_path), provider="glm",
        goal=_block("1 clarity: interface is understandable"),
    ))

    assert result["state"] == COMPLETED
    assert seen["goal"].rows[0].kind == "prose"
    assert callable(seen["goal_reviewer"])


def test_goal_unreviewed_prose_rows_remain_null(tmp_path: Path) -> None:
    goal = parse_goal(_block("1 clarity: interface is understandable"), tmp_path)

    result = evaluate_goal(goal, tmp_path, make_settings(tmp_path))

    assert result[0]["passed"] is None
    assert result[0]["output_tail"] == ""


def test_goal_result_and_delegation_record_have_contract_uat(tmp_path: Path) -> None:
    from subagent.loop.loop import _delegation_record
    from subagent.report import _delegation_row
    from tests.test_trace_schema import problems

    goal = parse_goal(_block("1 exists: `test -f marker.txt`"), tmp_path)
    uat = evaluate_goal(goal, tmp_path, make_settings(tmp_path))
    delegation = _delegation_record(
        mode="single", status="completed_unverified", stopped_because=None,
        changed_files=[], wall_seconds=1, rounds=[], reviews=[], test_writer=None,
        blocks=[], uat=uat,
    )
    trace = Trace(tmp_path / "trace.jsonl")
    trace.delegation(**delegation)
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))

    assert set(record["uat"][0]) == {
        "row", "label", "kind", "command", "passed", "output_tail"
    }
    assert problems(record) == []
    assert _delegation_row(record, None)["uat"] == uat


def test_goal_identity_uses_request_block_not_worker_text(tmp_path: Path, monkeypatch) -> None:
    request = parse_goal(_block("1 caller: `test -f caller.txt`"), tmp_path)
    (tmp_path / "caller.txt").write_text("yes", encoding="utf-8")
    replacement = _block("1 worker: `test -f worker.txt`")
    registry, agent = _fake_agent_registry(tmp_path, monkeypatch, replacement, request)
    try:
        run = agent.delegate("implement", "true")
        assert run.done.wait(5)
        assert run.detail()["uat"][0]["label"] == "caller"
        assert run.detail()["uat"][0]["command"] == "test -f caller.txt"
    finally:
        registry.shutdown()


def test_goal_command_outcome_is_written_to_delegation_trace(tmp_path: Path) -> None:
    from subagent.loop.loop import _delegation_record

    goal = parse_goal(_block("1 missing: `test -f missing.txt`"), tmp_path)
    uat = evaluate_goal(goal, tmp_path, make_settings(tmp_path))
    trace = Trace(tmp_path / "trace.jsonl")
    trace.delegation(**_delegation_record(
        mode="single", status="completed_unverified", stopped_because=None,
        changed_files=[], wall_seconds=1, rounds=[], reviews=[], test_writer=None,
        blocks=[], uat=uat,
    ))

    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))
    assert record["uat"][0]["passed"] is False
    assert record["uat"][0]["output_tail"] == ""


def test_goal_nonzero_command_returns_completed_unverified(tmp_path: Path, monkeypatch) -> None:
    goal = parse_goal(_block("1 missing: `test -f missing.txt`"), tmp_path)
    registry, agent = _fake_agent_registry(tmp_path, monkeypatch, "worker finished", goal)
    try:
        run = agent.delegate("implement", "true")
        assert run.done.wait(5)
        assert run.state == COMPLETED_UNVERIFIED
        assert run.detail()["uat"][0]["output_tail"] == ""
    finally:
        registry.shutdown()


def test_goal_cli_and_mcp_share_server_evaluator(tmp_path: Path, monkeypatch) -> None:
    from subagent import goal_runtime, runs

    assert loop_module.evaluate_goal is runs.evaluate_goal is goal_runtime.evaluate_goal
    goal = parse_goal(_block("1 exists: `test -f marker.txt`"), tmp_path)
    (tmp_path / "marker.txt").write_text("ready", encoding="utf-8")
    registry, agent = _fake_agent_registry(tmp_path, monkeypatch, "worker finished", goal)
    try:
        run = agent.delegate("implement", "true")
        assert run.done.wait(5)
        assert run.state == COMPLETED
        cli_result = loop_module._apply_goal(
            {"status": "done"}, goal, tmp_path,
            SimpleNamespace(settings=registry.settings, registry=registry),
        )
        assert cli_result["uat"][0]["passed"] is True
        assert run.detail()["uat"][0]["passed"] is True
    finally:
        registry.shutdown()


def test_goal_uat_output_tail_is_bounded_chars(tmp_path: Path, monkeypatch) -> None:
    from subagent.goal_runtime import evaluate_goal as evaluator
    from subagent.verify import VerificationResult

    goal = parse_goal(_block("1 noisy: `test -f marker.txt`"), tmp_path)
    monkeypatch.setattr(
        "subagent.goal_runtime.run_verification",
        lambda *_args, **_kwargs: VerificationResult(
            "test -f marker.txt", False, "exited 1", 1, "x" * 3000
        ),
    )

    result = evaluator(goal, tmp_path, make_settings(tmp_path))

    assert len(result[0]["output_tail"]) == 2000


def test_goal_no_command_rows_warns_doctor(tmp_path: Path, monkeypatch, fake_codex, capsys) -> None:
    from subagent.cli import main
    from tests.test_goal_cli import PROSE_GOAL, _project_config

    _project_config(tmp_path, monkeypatch, fake_codex)

    assert main(["--root", str(tmp_path), "doctor", "--no-probe", "--goal", PROSE_GOAL]) == 0
    assert "warning: goal has no command rows" in capsys.readouterr().err
