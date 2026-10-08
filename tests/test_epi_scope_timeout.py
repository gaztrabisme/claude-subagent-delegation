"""R2 native-lifecycle probes, exercised against the project-scoped fake CLI."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from subagent.providers.base import RunContext
from subagent.providers.epi import EPI_PROVIDER, EpiProcess

from .test_epi_review import wait
from .test_providers_epi import calls, epi  # noqa: F401 - fixture


def configure(fixture, **extra):
    settings, _, registry, _ = fixture
    cfg = dataclasses.replace(settings.provider(), **extra)
    registry.settings = dataclasses.replace(
        settings, providers={"epi": cfg}, child_env_passthrough=(
            *settings.child_env_passthrough, "FAKE_EPI_DEFAULT_PROJECT",
            "FAKE_EPI_SEND_GATE", "FAKE_EPI_RECEIPT_GATE", "FAKE_EPI_WORKING_WAIT",
            "FAKE_EPI_HUMAN_GATE",
        ),
    )
    return registry.settings


@pytest.mark.parametrize("project", ["default", "other", "omitted"])
@pytest.mark.parametrize("receipt_lost", ["before_admission", "after_admission"])
def test_lost_receipt_cancel_uses_submission_workspace(
    epi, tmp_path, monkeypatch, project, receipt_lost,  # noqa: F811 - fixture
):
    default, other = tmp_path / "default", tmp_path / "other"
    default.mkdir()
    other.mkdir()
    submitted = default if project == "default" else other
    gate = tmp_path / "receipt"
    monkeypatch.setenv("FAKE_EPI_DEFAULT_PROJECT", str(default))
    control = "FAKE_EPI_SEND_GATE" if receipt_lost == "before_admission" else "FAKE_EPI_RECEIPT_GATE"
    monkeypatch.setenv(control, str(gate))
    settings = configure(epi)
    if project == "omitted":
        # Exercise the optional argv workspace with a cwd different from the
        # caller default. Cancellation must preserve omission, not infer cwd.
        session = epi[1]
        context = RunContext(request_id="omitted-workspace", verification="true",
                             goal=None, deadline=None)
        session.data.update(context=context, request_id=context.request_id, receipt=threading.Event())
        session.data["env"] = EPI_PROVIDER.env(settings, "outer", settings.provider(), session)
        args = EPI_PROVIDER.argv(settings.provider(), settings, "outer", "question", None,
                                 session, None)
        process = EpiProcess(args, session.data["env"], str(other), session)
        session.data["process"] = process
        events = []
        worker = threading.Thread(target=lambda: events.extend(process.events()))
        worker.start()
        try:
            wait(Path(str(gate) + ".ready").exists)
            snapshot = EPI_PROVIDER.cancel(session, reason="caller cancellation", kind="cancel")
        finally:
            process.kill()
            worker.join(5)
        assert not worker.is_alive()
        assert snapshot["extra"]["cancellation_confirmed"]
        request_id = context.request_id
    else:
        agent = epi[2].create_agent("lost-receipt", submitted, provider="epi", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("question", "true")
        wait(Path(str(gate) + ".ready").exists)
        agent.close()
        assert run.done.wait(5)
        assert run.detail()["extra"]["cancellation_confirmed"]
        request_id = run.request_id
    cancel = next(c for c in calls(epi) if c["argv"][1] == "cancel")
    if project == "omitted":
        assert "--workspace" not in cancel["argv"]
        native_project = default
    else:
        assert "--workspace" in cancel["argv"], "lost-receipt cancel selected the caller default"
        assert cancel["argv"][cancel["argv"].index("--workspace") + 1] == str(submitted.resolve())
        native_project = submitted
    saved = json.loads(Path(str(epi[3]) + ".request.json").read_text())
    request = saved["requests"][json.dumps([str(native_project.resolve()), request_id])]
    assert request["cancelled"]
    assert request["admissions"] == (receipt_lost == "after_admission")
    # A repeated original submission must observe the same project's fence.
    delegate = next(c for c in calls(epi) if c["argv"][1] == "delegate")
    env = dict(delegate["env"])
    env.pop(control)
    replay = subprocess.run([settings.provider().binary, *delegate["argv"]],
                            cwd=delegate["cwd"], env=env, input="", text=True,
                            capture_output=True, timeout=5, check=True)
    replayed = json.loads(replay.stdout)
    assert replayed["state"] == "cancelled"
    assert replayed["admitted"] == (receipt_lost == "after_admission")
    if replayed["admitted"]:
        assert replayed["agent_id"] == request["receipt"]["agent_id"]
        assert replayed["run_id"] == request["receipt"]["run_id"]
    else:
        assert replayed["usage"]["total"] == 0


@pytest.mark.parametrize("client", ["polling", "open_stream"])
def test_human_wait_does_not_spend_outer_timeout(epi, tmp_path, monkeypatch, client):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    gate = tmp_path / "human-stream"
    if client == "open_stream":
        monkeypatch.setenv("FAKE_EPI_HUMAN_GATE", str(gate))
    configure(epi, run_timeout=1)
    agent = epi[2].create_agent("human-wait", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    human_snapshot = threading.Event()
    run = agent.delegate("question", "true", on_event=lambda event: (
        human_snapshot.set() if event.get("outcome", {}).get("needs_human") else None
    ))
    assert human_snapshot.wait(5)
    if client == "polling":
        assert run.await_ready.wait(5) and run.needs_human
    else:
        wait(Path(str(gate) + ".ready").exists)
        assert agent._current is not None
    original_deadline = run.deadline
    time.sleep(1.3)
    assert epi[2].reap_once() == [], "reaper charged the native human wait"
    assert run.state == "working" and not run.done.is_set()
    assert not any(c["argv"][1] == "cancel" for c in calls(epi))
    if client == "open_stream":
        gate.touch()
        assert run.await_ready.wait(5) and run.needs_human
    # Human acts; work remains active, so the remaining budget must resume.
    env = agent.session().data["env"]
    env["FAKE_EPI_WORKING_WAIT"] = "1"
    env.pop("FAKE_EPI_HUMAN_WAIT")
    wait(lambda: not run.needs_human)
    assert run.deadline >= original_deadline + 1.3
    assert not run.done.is_set()
    assert run.done.wait(5)
    assert run.state == "failed" and run.trip[0] == "timeout"
    assert any(c["argv"][1] == "cancel" for c in calls(epi))


def test_transport_loss_preserves_last_native_human_wait(epi, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    monkeypatch.setenv("FAKE_EPI_EXIT_AFTER_ADMISSION", "1")
    configure(epi, run_timeout=1)
    entered, release = threading.Event(), threading.Event()
    original = EPI_PROVIDER.poll

    def delayed_poll(session):
        entered.set()
        assert release.wait(5)
        return original(session)

    monkeypatch.setattr(EPI_PROVIDER, "poll", delayed_poll)
    agent = epi[2].create_agent("human-transport-loss", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    run = agent.delegate("question", "true")
    reaper = None
    try:
        assert entered.wait(5)
        # No human action has occurred and the next native snapshot is unknown.
        time.sleep(1.2)
        reaper = threading.Thread(target=epi[2].reap_once)
        reaper.start()
        time.sleep(0.2)
        cancelled = any(c["argv"][1] == "cancel" for c in calls(epi))
        assert not cancelled, "synthetic transport failure restarted the human-wait budget"
        assert run.state == "working" and run.needs_human
    finally:
        release.set()
        if reaper is not None:
            reaper.join(5)
            assert not reaper.is_alive()
