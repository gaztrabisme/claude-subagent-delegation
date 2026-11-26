"""RR-12 re-review findings on scripts/coord_meter.py, pinned as correct behaviour."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from tests.m0._synth import assistant_record, make_usage, write_session, write_subagent

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "coord_meter.py"
START = "2026-11-25T09:00:00Z"
END = "2026-11-25T11:00:00Z"


def _dt(iso_z: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso_z.replace("Z", "+00:00"))


@pytest.fixture
def cm() -> Any:
    spec = importlib.util.spec_from_file_location("coord_meter", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _meter(cm: Any, projects: Path, sessions: list[str], roles: dict[str, str] | None = None,
           **kw: Any) -> dict[str, Any]:
    return cm.meter(projects, sessions, _dt(START), _dt(END), roles or {}, None, **kw)


def _main(cm: Any, argv: list[str]) -> int:
    try:
        return int(cm.main(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


def _cli(projects: Path, sessions: list[str], extra: list[str] | None = None) -> list[str]:
    argv = ["--projects-dir", str(projects), "--start", START, "--end", END]
    for sid in sessions:
        argv += ["--session", sid]
    return argv + (extra or [])


def _ts(n: int) -> str:
    return f"2026-11-25T10:00:{n:02d}Z"


# (1) duplicate records of one message id: the most complete usage wins.


def test_iterations_record_beats_partial_top_level_record(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="a", timestamp=_ts(0), msg_id="m",
                         usage=make_usage(input_tokens=10, output_tokens=5)),
        assistant_record(uuid="b", timestamp=_ts(1), msg_id="m",
                         usage=make_usage(iterations=[
                             {"input_tokens": 10, "output_tokens": 30},
                             {"input_tokens": 0, "output_tokens": 20},
                         ])),
    ])
    tokens = _meter(cm, tmp_path, [sid])["roles"]["coordinator"]["tokens"]
    assert tokens["output"] == 50
    assert tokens["input"] == 10


def test_largest_of_several_iterations_records_is_kept(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="a", timestamp=_ts(0), msg_id="m",
                         usage=make_usage(iterations=[{"input_tokens": 10, "output_tokens": 5}])),
        assistant_record(uuid="b", timestamp=_ts(1), msg_id="m",
                         usage=make_usage(iterations=[{"input_tokens": 10, "output_tokens": 90}])),
    ])
    tokens = _meter(cm, tmp_path, [sid])["roles"]["coordinator"]["tokens"]
    assert tokens["output"] == 90


def test_all_zero_record_does_not_hide_later_nonzero_input(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="a", timestamp=_ts(0), msg_id="m", usage=make_usage()),
        assistant_record(uuid="b", timestamp=_ts(1), msg_id="m",
                         usage=make_usage(input_tokens=40, output_tokens=0)),
    ])
    coord = _meter(cm, tmp_path, [sid])["roles"]["coordinator"]
    assert coord["messages"] == 1
    assert coord["tokens"]["input"] == 40


def test_all_zero_record_does_not_hide_later_cache_only_usage(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="a", timestamp=_ts(0), msg_id="m", usage=make_usage()),
        assistant_record(uuid="b", timestamp=_ts(1), msg_id="m",
                         usage=make_usage(cache_read_input_tokens=70)),
    ])
    coord = _meter(cm, tmp_path, [sid])["roles"]["coordinator"]
    assert coord["tokens"]["cache_read"] == 70


# (2) --session must not escape --projects-dir.


@pytest.mark.parametrize("kind", ["parent", "nested", "absolute"])
def test_session_path_escape_refused(cm: Any, tmp_path: Path, kind: str,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    projects = tmp_path / "projects"
    (projects / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    write_session(tmp_path, "outside", [
        assistant_record(uuid="a", timestamp=_ts(0), msg_id="m",
                         usage=make_usage(input_tokens=1, output_tokens=1)),
    ])
    sid = {"parent": "../outside", "nested": "sub/../../outside",
           "absolute": str(outside)}[kind]
    code = _main(cm, _cli(projects, [sid]))
    out = capsys.readouterr().out
    assert code == 2
    assert "coordinator" not in out


def test_session_dotdot_refused(cm: Any, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    assert _main(cm, _cli(projects, [".."])) == 2


def test_session_with_separator_refused_even_if_inside(cm: Any, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    write_session(projects / "sub", "inner", [
        assistant_record(uuid="a", timestamp=_ts(0), msg_id="m"),
    ])
    assert _main(cm, _cli(projects, ["sub/inner"])) == 2


# (3) repeated --role NAME keeps every id.


def test_repeated_role_name_keeps_all_agent_ids(cm: Any, tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="c", timestamp=_ts(0), msg_id="mc",
                         usage=make_usage(input_tokens=1, output_tokens=1)),
    ])
    write_subagent(tmp_path, sid, "ag1", [
        assistant_record(uuid="w1", timestamp=_ts(1), msg_id="m1",
                         usage=make_usage(input_tokens=10, output_tokens=1)),
    ])
    write_subagent(tmp_path, sid, "ag2", [
        assistant_record(uuid="w2", timestamp=_ts(2), msg_id="m2",
                         usage=make_usage(input_tokens=20, output_tokens=2)),
    ])
    code = _main(cm, _cli(tmp_path, [sid], ["--role", "worker=ag1", "--role", "worker=ag2"]))
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert "unassigned" not in report["roles"]
    assert report["roles"]["worker"]["messages"] == 2
    assert report["roles"]["worker"]["tokens"]["input"] == 30


# (4) reserved role names are not counted twice.


def test_explicit_coordinator_role_not_double_counted(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="c", timestamp=_ts(0), msg_id="mc",
                         usage=make_usage(input_tokens=10, output_tokens=10)),
    ])
    write_subagent(tmp_path, sid, "ag1", [
        assistant_record(uuid="w", timestamp=_ts(1), msg_id="mw",
                         usage=make_usage(input_tokens=10, output_tokens=10)),
    ])
    report = _meter(cm, tmp_path, [sid], {"coordinator": "ag1"}, io_share=True)
    block = report["roles"]["coordinator"]
    assert block["tokens"]["total"] == 40
    assert block["share_of_claude_tokens"] == pytest.approx(1.0)
    assert block["share_of_claude_io_tokens"] == pytest.approx(1.0)


def test_explicit_unassigned_role_not_double_counted(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="c", timestamp=_ts(0), msg_id="mc",
                         usage=make_usage(input_tokens=10, output_tokens=10)),
    ])
    write_subagent(tmp_path, sid, "ag1", [
        assistant_record(uuid="w", timestamp=_ts(1), msg_id="mw",
                         usage=make_usage(input_tokens=10, output_tokens=10)),
    ])
    write_subagent(tmp_path, sid, "ag2", [
        assistant_record(uuid="x", timestamp=_ts(2), msg_id="mx",
                         usage=make_usage(input_tokens=10, output_tokens=10)),
    ])
    report = _meter(cm, tmp_path, [sid], {"unassigned": "ag1"})
    shares = sum(b["share_of_claude_tokens"] for b in report["roles"].values())
    assert shares == pytest.approx(1.0)


# (5) a duplicate --session id is counted once.


def test_duplicate_session_counted_once(cm: Any, tmp_path: Path) -> None:
    sid = "s1"
    write_session(tmp_path, sid, [
        assistant_record(uuid="c", timestamp=_ts(0), msg_id="mc",
                         usage=make_usage(input_tokens=10, output_tokens=10)),
        assistant_record(uuid="d", timestamp=_ts(30), msg_id="md",
                         usage=make_usage(input_tokens=5, output_tokens=5)),
    ])
    once = _meter(cm, tmp_path, [sid])["roles"]["coordinator"]
    twice = _meter(cm, tmp_path, [sid, sid])["roles"]["coordinator"]
    assert twice["records"] == once["records"] == 2
    assert twice["messages"] == once["messages"]
    assert twice["tokens"] == once["tokens"]
