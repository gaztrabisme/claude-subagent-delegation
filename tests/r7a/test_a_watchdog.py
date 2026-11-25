"""R7a (a): the no-progress watchdog.

New provider keys `no_progress_steps` and `no_progress_seconds` (0 = off). A run
with no write or edit tool call after that many tool calls, or that much run
time, fails with finish_reason "no_progress" and carries the read list.
"""

from __future__ import annotations

import pytest

from subagent.config import ConfigError
from subagent.runs import COMPLETED, FAILED

from ..conftest import make_settings
from . import _helpers as h

WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"]


def _dedup(paths):
    seen: list[str] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def _start(reg, tmp_path):
    agent = reg.create_agent("t", tmp_path, "glm")
    return agent, agent.delegate("look around", "true")


def test_a_six_reads_with_a_limit_of_five_stop_the_run(tmp_path, monkeypatch):
    paths = [f"src/mod{i}.py" for i in range(6)]
    events = [h.init(), *[h.read(p) for p in paths], h.success()]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events)],
                     provider_extra={"no_progress_steps": 5})
    try:
        assert reg.settings.providers["glm"].no_progress_steps == 5
        _, run = _start(reg, tmp_path)
        h.wait(run)
        assert run.state == FAILED
        assert run.finish_reason == "no_progress"
        assert "no_progress_steps" in (run.error or "") and "5" in (run.error or "")
        np = run.detail()["no_progress"]
        # Stopped at the limit: after the 5th or the 6th call, never later.
        assert np["steps"] in (5, 6)
        assert np["reads"] == paths[: np["steps"]]
        assert reg.spawned[0].killed
    finally:
        reg.shutdown()


def test_a_read_list_is_in_order_and_deduplicated(tmp_path, monkeypatch):
    paths = ["a.py", "b.py", "a.py", "c.py", "d.py", "e.py", "f.py", "g.py"]
    events = [h.init(), *[h.read(p) for p in paths], h.success()]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events)],
                     provider_extra={"no_progress_steps": 5})
    try:
        assert reg.settings.providers["glm"].no_progress_steps == 5
        _, run = _start(reg, tmp_path)
        h.wait(run)
        assert run.finish_reason == "no_progress"
        np = run.detail()["no_progress"]
        assert np["reads"] == _dedup(paths[: np["steps"]])
        assert len(np["reads"]) < np["steps"]  # the repeat of a.py is not listed twice
    finally:
        reg.shutdown()


@pytest.mark.parametrize("write_tool", WRITE_TOOLS)
def test_a_a_write_or_edit_satisfies_the_watchdog_for_the_rest_of_the_run(
    tmp_path, monkeypatch, write_tool
):
    events = [
        h.init(),
        *[h.read(f"r{i}.py") for i in range(3)],
        h.write("out.py", write_tool),
        *[h.read(f"later{i}.py") for i in range(10)],
        h.success(),
    ]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events)],
                     provider_extra={"no_progress_steps": 5})
    try:
        assert reg.settings.providers["glm"].no_progress_steps == 5
        _, run = _start(reg, tmp_path)
        h.wait(run)
        assert run.state == COMPLETED, run.error
        assert run.finish_reason != "no_progress"
        assert "no_progress" not in run.detail()
    finally:
        reg.shutdown()


def test_a_seconds_variant_stops_a_run_that_only_reads(tmp_path, monkeypatch):
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    paths = [f"f{i}.py" for i in range(8)]
    events = [h.init(), *[h.read(p) for p in paths], h.success()]
    # One simulated second passes before every event; the limit is 3.5 s.
    reg = h.registry(tmp_path, monkeypatch, [h.script(events, clock=clock, step_seconds=1.0)],
                     provider_extra={"no_progress_seconds": 3.5})
    try:
        assert reg.settings.providers["glm"].no_progress_seconds == 3.5
        _, run = _start(reg, tmp_path)
        h.wait(run)
        assert run.state == FAILED
        assert run.finish_reason == "no_progress"
        assert "no_progress_seconds" in (run.error or "") and "3.5" in (run.error or "")
        np = run.detail()["no_progress"]
        assert 1 <= np["steps"] < len(paths)
        assert np["reads"] == paths[: np["steps"]]
    finally:
        reg.shutdown()


def test_a_seconds_variant_is_satisfied_by_a_write(tmp_path, monkeypatch):
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    events = [h.init(), h.read("a.py"), h.write("a.py", "Edit"),
              *[h.read(f"f{i}.py") for i in range(8)], h.success()]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events, clock=clock, step_seconds=1.0)],
                     provider_extra={"no_progress_seconds": 3.5})
    try:
        assert reg.settings.providers["glm"].no_progress_seconds == 3.5
        _, run = _start(reg, tmp_path)
        h.wait(run)
        assert run.state == COMPLETED, run.error
        assert run.finish_reason != "no_progress"
    finally:
        reg.shutdown()


def test_a_both_keys_default_to_zero_and_never_stop_a_run(tmp_path, monkeypatch):
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    events = [h.init(), *[h.read(f"f{i}.py") for i in range(30)], h.success()]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events, clock=clock, step_seconds=60.0)])
    try:
        cfg = reg.settings.providers["glm"]
        assert cfg.no_progress_steps == 0
        assert cfg.no_progress_seconds == 0
        _, run = _start(reg, tmp_path)
        h.wait(run)
        assert run.state == COMPLETED, run.error
        assert run.finish_reason != "no_progress"
    finally:
        reg.shutdown()


@pytest.mark.parametrize("key,value", [
    ("no_progress_steps", -1),
    ("no_progress_seconds", -0.5),
])
def test_a_negative_limits_are_a_config_refusal(tmp_path, key, value):
    with pytest.raises(ConfigError, match=key):
        make_settings(tmp_path, providers=h.provider(**{key: value}))


def test_a_trace_run_record_names_the_overrun(tmp_path, monkeypatch, trace_records):
    events = [h.init(), *[h.read(f"f{i}.py") for i in range(6)], h.success()]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events)],
                     provider_extra={"no_progress_steps": 5})
    try:
        _, run = _start(reg, tmp_path)
        h.wait(run)
        records = h.run_records(trace_records)
        assert len(records) == 1
        assert records[0]["finish_reason"] == "no_progress"
        assert records[0]["extra"]["overrun"] == {"kind": "no_progress", "limit": 5}
    finally:
        reg.shutdown()
