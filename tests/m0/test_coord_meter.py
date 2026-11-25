"""Acceptance tests for scripts/coord_meter.py (M0 coordination meter)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests.m0._synth import (
    assistant_record,
    clone_for_resume,
    make_usage,
    meta_record,
    tool_result_record,
    user_record,
    write_pricing,
    write_session,
    write_subagent,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "coord_meter.py"


def _dt(iso_z: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso_z.replace("Z", "+00:00"))


def _run_main(coord_meter: Any, argv: list[str]) -> int:
    try:
        return int(coord_meter.main(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


class _MissingCoordMeter:
    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"missing script under test: {SCRIPT_PATH}")


@pytest.fixture
def coord_meter() -> Any:
    """Lazily load scripts/coord_meter.py via spec_from_file_location so collection succeeds and tests fail while absent."""
    if not SCRIPT_PATH.is_file():
        return _MissingCoordMeter()
    spec = importlib.util.spec_from_file_location("coord_meter", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dedup_within_file_uses_max_output_tokens(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (What is counted #1): One count per message.id within a file; largest output_tokens supplies usage."""
    sid = "sess-dedup-1"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="msg-1",
                model="claude-opus-5",
                usage=make_usage(input_tokens=100, output_tokens=10),
            ),
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:00:01Z",
                msg_id="msg-1",
                model="claude-opus-5",
                usage=make_usage(input_tokens=100, output_tokens=75),
            ),
            assistant_record(
                uuid="u3",
                timestamp="2026-11-25T10:00:02Z",
                msg_id="msg-1",
                model="claude-opus-5",
                usage=make_usage(input_tokens=100, output_tokens=30),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T09:59:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {},
        None,
    )
    coord = report["roles"]["coordinator"]
    assert coord["records"] == 3
    assert coord["messages"] == 1
    assert coord["tokens"] == {
        "input": 100,
        "cache_write": 0,
        "cache_read": 0,
        "output": 75,
        "total": 175,
    }
    assert coord["models"]["claude-opus-5"]["messages"] == 1
    assert coord["models"]["claude-opus-5"]["output"] == 75


def test_dedup_across_files_earliest_timestamp_and_session_tiebreak(
    coord_meter: Any, tmp_path: Path
) -> None:
    """Spec rule (What is counted #1 & #3): One count per message.id across files; earliest first-record timestamp wins, ties break by earlier --session."""
    s1 = "sess-a"
    s2 = "sess-b"
    write_session(
        tmp_path,
        s1,
        [
            assistant_record(
                uuid="u-coord-later",
                timestamp="2026-11-25T10:05:00Z",
                msg_id="msg-shared",
                model="claude-opus-5",
                usage=make_usage(input_tokens=500, output_tokens=500),
            ),
            assistant_record(
                uuid="u-tie",
                timestamp="2026-11-25T10:10:00Z",
                msg_id="msg-tie",
                model="claude-opus-5",
                usage=make_usage(input_tokens=20, output_tokens=10),
            ),
        ],
    )
    write_session(
        tmp_path,
        s2,
        [
            assistant_record(
                uuid="u-tie",
                timestamp="2026-11-25T10:10:00Z",
                msg_id="msg-tie",
                model="claude-opus-5",
                usage=make_usage(input_tokens=20, output_tokens=10),
            ),
        ],
    )
    write_subagent(
        tmp_path,
        s1,
        "ag1",
        [
            assistant_record(
                uuid="u-sub-early",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="msg-shared",
                model="claude-opus-5",
                usage=make_usage(input_tokens=80, output_tokens=40),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [s1, s2],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:15:00Z"),
        {"worker": "ag1"},
        None,
    )
    assert report["roles"]["worker"]["messages"] == 1
    assert report["roles"]["worker"]["tokens"]["output"] == 40
    assert report["roles"]["coordinator"]["messages"] == 1
    assert report["roles"]["coordinator"]["tokens"] == {
        "input": 20,
        "cache_write": 0,
        "cache_read": 0,
        "output": 10,
        "total": 30,
    }
    assert report["total"]["messages"] == 2


def test_iterations_fallback_when_toplevel_zero(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (What is counted #2): usage.iterations summed per field when non-empty while top-level fields are zero."""
    sid = "sess-iter"
    pricing_path = write_pricing(
        tmp_path / "pricing.toml",
        models={"claude-opus-5": {"input": 5.0, "output": 25.0}},
    )
    pricing = tomllib.loads(pricing_path.read_text(encoding="utf-8"))

    iter1 = make_usage(
        input_tokens=400_000,
        output_tokens=100_000,
        cache_read_input_tokens=200_000,
        cache_creation_input_tokens=500_000,
        ephemeral_5m=300_000,
        ephemeral_1h=200_000,
    )
    iter2 = make_usage(
        input_tokens=600_000,
        output_tokens=100_000,
        cache_read_input_tokens=800_000,
        cache_creation_input_tokens=500_000,
        ephemeral_5m=500_000,
        ephemeral_1h=0,
    )
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u-iter",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="msg-iter",
                model="claude-opus-5",
                usage=make_usage(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    iterations=[iter1, iter2],
                ),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T09:59:00Z"),
        _dt("2026-11-25T10:01:00Z"),
        {},
        pricing,
    )
    coord = report["roles"]["coordinator"]
    assert coord["tokens"] == {
        "input": 1_000_000,
        "cache_write": 1_000_000,
        "cache_read": 1_000_000,
        "output": 200_000,
        "total": 3_200_000,
    }
    # Expected USD: (1M*5 + 0.2M*25 + 0.8M*5*1.25 + 0.2M*5*2.0 + 1M*5*0.1) / 1M
    # = 5.0 + 5.0 + 5.0 + 2.0 + 0.5 = 17.5
    assert coord["usd"] == pytest.approx(17.5, abs=1e-9)


def test_cache_write_split_and_remainder_pricing_via_cli(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (What is counted #2 & #6): cache_write 5m/1h split, unattributed remainder priced as 5m, and model cache_read_multiplier override."""
    sid = "sess-cache"
    pricing_path = write_pricing(
        tmp_path / "custom_pricing.toml",
        cache_multipliers={"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
        models={
            "claude-test-1": {"input": 4.0, "output": 20.0},
            "claude-custom-1": {"input": 10.0, "output": 50.0, "cache_read_multiplier": 0.05},
        },
    )
    write_session(
        tmp_path,
        sid,
        [
            # 1M cache_write: 300k 5m + 500k 1h + 200k remainder (-> 5m => 500k 5m, 500k 1h)
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                model="claude-test-1",
                usage=make_usage(
                    input_tokens=1_000_000,
                    output_tokens=500_000,
                    cache_read_input_tokens=2_000_000,
                    cache_creation_input_tokens=1_000_000,
                    ephemeral_5m=300_000,
                    ephemeral_1h=500_000,
                ),
            ),
            # No cache_creation dict -> entire 1M cache_write priced as 5m; cache_read uses 0.05 override
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m2",
                model="claude-custom-1",
                usage=make_usage(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_input_tokens=1_000_000,
                    cache_creation_input_tokens=1_000_000,
                ),
            ),
        ],
    )

    rc = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            sid,
            "--start",
            "2026-11-25T10:00:00Z",
            "--end",
            "2026-11-25T10:05:00Z",
            "--pricing",
            str(pricing_path),
        ],
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    models = report["roles"]["coordinator"]["models"]
    # m1: (1M*4 + 0.5M*20 + 0.5M*4*1.25 + 0.5M*4*2.0 + 2M*4*0.1) / 1M = 4 + 10 + 2.5 + 4 + 0.8 = 21.3
    assert models["claude-test-1"]["usd"] == pytest.approx(21.3, abs=1e-9)
    # m2: (1M*10*1.25 + 1M*10*0.05) / 1M = 12.5 + 0.5 = 13.0
    assert models["claude-custom-1"]["usd"] == pytest.approx(13.0, abs=1e-9)
    assert report["roles"]["coordinator"]["usd"] == pytest.approx(34.3, abs=1e-9)
    assert report["total"]["usd"] == pytest.approx(34.3, abs=1e-9)


def test_longest_prefix_model_matching_and_boundary(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (What is counted #6): Longest [models] key matching exact id or prefix at '-' boundary (claude-opus-5-5 uses claude-opus-5; non-boundary does not match)."""
    sid = "sess-prefix"
    pricing_path = write_pricing(
        tmp_path / "pricing.toml",
        models={
            "claude-opus": {"input": 100.0, "output": 100.0},
            "claude-opus-5": {"input": 5.0, "output": 25.0},
        },
    )
    pricing = tomllib.loads(pricing_path.read_text(encoding="utf-8"))

    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                model="claude-opus-5-5",
                usage=make_usage(input_tokens=1_000_000, output_tokens=0),
            ),
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m2",
                model="claude-opus-50",
                usage=make_usage(input_tokens=1_000_000, output_tokens=0),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {},
        pricing,
    )
    models = report["roles"]["coordinator"]["models"]
    # claude-opus-5-5 -> claude-opus-5 ($5/M)
    assert models["claude-opus-5-5"]["usd"] == pytest.approx(5.0, abs=1e-9)
    # claude-opus-50 does NOT match claude-opus-5 (no '-' boundary), falls back to claude-opus ($100/M)
    assert models["claude-opus-50"]["usd"] == pytest.approx(100.0, abs=1e-9)


def test_unpriced_model_null_propagation(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (What is counted #6): Unpriced model yields usd null for model, containing role, and total; never 0 or partial sum."""
    sid = "sess-unpriced"
    pricing_path = write_pricing(
        tmp_path / "pricing.toml",
        models={"claude-opus-5": {"input": 5.0, "output": 25.0}},
    )
    pricing = tomllib.loads(pricing_path.read_text(encoding="utf-8"))

    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                model="claude-opus-5",
                usage=make_usage(input_tokens=1_000_000, output_tokens=0),
            ),
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m2",
                model="mystery-unpriced-9",
                usage=make_usage(input_tokens=1_000_000, output_tokens=0),
            ),
        ],
    )
    write_subagent(
        tmp_path,
        sid,
        "ag-priced",
        [
            assistant_record(
                uuid="u3",
                timestamp="2026-11-25T10:02:00Z",
                msg_id="m3",
                model="claude-opus-5",
                usage=make_usage(input_tokens=1_000_000, output_tokens=0),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {"helper": "ag-priced"},
        pricing,
    )
    coord = report["roles"]["coordinator"]
    assert coord["models"]["claude-opus-5"]["usd"] == pytest.approx(5.0, abs=1e-9)
    assert coord["models"]["mystery-unpriced-9"]["usd"] is None
    assert coord["usd"] is None
    assert report["roles"]["helper"]["usd"] == pytest.approx(5.0, abs=1e-9)
    assert report["total"]["usd"] is None


def test_pricing_null_unknown_and_missing_fields_unpriced(
    coord_meter: Any, tmp_path: Path
) -> None:
    """Spec rule (What is counted #6): Pricing values 'null', 'unknown', or absent input/output treat the model as unpriced."""
    sid = "sess-bad-price"
    pricing_path = write_pricing(
        tmp_path / "pricing.toml",
        models={
            "model-str-null": {"input": "null", "output": 10.0},
            "model-str-unknown": {"input": 5.0, "output": "unknown"},
            "model-no-output": {"input": 5.0},
        },
    )
    pricing = tomllib.loads(pricing_path.read_text(encoding="utf-8"))

    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                model="model-str-null",
                usage=make_usage(input_tokens=100, output_tokens=10),
            ),
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m2",
                model="model-str-unknown",
                usage=make_usage(input_tokens=100, output_tokens=10),
            ),
            assistant_record(
                uuid="u3",
                timestamp="2026-11-25T10:02:00Z",
                msg_id="m3",
                model="model-no-output",
                usage=make_usage(input_tokens=100, output_tokens=10),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {},
        pricing,
    )
    models = report["roles"]["coordinator"]["models"]
    for name in ("model-str-null", "model-str-unknown", "model-no-output"):
        assert models[name]["usd"] is None
    assert report["roles"]["coordinator"]["usd"] is None
    assert report["total"]["usd"] is None


def test_synthetic_and_all_zero_messages_ignored(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (What is counted #5): Model <synthetic> and messages whose four counts are all zero are not counted or listed under models."""
    sid = "sess-ignore"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m-synth",
                model="<synthetic>",
                usage=make_usage(input_tokens=999, output_tokens=999),
            ),
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m-zero",
                model="claude-opus-5",
                usage=make_usage(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            ),
            assistant_record(
                uuid="u3",
                timestamp="2026-11-25T10:02:00Z",
                msg_id="m-real",
                model="claude-sonnet-5",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {},
        None,
    )
    coord = report["roles"]["coordinator"]
    assert coord["records"] == 3
    assert coord["messages"] == 1
    assert set(coord["models"].keys()) == {"claude-sonnet-5"}
    assert coord["tokens"]["total"] == 15


def test_window_edges_inclusive(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (What is counted #4): Window [start, end] is inclusive at both boundaries; records outside are dropped."""
    sid = "sess-window"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u-before",
                timestamp="2026-11-25T09:59:59Z",
                msg_id="m-before",
                model="claude-opus-5",
                usage=make_usage(input_tokens=100, output_tokens=100),
            ),
            assistant_record(
                uuid="u-start",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m-start",
                model="claude-opus-5",
                usage=make_usage(input_tokens=10, output_tokens=1),
            ),
            assistant_record(
                uuid="u-end",
                timestamp="2026-11-25T10:10:00Z",
                msg_id="m-end",
                model="claude-opus-5",
                usage=make_usage(input_tokens=20, output_tokens=2),
            ),
            assistant_record(
                uuid="u-after",
                timestamp="2026-11-25T10:10:01Z",
                msg_id="m-after",
                model="claude-opus-5",
                usage=make_usage(input_tokens=200, output_tokens=200),
            ),
        ],
    )

    rc = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            sid,
            "--start",
            "2026-11-25T10:00:00Z",
            "--end",
            "2026-11-25T10:10:00Z",
        ],
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    coord = report["roles"]["coordinator"]
    assert coord["records"] == 2
    assert coord["messages"] == 2
    assert coord["tokens"] == {
        "input": 30,
        "cache_write": 0,
        "cache_read": 0,
        "output": 3,
        "total": 33,
    }


def test_naive_start_or_end_rejected_exit_2(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (Command line): Naive --start or --end ISO timestamps are rejected with exit code 2 and empty stdout."""
    sid = "sess-naive"
    write_session(tmp_path, sid, [])

    rc_start = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            sid,
            "--start",
            "2026-11-25T10:00:00",
            "--end",
            "2026-11-25T10:10:00Z",
        ],
    )
    cap1 = capsys.readouterr()
    assert rc_start == 2
    assert cap1.out == ""
    assert cap1.err.strip() != ""

    rc_end = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            sid,
            "--start",
            "2026-11-25T10:00:00Z",
            "--end",
            "2026-11-25T10:10:00",
        ],
    )
    cap2 = capsys.readouterr()
    assert rc_end == 2
    assert cap2.out == ""
    assert cap2.err.strip() != ""


def test_resumed_pair_additive_and_order_invariant(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (What is counted #3): Resumed [child, parent] equals parent-only plus child-only-new, counts replayed messages once, and [parent, child] gives identical totals."""
    parent = "sess-parent"
    child = "sess-child"

    parent_records = [
        user_record(
            uuid="u-p-user",
            timestamp="2026-11-25T10:00:00Z",
            content="start task",
            session_id=parent,
        ),
        assistant_record(
            uuid="u-p-m1-a",
            timestamp="2026-11-25T10:00:30Z",
            msg_id="m1",
            model="claude-opus-5",
            usage=make_usage(input_tokens=100, output_tokens=20),
            session_id=parent,
        ),
        assistant_record(
            uuid="u-p-m1-b",
            timestamp="2026-11-25T10:00:31Z",
            msg_id="m1",
            model="claude-opus-5",
            usage=make_usage(input_tokens=100, output_tokens=40),
            session_id=parent,
        ),
    ]
    child_new_records = [
        user_record(
            uuid="u-c-user",
            timestamp="2026-11-25T10:02:00Z",
            content="follow up",
            session_id=child,
        ),
        assistant_record(
            uuid="u-c-m2",
            timestamp="2026-11-25T10:02:30Z",
            msg_id="m2",
            model="claude-opus-5",
            usage=make_usage(input_tokens=50, output_tokens=15),
            session_id=child,
        ),
    ]

    proj_combined = tmp_path / "combined"
    write_session(proj_combined, parent, parent_records)
    write_session(
        proj_combined,
        child,
        clone_for_resume(parent_records, child_session_id=child) + child_new_records,
    )

    proj_child_new = tmp_path / "child_new_only"
    write_session(proj_child_new, "child-new", child_new_records)

    start = _dt("2026-11-25T09:59:00Z")
    end = _dt("2026-11-25T10:10:00Z")

    rep_cp = coord_meter.meter(proj_combined, [child, parent], start, end, {}, None)
    rep_pc = coord_meter.meter(proj_combined, [parent, child], start, end, {}, None)
    rep_parent = coord_meter.meter(proj_combined, [parent], start, end, {}, None)
    rep_child_new = coord_meter.meter(proj_child_new, ["child-new"], start, end, {}, None)

    c_cp = rep_cp["roles"]["coordinator"]
    c_pc = rep_pc["roles"]["coordinator"]
    c_p = rep_parent["roles"]["coordinator"]
    c_cn = rep_child_new["roles"]["coordinator"]

    assert c_cp == c_pc
    assert rep_cp["total"] == rep_pc["total"]

    assert c_cp["records"] == c_p["records"] + c_cn["records"] == 5
    assert c_cp["messages"] == c_p["messages"] + c_cn["messages"] == 2
    assert c_cp["human_messages"] == c_p["human_messages"] + c_cn["human_messages"] == 2
    for k in ("input", "cache_write", "cache_read", "output", "total"):
        assert c_cp["tokens"][k] == c_p["tokens"][k] + c_cn["tokens"][k]
    assert c_cp["usd"] == pytest.approx(c_p["usd"] + c_cn["usd"], abs=1e-12)


def test_role_assignment_unassigned_and_empty_named_role(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (Command line & Report): --role maps subagents; empty named role appears with zeros and usd 0.0; unassigned appears only when an unmapped subagent file exists."""
    sid = "sess-roles"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u-c",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m-c",
                model="claude-opus-5",
                usage=make_usage(input_tokens=100, output_tokens=50),
            ),
        ],
    )
    write_subagent(
        tmp_path,
        sid,
        "ag-coder",
        [
            assistant_record(
                uuid="u-s1",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m-s1",
                model="claude-opus-5",
                usage=make_usage(input_tokens=40, output_tokens=10),
            ),
        ],
    )

    rc = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            sid,
            "--start",
            "2026-11-25T10:00:00Z",
            "--end",
            "2026-11-25T10:05:00Z",
            "--role",
            "coder=ag-coder",
            "--role",
            "ghost=ag-missing",
        ],
    )
    assert rc == 0
    rep1 = json.loads(capsys.readouterr().out)
    assert set(rep1["roles"].keys()) == {"coordinator", "coder", "ghost"}
    assert rep1["roles"]["ghost"] == {
        "records": 0,
        "messages": 0,
        "models": {},
        "tokens": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "total": 0},
        "usd": 0.0,
        "wall_minutes": 0.0,
        "active_minutes": 0.0,
        "human_messages": 0,
        "share_of_claude_tokens": 0.0,
    }

    # Add an unmapped subagent file -> unassigned must now appear
    write_subagent(
        tmp_path,
        sid,
        "ag-stray",
        [
            assistant_record(
                uuid="u-s2",
                timestamp="2026-11-25T10:02:00Z",
                msg_id="m-s2",
                model="claude-opus-5",
                usage=make_usage(input_tokens=20, output_tokens=5),
            ),
        ],
    )
    rep2 = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {"coder": "ag-coder", "ghost": "ag-missing"},
        None,
    )
    assert "unassigned" in rep2["roles"]
    assert rep2["roles"]["unassigned"]["messages"] == 1
    assert rep2["roles"]["unassigned"]["tokens"]["total"] == 25


def test_missing_session_file_exits_2_empty_stdout(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (Command line): Missing session file returns exit code 2, writes message to stderr, and leaves stdout empty."""
    rc = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            "missing-session",
            "--start",
            "2026-11-25T10:00:00Z",
            "--end",
            "2026-11-25T10:05:00Z",
        ],
    )
    cap = capsys.readouterr()
    assert rc == 2
    assert cap.out == ""
    assert cap.err.strip() != ""


def test_wall_and_active_minutes_five_minute_threshold(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (Minutes and attention): wall_minutes spans first to last own record (0.0 if <2); active_minutes sums sorted gaps <= 5 minutes (300s counts, 301s does not)."""
    sid = "sess-mins"
    # Intentionally unsorted in file to verify timestamp sorting
    write_session(
        tmp_path,
        sid,
        [
            user_record(uuid="u3", timestamp="2026-11-25T10:10:01Z", content="step 3"),
            user_record(uuid="u1", timestamp="2026-11-25T10:00:00Z", content="step 1"),
            user_record(uuid="u4", timestamp="2026-11-25T10:12:01Z", content="step 4"),
            user_record(uuid="u2", timestamp="2026-11-25T10:05:00Z", content="step 2"),
        ],
    )
    write_subagent(
        tmp_path,
        sid,
        "ag-single",
        [
            assistant_record(
                uuid="u-one",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m-one",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:20:00Z"),
        {"solo": "ag-single"},
        None,
    )
    coord = report["roles"]["coordinator"]
    # Gaps: 5m00s (counts: 5.0), 5m01s (excluded: 0.0), 2m00s (counts: 2.0) => active=7.0
    # Wall: 10:12:01 - 10:00:00 = 721s = 12.016666... min
    assert coord["wall_minutes"] == pytest.approx(721 / 60.0, abs=1e-9)
    assert coord["active_minutes"] == pytest.approx(7.0, abs=1e-9)

    solo = report["roles"]["solo"]
    assert solo["records"] == 1
    assert solo["wall_minutes"] == 0.0
    assert solo["active_minutes"] == 0.0


def test_copies_excluded_from_own_records_and_minutes(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (Minutes and attention & Rule 3): Duplicate uuid records seen in an earlier-owner file are copies and are not counted as own records for minutes."""
    sid = "sess-copies"
    write_session(tmp_path, sid, [])
    write_subagent(
        tmp_path,
        sid,
        "ag-alpha",
        [
            assistant_record(
                uuid="u-dup",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m-dup",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
            user_record(uuid="u-alpha-2", timestamp="2026-11-25T10:01:00Z", content="alpha"),
        ],
    )
    # ag-beta has u-dup at the same timestamp (tie-broken by filename: agent-ag-alpha < agent-ag-beta),
    # so u-dup in ag-beta is a copy; ag-beta only has 1 own record (u-beta-own).
    write_subagent(
        tmp_path,
        sid,
        "ag-beta",
        [
            assistant_record(
                uuid="u-dup",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m-dup",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
            user_record(uuid="u-beta-own", timestamp="2026-11-25T10:04:00Z", content="beta"),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:10:00Z"),
        {"alpha": "ag-alpha", "beta": "ag-beta"},
        None,
    )
    alpha = report["roles"]["alpha"]
    beta = report["roles"]["beta"]
    assert alpha["records"] == 2
    assert alpha["wall_minutes"] == pytest.approx(1.0, abs=1e-9)
    assert alpha["active_minutes"] == pytest.approx(1.0, abs=1e-9)

    assert beta["records"] == 1
    assert beta["messages"] == 0
    assert beta["wall_minutes"] == 0.0
    assert beta["active_minutes"] == 0.0


def test_human_messages_filtering(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (Minutes and attention): human_messages counts non-subagent external non-meta non-compact non-sidechain user records with string or text-only content not starting with '<'."""
    sid = "sess-human"
    write_session(
        tmp_path,
        sid,
        [
            # Counted: plain string, external
            user_record(
                uuid="u-str",
                timestamp="2026-11-25T10:00:00Z",
                content="please fix the bug",
                user_type="external",
            ),
            # Counted: list of text blocks only, external
            user_record(
                uuid="u-text-blocks",
                timestamp="2026-11-25T10:00:10Z",
                content=[{"type": "text", "text": "part 1"}, {"type": "text", "text": "part 2"}],
                user_type="external",
            ),
            # Excluded: tool_result
            tool_result_record(
                uuid="u-tool-res",
                timestamp="2026-11-25T10:00:20Z",
                content="output",
                user_type="external",
            ),
            # Excluded: mixed text + tool_result
            user_record(
                uuid="u-mixed-tool",
                timestamp="2026-11-25T10:00:25Z",
                content=[
                    {"type": "text", "text": "hi"},
                    {"type": "tool_result", "tool_use_id": "t1", "content": "x"},
                ],
                user_type="external",
            ),
            # Excluded: isMeta
            meta_record(
                uuid="u-meta",
                timestamp="2026-11-25T10:00:30Z",
                content="meta prompt",
                user_type="external",
            ),
            # Excluded: isCompactSummary
            user_record(
                uuid="u-compact",
                timestamp="2026-11-25T10:00:40Z",
                content="summary",
                user_type="external",
                is_compact_summary=True,
            ),
            # Excluded: isSidechain
            user_record(
                uuid="u-side",
                timestamp="2026-11-25T10:00:50Z",
                content="sidechain prompt",
                user_type="external",
                is_sidechain=True,
            ),
            # Excluded: trimmed string starts with '<'
            user_record(
                uuid="u-xml-str",
                timestamp="2026-11-25T10:01:00Z",
                content="   <local-command-stdout>ok</local-command-stdout>",
                user_type="external",
            ),
            # Excluded: text block starts with '<' after trimming
            user_record(
                uuid="u-xml-block",
                timestamp="2026-11-25T10:01:10Z",
                content=[{"type": "text", "text": "\n<system-reminder>x</system-reminder>"}],
                user_type="external",
            ),
            # Excluded: non-external userType
            user_record(
                uuid="u-internal",
                timestamp="2026-11-25T10:01:20Z",
                content="internal user msg",
                user_type="internal",
            ),
        ],
    )
    write_subagent(
        tmp_path,
        sid,
        "ag-worker",
        [
            user_record(
                uuid="u-sub-user",
                timestamp="2026-11-25T10:01:30Z",
                content="subagent prompt",
                user_type="external",
                is_sidechain=True,
                agent_id="ag-worker",
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {"worker": "ag-worker"},
        None,
    )
    assert report["roles"]["coordinator"]["human_messages"] == 2
    assert report["roles"]["worker"]["human_messages"] == 0


def test_share_of_claude_tokens_sums_to_one_and_zero_when_empty(
    coord_meter: Any, tmp_path: Path
) -> None:
    """Spec rule (Report): share_of_claude_tokens is role total / grand total, sums to 1.0 across roles, and is 0.0 when grand total is 0."""
    sid_nonzero = "sess-share"
    proj_a = tmp_path / "nonzero"
    write_session(
        proj_a,
        sid_nonzero,
        [
            assistant_record(
                uuid="u-c",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m-c",
                usage=make_usage(input_tokens=250, output_tokens=50),
            ),
        ],
    )
    write_subagent(
        proj_a,
        sid_nonzero,
        "ag-w",
        [
            assistant_record(
                uuid="u-w",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m-w",
                usage=make_usage(input_tokens=80, output_tokens=20),
            ),
        ],
    )

    rep_a = coord_meter.meter(
        proj_a,
        [sid_nonzero],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {"worker": "ag-w"},
        None,
    )
    assert rep_a["roles"]["coordinator"]["share_of_claude_tokens"] == pytest.approx(0.75, abs=1e-9)
    assert rep_a["roles"]["worker"]["share_of_claude_tokens"] == pytest.approx(0.25, abs=1e-9)
    assert sum(r["share_of_claude_tokens"] for r in rep_a["roles"].values()) == pytest.approx(
        1.0, abs=1e-9
    )

    proj_zero = tmp_path / "zero"
    sid_zero = "sess-empty"
    write_session(proj_zero, sid_zero, [])
    rep_z = coord_meter.meter(
        proj_zero,
        [sid_zero],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {"worker": "ag-missing"},
        None,
    )
    assert rep_z["total"]["tokens"]["total"] == 0
    for role_data in rep_z["roles"].values():
        assert role_data["share_of_claude_tokens"] == 0.0


def test_total_block_equals_sum_of_roles(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (Report): Top-level total block equals the sum of tokens, messages, and usd across all roles."""
    sid = "sess-sum"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                model="claude-opus-5",
                usage=make_usage(
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_input_tokens=300,
                    cache_creation_input_tokens=50,
                ),
            ),
        ],
    )
    write_subagent(
        tmp_path,
        sid,
        "ag-rev",
        [
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m2",
                model="claude-sonnet-5",
                usage=make_usage(input_tokens=40, output_tokens=10, cache_read_input_tokens=20),
            ),
        ],
    )
    write_subagent(
        tmp_path,
        sid,
        "ag-unmapped",
        [
            assistant_record(
                uuid="u3",
                timestamp="2026-11-25T10:02:00Z",
                msg_id="m3",
                model="claude-haiku-4-5",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {"reviewer": "ag-rev"},
        None,
    )
    roles = report["roles"].values()
    assert report["total"]["messages"] == sum(r["messages"] for r in roles)
    for key in ("input", "cache_write", "cache_read", "output", "total"):
        assert report["total"]["tokens"][key] == sum(r["tokens"][key] for r in roles)
    assert report["total"]["usd"] == pytest.approx(sum(r["usd"] for r in roles), abs=1e-12)


def test_out_flag_writes_file_and_leaves_stdout_empty(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (Command line): --out writes the JSON report to FILE and leaves stdout empty; omitting --out prints JSON to stdout."""
    sid = "sess-out"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
        ],
    )
    base_argv = [
        "--projects-dir",
        str(tmp_path),
        "--session",
        sid,
        "--start",
        "2026-11-25T10:00:00Z",
        "--end",
        "2026-11-25T10:05:00Z",
    ]

    rc_stdout = _run_main(coord_meter, base_argv)
    assert rc_stdout == 0
    stdout_report = json.loads(capsys.readouterr().out)

    out_file = tmp_path / "out" / "report.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    rc_file = _run_main(coord_meter, [*base_argv, "--out", str(out_file)])
    cap = capsys.readouterr()
    assert rc_file == 0
    assert cap.out == ""
    assert out_file.is_file()
    file_report = json.loads(out_file.read_text(encoding="utf-8"))
    assert file_report == stdout_report


def test_report_exact_schema_keys(
    coord_meter: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec rule (Report): Report JSON has the exact keys specified for top-level, window, roles, models, tokens, and total."""
    sid = "sess-schema"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                model="claude-opus-5",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
        ],
    )
    rc = _run_main(
        coord_meter,
        [
            "--projects-dir",
            str(tmp_path),
            "--session",
            sid,
            "--start",
            "2026-11-25T10:00:00Z",
            "--end",
            "2026-11-25T10:05:00Z",
        ],
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)

    assert set(report.keys()) == {"window", "sessions", "roles", "total"}
    assert set(report["window"].keys()) == {"start", "end"}
    assert report["window"]["start"].endswith("Z")
    assert report["window"]["end"].endswith("Z")
    assert report["sessions"] == [sid]

    coord = report["roles"]["coordinator"]
    assert set(coord.keys()) == {
        "records",
        "messages",
        "models",
        "tokens",
        "usd",
        "wall_minutes",
        "active_minutes",
        "human_messages",
        "share_of_claude_tokens",
    }
    assert set(coord["tokens"].keys()) == {"input", "cache_write", "cache_read", "output", "total"}
    assert set(coord["models"]["claude-opus-5"].keys()) == {
        "input",
        "cache_write",
        "cache_read",
        "output",
        "total",
        "messages",
        "usd",
    }
    assert set(report["total"].keys()) == {"tokens", "usd", "messages"}
    assert set(report["total"]["tokens"].keys()) == {
        "input",
        "cache_write",
        "cache_read",
        "output",
        "total",
    }


def test_invalid_json_lines_skipped(coord_meter: Any, tmp_path: Path) -> None:
    """Spec rule (Input layout): Non-JSON lines in a transcript file are skipped without error."""
    sid = "sess-badline"
    write_session(
        tmp_path,
        sid,
        [
            assistant_record(
                uuid="u1",
                timestamp="2026-11-25T10:00:00Z",
                msg_id="m1",
                usage=make_usage(input_tokens=10, output_tokens=5),
            ),
            "{broken json line",
            "",
            assistant_record(
                uuid="u2",
                timestamp="2026-11-25T10:01:00Z",
                msg_id="m2",
                usage=make_usage(input_tokens=20, output_tokens=10),
            ),
        ],
    )

    report = coord_meter.meter(
        tmp_path,
        [sid],
        _dt("2026-11-25T10:00:00Z"),
        _dt("2026-11-25T10:05:00Z"),
        {},
        None,
    )
    coord = report["roles"]["coordinator"]
    assert coord["records"] == 2
    assert coord["messages"] == 2
    assert coord["tokens"]["total"] == 45
