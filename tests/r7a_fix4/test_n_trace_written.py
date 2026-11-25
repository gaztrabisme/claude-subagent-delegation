"""R7a fix round 4: exactly one `run` trace record per run, in every ordering of
end_run / close / worker finish. Offline, in-process scripted children.
"""

from __future__ import annotations

import threading
import time

from subagent.runs import KILL_CANCEL, KILL_TIMEOUT, Agent

from ..r7a import _helpers as h
from ..r7a_fix2.test_k_fix_round2 import SVC, StubbornProcess, _clock, _service_registry

TERMINAL = {"completed", "completed_unverified", "failed", "cancelled", "error", "timeout"}


def _records(trace_records, run):
    return [r for r in h.run_records(trace_records) if r["run_id"] == run.run_id]


def _assert_one_terminal(trace_records, run):
    recs = _records(trace_records, run)
    assert len(recs) == 1, [(r["state"], r.get("finish_reason")) for r in recs]
    assert recs[0]["state"] in TERMINAL, recs[0]["state"]


def _close_in_thread(agent, release, kind=KILL_CANCEL):
    t = threading.Thread(target=agent.close, kwargs={"kind": kind}, daemon=True)
    t.start()
    time.sleep(0.3)  # close has marked the run and set done
    release.set()  # the child's stream drains after the cancel
    t.join(10)
    assert not t.is_alive()
    time.sleep(0.5)


def _plain(tmp_path, monkeypatch):
    monkeypatch.setattr(Agent, "END_RUN_WAIT", 0.2)
    release = threading.Event()
    reg = h.registry(tmp_path, monkeypatch, [lambda a, e: StubbornProcess(release)],
                     provider_extra=SVC)
    return reg, release


def test_n1_reaper_end_run_then_close_then_drain_writes_one_record(
    tmp_path, monkeypatch, trace_records
):
    """Red on base: close() sets done with no record, the drain's finish returns early: zero."""
    clock = _clock(monkeypatch)
    reg, release = _plain(tmp_path, monkeypatch)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        clock.tick(500)
        agent.end_run(run, "run deadline exceeded", KILL_TIMEOUT, finish_stuck=False)
        assert not run.done.is_set(), "precondition: reaper pass leaves the run working"
        _close_in_thread(agent, release)
        _assert_one_terminal(trace_records, run)
    finally:
        release.set()
        reg.shutdown()


def test_n2_service_driver_reaper_end_run_then_close_then_drain_one_record(
    tmp_path, monkeypatch, trace_records
):
    """Service-job driver variant (regression guard)."""
    clock = _clock(monkeypatch)
    reg, release = _service_registry(tmp_path, monkeypatch)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        assert agent.wait_ready(5) is None
        run = agent.delegate("work", "true")
        end = time.monotonic() + 3
        while run.deadline is None and time.monotonic() < end:
            time.sleep(0.01)
        clock.tick(500)
        agent.end_run(run, "run deadline exceeded", KILL_TIMEOUT, finish_stuck=False)
        _close_in_thread(agent, release)
        _assert_one_terminal(trace_records, run)
    finally:
        release.set()
        reg.shutdown()


def test_n3_close_first_then_worker_finish_writes_one_record(
    tmp_path, monkeypatch, trace_records
):
    """close() on a stuck run, then the child drains."""
    _clock(monkeypatch)
    reg, release = _plain(tmp_path, monkeypatch)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        _close_in_thread(agent, release)
        _assert_one_terminal(trace_records, run)
    finally:
        release.set()
        reg.shutdown()


def test_n4_end_run_server_side_finish_then_worker_finish_one_record(
    tmp_path, monkeypatch, trace_records
):
    """Guard: end_run finishes a stuck run itself, then the worker drains."""
    clock = _clock(monkeypatch)
    reg, release = _plain(tmp_path, monkeypatch)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        clock.tick(500)
        agent.end_run(run, "run deadline exceeded", KILL_TIMEOUT)
        release.set()
        h.wait(run)
        time.sleep(0.5)
        _assert_one_terminal(trace_records, run)
    finally:
        release.set()
        reg.shutdown()


def test_n5_plain_cancel_mid_flight_writes_one_record(tmp_path, monkeypatch, trace_records):
    """Guard: a cooperative child cancelled mid-flight."""
    _clock(monkeypatch)
    reg = h.registry(tmp_path, monkeypatch, [h.blocked()], provider_extra=SVC)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        agent.close(kind=KILL_CANCEL)
        h.wait(run)
        time.sleep(0.5)
        _assert_one_terminal(trace_records, run)
    finally:
        reg.shutdown()
