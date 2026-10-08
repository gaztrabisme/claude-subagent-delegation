"""Opt-in service-provider hooks exercised through the real run lifecycle."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import threading
from collections import deque

import pytest

from subagent import providers
from subagent.goal import parse_goal
from subagent.providers import base
from subagent.runs import COMPLETED, COMPLETED_UNVERIFIED, WORKING, Registry
from subagent.telemetry.trace import Trace

from .conftest import make_settings
from .test_runs import FakeProcess, _result, _wait


def outcome(state=COMPLETED, **extra):
    return {
        "state": state,
        "phase": "done",
        "finish_reason": "service_finished",
        "result": "service result",
        "verification": {
            "command": "native check", "passed": True, "reason": "passed",
            "duration_seconds": 0.5, "exit_code": 0, "output_tail": "ok",
            "service_check_id": "check-7",
        },
        "uat": [],
        **extra,
    }


class NativeProvider:
    name = "fake-native"
    needs_api_key = False
    prompt_on_stdin = False

    def __init__(self):
        self.capabilities = base.ProviderCapabilities(
            run_context=True, native_outcome=True, native_verification=True,
            native_usage=True, cancel=True, poll=True,
        )
        self.results = deque([outcome()])
        self.poll_results = deque()
        self.contexts = []
        self.processes = []
        self.cancels = []
        self.cancel_error = None
        self.gate = None
        self.spawned = threading.Event()
        self.polled = threading.Event()

    def guard(self, settings, cfg):
        return "native"

    def boot(self, settings, agent_id, cfg):
        return None, base.Session(provider=cfg.name)

    def spawn(self, cfg, settings, agent_id, prompt, cwd, session, model, *, context):
        self.contexts.append(context)
        result = self.results.popleft()
        session.session_id = "service-agent"
        session.data["run_id"] = "service-run"
        events = result if isinstance(result, list) else [
            {"type": "result", "session_id": session.session_id, "outcome": result}
        ]
        process = FakeProcess(events, [], {}, gate=self.gate)
        self.processes.append(process)
        self.spawned.set()
        return process

    def refusal(self, cfg, events):
        return None

    def cancel(self, session, *, reason, kind):
        assert not any(p.killed for p in self.processes)
        self.cancels.append((session.session_id, reason, kind))
        if self.cancel_error:
            raise self.cancel_error

    def poll(self, session):
        assert session.data["run_id"] == "service-run"
        self.polled.set()
        return self.poll_results.popleft() if self.poll_results else None


@pytest.fixture
def native(tmp_path, monkeypatch):
    driver = NativeProvider()
    monkeypatch.setattr(providers, "for_driver", lambda _: driver)
    settings = make_settings(tmp_path, run_timeout=30, rate_limit_backoff=0.001,
                             rate_limit_retries=1)
    trace = Trace(tmp_path / "trace.jsonl")
    registry = Registry(settings, start_reaper=False, trace=trace)
    monkeypatch.setenv("SUBAGENT_CONFIG", str(tmp_path / "subagent.toml"))
    server = importlib.import_module("subagent.mcp_server")
    monkeypatch.setattr(server, "registry", registry)
    yield driver, registry, server
    registry.shutdown()


def start(native, tmp_path, *, goal=None):
    driver, registry, _ = native
    agent = registry.create_agent("native", tmp_path, "model", fallback="none", goal=goal)
    assert agent.wait_ready(5) is None
    run = agent.delegate("do work", "caller check")
    assert driver.spawned.wait(5)
    return agent, run


def test_spawn_receives_run_context(native, tmp_path):
    driver, _, _ = native
    goal = parse_goal('Execute plan "test" (plan.md). Goal rows:\n1 works: `true`', tmp_path)
    _, run = start(native, tmp_path, goal=goal)
    _wait(run)
    context = driver.contexts[0]
    assert context.verification == "caller check"
    assert context.goal == goal
    assert context.request_id == run.request_id
    assert context.deadline == run.deadline
    assert context.deadline > run.started_at


def test_request_id_stable_across_retry_of_same_run(native, tmp_path):
    driver, _, _ = native
    driver.results = deque([
        [{"type": "result", "is_error": True, "error_kind": "rate_limited",
          "error": "retry later", "session_id": "service-agent"}],
        outcome(), outcome(),
    ])
    agent, first = start(native, tmp_path)
    _wait(first)
    _wait(agent.follow_up("another run"))
    ids = [context.request_id for context in driver.contexts]
    assert len(ids) == 3
    assert ids[0] == ids[1] == first.request_id
    assert ids[2] != ids[0]


@pytest.mark.parametrize("action", ["cancel", "timeout", "close"])
def test_cancel_hook_runs_on_cancel_timeout_and_close(native, tmp_path, action):
    driver, registry, server = native
    driver.gate = threading.Event()
    agent, run = start(native, tmp_path)
    if action == "cancel":
        asyncio.run(server.cancel(agent.agent_id))
    elif action == "timeout":
        run.deadline = 1
        assert registry.reap_once() == [(agent.agent_id, "timeout")]
    else:
        registry.shutdown()
    assert len(driver.cancels) == 1
    assert driver.cancels[0][0] == "service-agent"
    assert driver.cancels[0][2] == {"cancel": "cancel", "timeout": "timeout",
                                      "close": "shutdown"}[action]
    assert driver.processes[0].killed
    assert run.done.is_set()


def test_cancel_hook_error_does_not_block_kill(native, tmp_path, caplog):
    driver, _, server = native
    driver.cancel_error = RuntimeError("cancel failed")
    driver.gate = threading.Event()
    agent, run = start(native, tmp_path)
    asyncio.run(server.cancel(agent.agent_id))
    assert driver.processes[0].killed
    assert run.state == "cancelled"
    assert "provider cancel failed" in caplog.text


@pytest.mark.parametrize("phase", ["awaiting_approval", "awaiting_human"])
def test_human_wait_returns_await_and_stays_working(native, tmp_path, monkeypatch, phase):
    driver, _, server = native
    driver.results = deque([outcome(WORKING, phase=phase, needs_human=True,
                                   human_action="human approve plan-7", plan_id="plan-7")])
    monkeypatch.setattr("subagent.runs.run_verification", lambda *a, **kw: pytest.fail("local check"))
    _, run = start(native, tmp_path)

    async def await_with_limit():
        return await asyncio.wait_for(server.await_run(run.run_id, wait_seconds=30), timeout=2)

    result = asyncio.run(await_with_limit())
    assert result["state"] == WORKING
    assert result["phase"] == phase
    assert result["needs_human"] is True
    assert result["human_action"] == "human approve plan-7"
    assert result["plan_id"] == "plan-7"
    assert not run.done.is_set()
    assert run.finished_at is None


def test_human_wait_resumes_to_terminal_on_poll(native, tmp_path):
    driver, _, server = native
    driver.results = deque([outcome(WORKING, phase="awaiting_approval", needs_human=True,
                                   human_action="human approve", plan_id="plan-7")])
    _, run = start(native, tmp_path)
    assert driver.polled.wait(5)
    assert run.state == WORKING
    driver.poll_results.append(outcome(needs_human=False, human_action=None, plan_id="plan-7"))
    result = _wait(run).detail()
    assert result["state"] == COMPLETED
    assert result["needs_human"] is False
    assert result["human_action"] is None
    assert len(driver.processes) == 1
    assert asyncio.run(server.await_run(run.run_id, 0))["state"] == COMPLETED


def test_native_verification_skips_local_check(native, tmp_path, monkeypatch):
    driver, _, _ = native
    reported = outcome()
    driver.results = deque([reported])
    monkeypatch.setattr("subagent.runs.run_verification", lambda *a, **kw: pytest.fail("local check"))
    monkeypatch.setattr("subagent.runs.evaluate_goal", lambda *a, **kw: pytest.fail("local UAT"))
    goal = parse_goal('Execute plan "test" (plan.md). Goal rows:\n1 works: `true`', tmp_path)
    _, run = start(native, tmp_path, goal=goal)
    detail = _wait(run).detail()
    assert detail["verification"] == reported["verification"]
    assert detail["uat"] == []
    assert detail["finish_reason"] == "service_finished"


@pytest.mark.parametrize("state", [COMPLETED_UNVERIFIED, "failed", "cancelled"])
def test_native_completed_unverified_kept(native, tmp_path, state):
    driver, _, _ = native
    reported = outcome(state, finish_reason="check_not_run",
                       verification={"passed": False, "exit_code": None, "output_tail": "unrun"},
                       extra={"profile": "test-profile", "hops": [{"endpoint": "local"}]})
    driver.results = deque([reported])
    _, run = start(native, tmp_path)
    detail = _wait(run).detail()
    for key, value in reported.items():
        assert detail[key] == value
    assert detail["hops"] == run.hops
    assert detail["hops"] != detail["extra"]["hops"]


@pytest.mark.parametrize("cash_usd,credits", [(0.00123, None), (0, 2), (None, None)])
def test_native_usage_and_cost_not_repriced(native, tmp_path, monkeypatch, cash_usd, credits):
    driver, _, server = native
    usage = {"input": 20, "output": 7, "cache_read": 0, "cache_write": 0,
             "reasoning": 0, "total": 27, "steps": 2, "turns": 1,
             "usage_unreported": ["cache_write", "reasoning"]}
    turn = {"ts_start": 1, "ts_first_token": 2, "ts_end": 3, "provider": "endpoint",
            "model": "model", "input": 20, "output": 7, "cache_read": 0,
            "cache_write": 0, "reasoning": 0, "tool_calls": [], "stop_reason": "end",
            "usage_unreported": ["cache_write", "reasoning"]}
    reported = outcome(usage=usage, usage_unreported=["cache_write", "reasoning"],
                       credits=credits, credits_unit="service_units", cash_usd=cash_usd,
                       turn_usage=[turn])
    driver.results = deque([reported])
    monkeypatch.setattr("subagent.runs.price_run", lambda *a, **kw: pytest.fail("repriced"))
    _, run = start(native, tmp_path)
    detail = _wait(run).detail()
    for key in ("usage", "usage_unreported", "credits", "credits_unit", "cash_usd", "turn_usage"):
        assert detail[key] == reported[key]
    assert asyncio.run(server.transcript(run.run_id))["usage"] == usage
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    record = next(row for row in records if row["kind"] == "run")
    for key in ("usage", "usage_unreported", "credits", "credits_unit", "cash_usd", "turn_usage"):
        assert record[key] == reported[key]
    assert record["turns"] == usage["turns"]
    from subagent.report import _price_row, _run_row
    from subagent.telemetry.cost import provider_costs

    specs = {run.provider: {"kind": "per_token", "input": 999, "output": 999, "cache_read": 999}}
    row = _run_row(record, None, None, specs)
    provider_costs(None, [row], specs)
    _price_row(row, specs)
    assert row["provider_usd"] == reported["cash_usd"]
    from subagent import report

    built = report.build([tmp_path / "trace.jsonl"], None, None)
    assert built.providers[0]["provider_usd"] == cash_usd
    assert built.providers[0]["credits"] == credits
    assert "<html" in report.render_html(built)


def test_native_outcome_alone_does_not_skip_local_verification(native, tmp_path):
    driver, _, _ = native
    driver.capabilities = dataclasses.replace(driver.capabilities, native_verification=False)
    agent, run = start(native, tmp_path)
    _wait(run)
    driver.results.append(outcome())
    failed_check = _wait(agent.follow_up("check again", verification="false"))
    assert failed_check.state == COMPLETED_UNVERIFIED
    assert failed_check.detail()["verification"]["passed"] is False
    assert failed_check.detail()["verification"]["command"] == "false"
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    record = next(row for row in records if row.get("run_id") == failed_check.run_id and row["kind"] == "run")
    assert record["verification_passed"] is False
    assert record["verification"]["command"] == "false"


def test_cancel_during_spawn_waits_for_session_publication(native, tmp_path, monkeypatch):
    driver, registry, _ = native
    entered, release = threading.Event(), threading.Event()
    original = driver.spawn

    def blocked_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(driver, "spawn", blocked_spawn)
    agent = registry.create_agent("race", tmp_path, "model", fallback="none")
    assert agent.wait_ready(5) is None
    run = agent.delegate("task", "true")
    assert entered.wait(5)
    closer = threading.Thread(target=agent.close)
    closer.start()
    with agent._wake:
        assert agent._wake.wait_for(lambda: agent._closing, timeout=5)
    assert not driver.cancels
    release.set()
    closer.join(5)
    assert not closer.is_alive()
    assert len(driver.cancels) == 1
    assert driver.cancels[0][0] == "service-agent"
    assert driver.processes[0].killed
    assert run.state == "cancelled"


@pytest.mark.parametrize("native_outcome", [True, False])
def test_cancel_prevents_a_native_client_starting_after_kill(native, tmp_path, monkeypatch, native_outcome):
    driver, _, _ = native
    if not native_outcome:
        driver.capabilities = dataclasses.replace(
            driver.capabilities, native_outcome=False, native_verification=False, native_usage=False,
        )
    entered, release = threading.Event(), threading.Event()
    started = threading.Event()
    original = driver.spawn

    class DelayedClient(base.Process):
        killed = False

        def events(self):
            entered.set()
            assert release.wait(5)
            self._start()
            yield {"type": "result", "outcome": outcome()}

        def kill(self):
            super().kill()
            self.killed = True
            release.set()

    def delayed_spawn(*args, **kwargs):
        original(*args, **kwargs)
        process = DelayedClient(["unused-client"], {}, str(tmp_path))
        driver.processes[-1] = process
        return process

    monkeypatch.setattr(driver, "spawn", delayed_spawn)

    def unexpected_start(*args, **kwargs):
        started.set()
        raise RuntimeError("cancelled client started")

    monkeypatch.setattr("subagent.providers.base.subprocess.Popen", unexpected_start)
    agent, run = start(native, tmp_path)
    assert entered.wait(5)
    agent.close()
    assert run.state == "cancelled"
    assert len(driver.cancels) == 1
    assert driver.processes[0].killed
    assert not started.is_set()


def test_poll_exception_during_cancel_keeps_cancelled(native, tmp_path, monkeypatch):
    driver, _, _ = native
    entered, release = threading.Event(), threading.Event()
    driver.results = deque([outcome(WORKING, phase="awaiting_human", needs_human=True)])

    def blocked_poll(session):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("poll failed after cancel")

    original = driver.cancel

    def cancel_then_release(session, **kwargs):
        original(session, **kwargs)
        release.set()

    monkeypatch.setattr(driver, "poll", blocked_poll)
    monkeypatch.setattr(driver, "cancel", cancel_then_release)
    agent, run = start(native, tmp_path)
    assert entered.wait(5)
    agent.close()
    assert run.state == "cancelled"
    assert run.finish_reason == "cancel"
    assert run.error == "cancelled by caller"
    assert run.done.is_set()


def test_cancel_while_waiting_for_human(native, tmp_path):
    driver, _, server = native
    driver.results = deque([outcome(WORKING, phase="awaiting_approval", needs_human=True)])
    agent, run = start(native, tmp_path)
    assert driver.polled.wait(5)
    asyncio.run(server.cancel(agent.agent_id))
    assert run.state == "cancelled"
    assert len(driver.cancels) == 1


def test_native_snapshot_omissions_do_not_invent_checks(native, tmp_path):
    driver, _, _ = native
    driver.results = deque([{"state": COMPLETED_UNVERIFIED, "finish_reason": "no_check"}])
    goal = parse_goal('Execute plan "test" (plan.md). Goal rows:\n1 works: `true`', tmp_path)
    _, run = start(native, tmp_path, goal=goal)
    detail = _wait(run).detail()
    assert "uat" not in detail
    assert "verification" not in detail
    assert "verification_note" not in detail


def test_native_usage_snapshot_totals_are_not_recomputed(native, tmp_path):
    driver, _, _ = native
    usage = {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0,
             "reasoning": 0, "total": 17, "steps": 1, "turns": 2,
             "usage_unreported": ["cache_write"]}
    driver.results = deque([outcome(usage=usage)])
    agent, run = start(native, tmp_path)
    _wait(run)
    assert run.usage.as_dict() == usage
    assert agent.usage().as_dict() == usage
    assert run.usage.total == 17


def test_native_missing_outcome_does_not_claim_completed(native, tmp_path):
    driver, _, _ = native
    driver.results = deque([_result("no native outcome")])
    _, run = start(native, tmp_path)
    assert _wait(run).state == "failed"
    assert "returned no outcome" in run.error


def test_native_cost_does_not_dilute_legacy_flat_plan():
    from subagent.telemetry.cost import provider_costs

    rows = [{"lane": "shared", "ts": 1},
            {"lane": "shared", "ts": 1, "native_usage": True, "cash_usd": 0}]
    provider_costs(None, rows, {"shared": {"kind": "flat_plan", "monthly_usd": 100}})
    assert rows[0]["provider_usd"] == 100
    assert rows[1]["provider_usd"] == 0


def test_native_spawn_error_is_not_repriced(native, tmp_path, monkeypatch):
    driver, registry, _ = native

    def failed_spawn(*args, **kwargs):
        raise RuntimeError("cannot connect")

    monkeypatch.setattr(driver, "spawn", failed_spawn)
    monkeypatch.setattr("subagent.runs.price_run", lambda *a, **kw: pytest.fail("repriced"))
    agent = registry.create_agent("failed", tmp_path, "model", fallback="none")
    assert agent.wait_ready(5) is None
    run = _wait(agent.delegate("task", "true"))
    assert run.state == "failed"
    assert run.native_usage is True
    assert run.cost is None


def test_native_event_reader_error_cancels_before_local_kill(native, tmp_path, monkeypatch):
    driver, registry, _ = native
    original = driver.spawn

    def broken_spawn(*args, **kwargs):
        process = original(*args, **kwargs)

        def broken_events():
            yield {"type": "system", "subtype": "init", "session_id": "service-agent"}
            raise RuntimeError("reader failed")

        process.events = broken_events
        return process

    monkeypatch.setattr(driver, "spawn", broken_spawn)
    agent = registry.create_agent("reader", tmp_path, "model", fallback="none")
    assert agent.wait_ready(5) is None
    run = _wait(agent.delegate("task", "true"))
    assert run.state == "failed"
    assert len(driver.cancels) == 1
    assert driver.processes[0].killed


@pytest.mark.parametrize("finish", ["completed", "cancelled", "poll_error"])
def test_human_wait_keeps_caller_guard_until_service_finishes(native, tmp_path, monkeypatch, finish):
    driver, registry, _ = native
    driver.results = deque([outcome(WORKING, phase="awaiting_human", needs_human=True)])
    released = threading.Event()
    agent = registry.create_agent("guard", tmp_path, "model", fallback="none")
    assert agent.wait_ready(5) is None

    def release_guard():
        if finish != "completed":
            assert driver.cancels
        released.set()
        return {}

    run = agent.delegate("task", "true", pre_verify=release_guard)
    assert driver.polled.wait(5)
    assert not released.is_set()
    if finish == "completed":
        driver.poll_results.append(outcome())
        _wait(run)
    elif finish == "cancelled":
        agent.close()
    else:
        def failed_poll(session):
            raise RuntimeError("poll failed")

        monkeypatch.setattr(driver, "poll", failed_poll)
        _wait(run)
    assert released.is_set()


def test_cancel_hook_errors_redact_configured_key(native, tmp_path, monkeypatch, caplog):
    driver, _, server = native
    monkeypatch.setenv("GLM_API_KEY", "test-token-canary")
    driver.cancel_error = RuntimeError("failed with test-token-canary")
    driver.gate = threading.Event()
    agent, run = start(native, tmp_path)
    result = asyncio.run(server.cancel(agent.agent_id))
    assert "test-token-canary" not in caplog.text
    assert "test-token-canary" not in json.dumps(result)
    assert "test-token-canary" not in json.dumps(run.detail())
    for process in driver.processes:
        assert "test-token-canary" not in json.dumps(process.argv)
        assert "test-token-canary" not in json.dumps(process.env)
    assert driver.processes[0].killed


def test_providers_without_hooks_unchanged(tmp_path, monkeypatch):
    captured = []

    def spawn(argv, env, cwd):
        captured.append((argv, env, cwd))
        return FakeProcess(_result("legacy result"), argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    registry = Registry(make_settings(tmp_path), start_reaper=False)
    try:
        agent = registry.create_agent("legacy", tmp_path, "model", fallback="none")
        assert agent.wait_ready(5) is None
        run = _wait(agent.delegate("task", "true"))
        assert run.state == COMPLETED
        assert run.detail()["verification"]["command"] == "true"
        assert run.detail()["result"] == "legacy result"
        assert run.usage.total == 2
        assert "needs_human" not in run.detail()
        assert run.cost is not None
        assert len(captured) == 1
    finally:
        registry.shutdown()
