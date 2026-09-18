"""Lane memory on disk: <session_root>/lane_state.json."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from subagent_mcp.config import log
from subagent_mcp.lane_state import LaneState


@pytest.fixture
def caplog_sam(caplog):
    # The "sam" logger does not propagate; attach caplog's handler directly.
    log.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="sam")
    yield caplog
    log.removeHandler(caplog.handler)


def test_lane_state_close_then_closed(tmp_path: Path):
    state = LaneState(tmp_path)
    until = datetime.now().astimezone() + timedelta(hours=2)
    state.close("glm", until, "zai_1308", "limit reached")
    entry = state.closed("glm")
    assert entry is not None
    assert entry["closed_until"] == until
    assert (entry["code"], entry["message"]) == ("zai_1308", "limit reached")
    assert state.closed("deepseek") is None
    raw = json.loads((tmp_path / "lane_state.json").read_text())
    assert set(raw["glm"]) == {"closed_until", "code", "message", "set_at"}
    datetime.fromisoformat(raw["glm"]["set_at"])


def test_lane_state_write_is_atomic_and_keeps_other_lanes(tmp_path: Path):
    state = LaneState(tmp_path)
    until = datetime.now().astimezone() + timedelta(hours=1)
    state.close("glm", until, "zai_1308", "a")
    state.close("deepseek", until, "deepseek_balance", "b")
    assert state.closed("glm") is not None and state.closed("deepseek") is not None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lane_state.json"]


def test_lane_state_expired_entry_is_ignored(tmp_path: Path):
    state = LaneState(tmp_path)
    state.close("glm", datetime.now().astimezone() - timedelta(seconds=1), "zai_1308", "old")
    assert state.closed("glm") is None


def test_lane_state_naive_time_is_read_as_local(tmp_path: Path):
    later = (datetime.now() + timedelta(hours=1)).isoformat()
    (tmp_path / "lane_state.json").write_text(json.dumps({"glm": {"closed_until": later}}))
    assert LaneState(tmp_path).closed("glm") is not None


@pytest.mark.parametrize("content", [
    "{not json",
    "[1, 2, 3]",
    '{"glm": "closed"}',
    '{"glm": {"closed_until": "tomorrow"}}',
    "",
])
def test_lane_state_malformed_file_is_empty(tmp_path: Path, caplog_sam, content):
    (tmp_path / "lane_state.json").write_text(content)
    state = LaneState(tmp_path)
    assert state.closed("glm") is None
    # And it can still be written over.
    state.close("glm", datetime.now().astimezone() + timedelta(hours=1), "zai_1310", "m")
    assert state.closed("glm") is not None


def test_lane_state_malformed_file_is_logged(tmp_path: Path, caplog_sam):
    (tmp_path / "lane_state.json").write_text("{not json")
    LaneState(tmp_path).closed("glm")
    assert any("lane state" in r.getMessage() for r in caplog_sam.records)


def test_lane_state_missing_file_is_empty(tmp_path: Path):
    assert LaneState(tmp_path / "nowhere").closed("glm") is None
