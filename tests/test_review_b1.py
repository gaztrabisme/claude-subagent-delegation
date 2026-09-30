"""Regression coverage for the runtime findings in review B1."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from subagent.goal import parse_goal
from subagent.loop import loop as loop_module
from subagent.router import Hop
from subagent.runs import COMPLETED, COMPLETED_UNVERIFIED, Registry, Run
from subagent.secrets import redact_secrets
from tests.conftest import make_settings
from tests.test_runs import FakeProcess, _result

FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"


def test_review_b1_redacts_wrapped_credentials(monkeypatch):
    from subagent.loop.loop import _clip_provider_error

    secret = "custom-review-b1-credential-value"
    monkeypatch.setenv("REVIEW_B1_API_KEY", secret)
    message = "provider rejected custom-review-b1-credential-\nvalue"

    redacted = _clip_provider_error(message, 500, ("REVIEW_B1_API_KEY",))

    assert "[REDACTED]" in redacted
    assert "custom-review-b1-credential-value" not in redacted
    assert "credential- value" not in redacted


def test_review_b1_ignores_whitespace_only_secret_values(monkeypatch):
    monkeypatch.setenv("REVIEW_B1_API_KEY", " \t\n ")

    assert redact_secrets("Connection refused by peer", ("REVIEW_B1_API_KEY",)) == (
        "Connection refused by peer"
    )


def test_review_b1_run_lock_closes_when_result_write_fails(tmp_path, monkeypatch):
    args = SimpleNamespace(
        goal=None,
        run_id="review-b1-lock",
        test_outline=None,
        auto=False,
        background=False,
        wait=None,
        parallel=None,
        continue_session=False,
        plan="plan.md",
        tier="normal",
        model=None,
        provider=None,
    )
    server = SimpleNamespace(settings=SimpleNamespace(loop=SimpleNamespace()), registry=None)
    monkeypatch.setattr(loop_module, "_set_delegation_id", lambda *_args: None)
    monkeypatch.setattr(
        loop_module,
        "run_round",
        lambda *_args, **_kwargs: {"status": "done", "_runs": []},
    )
    monkeypatch.setattr(loop_module, "_write_delegation", lambda *_args, **_kwargs: None)

    class HeldLock:
        closed = False

        def close(self):
            self.closed = True

    held_lock = HeldLock()
    monkeypatch.setattr(loop_module, "_try_run_lock", lambda _root: held_lock)
    original_write_json = loop_module.write_json

    def fail_result(path, data):
        if Path(path).name == "review-b1-lock.json":
            raise OSError("disk full")
        return original_write_json(path, data)

    monkeypatch.setattr(loop_module, "write_json", fail_result)
    with pytest.raises(OSError, match="disk full"):
        loop_module.cmd_run(tmp_path, server, args, [])

    assert held_lock.closed


def test_review_b1_protects_and_restores_replaced_symlink_goal(tmp_path):
    from subagent.loop.testguard import TestGuard as GoalTestGuard

    target = tmp_path / "docs" / "goal.md"
    target.parent.mkdir()
    original = 'Execute plan "Protected" (plan.md). Goal rows:\n1 check: `true`\n'
    target.write_text(original, encoding="utf-8")
    goal = tmp_path / "GOAL.md"
    goal.symlink_to(Path("docs") / "goal.md")
    parsed_goal = parse_goal("GOAL.md", tmp_path)
    assert parsed_goal.path == "docs/goal.md"
    guard = GoalTestGuard(tmp_path, (), protected_paths=(parsed_goal.path,))
    guard.lock()

    goal.unlink()
    goal.write_text("worker replacement", encoding="utf-8")
    violations, _ = guard.release()

    assert {item["file"] for item in violations} == {"GOAL.md", "docs/goal.md"}
    assert goal.is_symlink()
    assert goal.read_text(encoding="utf-8") == original
    assert target.read_text(encoding="utf-8") == original


def _mcp_goal_runtime(tmp_path, monkeypatch, edited_turns=()):
    from subagent import mcp_server

    goal_file = tmp_path / "GOAL.md"
    goal_file.write_text(
        'Execute plan "Caller plan" (plan.md). Goal rows:\n'
        "1 exists: `test -f present.txt`\n",
        encoding="utf-8",
    )
    (tmp_path / "present.txt").write_text("ready", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        providers={
            "glm": {
                "driver": "claude",
                "base_url": "https://api.z.ai/api/anthropic",
                "api_key_env": "GLM_API_KEY",
                "binary": str(FAKE_CLAUDE),
                "model": "test-model",
            }
        },
        run_timeout=10,
    )
    spawned = []

    def spawn(argv, env, cwd):
        turn = len(spawned)
        spawned.append(turn)
        if turn in edited_turns:
            goal_file.chmod(0o600)
            goal_file.write_text(f"worker replacement {turn}", encoding="utf-8")
        return FakeProcess(_result("done", session_id=f"session-{turn}"), argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    registry = Registry(settings, start_reaper=False)
    monkeypatch.setattr(mcp_server, "settings", settings)
    monkeypatch.setattr(mcp_server, "registry", registry)
    return mcp_server, registry, goal_file, spawned


def test_review_b1_mcp_delegate_accepts_goal_file_release_result(tmp_path, monkeypatch):
    from subagent.runs import COMPLETED_UNVERIFIED

    mcp_server, registry, goal_file, _spawned = _mcp_goal_runtime(
        tmp_path, monkeypatch, edited_turns=(0,)
    )
    try:
        result = asyncio.run(mcp_server.delegate(
            "implement", "true", workspace=str(tmp_path), provider="glm",
            goal="GOAL.md", wait_seconds=10,
        ))

        run = registry.agent(result["agent_id"]).get_run(result["run_id"])
        assert result["state"] == COMPLETED_UNVERIFIED
        assert run.pre_verify_result["violations"][0]["file"] == "GOAL.md"
        assert goal_file.read_text(encoding="utf-8").startswith('Execute plan "Caller plan"')
    finally:
        registry.shutdown()


def test_review_b1_mcp_continue_relocks_and_releases_goal_guard(tmp_path, monkeypatch):
    mcp_server, registry, goal_file, _spawned = _mcp_goal_runtime(
        tmp_path, monkeypatch, edited_turns=(1,)
    )
    try:
        first = asyncio.run(mcp_server.delegate(
            "implement", "true", workspace=str(tmp_path), provider="glm",
            goal="GOAL.md", wait_seconds=10,
        ))
        assert first["state"] == COMPLETED

        second = asyncio.run(mcp_server.continue_agent(
            first["agent_id"], "continue", wait_seconds=10,
        ))

        run = registry.agent(first["agent_id"]).get_run(second["run_id"])
        assert second["state"] == COMPLETED_UNVERIFIED
        assert run.pre_verify_result["violations"][0]["file"] == "GOAL.md"
        assert goal_file.read_text(encoding="utf-8").startswith('Execute plan "Caller plan"')
    finally:
        registry.shutdown()


def test_review_b1_apply_goal_preserves_failure_statuses(tmp_path, monkeypatch):
    goal = parse_goal(
        'Execute plan "Caller plan" (plan.md). Goal rows:\n1 check: `false`', tmp_path
    )
    monkeypatch.setattr(
        loop_module,
        "evaluate_goal",
        lambda *_args, **_kwargs: [{"kind": "command", "passed": False}],
    )
    server = SimpleNamespace(settings=make_settings(tmp_path))

    for status in (
        "violated_tests", "backend_error", "timeout", "max_rounds", "needs_test_change"
    ):
        result = loop_module._apply_goal({"status": status}, goal, tmp_path, server)
        assert result["status"] == status

    completed = loop_module._apply_goal({"status": "done"}, goal, tmp_path, server)
    assert completed["status"] == "completed_unverified"

    goal_file = tmp_path / "GOAL.md"
    goal_file.write_text(
        'Execute plan "Caller plan" (plan.md). Goal rows:\n1 check: `false`',
        encoding="utf-8",
    )
    file_goal = parse_goal("GOAL.md", tmp_path)
    changed_goal = loop_module._apply_goal(
        {"status": "done", "violations": [{"file": "GOAL.md"}]},
        file_goal, tmp_path, server,
    )
    assert changed_goal["status"] == "completed_unverified"


def test_review_b1_autopilot_goal_uses_verification_budget(tmp_path, monkeypatch):
    (tmp_path / "GOAL.md").write_text(
        'Execute plan "Caller plan" (plan.md). Goal rows:\n1 check: `true`',
        encoding="utf-8",
    )
    goal = parse_goal("GOAL.md", tmp_path)
    settings = make_settings(tmp_path)
    settings = replace(settings, loop=replace(settings.loop, auto_review=False))
    server = SimpleNamespace(settings=settings, registry=None)
    args = SimpleNamespace(
        parallel=None, plan="plan.md", tier="normal", model=None, provider=None
    )
    captured = []
    monkeypatch.setattr(loop_module, "_set_delegation_id", lambda *_args: None)
    monkeypatch.setattr(loop_module, "open_live_view", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop_module, "_prepare_tests", lambda *_args: (None, {
        "test_review": None, "credits": [], "test_writer": None, "blocks": []
    }))
    rounds = iter((
        {
            "status": "violated_tests",
            "violations": [{"file": goal.path, "change": "modified"}],
            "_runs": [],
        },
        {"status": "done", "_goal_budget": 0.0, "_runs": []},
    ))
    monkeypatch.setattr(
        loop_module, "run_round", lambda *_args, **_kwargs: next(rounds)
    )
    monkeypatch.setattr(loop_module, "_write_delegation", lambda *_args, **_kwargs: None)

    def evaluate(_goal, _root, _settings, reviewer=None, budget=None):
        captured.append(budget)
        return [{"kind": "command", "passed": True}]

    monkeypatch.setattr(loop_module, "evaluate_goal", evaluate)

    result = loop_module.autopilot(tmp_path, server, args, "review-b1", goal=goal)

    assert captured == [settings.verify_timeout]
    assert result["status"] == "completed_unverified"
    assert result["violations"][0]["file"] == goal.path


def test_review_b1_boot_and_hop_errors_redact_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_B1_API_KEY", "review-b1-fake-secret")
    settings = make_settings(tmp_path, providers={
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "api_key_env": "REVIEW_B1_API_KEY",
            "binary": str(FAKE_CLAUDE),
        }
    })
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent("review-b1", tmp_path, provider="glm")

    class FailedBoot:
        def boot(self, *_args):
            from subagent.providers.base import Session

            return "boot failed: review-b1-fake-secret", Session(provider="glm")

    try:
        assert agent.wait_ready(5) is None
        agent._booted.pop("glm")
        agent._sessions.pop("glm")
        assert agent._boot_driver(FailedBoot(), agent.cfg) == "boot failed: [REDACTED]"
        run = Run(run_id="review-b1-run", agent_id=agent.agent_id, prompt="task")
        hop = Hop(0, "glm", "zai", None, "refused", message="refused review-b1-fake-secret")

        agent._hop(run, hop)

        assert run.hops[0]["message"] == "refused [REDACTED]"
    finally:
        registry.shutdown()
