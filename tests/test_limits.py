"""Report items 6, 3 and 9: step semantics, live trips, continue verification,
and z.ai rate-limit codes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from subagent import runs
from subagent.config import Settings
from subagent.runs import (
    COMPLETED,
    COMPLETED_UNVERIFIED,
    FAILED,
    Registry,
    exit_event,
    zai_code,
    zai_reset,
)

from .conftest import make_settings
from .test_runs import FakeProcess, _result, _wait

WARNING_LINE = '[claude-code:unrecognized_model] {"model":"glm-5.3[1m]"}'


def _registry(tmp_path: Path, monkeypatch, script: list[list[dict[str, Any]]], **overrides):
    settings = make_settings(tmp_path, **overrides)
    spawned: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        events = script[len(spawned)] if len(spawned) < len(script) else script[-1]
        spawned.append(FakeProcess(events, argv, env))
        return spawned[-1]

    monkeypatch.setattr("subagent.runs._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    reg.spawned = spawned  # type: ignore[attr-defined]
    return reg


def _tool(command: str, message_id: str = "m", usage: dict | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": message_id,
        "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}],
    }
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "message": message}


def _init(session_id: str = "sess-1") -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "session_id": session_id}


def _success(text: str = "done", turns: int = 1) -> dict[str, Any]:
    return {
        "type": "result", "subtype": "success", "is_error": False, "result": text,
        "session_id": "sess-1", "usage": {"input_tokens": 1, "output_tokens": 1},
        "num_turns": turns,
    }


# --- item 6: SAM_MAX_STEPS counts turns ---------------------------------------


def test_a_child_that_finishes_under_the_turn_cap_is_not_relabelled(tmp_path, monkeypatch):
    """Five parallel tool calls in two turns, with SAM_MAX_STEPS=2: completed."""
    events = [_init(), *[_tool(f"ls {i}") for i in range(5)], _success(turns=2)]
    reg = _registry(tmp_path, monkeypatch, [events], max_steps=2)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == COMPLETED, run.error
        assert run.trip is None
        assert run.usage.steps == 5 and run.usage.turns == 2
        argv = reg.spawned[0].argv  # type: ignore[attr-defined]
        assert argv[argv.index("--max-turns") + 1] == "2"
    finally:
        reg.shutdown()


def test_claude_codes_max_turns_result_is_failed_steps(tmp_path, monkeypatch):
    """The shape Claude Code 2.1.276 prints at --max-turns, then exit 1."""
    max_turns = {
        "type": "result", "subtype": "error_max_turns", "is_error": True,
        "result": None, "num_turns": 2, "terminal_reason": "max_turns",
        "errors": ["Reached maximum number of turns (1)"], "session_id": "sess-1",
    }
    events = [_init(), _tool("ls"), max_turns, exit_event(1, WARNING_LINE, max_turns)]
    reg = _registry(tmp_path, monkeypatch, [events], max_steps=1)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED
        assert run.finish_reason == "steps"
        assert "--max-turns of 1" in (run.error or "")
        assert run.verification_result is None
        assert len(reg.spawned) == 1  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


def test_the_loop_detector_kills_the_child_mid_stream(tmp_path, monkeypatch):
    events = [_init(), *[_tool("pytest -q") for _ in range(4)], _tool("ls"), _success("late")]
    reg = _registry(tmp_path, monkeypatch, [events], loop_strikes=3)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        proc = reg.spawned[0]  # type: ignore[attr-defined]
        assert proc.killed
        assert run.state == FAILED and run.finish_reason == "loop"
        # Stopped at the third repeat: nothing after it was read.
        assert run.usage.steps == 3
        assert run.final_response == ""
    finally:
        reg.shutdown()


def test_the_token_budget_kills_the_child_mid_stream(tmp_path, monkeypatch):
    usage = {"input_tokens": 80, "output_tokens": 0}
    events = [
        _init(),
        _tool("ls a", "m1", usage),
        _tool("ls b", "m1", usage),   # same response: counted once
        _tool("ls c", "m2", usage),   # 160 > 100
        _tool("ls d", "m3", usage),
        _success("late"),
    ]
    reg = _registry(tmp_path, monkeypatch, [events], turn_token_budget=100)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert reg.spawned[0].killed  # type: ignore[attr-defined]
        assert run.state == FAILED and run.finish_reason == "budget"
        assert run.usage.steps == 3
    finally:
        reg.shutdown()


# --- item 3: continue reuses the delegate's verification ------------------------


def test_continue_without_verification_reuses_the_delegates(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_result("ok")])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        _wait(agent.delegate("first", "test -d ."))
        run = _wait(agent.follow_up("wrap up"))
        assert run.verification == "test -d ."
        assert run.state == COMPLETED
        assert "reused the delegate command" in run.detail()["verification_note"]
    finally:
        reg.shutdown()


def test_continue_with_an_empty_verification_skips_it(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_result("ok")])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        _wait(agent.delegate("first", "true"))
        run = _wait(agent.follow_up("wrap up", ""))
        assert run.verification is None
        assert run.state == COMPLETED_UNVERIFIED
        assert "skipped" in run.detail()["verification_note"]
    finally:
        reg.shutdown()


def test_continue_with_its_own_verification_uses_it(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_result("ok")])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        _wait(agent.delegate("first", "true"))
        run = _wait(agent.follow_up("more", "false"))
        assert run.verification == "false"
        assert run.state == COMPLETED_UNVERIFIED
        assert "verification_note" not in run.detail()
    finally:
        reg.shutdown()


# --- item 9: z.ai codes ---------------------------------------------------------


QUOTA_1308 = (
    "API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour. "
    "Your limit will reset at 2026-09-06 15:00:00]"
)
QUOTA_1310 = "API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted.]"
THROTTLE_1313 = (
    "API Error: Request rejected (429) · [1313][Your account's current usage pattern "
    "does not comply with the Fair Usage Policy, and your request frequency has been limited.]"
)


def _limited(text: str) -> list[dict[str, Any]]:
    cli = {"type": "result", "subtype": "success", "is_error": True, "result": text,
           "session_id": "sess-1"}
    return [_init(), cli, exit_event(1, WARNING_LINE, cli)]


def test_zai_code_and_reset_parsing():
    assert zai_code(QUOTA_1308) == "1308"
    assert zai_code(THROTTLE_1313) == "1313"
    assert zai_code('API Error: 429 {"error":{"code":"1310","message":"x"}}') == "1310"
    assert zai_code("HTTP 429 Too Many Requests") is None
    assert zai_reset(QUOTA_1308) == "2026-09-06 15:00:00"
    assert zai_reset(QUOTA_1310) is None


def test_plan_quota_fails_at_once_with_the_reset_time(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_limited(QUOTA_1308), _result("never")],
                    rate_limit_retries=3, rate_limit_backoff=0.01)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED and run.finish_reason == "rate_limited"
        assert "1308" in run.error and "resets at 2026-09-06 15:00:00" in run.error
        assert len(reg.spawned) == 1  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


def test_plan_quota_without_a_reset_time_says_so(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_limited(QUOTA_1310)],
                    rate_limit_retries=3, rate_limit_backoff=0.01)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED
        assert "1310" in run.error and "reset time not given" in run.error
        assert len(reg.spawned) == 1  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


def test_fair_use_throttle_backs_off_on_its_own_clock(tmp_path, monkeypatch):
    slices: list[float] = []
    real_sleep = runs._sleep
    monkeypatch.setattr(runs, "_sleep", lambda s: (slices.append(s), real_sleep(s)))
    script = [_limited(THROTTLE_1313), _limited(THROTTLE_1313), _result("done")]
    reg = _registry(tmp_path, monkeypatch, script, rate_limit_retries=2,
                    rate_limit_backoff=0.001, throttle_backoff=0.02)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == COMPLETED
        assert len(reg.spawned) == 3  # type: ignore[attr-defined]
        # 0.02 then 0.04 from SAM_THROTTLE_BACKOFF, not 0.001 + 0.002.
        assert sum(slices) >= 0.05
    finally:
        reg.shutdown()


def test_fair_use_throttle_exhaustion_tells_the_caller_to_run_fewer(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_limited(THROTTLE_1313)],
                    rate_limit_retries=1, throttle_backoff=0.001)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED
        assert "1313" in run.error and "run fewer children" in run.error
        assert len(reg.spawned) == 2  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


def test_throttle_backoff_default_is_a_minute(monkeypatch):
    monkeypatch.delenv("SAM_THROTTLE_BACKOFF", raising=False)
    assert Settings.from_env().throttle_backoff == 60.0
