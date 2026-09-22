"""Router: the lane chain, refusal classification, and rerouting a delegate.

Children are fakes that make one real HTTP request to their lane's
ANTHROPIC_BASE_URL, which points at the local mock endpoint, and turn the
answer into the events `claude -p` would print. So a lane that was skipped
shows up as zero hits on its mock path.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from subagent import router
from subagent.lane_state import LaneState
from subagent.lanes import load_lanes
from subagent.runs import COMPLETED, FAILED, Registry, exit_event
from subagent.telemetry.trace import SCHEMA

from .conftest import make_settings
from .test_runs import _result, _wait

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
SRC = Path(__file__).parent.parent / "src"
LANES = ("codex", "deepseek", "glm", "bppc", "omlx")


def _later(hours: float = 5) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


# --- chain() ---------------------------------------------------------------------


@pytest.mark.parametrize("primary", LANES)
def test_router_chain_full(primary):
    expected = [primary] + [n for n in ("codex", "deepseek", "glm", "bppc", "omlx")
                            if n != primary]
    assert router.chain(primary, "full") == expected


@pytest.mark.parametrize("primary", LANES)
def test_router_chain_local(primary):
    expected = [primary] + [n for n in ("bppc", "omlx") if n != primary]
    assert router.chain(primary, "local") == expected


@pytest.mark.parametrize("primary", LANES)
def test_router_chain_none(primary):
    assert router.chain(primary, "none") == [primary]


def test_router_chain_unknown_mode():
    with pytest.raises(ValueError):
        router.chain("glm", "sideways")


# --- classify_refusal() ----------------------------------------------------------


def _cli_error(text: str) -> list[dict[str, Any]]:
    cli = {"type": "result", "subtype": "success", "is_error": True, "result": text,
           "session_id": "s"}
    return [{"type": "system", "subtype": "init", "session_id": "s"}, cli,
            exit_event(1, "", cli)]


def test_router_classify_zai_1308_with_reset():
    reset = _later()
    found = router.classify_refusal("glm", _cli_error(
        f"API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour. "
        f"Your limit will reset at {reset}]"))
    assert found is not None and found.code == "zai_1308"
    assert found.reset_at == datetime.fromisoformat(reset).astimezone()


def test_router_classify_zai_1310_and_1313():
    quota = router.classify_refusal(
        "glm", _cli_error("API Error: Request rejected (429) · [1310][Weekly Limit Exhausted.]"))
    assert quota is not None and quota.code == "zai_1310" and quota.reset_at is None
    throttle = router.classify_refusal(
        "glm", _cli_error("API Error: Request rejected (429) · [1313][Fair Usage]"))
    assert throttle is not None and throttle.code == "zai_1313_exhausted"


@pytest.mark.parametrize("text", [
    'API Error: 402 {"error":{"message":"Insufficient Balance","type":"unknown_error"}}',
    "Payment required: Insufficient Balance",
])
def test_router_classify_deepseek_balance(text):
    found = router.classify_refusal("deepseek", _cli_error(text))
    assert found is not None and found.code == "deepseek_balance" and found.reset_at is None


def test_router_classify_codex_usage_limit_fixture_with_date():
    events = [json.loads(line) for line in (FIXTURES / "usage_limit.jsonl").read_text().splitlines()]
    found = router.classify_refusal("codex", events)
    assert found is not None and found.code == "codex_usage_limit"
    assert found.reset_at == datetime(2026, 9, 20, 13, 29).astimezone()


def test_router_codex_reset_clock_time_today_or_tomorrow():
    now = datetime(2026, 9, 18, 12, 0).astimezone()
    text = "You've hit your usage limit. Upgrade or try again at 1:01 PM."
    assert router.codex_reset(text, now) == now.replace(hour=13, minute=1)
    later = now.replace(hour=14)
    assert router.codex_reset(text, later) == (later + timedelta(days=1)).replace(
        hour=13, minute=1)
    assert router.codex_reset("try again at Sep 18th, 2026 3:22 PM", now) == datetime(
        2026, 9, 18, 15, 22).astimezone()


def test_router_classify_not_a_refusal():
    assert router.classify_refusal("glm", _cli_error("API Error: 429 Too Many Requests")) is None
    assert router.classify_refusal("glm", _result("I saw [1308] and Insufficient Balance")) is None
    context = [json.loads(line) for line in
               (FIXTURES / "context_full.jsonl").read_text().splitlines()]
    assert router.classify_refusal("codex", context) is None
    assert router.classify_refusal("glm", "") is None


def test_router_did_work():
    assert router.did_work(_cli_error("x")) is False
    synthetic = {"type": "assistant", "message": {
        "model": "<synthetic>", "content": [{"type": "text", "text": "API Error: 429"}],
        "usage": {"input_tokens": 0, "output_tokens": 0}}}
    assert router.did_work([synthetic]) is False
    assert router.did_work([_tool_use()]) is True
    spoke = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}],
                                              "usage": {"output_tokens": 3}}}
    assert router.did_work([spoke]) is True


def test_router_close_until():
    now = datetime(2026, 9, 18, 12, 0).astimezone()
    kw = {"balance_close_hours": 6, "throttle_close_minutes": 15, "now": now}
    reset = now + timedelta(hours=3)
    assert router.close_until(router.Refusal("zai_1308", "m", reset), **kw) == reset
    assert router.close_until(router.Refusal("zai_1308", "m", now - timedelta(hours=1)),
                              **kw) is None
    assert router.close_until(router.Refusal("zai_1310", "m"), **kw) is None
    assert router.close_until(router.Refusal("deepseek_balance", "m"), **kw) == now + timedelta(
        hours=6)
    assert router.close_until(router.Refusal("zai_1313_exhausted", "m"), **kw) == (
        now + timedelta(minutes=15))
    assert router.close_until(router.Refusal("health_failed", "m"), **kw) is None


# --- delegate() walks the chain ----------------------------------------------------


def _tool_use() -> dict[str, Any]:
    return {"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": "ls"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}}


class FakeClaude:
    """One `claude -p` whose only I/O is a POST to its lane's endpoint.

    200 {"mode": "ok"}: a successful run. 200 {"mode": "work_then_error"}: a
    tool call, then the API error in "error". Any other status: the CLI's
    report of that API error, before any work.
    """

    def __init__(self, argv, env):
        self.argv = argv
        self.env = env

    def events(self) -> Iterator[dict[str, Any]]:
        url = f"{self.env['ANTHROPIC_BASE_URL']}/v1/messages"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, data=b"{}", method="POST")
        try:
            with opener.open(request, timeout=5) as response:
                status, body = response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read() or b"{}")
        if status == 200 and body.get("mode") == "work_then_error":
            cli = {"type": "result", "is_error": True, "session_id": "s",
                   "result": f"API Error: Request rejected (429) · {body['error']}"}
            yield from [{"type": "system", "subtype": "init", "session_id": "s"},
                        _tool_use(), cli, exit_event(1, "", cli)]
            return
        if status == 200:
            yield from _result("done", session_id=f"sess-{self.env['ANTHROPIC_BASE_URL'][-4:]}")
            return
        yield from _cli_error(f"API Error: Request rejected ({status}) · {body.get('error')}")

    def kill(self) -> None:
        pass


@pytest.fixture
def lanes_on_mock(tmp_path: Path, monkeypatch, mock_endpoint):
    """Every claude lane pointed at the mock; every route answers healthy/ok."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    monkeypatch.setenv("SAM_OMLX_API_KEY", "omlx-test")
    env = {f"SAM_{n.upper()}_BASE_URL": mock_endpoint.url(n)
           for n in ("deepseek", "glm", "bppc", "omlx")}
    lanes = load_lanes(env)
    lanes["omlx"] = replace(lanes["omlx"], health_url=f"{mock_endpoint.url('omlx')}/api/status")
    for name in ("deepseek", "glm", "bppc", "omlx"):
        mock_endpoint.routes[f"/{name}/v1/messages"] = (200, {"mode": "ok"})
    mock_endpoint.routes["/bppc/health"] = (200, {"status": "ok"})
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 1})
    spawned: list[FakeClaude] = []

    def spawn(argv, env, cwd):
        spawned.append(FakeClaude(argv, env))
        return spawned[-1]

    monkeypatch.setattr("subagent.runs._spawn_claude", spawn)
    mock_endpoint.lanes = lanes  # type: ignore[attr-defined]
    mock_endpoint.spawned = spawned  # type: ignore[attr-defined]
    return mock_endpoint


def _registry(tmp_path: Path, ep, **overrides) -> Registry:
    settings = make_settings(
        tmp_path, lanes=ep.lanes, rate_limit_retries=1, rate_limit_backoff=0.001,
        throttle_backoff=0.001, **overrides)
    return Registry(settings, start_reaper=False)


def _delegate(reg: Registry, tmp_path: Path, lane: str = "glm", fallback: str = "full"):
    agent = reg.create_agent("t", tmp_path, lane=lane, fallback=fallback)
    return agent, _wait(agent.delegate("do it", "true"))


def _outcomes(run) -> list[tuple[str, str, str | None]]:
    return [(h["lane"], h["outcome"], h["code"]) for h in run.hops]


def _msgs(ep, lane: str) -> int:
    return ep.hits[f"/{lane}/v1/messages"]


REFUSALS = {
    "zai_1308": (429, lambda: f"[1308][Usage limit reached for 5 hour. "
                              f"Your limit will reset at {_later()}]"),
    "zai_1310": (429, lambda: "[1310][Weekly/Monthly Limit Exhausted.]"),
    "zai_1313_exhausted": (429, lambda: "[1313][Fair Usage]"),
}
# Refusals of other providers. Arriving on glm they are just errors (M1); the
# codex driver's own refusal is covered by the mixed-driver tests below.
FOREIGN = {
    "deepseek_balance": (402, lambda: "Insufficient Balance"),
    "codex_usage_limit": (429, lambda: "You've hit your usage limit. Visit "
                                       "https://chatgpt.com/codex/settings/usage to purchase "
                                       "more credits or try again at 1:01 PM."),
}


@pytest.mark.parametrize("code", list(FOREIGN))
def test_router_foreign_refusal_text_fails_on_its_lane(tmp_path, lanes_on_mock, code):
    ep = lanes_on_mock
    status, text = FOREIGN[code]
    ep.routes["/glm/v1/messages"] = (status, {"error": text()})
    reg = _registry(tmp_path, ep)
    try:
        agent, run = _delegate(reg, tmp_path, "glm")
        assert run.state == "failed"
        assert _outcomes(run) == [("glm", "ran", None)]
        assert reg.lane_state.closed("glm") is None
    finally:
        reg.shutdown()


def test_router_deepseek_balance_reroutes_from_deepseek(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/deepseek/v1/messages"] = (402, {"error": "Insufficient Balance"})
    reg = _registry(tmp_path, ep)
    try:
        agent, run = _delegate(reg, tmp_path, "deepseek")
        assert run.state == COMPLETED
        assert _outcomes(run)[:3] == [
            ("deepseek", "refused", "deepseek_balance"),
            ("codex", "unavailable", None),
            ("glm", "ran", None),
        ]
        assert reg.lane_state.closed("deepseek") is not None
    finally:
        reg.shutdown()


@pytest.mark.parametrize("code", list(REFUSALS))
def test_router_refusal_reroutes_to_next_lane(tmp_path, lanes_on_mock, code):
    ep = lanes_on_mock
    status, text = REFUSALS[code]
    ep.routes["/glm/v1/messages"] = (status, {"error": text()})
    reg = _registry(tmp_path, ep)
    try:
        agent, run = _delegate(reg, tmp_path, "glm")
        assert run.state == COMPLETED
        assert _outcomes(run)[:3] == [
            ("glm", "refused", code),
            ("codex", "unavailable", None),
            ("deepseek", "ran", None),
        ]
        assert run.lane == agent.lane.name == "deepseek"
        assert run.provider == "deepseek"
        assert _msgs(ep, "deepseek") == 1
        assert ep.spawned[-1].env["ANTHROPIC_BASE_URL"] == ep.url("deepseek")
        # A spent throttle was retried on its lane before it rerouted.
        assert _msgs(ep, "glm") == (2 if code == "zai_1313_exhausted" else 1)
    finally:
        reg.shutdown()


def test_router_health_failed_reroutes(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/bppc/health"] = (503, {"status": "loading"})
    reg = _registry(tmp_path, ep)
    try:
        _, run = _delegate(reg, tmp_path, "bppc", "local")
        assert run.state == COMPLETED
        assert _outcomes(run) == [("bppc", "health_failed", "health_failed"),
                                  ("omlx", "ran", None)]
        assert _msgs(ep, "bppc") == 0 and _msgs(ep, "omlx") == 1
        # A failed health gate is checked every time, never remembered.
        assert LaneState(reg.settings.session_root).closed("bppc") is None
    finally:
        reg.shutdown()


def test_router_mid_run_failure_does_not_reroute(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (200, {"mode": "work_then_error",
                                           "error": "[1308][Usage limit reached]"})
    reg = _registry(tmp_path, ep)
    try:
        _, run = _delegate(reg, tmp_path, "glm")
        assert run.state == FAILED
        assert _outcomes(run) == [("glm", "ran", None)]
        assert run.usage.steps == 1
        assert "1308" in run.error
        assert _msgs(ep, "deepseek") == 0
        assert LaneState(reg.settings.session_root).closed("glm") is None
    finally:
        reg.shutdown()


def test_router_lane_memory_second_delegate_skips_closed_lane(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": f"[1308][Usage limit reached. "
                                                    f"Your limit will reset at {_later()}]"})
    reg = _registry(tmp_path, ep)
    try:
        _, first = _delegate(reg, tmp_path, "glm")
        assert first.state == COMPLETED and _msgs(ep, "glm") == 1
        state = json.loads((reg.settings.session_root / "lane_state.json").read_text())
        assert state["glm"]["code"] == "zai_1308"
        assert set(state["glm"]) == {"closed_until", "code", "message", "set_at"}

        _, second = _delegate(reg, tmp_path, "glm")
        assert second.state == COMPLETED
        assert _msgs(ep, "glm") == 1  # zero calls to the closed lane
        assert _outcomes(second)[0] == ("glm", "skipped_closed", "zai_1308")
        assert second.hops[0]["closed_until"] is not None
        assert second.lane == "deepseek"
    finally:
        reg.shutdown()


def test_router_balance_closes_lane_for_configured_hours(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/deepseek/v1/messages"] = (402, {"error": "Insufficient Balance"})
    reg = _registry(tmp_path, ep, balance_close_hours=2.0)
    try:
        _, run = _delegate(reg, tmp_path, "deepseek")
        assert run.lane == "glm"
        # Not retried: an empty balance does not refill in seconds.
        assert _msgs(ep, "deepseek") == 1
        entry = LaneState(reg.settings.session_root).closed("deepseek")
        assert entry is not None and entry["code"] == "deepseek_balance"
        left = entry["closed_until"] - datetime.now().astimezone()
        assert timedelta(hours=1.9) < left <= timedelta(hours=2)
    finally:
        reg.shutdown()


def test_router_expired_closure_is_ignored(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    reg = _registry(tmp_path, ep)
    try:
        past = datetime.now().astimezone() - timedelta(minutes=1)
        LaneState(reg.settings.session_root).close("glm", past, "zai_1308", "old")
        _, run = _delegate(reg, tmp_path, "glm")
        assert _outcomes(run) == [("glm", "ran", None)]
        assert _msgs(ep, "glm") == 1
    finally:
        reg.shutdown()


def test_router_malformed_lane_state_is_empty(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    reg = _registry(tmp_path, ep)
    try:
        (reg.settings.session_root / "lane_state.json").write_text("{not json")
        _, run = _delegate(reg, tmp_path, "glm")
        assert run.state == COMPLETED and _outcomes(run) == [("glm", "ran", None)]
    finally:
        reg.shutdown()


def test_router_all_lanes_refused_fails_with_hops(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": "[1310][Weekly/Monthly Limit Exhausted.]"})
    ep.routes["/deepseek/v1/messages"] = (402, {"error": "Insufficient Balance"})
    ep.routes["/bppc/health"] = (503, {})
    ep.routes["/omlx/api/status"] = (200, {"status": "loading"})
    reg = _registry(tmp_path, ep)
    try:
        _, run = _delegate(reg, tmp_path, "glm")
        assert run.state == FAILED and run.finish_reason == "no_lane"
        assert _outcomes(run) == [
            ("glm", "refused", "zai_1310"),
            ("codex", "unavailable", None),
            ("deepseek", "refused", "deepseek_balance"),
            ("bppc", "health_failed", "health_failed"),
            ("omlx", "health_failed", "health_failed"),
        ]
        assert "no lane took the run" in run.error
        assert "Insufficient Balance" in (run.error_detail or "")
        assert run.detail()["hops"] == run.hops
    finally:
        reg.shutdown()


def test_router_fallback_none_stays_on_primary(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": "[1310][Weekly Limit Exhausted.]"})
    reg = _registry(tmp_path, ep)
    try:
        _, run = _delegate(reg, tmp_path, "glm", "none")
        assert run.state == FAILED
        assert _outcomes(run) == [("glm", "refused", "zai_1310")]
        assert _msgs(ep, "deepseek") == 0
    finally:
        reg.shutdown()


def test_router_plain_failure_is_not_rerouted(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (401, {"error": "invalid api key"})
    reg = _registry(tmp_path, ep)
    try:
        _, run = _delegate(reg, tmp_path, "glm")
        assert run.state == FAILED and _outcomes(run) == [("glm", "ran", None)]
        assert _msgs(ep, "deepseek") == 0
    finally:
        reg.shutdown()


def test_router_continue_stays_on_the_lane_that_ran(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": "[1310][Weekly Limit Exhausted.]"})
    reg = _registry(tmp_path, ep)
    try:
        agent, _ = _delegate(reg, tmp_path, "glm")
        ep.routes["/deepseek/v1/messages"] = (402, {"error": "Insufficient Balance"})
        run = _wait(agent.follow_up("more"))
        assert run.state == FAILED and run.hops == []
        assert ep.spawned[-1].env["ANTHROPIC_BASE_URL"] == ep.url("deepseek")
        assert _msgs(ep, "glm") == 1 and _msgs(ep, "bppc") == 0
    finally:
        reg.shutdown()


def test_router_codex_without_cli_is_unavailable(tmp_path, lanes_on_mock):
    # conftest points SAM_CODEX_BIN at a path that does not exist.
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": "[1310][Weekly Limit Exhausted.]"})
    reg = _registry(tmp_path, ep)
    try:
        _, run = _delegate(reg, tmp_path, "glm")
        codex = run.hops[1]
        assert (codex["lane"], codex["outcome"]) == ("codex", "unavailable")
        assert (codex["driver"], codex["guard"]) == ("codex", "sandbox")
        assert "not on PATH" in codex["message"]
    finally:
        reg.shutdown()


def test_router_trace_writes_one_hop_record_per_lane(tmp_path, lanes_on_mock):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": f"[1308][Usage limit reached. "
                                                    f"Your limit will reset at {_later()}]"})
    trace = tmp_path / "trace.jsonl"
    reg = _registry(tmp_path, ep, trace=str(trace))
    try:
        _, run = _delegate(reg, tmp_path, "glm")
    finally:
        reg.shutdown()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    hops = [r for r in records if r["kind"] == "hop"]
    assert [(h["hop"], h["lane"], h["outcome"]) for h in hops] == [
        (0, "glm", "refused"), (1, "codex", "unavailable"), (2, "deepseek", "ran")]
    fields = {"schema", "ts", "kind", "run_id", "agent_id", "hop", "lane", "provider",
              "model", "outcome", "code", "reset_at", "closed_until"}
    assert all(fields <= set(h) for h in hops)
    assert all(h["schema"] == SCHEMA == 3 and h["run_id"] == run.run_id for h in hops)
    assert hops[0]["code"] == "zai_1308" and hops[0]["reset_at"] and hops[0]["closed_until"]
    assert hops[2]["model"] == "deepseek-v4-pro"
    (record,) = [r for r in records if r["kind"] == "run"]
    assert (record["lane"], record["provider"]) == ("deepseek", "deepseek")
    assert record["model"] == "deepseek-v4-pro"


def test_router_src_never_starts_a_local_model():
    # "docker" as a command word; guard.py names ~/.docker/config.json as a
    # secret path, which starts nothing.
    found = subprocess.run(
        ["grep", "-rnE", "--include=*.py", r"start-llm|llama-server|[\"']docker[\"']",
         str(SRC)],
        capture_output=True, text=True,
    )
    assert found.returncode == 1 and found.stdout == ""


# --- mixed drivers: codex and claude hops in one delegation ------------------------

CODEX_RESET = datetime(2026, 9, 20, 13, 29).astimezone()
CODEX_THREAD = "01a0a0de-91f8-7441-a178-a154caba9282"  # success.jsonl


@pytest.fixture
def frozen_now(monkeypatch):
    """Local time fixed before the fixture's 2026-09-20 13:29 reset, so the
    closure is live whatever day the suite runs."""
    from subagent import lane_state

    now = datetime(2026, 9, 18, 12, 0).astimezone()
    monkeypatch.setattr(router, "_local_now", lambda: now)
    monkeypatch.setattr(lane_state, "_now", lambda: now)
    return now


def _mixed(tmp_path: Path, ep, names: tuple[str, ...], **overrides) -> Registry:
    """A registry whose only lanes are `names`, so the chain is exactly them."""
    settings = make_settings(
        tmp_path, lanes={n: ep.lanes[n] for n in names}, rate_limit_retries=1,
        rate_limit_backoff=0.001, throttle_backoff=0.001, **overrides)
    return Registry(settings, start_reaper=False)


def test_router_codex_usage_limit_then_glm_and_continue_stays(
    tmp_path, lanes_on_mock, fake_codex, frozen_now
):
    ep = lanes_on_mock
    fake_codex.use("usage_limit.jsonl")
    trace = tmp_path / "trace.jsonl"
    reg = _mixed(tmp_path, ep, ("codex", "glm"), trace=str(trace))
    try:
        agent, run = _delegate(reg, tmp_path, "codex")
        assert run.state == COMPLETED, run.error
        assert _outcomes(run) == [("codex", "refused", "codex_usage_limit"),
                                  ("glm", "ran", None)]
        codex_hop, glm_hop = run.hops
        assert codex_hop["reset_at"] == CODEX_RESET.isoformat()
        assert codex_hop["closed_until"] == CODEX_RESET.isoformat()
        assert (codex_hop["driver"], codex_hop["guard"]) == ("codex", "sandbox")
        assert (glm_hop["driver"], glm_hop["guard"]) == ("claude", "hook")
        entry = LaneState(reg.settings.session_root).closed("codex")
        assert entry is not None and entry["code"] == "codex_usage_limit"
        assert entry["closed_until"] == CODEX_RESET
        assert len(fake_codex.calls()) == 1
        assert _msgs(ep, "glm") == 1
        assert (run.lane, run.driver, run.guard) == ("glm", "claude", "hook")
        assert agent.lane.name == "glm" and agent.driver.name == "claude"
        glm_session = agent.session_id
        assert glm_session == f"sess-{ep.url('glm')[-4:]}"

        more = _wait(agent.follow_up("more"))
        assert more.state == COMPLETED and more.hops == []
        assert (more.lane, more.driver) == ("glm", "claude")
        assert len(fake_codex.calls()) == 1
        assert _msgs(ep, "glm") == 2
        argv = ep.spawned[-1].argv
        assert argv[argv.index("--resume") + 1] == glm_session
    finally:
        reg.shutdown()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    hops = [(r["lane"], r["driver"], r["guard"]) for r in records if r["kind"] == "hop"]
    assert hops == [("codex", "codex", "sandbox"), ("glm", "claude", "hook")]
    runs_ = [(r["lane"], r["driver"], r["guard"]) for r in records if r["kind"] == "run"]
    assert runs_ == [("glm", "claude", "hook")] * 2


def test_router_glm_refused_then_codex_runs_and_continue_resumes_thread(
    tmp_path, lanes_on_mock, fake_codex, frozen_now
):
    ep = lanes_on_mock
    ep.routes["/glm/v1/messages"] = (429, {"error": "[1313][Fair Usage]"})
    trace = tmp_path / "trace.jsonl"
    reg = _mixed(tmp_path, ep, ("glm", "codex"), trace=str(trace), supervisor="auto")
    try:
        agent, run = _delegate(reg, tmp_path, "glm")
        assert run.state == COMPLETED, run.error
        assert _outcomes(run) == [("glm", "refused", "zai_1313_exhausted"),
                                  ("codex", "ran", None)]
        assert _msgs(ep, "glm") == 2  # one throttle retry on the lane, then reroute
        assert (run.lane, run.driver, run.guard) == ("codex", "codex", "sandbox+hook")
        assert run.session_id == agent.session_id == CODEX_THREAD
        first = fake_codex.calls()[0]["argv"]
        assert "resume" not in first and "-m" not in first

        more = _wait(agent.follow_up("keep going"), timeout=10)
        assert more.state == COMPLETED and more.hops == []
        second = fake_codex.calls()[1]["argv"]
        assert second[second.index("resume") + 1] == CODEX_THREAD
        assert _msgs(ep, "glm") == 2
    finally:
        reg.shutdown()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    hops = [(r["lane"], r["driver"], r["guard"]) for r in records if r["kind"] == "hop"]
    assert hops == [("glm", "claude", "hook"), ("codex", "codex", "sandbox+hook")]
    runs_ = [r for r in records if r["kind"] == "run"]
    # The fake codex runs a command without calling the hook, so the run
    # record says the hook stayed silent (M4); the hop keeps the configured guard.
    assert [(r["lane"], r["driver"], r["guard"]) for r in runs_] == [
        ("codex", "codex", "sandbox (hook silent)")] * 2
    assert runs_[0]["session_id"] == CODEX_THREAD


def test_router_codex_closed_second_delegate_spawns_no_codex(
    tmp_path, lanes_on_mock, fake_codex, frozen_now
):
    ep = lanes_on_mock
    fake_codex.use("usage_limit.jsonl")
    reg = _mixed(tmp_path, ep, ("codex", "glm"))
    try:
        _, first = _delegate(reg, tmp_path, "codex")
        assert first.lane == "glm" and len(fake_codex.calls()) == 1

        _, second = _delegate(reg, tmp_path, "codex")
        assert second.state == COMPLETED
        assert _outcomes(second) == [("codex", "skipped_closed", "codex_usage_limit"),
                                     ("glm", "ran", None)]
        assert second.hops[0]["closed_until"] == CODEX_RESET.isoformat()
        assert len(fake_codex.calls()) == 1  # zero codex spawns while closed
    finally:
        reg.shutdown()


def test_router_codex_usage_limit_after_work_fails_without_reroute(
    tmp_path, lanes_on_mock, fake_codex, frozen_now, monkeypatch
):
    ep = lanes_on_mock
    lines = (FIXTURES / "context_full.jsonl").read_text().splitlines()[:-2]
    lines.append(json.dumps({"type": "turn.failed", "error": {"message": (
        "You've hit your usage limit. Try again at Sep 20th, 2026 1:29 PM.")}}))
    fixture = tmp_path / "limit_after_work.jsonl"
    fixture.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("FAKE_CODEX_FIXTURE", str(fixture))
    reg = _mixed(tmp_path, ep, ("codex", "glm"))
    try:
        _, run = _delegate(reg, tmp_path, "codex")
        assert run.state == FAILED
        assert run.finish_reason == "usage_limit_after_work"
        assert _outcomes(run) == [("codex", "ran", None)]
        assert _msgs(ep, "glm") == 0
        assert LaneState(reg.settings.session_root).closed("codex") is None
    finally:
        reg.shutdown()
