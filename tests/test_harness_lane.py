"""Recorded epi streams through the registered MCP tools and real Registry.

The CLI is fake; provider, routing, run lifecycle and MCP validation are real.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from subagent import cli
from subagent.guard.supervisor import Supervisor
from subagent.providers.epi import EPI_PROVIDER
from subagent.runs import Registry

from .conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "harness"
FAKE = Path(__file__).parent / "fakes" / "fake_epi"
CANARY = "test-token-canary"
CONTROLS = ["FAKE_EPI_RECORD", "FAKE_EPI_SCENARIO", "FAKE_EPI_SCOPE",
            "FAKE_EPI_HUMAN_WAIT", "FAKE_EPI_PROGRESS_GATE", "FAKE_EPI_RECEIPT_GATE",
            "FAKE_EPI_RECOVERY_FAIL", "FAKE_EPI_EXIT_AFTER_ADMISSION", "FAKE_EPI_NO_SCOPE"]


def recording(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


class Lane:
    def __init__(self, server, record):
        self.server = server
        self.record = record
        self.progress = []
        # Only the client's progress sink is substituted. Tool registration,
        # schema validation, result conversion and supervisor binding run normally.
        self.context = SimpleNamespace(session=object(),
                                       request_context=SimpleNamespace(meta=None),
                                       report_progress=self.report_progress)

    async def report_progress(self, progress, total, message):
        self.progress.append((progress, total, message))

    def call(self, tool, **arguments):
        result = asyncio.run(self.server.app.call_tool(tool, arguments, self.context))
        assert not result.is_error
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    def delegate(self, **arguments):
        native = recording("delegate_change_completed.jsonl")[-1]
        params = {"task": native["task"], "verification": native["verification"]["command"],
                  "provider": "epi"}
        params.update(arguments)
        return self.call("delegate", **params)

    def calls(self, tool=None):
        records = ([json.loads(line) for line in self.record.read_text().splitlines()]
                   if self.record.exists() else [])
        return [r for r in records if tool is None or r["argv"][1] == tool]

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, "fake CLI did not reach the expected gate"
            time.sleep(0.01)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    token = tmp_path / "caller.token"
    token.write_text(CANARY + "\n")
    token.chmod(0o600)
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".pi" / "board.db").touch()
    record = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_EPI_RECORD", str(record))
    monkeypatch.setenv("EPI_CALLER_TOKEN", CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    settings = make_settings(tmp_path, providers={"epi": {
        "driver": "epi", "binary": str(FAKE), "token_file": str(token),
        "project_root": str(tmp_path), "experimental": True, "local": True,
        "model": "outer-default-must-not-be-forwarded", "run_timeout": 30,
    }}, child_env_passthrough=CONTROLS)
    registry = Registry(settings, start_reaper=False)
    monkeypatch.setenv("SUBAGENT_CONFIG", str(tmp_path / "subagent.toml"))
    server = importlib.import_module("subagent.mcp_server")
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "supervisor", Supervisor(settings, registry))
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.02)
    yield Lane(server, record)
    registry.shutdown()


def test_lane_delegate_progress_and_completed(lane, tmp_path, monkeypatch, caplog):
    gate = tmp_path / "finish"
    monkeypatch.setenv("FAKE_EPI_PROGRESS_GATE", str(gate))
    first = lane.delegate(wait_seconds=0)
    lane.wait_until(lambda: Path(str(gate) + ".ready").exists())
    for _ in range(2):
        working = lane.call("await", run_id=first["run_id"], wait_seconds=0.06)
        assert working["state"] == "working"
        assert working["run_id"] == first["run_id"]
    assert lane.progress and all("running:" in p[2] for p in lane.progress)
    gate.touch()
    result = lane.call("await", run_id=first["run_id"], wait_seconds=5)
    native = recording("delegate_change_completed.jsonl")[-1]
    assert result["state"] == "completed" and result["phase"] == "done"
    for key in ("verification", "tickets", "usage", "usage_unreported", "cash_usd",
                "credits", "credits_unit", "turn_usage"):
        assert result[key] == native[key]
    assert result["lane"] == result["provider"] == "epi"
    assert result["run_id"] != native["run_id"]
    assert result["extra"]["epi"]["run_id"] == native["run_id"]
    assert lane.call("await", run_id=first["run_id"], wait_seconds=0) == result
    run = lane.server.registry.find_run(first["run_id"])
    assert "provider: queued" in run.transcript
    assert run.verification_result is None  # no second, local check
    assert len(lane.calls("delegate")) == 1
    assert CANARY not in json.dumps(lane.calls()) + json.dumps(result) + caplog.text


def test_lane_await_returns_at_human_wait_then_resumes(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    result = lane.delegate(wait_seconds=5)
    native = recording("delegate_awaiting_approval.jsonl")[-1]
    run = lane.server.registry.find_run(result["run_id"])
    for _ in range(2):
        started = time.monotonic()
        waiting = lane.call("await", run_id=run.run_id, wait_seconds=5)
        assert time.monotonic() - started < 1, "human wait consumed the full timeout"
        for key in ("state", "phase", "needs_human", "human_action", "plan_id"):
            assert waiting[key] == native[key]
        assert "human_action outside MCP" in waiting["hint"]
        assert not run.done.is_set()
    # Simulate a human acting outside MCP, never an approval tool call.
    agent = lane.server.registry.agent(result["agent_id"])
    agent.session().data["env"].pop("FAKE_EPI_HUMAN_WAIT")
    lane.wait_until(run.done.is_set)
    resumed = lane.call("await", run_id=run.run_id, wait_seconds=5)
    assert resumed["state"] == "completed" and not run.needs_human
    assert resumed["verification"] == recording("await_after_approval.json")[0]["verification"]
    assert {c["argv"][1] for c in lane.calls()} == {"delegate", "await"}


def test_lane_completed_unverified(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "change_unverified")
    native = recording("delegate_change_unverified.jsonl")[-1]
    result = lane.delegate(verification=native["verification"]["command"], wait_seconds=5)
    assert result["state"] == "completed_unverified"
    assert result["verification"] == native["verification"]
    assert result["verification"]["exit_code"] == 7
    assert result["verification"]["output_tail"] == "failed"


def test_lane_goal_uat(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "goal_uat")
    native = recording("delegate_goal_uat.jsonl")[-1]
    command = native["uat"][0]["command"]
    block = f'Execute plan "marker" (plan.md). Goal rows:\n1 marker: `{command}`'
    result = lane.delegate(goal=block, wait_seconds=5)
    assert result["state"] == "completed"
    assert result["uat"] == native["uat"]
    assert result["verification"] == native["verification"]
    assert result["uat"][0]["output_tail"] == "goal-final"
    assert f"--goal={block}" in lane.calls("delegate")[0]["argv"]


def test_lane_question(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    result = lane.call("delegate", task="Explain the fixture marker", verification="true",
                       provider="epi", wait_seconds=5)
    native = recording("delegate_question_completed.jsonl")[-1]
    assert result["state"] == "completed"
    assert result["verification"] == native["verification"]
    assert "decision_linked" in result["verification"]["reason"]
    assert result["result"] == native["result"]


def test_lane_continue_same_agent(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    first = lane.delegate(task="Explain the fixture marker", verification="true", wait_seconds=5)
    second = lane.call("continue", agent_id=first["agent_id"], message="Explain the marker once more",
                       verification="true", wait_seconds=5)
    native = recording("continue_completed.jsonl")[-1]
    assert second["state"] == "completed" and second["agent_id"] == first["agent_id"]
    assert second["run_id"] != first["run_id"]
    assert second["extra"]["epi"]["agent_id"] == first["extra"]["epi"]["agent_id"]
    assert second["extra"]["epi"]["run_id"] == native["run_id"]
    assert second["verification"] == native["verification"]
    delegate_args = lane.calls("delegate")[0]["argv"]
    args = lane.calls("continue")[0]["argv"]
    assert args[args.index("--agent-id") + 1] == native["agent_id"]
    assert args[args.index("--request-id") + 1] != delegate_args[delegate_args.index("--request-id") + 1]
    assert second["lane"] == first["lane"] == "epi" and not second.get("hops")


def test_lane_cancel_reaches_epi(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    first = lane.delegate(wait_seconds=5)
    result = lane.call("cancel", agent_id=first["agent_id"])
    assert result["closed"] and result["interrupted_running_work"]
    run = lane.server.registry.find_run(first["run_id"])
    assert run.done.is_set() and run.state == "cancelled" and not run.needs_human
    assert run.detail()["extra"]["cancellation_confirmed"] is True
    args = lane.calls("cancel")[0]["argv"]
    assert args[args.index("--agent-id") + 1] == first["extra"]["epi"]["agent_id"]


def test_lane_cancel_before_receipt_recovers_agent(lane, tmp_path, monkeypatch):
    gate = tmp_path / "receipt"
    monkeypatch.setenv("FAKE_EPI_RECEIPT_GATE", str(gate))
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(lane.delegate, name="lost receipt",
                              instructions="Keep the approved check", model="scripted-fake",
                              fallback="none", wait_seconds=0)
        lane.wait_until(lambda: Path(str(gate) + ".ready").exists())
        agent = next(iter(lane.server.registry._agents.values()))
        assert agent.session().session_id is None
        result = lane.call("cancel", agent_id=agent.agent_id)
        assert result["closed"]
        first = pending.result(timeout=5)
    run = lane.server.registry.find_run(first["run_id"])
    assert run.done.is_set() and run.state == "cancelled"
    assert run.detail()["extra"]["cancellation_confirmed"] is True
    assert len(lane.calls("delegate")) == 1
    saved = json.loads(Path(str(lane.record) + ".request.json").read_text())
    assert saved["admissions"] == 1
    args = lane.calls("cancel")[0]["argv"]
    assert args[args.index("--request-id") + 1] == run.request_id
    assert run.detail()["extra"]["epi"]["agent_id"] == saved["receipt"]["agent_id"]


def test_lane_cancel_before_receipt_lookup_failure_is_unconfirmed(lane, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_RECEIPT_GATE", str(tmp_path / "lost"))
    monkeypatch.setenv("FAKE_EPI_RECOVERY_FAIL", "1")
    cfg = lane.server.registry.settings.provider()
    lane.server.registry.settings = dataclasses.replace(
        lane.server.registry.settings, providers={"epi": dataclasses.replace(
            cfg, extra={**cfg.extra, "cancel_retry_seconds": 0.1},
        )},
    )
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(lane.delegate, wait_seconds=0)
        lane.wait_until(lambda: Path(str(tmp_path / "lost") + ".ready").exists())
        agent = next(iter(lane.server.registry._agents.values()))
        run = agent.runs()[0]
        result = lane.call("cancel", agent_id=agent.agent_id)
        assert not result["closed"]
        assert not run.done.is_set() and run.state == "working"
        assert run.detail()["extra"]["cancellation_confirmed"] is False
        assert len(lane.calls("delegate")) == 1 and len(lane.calls("cancel")) >= 2
        assert CANARY not in json.dumps(run.detail())
        agent.session().data["env"].pop("FAKE_EPI_RECOVERY_FAIL", None)
        agent.close()
        assert pending.result(timeout=5)["run_id"] == run.run_id
        assert lane.call("await", run_id=run.run_id, wait_seconds=0)["state"] == "cancelled"


@pytest.mark.parametrize("scenario", ["refused_then_available", "socket_down"])
def test_lane_cancel_after_known_refusal_never_submits_work(lane, monkeypatch, scenario):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", scenario)
    first = lane.delegate(fallback="none", wait_seconds=5)
    assert first["state"] == "failed" and first["finish_reason"] == "refused"
    lane.call("cancel", agent_id=first["agent_id"])
    assert len(lane.calls("delegate")) == 1
    assert not lane.calls("cancel")  # conclusively no native agent exists


def test_lane_transcript_after_reap(lane, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    first = lane.delegate(task="Explain the fixture marker", verification="true", wait_seconds=5)
    lane.call("continue", agent_id=first["agent_id"], message="Explain again", wait_seconds=5)
    lane.call("cancel", agent_id=first["agent_id"])
    lane.server.registry.reap_once()
    assert lane.server.registry.find_agent(first["agent_id"]) is None
    result = lane.call("transcript", run_id=first["run_id"], limit=4, raw=True)
    native = recording("transcript.json")[0]
    assert result["run_id"] == first["run_id"] and result["agent_id"] == first["agent_id"]
    assert result["activity"] == native["activity"]
    assert result["raw_response"] == native["raw_response"]
    args = lane.calls("transcript")[0]["argv"]
    assert args[args.index("--run-id") + 1] == first["extra"]["epi"]["run_id"]
    assert args[args.index("--limit") + 1] == "4" and "--raw" in args


def test_lane_list_profile_and_scope(lane, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path))
    value = lane.call("list")
    provider = value["providers"][0]
    assert provider["name"] == "epi"
    assert provider["extra"]["profile"] == "fake"
    assert provider["extra"]["default_project"] == str(tmp_path.resolve())
    assert provider["extra"]["allowed_projects"] == [str(tmp_path.resolve())]
    scope = provider["extra"]["epi"]["providers"][0]["extra"]
    assert scope == {"profile": "fake", "default_project": str(tmp_path.resolve()),
                     "allowed_projects": [str(tmp_path.resolve())]}
    assert {c["argv"][1] for c in lane.calls()} == {"list"}
    assert not any(arg.startswith("--profile") for c in lane.calls() for arg in c["argv"])


def add_backup(lane, tmp_path):
    # The fallback is another actual driver + subprocess, with a successful
    # replay regardless of the primary's fault scenario.
    binary = tmp_path / "backup-epi"
    binary.write_text('#!/usr/bin/env python3\nimport os, sys\n'
                      'os.environ["FAKE_EPI_SCENARIO"] = "change_completed"\n'
                      f'os.execv(sys.executable, [sys.executable, {str(FAKE)!r}, *sys.argv[1:]])\n')
    binary.chmod(0o755)
    settings = lane.server.settings
    backup = dataclasses.replace(settings.provider(), name="backup", binary=str(binary))
    settings = dataclasses.replace(settings, providers={**settings.providers, "backup": backup},
                                   fallback_chain=("backup",))
    lane.server.settings = lane.server.registry.settings = settings


@pytest.mark.parametrize("scenario,falls_back", [
    ("rejected", True), ("socket_down", True), ("failed_no_lane", False),
    ("ambiguous_error", False), ("argument_error", False),
])
def test_lane_fallback_only_on_prework_refusal(lane, tmp_path, monkeypatch, scenario, falls_back):
    add_backup(lane, tmp_path)
    monkeypatch.setenv("FAKE_EPI_SCENARIO", scenario)
    if scenario == "argument_error":
        with pytest.raises(ToolError, match="schema-invalid:.*verification"):
            lane.delegate(fallback="full", wait_seconds=5)
        assert len(lane.calls("delegate")) == 1
        return
    result = lane.delegate(fallback="full", wait_seconds=5)
    assert len(lane.calls("delegate")) == (2 if falls_back else 1)
    assert result["lane"] == ("backup" if falls_back else "epi")
    assert result["state"] == ("completed" if falls_back else "failed")
    assert result["hops"][0]["outcome"] == ("refused" if falls_back else "ran")
    if falls_back:
        assert result["hops"][1]["outcome"] == "ran"


@pytest.mark.parametrize("scope", ["allowed", "other", "missing"])
def test_lane_doctor_requires_allowed_project(lane, tmp_path, monkeypatch, capsys, scope):
    # A noncanonical config path must match the canonical path in caller scope.
    alias = tmp_path / "project-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    cfg = lane.server.settings.provider()
    cfg = dataclasses.replace(cfg, extra={**cfg.extra, "project_root": str(alias)})
    if scope != "missing":
        monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path if scope == "allowed" else tmp_path / "other"))
    else:
        monkeypatch.setenv("FAKE_EPI_NO_SCOPE", "1")
    row = EPI_PROVIDER.doctor(lane.server.settings, cfg)
    assert row["health_ok"] is (scope == "allowed")
    assert row["project_allowed"] is (None if scope == "missing" else scope == "allowed")
    assert row["project_initialized"] and row["profile"] == "fake"
    monkeypatch.setattr(cli, "_prompt_turn", lambda *a: pytest.fail("doctor made a model call"))
    result = cli.main(["doctor", "--json"])
    assert result == (0 if scope == "allowed" else 1)
    assert json.loads(capsys.readouterr().out)["providers"][0]["health_ok"] is (scope == "allowed")
    assert {c["argv"][1] for c in lane.calls()} == {"list"}


def test_lane_fixture_readme_names_source_commit():
    readme = (FIXTURES / "README.md").read_text()
    assert "b9963cb6" in readme
    assert "CARGO_NET_OFFLINE=true cargo test -p cli record_harness_fixtures -- --ignored" in readme
    assert "real confined check runner" in readme
    assert "test-token-canary" not in "".join(p.read_text() for p in FIXTURES.glob("*.json*"))
    extra = recording("list.json")[0]["providers"][0]["extra"]
    assert extra["default_project"] in extra["allowed_projects"]
    assert extra["allowed_projects"] == sorted(extra["allowed_projects"])
