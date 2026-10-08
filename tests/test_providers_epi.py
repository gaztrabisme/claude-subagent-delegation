"""Epi conformance against recorded service streams, with no live service."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib
import json
import tomllib
from pathlib import Path

import pytest

from subagent import cli, providers
from subagent.goal import parse_goal
from subagent.providers.base import ProviderConfig, RunContext, Session
from subagent.providers.epi import EPI_PROVIDER, Translator
from subagent.runs import Registry

from .conftest import make_settings
from .test_provider_hooks import NativeProvider
from .test_runs import _wait

FIXTURES = Path(__file__).parent / "fixtures" / "harness"
FAKE = Path(__file__).parent / "fakes" / "fake_epi"
CANARY = "test-token-canary"
FAKE_ENV = ["FAKE_EPI_RECORD", "FAKE_EPI_SCENARIO", "FAKE_EPI_SCOPE",
            "FAKE_EPI_HUMAN_WAIT", "FAKE_EPI_EXIT_AFTER_ADMISSION", "FAKE_EPI_AWAIT_FAILED"]


def load(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


@pytest.fixture
def epi(tmp_path, monkeypatch):
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
        "model": "outer-config-model-must-not-leak",
        "run_timeout": 15, "idle_timeout": 30,
    }}, child_env_passthrough=FAKE_ENV)
    error, session = EPI_PROVIDER.boot(settings, "outer", settings.provider())
    assert error is None
    registry = Registry(settings, start_reaper=False)
    yield settings, session, registry, record
    registry.shutdown()


def calls(epi):
    record = epi[3]
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def start(epi, tmp_path, *, scenario="change_completed", monkeypatch, **options):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", scenario)
    agent = epi[2].create_agent("outer-label", tmp_path, provider="epi", **options)
    assert agent.wait_ready(5) is None
    run = agent.delegate("Target: marker\nChange: create marker", "caller check must never run locally")
    return agent, run


@pytest.mark.parametrize("bad", ["missing", "directory", "symlink", "0644", "0400"])
def test_boot_refuses_bad_token_file(epi, tmp_path, bad):
    settings, _, _, _ = epi
    token = tmp_path / "bad.token"
    if bad == "directory":
        token.mkdir()
    elif bad == "symlink":
        token.symlink_to(tmp_path / "caller.token")
    elif bad != "missing":
        token.write_text(CANARY)
        token.chmod(int(bad, 8))
    cfg = dataclasses.replace(settings.provider(), extra={"token_file": str(token)})
    error, _ = EPI_PROVIDER.boot(settings, "a", cfg)
    assert error and "token_file" in error
    assert CANARY not in error
    assert calls(epi) == []


def test_delegate_argv_has_no_token_value(epi, tmp_path, monkeypatch, caplog):
    agent, run = start(epi, tmp_path, monkeypatch=monkeypatch,
                       task="original task", instructions="standing guidance")
    _wait(run)
    call = calls(epi)[0]
    args = call["argv"]
    assert args[:3] == ["call", "delegate", "--json"]
    assert "--follow" in args
    assert args[args.index("--token-file") + 1] == str(tmp_path / "caller.token")
    assert args[args.index("--task") + 1] == "original task"
    assert args[args.index("--verification") + 1] == "caller check must never run locally"
    assert args[args.index("--request-id") + 1] == run.request_id
    assert args[args.index("--workspace") + 1] == str(tmp_path)
    assert "--instructions=standing guidance" in args
    assert "--name=outer-label" in args
    assert not any(a.startswith(("--provider", "--lane", "--model", "--fallback", "--socket"))
                   for a in args)
    assert call["cwd"] == str(tmp_path) and call["stdin"] == ""
    assert CANARY not in json.dumps(calls(epi)) + caplog.text + json.dumps(run.detail())
    assert agent.session_id == agent.session().session_id


def test_translate_completed_change(epi, tmp_path, monkeypatch):
    agent, run = start(epi, tmp_path, monkeypatch=monkeypatch)
    _wait(run)
    native = load("delegate_change_completed.jsonl")[-1]
    detail = run.detail()
    assert detail["state"] == "completed" and detail["phase"] == "done"
    assert detail["verification"] == native["verification"]
    assert detail["tickets"] == native["tickets"]
    assert detail["finish_reason"] == native["finish_reason"]
    assert agent.session_id == native["agent_id"]
    assert agent.session().data["run_id"] == native["run_id"]
    assert detail["run_id"] != native["run_id"]
    assert detail["lane"] == detail["provider"] == "epi"
    assert EPI_PROVIDER.capabilities.native_verification


def test_translate_completed_unverified(epi, tmp_path, monkeypatch):
    _, run = start(epi, tmp_path, scenario="change_unverified", monkeypatch=monkeypatch)
    _wait(run)
    native = load("delegate_change_unverified.jsonl")[-1]
    assert run.state == "completed_unverified"
    assert run.detail()["verification"] == native["verification"]
    assert run.verification_result is None


def test_awaiting_approval_surfaces_human_action(epi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    agent, run = start(epi, tmp_path, scenario="awaiting_approval", monkeypatch=monkeypatch)
    assert run.await_ready.wait(5)
    assert not run.done.is_set()
    detail = run.detail()
    native = load("delegate_awaiting_approval.jsonl")[-1]
    for key in ("state", "phase", "needs_human", "human_action", "plan_id"):
        assert detail[key] == native[key]
    # Removing the test-only gate represents external approval. The real
    # driver only polls; it never calls a plan command.
    monkeypatch.delenv("FAKE_EPI_HUMAN_WAIT")
    agent.session().data["env"].pop("FAKE_EPI_HUMAN_WAIT", None)
    _wait(run)
    assert not run.needs_human
    assert run.state == "completed"
    assert all(c["argv"][0] == "call" for c in calls(epi))
    assert any(c["argv"][1] == "await" for c in calls(epi))


def test_question_completed(epi, tmp_path, monkeypatch):
    _, run = start(epi, tmp_path, scenario="question_completed", monkeypatch=monkeypatch)
    _wait(run)
    native = load("delegate_question_completed.jsonl")[-1]
    assert run.state == "completed"
    assert run.detail()["verification"] == native["verification"]
    assert "decision_linked" in native["verification"]["reason"]


def test_goal_uat_carried(epi, tmp_path, monkeypatch):
    block = 'Execute plan "marker" (plan.md). Goal rows:\n1 marker: `test -f marker`'
    goal = parse_goal(block, tmp_path)
    _, run = start(epi, tmp_path, scenario="goal_uat", monkeypatch=monkeypatch, goal=goal)
    _wait(run)
    assert f"--goal={block}" in calls(epi)[0]["argv"]
    assert run.detail()["uat"] == load("delegate_goal_uat.jsonl")[-1]["uat"]
    assert all(row["passed"] for row in run.detail()["uat"])


def fallback(epi, tmp_path, monkeypatch, scenario):
    settings, _, registry, _ = epi
    backup = NativeProvider()
    original = providers.for_driver
    monkeypatch.setattr(providers, "for_driver",
                        lambda name: EPI_PROVIDER if name == "epi" else backup)
    cfg = ProviderConfig(name="backup", driver="codex", vendor="backup")
    registry.settings = dataclasses.replace(settings, providers={**settings.providers, "backup": cfg},
                                            fallback_chain=("backup",))
    agent, run = start(epi, tmp_path, scenario=scenario, monkeypatch=monkeypatch, fallback="full")
    _wait(run)
    monkeypatch.setattr(providers, "for_driver", original)
    return agent, run, backup


def test_socket_down_is_prework_refusal(epi, tmp_path, monkeypatch):
    _, run, backup = fallback(epi, tmp_path, monkeypatch, "socket_down")
    assert run.state == "completed" and backup.spawned.is_set()
    assert run.hops[0]["outcome"] == "refused"
    assert run.hops[0]["code"] == "epi_refused"


def test_rejected_is_prework_refusal(epi, tmp_path, monkeypatch):
    _, run, backup = fallback(epi, tmp_path, monkeypatch, "rejected")
    assert run.state == "completed" and backup.spawned.is_set()
    assert run.hops[0]["outcome"] == "refused"


def test_failed_after_admission_no_fallback(epi, tmp_path, monkeypatch):
    _, run, backup = fallback(epi, tmp_path, monkeypatch, "failed_no_lane")
    assert run.state == "failed" and run.finish_reason == "no_lane"
    assert not backup.spawned.is_set()
    assert len(run.hops) == 1 and run.hops[0]["outcome"] == "ran"
    assert run.detail()["extra"]["epi"]["hops"] == []


def test_continue_uses_saved_agent_id(epi, tmp_path, monkeypatch):
    agent, first = start(epi, tmp_path, scenario="question_completed", monkeypatch=monkeypatch)
    _wait(first)
    saved = agent.session_id
    second = _wait(agent.follow_up("Explain more", ""))
    args = calls(epi)[-1]["argv"]
    assert args[1] == "continue"
    assert args[args.index("--agent-id") + 1] == saved
    assert args[args.index("--message") + 1] == "Explain more"
    assert args[args.index("--verification") + 1] == ""
    assert args[args.index("--request-id") + 1] == second.request_id != first.request_id
    assert agent.session().data["run_id"] == load("continue_completed.jsonl")[-1]["run_id"]


def test_cancel_calls_epi_cancel(epi, tmp_path, monkeypatch):
    agent, run = start(epi, tmp_path, monkeypatch=monkeypatch)
    _wait(run)
    saved = agent.session_id
    agent.close("user cancelled")
    args = calls(epi)[-1]["argv"]
    assert args[1] == "cancel"
    assert args[args.index("--agent-id") + 1] == saved


def mcp(epi, monkeypatch, tmp_path):
    monkeypatch.setenv("SUBAGENT_CONFIG", str(tmp_path / "subagent.toml"))
    server = importlib.import_module("subagent.mcp_server")
    monkeypatch.setattr(server, "registry", epi[2])
    monkeypatch.setattr(server, "settings", epi[0])
    return server


def test_transcript_uses_saved_run_id(epi, tmp_path, monkeypatch):
    agent, first = start(epi, tmp_path, scenario="question_completed", monkeypatch=monkeypatch)
    _wait(first)
    first_id = agent.session().data["run_id"]
    _wait(agent.follow_up("Explain again"))
    server = mcp(epi, monkeypatch, tmp_path)
    detail = asyncio.run(server.transcript(first.run_id, limit=4, raw=True))
    args = calls(epi)[-1]["argv"]
    assert args[1] == "transcript"
    assert args[args.index("--run-id") + 1] == first_id
    assert "--raw" in args
    assert detail["run_id"] == first.run_id and detail["agent_id"] == agent.agent_id
    assert detail["activity"] == load("transcript.json")[0]["activity"]


def test_usage_cost_preserved(epi, tmp_path, monkeypatch):
    _, run = start(epi, tmp_path, monkeypatch=monkeypatch)
    _wait(run)
    native = load("delegate_change_completed.jsonl")[-1]
    detail = run.detail()
    for key in ("usage", "usage_unreported", "credits", "credits_unit", "cash_usd", "turn_usage"):
        assert detail[key] == native[key]
    # The recordings have nullable cost and no turns. Exercise nonzero cost
    # and extensions without altering their immutable provenance.
    turn = {"ts_start": 1, "ts_first_token": 2, "ts_end": 3, "provider": "inner",
            "model": "inner-model", "input": 32, "output": 5, "cache_read": 0,
            "cache_write": 0, "reasoning": 0, "tool_calls": [], "stop_reason": "end",
            "usage_unreported": ["reasoning"]}
    native = {**native, "credits": 2.25, "credits_unit": "plan_requests", "cash_usd": 0.012,
              "cold_load": True, "turn_usage": [turn]}
    snapshot = Translator(Session(provider="epi")).feed(native)[-1]["outcome"]
    agent = epi[2].create_agent("cost", tmp_path, provider="epi")
    assert agent.wait_ready(5) is None
    from .test_provider_hooks import outcome
    from .test_runs import FakeProcess

    monkeypatch.setattr(EPI_PROVIDER, "spawn", lambda *a, **k: FakeProcess(
        [{"type": "result", "outcome": outcome(**snapshot)}], [], {}
    ))
    result = _wait(agent.delegate("question", "true")).detail()
    for key in ("credits", "credits_unit", "cash_usd", "turn_usage", "cold_load"):
        assert result[key] == native[key]


def test_doctor_makes_no_model_call(epi, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SUBAGENT_CONFIG", str(tmp_path / "subagent.toml"))
    # Recorded scope names /fixture/project, not this temporary project.
    assert cli.main(["provider", "test", "epi", "--json"]) == 1
    row = json.loads(capsys.readouterr().out)["providers"][0]
    assert row["socket_ok"] and row["profile"] == "fake"
    assert row["project_allowed"] is False
    monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path))
    assert cli.main(["doctor", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["providers"][0]
    assert row["project_initialized"] and row["project_allowed"]
    assert all(c["argv"][1] == "list" for c in calls(epi))


def test_example_config_has_no_secrets():
    path = Path(__file__).parents[1] / "examples" / "config.epi.toml"
    table = tomllib.loads(path.read_text())["providers"]["epi"]
    assert table["driver"] == "epi" and table["experimental"] and table["local"]
    assert {"binary", "token_file", "project_root", "run_timeout", "idle_timeout"} <= table.keys()
    assert not {"api_key", "token", "api_key_env", "socket"} & table.keys()
    assert CANARY not in path.read_text()


def test_explicit_model_and_fallback_only(epi, tmp_path, monkeypatch):
    _, run = start(epi, tmp_path, monkeypatch=monkeypatch, model="inner-model", fallback="local")
    _wait(run)
    assert "--model=inner-model" in calls(epi)[0]["argv"]
    assert "--fallback=local" in calls(epi)[0]["argv"]


def test_delegate_workspace_may_be_omitted(epi):
    settings, session, _, _ = epi
    session.data["context"] = RunContext("true", None, "request-1", None)
    args = EPI_PROVIDER.argv(settings.provider(), settings, "a", "task", None, session, "outer")
    assert "--workspace" not in args and not any(a.startswith("--model") for a in args)


def test_metadata_nested_and_profile_readonly(epi, tmp_path, monkeypatch):
    server = mcp(epi, monkeypatch, tmp_path)
    value = asyncio.run(server.list_agents())
    extra = value["providers"][0]["extra"]
    assert extra["profile"] == "fake"
    assert extra["epi"] == load("list.json")[0]
    assert all(c["argv"][1] == "list" for c in calls(epi))


@pytest.mark.parametrize("project", [None, "uninitialized"])
def test_doctor_prompt_refuses_unprepared_project(epi, tmp_path, monkeypatch, project):
    settings = epi[0]
    cfg = settings.provider()
    extra = {k: v for k, v in cfg.extra.items() if k != "project_root"}
    if project:
        root = tmp_path / project
        root.mkdir()
        extra["project_root"] = str(root)
    cfg = dataclasses.replace(cfg, extra=extra)
    monkeypatch.setattr(cli, "_prompt_turn", lambda *a: pytest.fail("model call attempted"))
    row, hard = cli._doctor_row(settings, cfg, argparse.Namespace(prompt=True, no_probe=False))
    assert hard and row["prompt"]["state"] == "refused"
    assert all(c["argv"][1] == "list" for c in calls(epi))


def test_bad_client_output_does_not_leak_or_fallback(epi, tmp_path, monkeypatch, caplog):
    _, run, backup = fallback(epi, tmp_path, monkeypatch, "bad_output")
    assert run.state == "failed" and not backup.spawned.is_set()
    assert CANARY not in caplog.text + json.dumps(run.detail())


def test_socket_error_after_admission_stays_failed(epi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_EXIT_AFTER_ADMISSION", "1")
    _, run, backup = fallback(epi, tmp_path, monkeypatch, "failed_no_lane")
    assert run.state == "failed" and not backup.spawned.is_set()


def test_awaiting_human_and_inner_hops(epi):
    session = epi[1]
    detail = load("delegate_awaiting_approval.jsonl")[-1]
    detail.update(phase="awaiting_human", hops=[{"provider": "inner", "code": "harness_busy"}])
    snapshot = Translator(session).feed(detail)[-1]["outcome"]
    assert snapshot["phase"] == "awaiting_human" and snapshot["needs_human"]
    assert "hops" not in snapshot
    assert snapshot["extra"]["epi"]["hops"] == detail["hops"]


def test_mcp_preserves_original_inputs_without_outer_defaults(epi, tmp_path, monkeypatch):
    server = mcp(epi, monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    result = asyncio.run(server.delegate(
        "Question: explain marker", "true", instructions="Do not edit files",
        name="question", provider="epi", wait_seconds=5,
    ))
    assert result["state"] == "completed"
    args = calls(epi)[0]["argv"]
    assert args[args.index("--task") + 1] == "Question: explain marker"
    assert "--instructions=Do not edit files" in args
    assert "--name=question" in args
    assert not any(arg.startswith(("--model", "--provider", "--lane", "--fallback"))
                   for arg in args)


def test_doctor_prompt_uses_configured_initialized_workspace(epi, tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from subagent import core

    captured = []

    class ProbeServer:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def delegate(self, **kwargs):
            captured.append(kwargs)
            done = threading.Event()
            done.set()
            return SimpleNamespace(done=done, state="completed", error=None,
                                   usage=SimpleNamespace(output=1), ttft_seconds=None,
                                   started_at=1, finished_at=2)

        def stop(self):
            pass

    monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path))
    monkeypatch.setattr(core, "InProcessServer", ProbeServer)
    result = cli._prompt_turn(epi[0], epi[0].provider())
    assert result["ok"]
    assert captured[0]["workspace"] == tmp_path
    assert captured[0]["verification"] == "true"
    assert (tmp_path / ".pi" / "board.db").exists()
    assert all(call["argv"][1] == "list" for call in calls(epi))


def test_boot_refuses_missing_binary_and_unapproved_experimental(epi):
    settings = epi[0]
    cfg = dataclasses.replace(settings.provider(), binary="/nonexistent/epi")
    error, _ = EPI_PROVIDER.boot(settings, "a", cfg)
    assert error == "epi binary is unavailable"
    cfg = dataclasses.replace(settings.provider(), experimental=False)
    error, _ = EPI_PROVIDER.boot(settings, "a", cfg)
    assert "experimental" in error


def test_poll_preserves_native_failure(epi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_AWAIT_FAILED", "1")
    _, run = start(epi, tmp_path, scenario="awaiting_approval", monkeypatch=monkeypatch)
    _wait(run)
    expected = load("delegate_failed_no_lane.jsonl")[-1]
    actual = run.detail()
    for key in ("state", "phase", "error", "finish_reason", "result", "usage", "cash_usd"):
        assert actual[key] == expected[key]
    assert actual["extra"]["epi"]["hops"] == expected["hops"]


def test_transcript_after_agent_reaped(epi, tmp_path, monkeypatch):
    agent, run = start(epi, tmp_path, scenario="question_completed", monkeypatch=monkeypatch)
    _wait(run)
    native_id = agent.session().data["run_id"]
    server = mcp(epi, monkeypatch, tmp_path)
    first = asyncio.run(server.transcript(run.run_id, raw=True))
    agent.close("finished")
    epi[2].reap_once()
    assert epi[2].find_agent(agent.agent_id) is None
    archived = asyncio.run(server.transcript(run.run_id, raw=True))
    assert archived["activity"] == first["activity"]
    assert archived["raw_response"] == first["raw_response"]
    assert calls(epi)[-1]["argv"][calls(epi)[-1]["argv"].index("--run-id") + 1] == native_id


@pytest.mark.parametrize("scenario", ["no_receipt", "ambiguous_error", "oversized"])
def test_unproven_admission_never_completes_or_falls_back(epi, tmp_path, monkeypatch, scenario):
    _, run, backup = fallback(epi, tmp_path, monkeypatch, scenario)
    assert run.state == "failed"
    assert not backup.spawned.is_set()
    assert not run.needs_human


@pytest.mark.parametrize("mutation", ["agent_id", "run_id"])
def test_native_identity_cannot_change_within_stream(epi, mutation):
    session = epi[1]
    translator = Translator(session)
    snapshots = load("delegate_change_completed.jsonl")
    translator.feed(snapshots[0])
    saved = (session.session_id, session.data["run_id"])
    snapshots[-1][mutation] = "different-native-identity"
    with pytest.raises(ValueError, match="different admitted"):
        translator.feed(snapshots[-1])
    assert (session.session_id, session.data["run_id"]) == saved


def test_poll_requires_saved_native_identity(epi, monkeypatch):
    session = epi[1]
    snapshots = load("delegate_change_completed.jsonl")
    Translator(session).feed(snapshots[0])
    snapshots[-1]["run_id"] = "wrong-run"
    monkeypatch.setattr(EPI_PROVIDER, "_call", lambda *a: snapshots[-1])
    with pytest.raises(ValueError, match="different admitted run"):
        EPI_PROVIDER.poll(session)
    assert session.data["run_id"] == snapshots[0]["run_id"]


@pytest.mark.parametrize("bad", ["verification", "uat"])
def test_completed_requires_native_passed_checks(epi, bad):
    detail = load("delegate_change_completed.jsonl")[-1]
    if bad == "verification":
        detail["verification"]["passed"] = False
    else:
        detail["uat"] = [{"passed": None}]
    with pytest.raises(ValueError, match="without"):
        Translator(epi[1]).feed(detail)


def test_cancel_final_usage_preserved_and_human_gate_cleared(epi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    agent, run = start(epi, tmp_path, scenario="awaiting_approval", monkeypatch=monkeypatch)
    assert run.await_ready.wait(5)
    real_call = EPI_PROVIDER._call
    usage = load("delegate_change_completed.jsonl")[-1]["usage"]

    def call(session, tool, *args):
        if tool == "cancel":
            return {"agent_id": session.session_id, "closed": True,
                    "usage": {**usage, "total": 999},  # aggregate must not become run usage
                    "runs": [{"run_id": session.data["run_id"], "usage": usage}]}
        return real_call(session, tool, *args)

    monkeypatch.setattr(EPI_PROVIDER, "_call", call)
    agent.close("cancelled")
    _wait(run)
    assert run.state == "cancelled" and not run.needs_human
    assert "human_action" not in run.detail()
    assert run.usage.as_dict() == usage
    assert agent.usage().total == usage["total"]
    assert run.detail()["extra"]["cancellation_confirmed"] is True


def test_unconfirmed_cancel_is_reported(epi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    agent, run = start(epi, tmp_path, scenario="awaiting_approval", monkeypatch=monkeypatch)
    assert run.await_ready.wait(5)
    monkeypatch.setattr(EPI_PROVIDER, "_call",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("epi cancel client failed")))
    agent.close("cancelled")
    _wait(run)
    assert not run.needs_human
    assert run.detail()["extra"]["cancellation_confirmed"] is False
    assert run.detail()["extra"]["cancellation_error"] == "epi cancel client failed"


def test_auxiliary_output_is_bounded(epi, monkeypatch):
    # Fake list emits an oversized payload, without exercising a model or socket.
    import sys

    session = epi[1]
    monkeypatch.setattr(EPI_PROVIDER, "_call_argv",
                        lambda *a: [sys.executable, "-c", "print('x' * 1048577)"])
    with pytest.raises(RuntimeError, match="epi list client failed"):
        EPI_PROVIDER._call(session, "list")


def test_slow_cancel_finishes_after_native_usage_and_guard_release(epi, tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    agent, run = start(epi, tmp_path, scenario="awaiting_approval", monkeypatch=monkeypatch)
    assert run.await_ready.wait(5)
    entered = threading.Event()
    respond = threading.Event()
    released = threading.Event()
    run.pre_verify = lambda: (released.set() or {})
    real_call = EPI_PROVIDER._call
    usage = load("delegate_change_completed.jsonl")[-1]["usage"]

    def call(session, tool, *args):
        if tool == "cancel":
            entered.set()
            assert respond.wait(3)
            return {"agent_id": session.session_id, "closed": True, "usage": usage,
                    "runs": [{"run_id": session.data["run_id"], "usage": usage}]}
        return real_call(session, tool, *args)

    monkeypatch.setattr(EPI_PROVIDER, "_call", call)
    closer = threading.Thread(target=agent.close)
    closer.start()
    assert entered.wait(3)
    assert not run.done.wait(0.1)
    assert not released.is_set()
    respond.set()
    closer.join(5)
    assert not closer.is_alive()
    assert run.done.is_set() and released.is_set()
    assert run.usage.as_dict() == usage and not run.needs_human


def test_continue_cancel_before_receipt_does_not_reuse_previous_usage(epi, tmp_path, monkeypatch):
    agent, first = start(epi, tmp_path, scenario="question_completed", monkeypatch=monkeypatch)
    _wait(first)
    session = agent.session()
    old_id = session.data["run_id"]
    previous = first.usage.as_dict()
    EPI_PROVIDER.spawn(epi[0].provider(), epi[0], "outer", "next", tmp_path, session, None,
                       context=RunContext("true", None, "next-request", None))
    assert "run_id" not in session.data and "last_outcome" not in session.data
    monkeypatch.setattr(EPI_PROVIDER, "_call", lambda *a: {
        "agent_id": session.session_id, "closed": True, "usage": previous,
        "runs": [{"run_id": old_id, "usage": previous}],
    })
    snapshot = EPI_PROVIDER.cancel(session, reason="cancel", kind="cancel")
    assert "usage" not in snapshot
    assert "run_id" not in snapshot["extra"]["epi"]
    assert snapshot["extra"]["cancellation_confirmed"]


@pytest.mark.parametrize("uat", [None, [], [{"row": 9, "passed": True}]])
def test_completed_goal_requires_exact_uat_rows(epi, tmp_path, uat):
    session = epi[1]
    session.data["context"] = RunContext(
        "true", parse_goal('Execute plan "marker" (plan.md). Goal rows:\n1 ok: `true`', tmp_path),
        "request", None,
    )
    detail = load("delegate_goal_uat.jsonl")[-1]
    if uat is None:
        detail.pop("uat")
    else:
        detail["uat"] = uat
    with pytest.raises(ValueError, match="all requested goal rows"):
        Translator(session).feed(detail)


def test_text_starting_with_option_prefix_stays_a_value(epi, tmp_path):
    settings, session, _, _ = epi
    session.data["context"] = RunContext("--check", None, "r1", None)
    args = EPI_PROVIDER.argv(settings.provider(), settings, "a", "--task text", tmp_path,
                             session, None)
    assert "--task=--task text" in args and "--verification=--check" in args
    session.session_id = "native-agent"
    args = EPI_PROVIDER.argv(settings.provider(), settings, "a", "--message text", tmp_path,
                             session, None)
    assert "--message=--message text" in args


def test_doctor_rejects_symlink_board(epi, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path))
    board = tmp_path / ".pi" / "board.db"
    board.unlink()
    real = tmp_path / "other.db"
    real.touch()
    board.symlink_to(real)
    row = EPI_PROVIDER.doctor(epi[0], epi[0].provider())
    assert not row["project_initialized"] and not row["health_ok"]
