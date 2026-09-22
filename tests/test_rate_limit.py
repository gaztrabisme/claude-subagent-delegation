"""Rate-limit outage behaviour: honest classification + bounded retry.

Uses the FakeProcess seam from test_runs (spawn is monkeypatched via
``subagent.runs._spawn_claude``; no real ``claude`` is ever spawned).
Failure events are built through the real ``classify_exit`` so the fakes
produce exactly what a real ``ClaudeProcess.events()`` would yield.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from subagent import runs
from subagent.runs import (
    COMPLETED,
    FAILED,
    ClaudeProcess,
    Registry,
    classify_exit,
)

from .conftest import make_settings
from .test_runs import FakeProcess, _result, _wait

MODEL_WARNING = "[claude-code:unrecognized_model]"
WARNING_LINE = MODEL_WARNING + ' {"model":"glm-5.3[1m]","query_source":"sdk"}'


def _failure_events(code: int, stderr: str, session_id: str = "sess-rl") -> list[dict[str, Any]]:
    """The synthetic event a real ClaudeProcess.events() yields on bad exit."""
    kind, message = classify_exit(code, stderr, None)
    return [
        {"type": "system", "subtype": "init", "session_id": session_id},
        {
            "type": "result",
            "is_error": True,
            "result": "",
            "error": message,
            "error_kind": kind,
            "session_id": session_id,
        },
    ]


def _scripted_spawn(script: list[list[dict[str, Any]]]):
    """Spawn factory returning one FakeProcess per scripted attempt."""
    spawned: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        events = script[len(spawned)] if len(spawned) < len(script) else script[-1]
        proc = FakeProcess(events, argv, env)
        spawned.append(proc)
        return proc

    return spawn, spawned


def _registry(tmp_path: Path, monkeypatch, script, **overrides):
    settings = make_settings(
        tmp_path, rate_limit_retries=2, rate_limit_backoff=0.01, **overrides
    )
    spawn, spawned = _scripted_spawn(script)
    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    reg.spawned = spawned  # type: ignore[attr-defined]
    return reg


# --- D(1): rate limit twice, then success ---------------------------------

def test_rate_limit_retries_then_succeeds(tmp_path: Path, monkeypatch):
    stderr = WARNING_LINE + "\nHTTP 429 Too Many Requests"
    script = [_failure_events(1, stderr), _failure_events(1, stderr), _result("done", session_id="sess-rl")]
    real_sleep = runs._sleep
    slices: list[float] = []
    monkeypatch.setattr(runs, "_sleep", lambda s: (slices.append(s), real_sleep(s)))
    reg = _registry(tmp_path, monkeypatch, script)
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == COMPLETED
        assert run.result_text == "done"
        assert run.error in (None, "")
        assert MODEL_WARNING not in (run.error or "")
        assert len(reg.spawned) == 3  # type: ignore[attr-defined]
        # Retries resumed the session the failed attempt had established.
        assert "--resume" in reg.spawned[1].argv  # type: ignore[attr-defined]
        assert "sess-rl" in reg.spawned[1].argv  # type: ignore[attr-defined]
        # Backoff 0.01 with exponential base: 0.01 then 0.02.
        assert 2 <= len(slices) <= 4
        assert abs(sum(slices) - 0.03) < 0.02
    finally:
        reg.shutdown()


# --- D(2): permanent rate limit exhausts retries honestly ------------------

def test_rate_limit_exhaustion_fails_after_all_retries(tmp_path: Path, monkeypatch):
    stderr = WARNING_LINE + "\nError: 529 overloaded"
    script = [_failure_events(1, stderr)]  # always rate-limits
    reg = _registry(tmp_path, monkeypatch, script)  # retries=2
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED
        assert run.finish_reason == "rate_limited"
        assert run.error is not None and run.error.startswith("rate_limited")
        assert "529" in (run.error or "")
        assert MODEL_WARNING not in (run.error or "")
        assert len(reg.spawned) == 3  # type: ignore[attr-defined]  # retries + 1
    finally:
        reg.shutdown()


# --- D(4): non-rate-limit exits are never retried --------------------------

def test_plain_cli_error_is_not_retried(tmp_path: Path, monkeypatch):
    script = [_failure_events(1, "boom")]  # no rate-limit token anywhere
    reg = _registry(tmp_path, monkeypatch, script)  # retries=2, unused
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED
        assert run.finish_reason == "cli_error"
        assert "boom" in (run.error or "")
        assert MODEL_WARNING not in (run.error or "")
        assert len(reg.spawned) == 1  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


# --- D(3): classify_exit unit tests ----------------------------------------

def test_classify_exit_rate_limit_markers():
    for stderr in (
        WARNING_LINE + "\nHTTP 429 Too Many Requests",
        "API error: 529 Service Overloaded",
        "Payment required: Insufficient Balance",
        "rate_limit_error: please retry later",
        "You have exceeded your quota",
        "we're overloaded right now",
    ):
        kind, message = classify_exit(1, stderr, None)
        assert kind == "rate_limited", stderr
        assert message.startswith("rate_limited: ")
        assert MODEL_WARNING not in message


def test_classify_exit_detects_rate_limit_in_result_event():
    # stderr carries only the cosmetic warning; the real cause is in the
    # last result event the CLI printed before dying.
    event = {
        "type": "result",
        "subtype": "error_during_execution",
        "error": "Anthropic API error: rate_limit_error (429)",
    }
    kind, message = classify_exit(1, WARNING_LINE, event)
    assert kind == "rate_limited"
    assert MODEL_WARNING not in message
    assert "429" in message or "rate_limit" in message


def test_classify_exit_plain_cli_error_strips_model_warning():
    stderr = WARNING_LINE + "\nTypeError: Cannot read properties of undefined"
    kind, message = classify_exit(1, stderr, None)
    assert kind == "cli_error"
    assert message.startswith("cli_error: claude exited 1:")
    assert "TypeError" in message
    assert MODEL_WARNING not in message and "unrecognized_model" not in message


def test_classify_exit_auth_and_clean_exit():
    kind, message = classify_exit(1, "401 Unauthorized: invalid api key", None)
    assert kind == "auth"
    assert "invalid api key" in message
    # A clean exit is never classified as an error by events(); defensive path.
    assert classify_exit(0, WARNING_LINE, None) == ("cli_error", "")


# --- Fix (2026-09-18): "rate_limited: success" ------------------------------

def _live_429_event(session_id: str = "sess-live") -> dict[str, Any]:
    """The live-trace shape behind the 11 mislabelled runs (2026-09-18).

    A real z.ai 429 (code 1313, Fair Usage throttle): the CLI printed a
    result event with subtype "success" but is_error true and the API error
    text in `result` -- no `error` field -- then exited non-zero with only
    the cosmetic warning on stderr. The rate_limited class was right; the
    old detail text fell back to `subtype` and read "success".
    """
    return {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "API Error: Request rejected (429) · [1313][Your account's "
        "current usage pattern does not comply with the Fair Usage Policy, "
        "and your request frequency has been limited.]",
        "session_id": session_id,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _live_failure_events(session_id: str = "sess-live") -> list[dict[str, Any]]:
    """The live CLI event plus the synthetic exit event a real
    ClaudeProcess.events() would yield for it, via classify_exit."""
    cli_event = _live_429_event(session_id)
    kind, message = classify_exit(1, WARNING_LINE, cli_event)
    return [
        {"type": "system", "subtype": "init", "session_id": session_id},
        cli_event,
        {
            "type": "result",
            "is_error": True,
            "result": "",
            "error": message,
            "error_kind": kind,
            "session_id": session_id,
        },
    ]


def test_live_fair_usage_429_label_carries_real_detail():
    kind, message = classify_exit(1, WARNING_LINE, _live_429_event())
    assert kind == "rate_limited"
    assert "1313" in message and "429" in message
    # The subtype is metadata, never the detail text.
    assert "success" not in message


def test_live_fair_usage_429_shape_is_retried(tmp_path: Path, monkeypatch):
    # The run loop must treat the live shape as a transient limit: retry the
    # same prompt into the same session, then complete on the surviving turn.
    script = [
        _live_failure_events(),
        _live_failure_events(),
        _result("done", session_id="sess-live"),
    ]
    reg = _registry(tmp_path, monkeypatch, script)  # retries=2
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == COMPLETED
        assert run.result_text == "done"
        assert len(reg.spawned) == 3  # type: ignore[attr-defined]
        assert "--resume" in reg.spawned[1].argv  # type: ignore[attr-defined]
        assert "sess-live" in reg.spawned[1].argv  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


def test_successful_event_mentioning_limit_is_not_rate_limited():
    # Guard, not the live shape: a genuinely successful result event whose
    # answer text merely mentions limit tokens stays cli_error -- the old
    # whole-event haystack would have classified it rate_limited, and the
    # subtype fallback would have labelled it "success".
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "429 rate limit exceeded, quota exhausted",
        "session_id": "sess-x",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    kind, message = classify_exit(1, WARNING_LINE, event)
    assert kind == "cli_error"
    assert "rate_limited" not in message
    assert "success" not in message
    assert "429" not in message  # answer text is not a diagnostic


def test_error_result_event_yields_real_rate_limit_detail():
    # When the event really reports the limit, the label carries its status,
    # message and retry info -- not the subtype.
    event = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": 'API Error: 429 {"error":{"type":"rate_limit_error",'
        '"message":"Too many requests","retry_after":30}}',
    }
    kind, message = classify_exit(1, WARNING_LINE, event)
    assert kind == "rate_limited"
    assert "429" in message and "retry_after" in message
    assert "error_during_execution" not in message


def test_run_with_successful_event_is_not_labelled_rate_limited(
    tmp_path: Path, monkeypatch
):
    # Guard one level up, through the run loop's terminal handling (_collect
    # / _ingest): a success (is_error false) event followed by a non-zero
    # exit is failed honestly, never labelled rate_limited.
    success_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "429 rate limit exceeded, quota exhausted",
        "session_id": "sess-x",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    kind, message = classify_exit(1, WARNING_LINE, success_event)
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-x"},
        success_event,
        {
            "type": "result",
            "is_error": True,
            "result": "",
            "error": message,
            "error_kind": kind,
            "session_id": "sess-x",
        },
    ]
    reg = _registry(tmp_path, monkeypatch, [events])
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("do", verification="true"))
        assert run.state == FAILED
        assert run.finish_reason == "cli_error"
        assert "rate_limited" not in (run.error or "")
        assert "success" not in (run.error or "")
    finally:
        reg.shutdown()


# --- Fix A: stderr drained concurrently (real subprocess, not claude) ------

def test_events_drains_stderr_concurrently_and_classifies(tmp_path: Path):
    # >200KB of stderr (far over the 64KB pipe buffer) would deadlock the old
    # read-after-exit code. Uses `sh`, never `claude`.
    ok_line = '{"type":"result","is_error":false,"result":"ok"}'
    script = (
        "for i in $(seq 1 4000); do echo '" + WARNING_LINE + "' >&2; done; "
        "echo '" + ok_line + "'; exit 3"
    )
    proc = ClaudeProcess(["sh", "-c", script], {}, str(tmp_path))
    events: list[dict[str, Any]] = []
    finished = threading.Event()

    def consume():
        events.extend(proc.events())
        finished.set()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        assert finished.wait(15), "events() deadlocked on a full stderr pipe"
    finally:
        proc.kill()
        reader.join(timeout=5)
    synthetic = [e for e in events if e.get("is_error")]
    assert synthetic and synthetic[0]["error_kind"] == "cli_error"
    assert synthetic[0]["error"] == "cli_error: claude exited 3:"
    assert MODEL_WARNING not in synthetic[0]["error"]
