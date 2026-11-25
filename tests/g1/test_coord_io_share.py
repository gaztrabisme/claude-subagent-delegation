"""G1 part A: `coord_meter.py --io-share` adds input+output shares and a coordination block."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from tests.g1._synth import assistant, user, write_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coord_meter.py"
START = "2026-11-25T09:00:00Z"
END = "2026-11-25T11:00:00Z"
# role name -> agent id, in report order after the coordinator
ROLES = {"m-b": "ag-b", "m-c": "ag-c", "worker": "ag-w"}


def _load() -> Any:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    spec = importlib.util.spec_from_file_location("coord_meter_g1", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _transcripts(
    root: Path, coord: tuple[int, int, int, int] | None, subs: dict[str, tuple[int, int, int, int]]
) -> None:
    """coord and each sub: (input, output, cache_read, cache_write). None means no usage."""
    recs: list[dict[str, Any]] = [user("c-u", "2026-11-25T10:00:00Z")]
    if coord is not None:
        recs.append(assistant("c-a", "2026-11-25T10:00:01Z", "m-coord", *coord))
    write_jsonl(root / "sess.jsonl", recs)
    for agent, usage in subs.items():
        write_jsonl(
            root / "sess" / "subagents" / f"agent-{agent}.jsonl",
            [assistant(f"u-{agent}", "2026-11-25T10:01:00Z", f"m-{agent}", *usage, agent=agent)],
        )


def _argv(root: Path, out: Path, roles: dict[str, str], flag: bool) -> list[str]:
    argv = ["--projects-dir", str(root), "--session", "sess", "--start", START, "--end", END]
    for name, agent in roles.items():
        argv += ["--role", f"{name}={agent}"]
    argv += ["--out", str(out)]
    if flag:
        argv.append("--io-share")
    return argv


def _run(root: Path, tmp_path: Path, roles: dict[str, str], flag: bool = True) -> dict[str, Any]:
    out = tmp_path / ("with.json" if flag else "without.json")
    try:
        rc = _load().main(_argv(root, out, roles, flag))
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    assert rc == 0, "coord_meter exited non-zero (unknown flag?)"
    return json.loads(out.read_text(encoding="utf-8"))


MAIN_SUBS = {
    "ag-b": (50, 250, 400_000, 40_000),
    "ag-c": (20, 180, 100_000, 0),
    "ag-w": (500, 500, 1_000, 0),
}
MAIN_COORD = (100, 400, 90_000, 9_000)


def test_exact_io_numbers_and_coordination_block(tmp_path: Path) -> None:
    _transcripts(tmp_path, MAIN_COORD, MAIN_SUBS)
    rep = _run(tmp_path, tmp_path, ROLES)
    assert list(rep["roles"]) == ["coordinator", "m-b", "m-c", "worker"]
    io = {n: b["io_tokens"] for n, b in rep["roles"].items()}
    assert io == {"coordinator": 500, "m-b": 300, "m-c": 200, "worker": 1000}
    for block in rep["roles"].values():
        assert isinstance(block["io_tokens"], int)
        assert block["io_tokens"] == block["tokens"]["input"] + block["tokens"]["output"]
    shares = {n: b["share_of_claude_io_tokens"] for n, b in rep["roles"].items()}
    assert shares == pytest.approx(
        {"coordinator": 0.25, "m-b": 0.15, "m-c": 0.1, "worker": 0.5}, abs=1e-12
    )
    coordination = rep["coordination"]
    assert list(coordination) == ["roles", "io_tokens", "share_of_claude_io_tokens"]
    assert coordination["roles"] == ["coordinator", "m-b", "m-c"]
    assert coordination["io_tokens"] == 1000
    assert isinstance(coordination["io_tokens"], int)
    assert coordination["share_of_claude_io_tokens"] == pytest.approx(0.5, abs=1e-12)


def test_cache_never_enters_io_share(tmp_path: Path) -> None:
    _transcripts(tmp_path, MAIN_COORD, MAIN_SUBS)
    rep = _run(tmp_path, tmp_path, ROLES)
    coordination_total = sum(rep["roles"][n]["tokens"]["total"] for n in ("coordinator", "m-b", "m-c"))
    cache_inclusive = coordination_total / rep["total"]["tokens"]["total"]
    assert cache_inclusive > 0.99  # the cache-inclusive view would say ~99.7%
    assert rep["coordination"]["share_of_claude_io_tokens"] == pytest.approx(0.5, abs=1e-12)
    assert rep["roles"]["m-b"]["io_tokens"] == 300  # not 440_300


def test_default_output_unchanged_without_flag(tmp_path: Path) -> None:
    _transcripts(tmp_path, MAIN_COORD, MAIN_SUBS)
    without = tmp_path / "without.json"
    assert _load().main(_argv(tmp_path, without, ROLES, False)) == 0
    text = without.read_text(encoding="utf-8")
    expected = _load().meter(
        tmp_path,
        ["sess"],
        dt.datetime.fromisoformat(START.replace("Z", "+00:00")),
        dt.datetime.fromisoformat(END.replace("Z", "+00:00")),
        dict(ROLES),
        None,
    )
    assert text == json.dumps(expected, indent=2) + "\n"
    assert "io_tokens" not in text and "coordination" not in text


def test_flag_only_adds_fields(tmp_path: Path) -> None:
    _transcripts(tmp_path, MAIN_COORD, MAIN_SUBS)
    base = _run(tmp_path, tmp_path, ROLES, flag=False)
    rep = _run(tmp_path, tmp_path, ROLES, flag=True)
    assert "coordination" not in base
    rep.pop("coordination")
    for block in rep["roles"].values():
        block.pop("io_tokens")
        block.pop("share_of_claude_io_tokens")
    assert rep == base


def test_no_m_roles_coordination_is_coordinator_only(tmp_path: Path) -> None:
    _transcripts(tmp_path, (100, 400, 0, 0), {"ag-w": (500, 1000, 7, 7)})
    rep = _run(tmp_path, tmp_path, {"worker": "ag-w"})
    assert rep["coordination"] == {
        "roles": ["coordinator"],
        "io_tokens": 500,
        "share_of_claude_io_tokens": pytest.approx(500 / 2000),
    }
    assert list(rep["coordination"]) == ["roles", "io_tokens", "share_of_claude_io_tokens"]


def test_unassigned_role_counts_in_denominator_only(tmp_path: Path) -> None:
    _transcripts(tmp_path, (100, 100, 0, 0), {"ag-b": (50, 50, 0, 0), "ag-x": (300, 300, 0, 0)})
    rep = _run(tmp_path, tmp_path, {"m-b": "ag-b"})
    assert list(rep["roles"]) == ["coordinator", "m-b", "unassigned"]
    assert rep["roles"]["unassigned"]["io_tokens"] == 600
    assert rep["coordination"]["roles"] == ["coordinator", "m-b"]
    assert rep["coordination"]["io_tokens"] == 300
    assert rep["coordination"]["share_of_claude_io_tokens"] == pytest.approx(300 / 900)


def test_nothing_counted_gives_zero_not_division_error(tmp_path: Path) -> None:
    _transcripts(tmp_path, None, {})
    rep = _run(tmp_path, tmp_path, {})
    assert rep["roles"]["coordinator"]["io_tokens"] == 0
    assert rep["roles"]["coordinator"]["share_of_claude_io_tokens"] == 0.0
    assert rep["coordination"] == {
        "roles": ["coordinator"],
        "io_tokens": 0,
        "share_of_claude_io_tokens": 0.0,
    }
    assert isinstance(rep["coordination"]["share_of_claude_io_tokens"], float)
