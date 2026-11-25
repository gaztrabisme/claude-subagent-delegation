"""G1b: the delegation row's `tests` tolerates the real dict shape of rounds[*].tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from subagent import report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rounds.jsonl"

CONFIG = """\
[providers.glm]
driver = "claude"
model = "glm-5.3-flash[1m]"
vendor = "zai"
"""

REAL = {"passed": True, "cmd": "uv run pytest -q",
        "counts": {"total": 1731, "passed": 1730, "failed": 0, "skipped": 1}}


@pytest.fixture(autouse=True)
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(cfg))


def run_rec(rid: str) -> dict:
    return {
        "schema": 3, "ts": 1789700100.0, "kind": "run", "run_id": rid, "agent_id": "a-" + rid,
        "session_id": "s", "model": "m", "lane": "glm", "provider": "glm",
        "driver": "claude", "state": "completed", "verification_passed": True,
        "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
    }


def deleg(tests_per_round: list[Any]) -> list[dict]:
    ids = [f"r{i}" for i in range(len(tests_per_round))]
    d = {
        "schema": 3, "ts": 1789700400.0, "kind": "delegation", "delegation_id": "d1",
        "bench_run_id": "b", "orchestrator": {"harness": "cli", "model": "x"},
        "mode": "auto", "status": "done", "stopped_because": None,
        "rounds": [{"run_id": i, "provider": "glm", "tests": t}
                   for i, t in zip(ids, tests_per_round, strict=True)],
        "reviews": [],
    }
    return [run_rec(i) for i in ids] + [d]


def build(tmp_path: Path, records: list[dict], capsys) -> list[dict]:
    trace = tmp_path / "t.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    assert report.main(["--trace", str(trace), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["delegations"]


def test_real_dict_shape_sums_counts_total(tmp_path: Path, capsys) -> None:
    rows = build(tmp_path, deleg([dict(REAL)]), capsys)
    assert len(rows) == 1
    assert rows[0]["tests"] == 1731
    assert isinstance(rows[0]["tests"], int)


def test_mixed_shapes(tmp_path: Path, capsys) -> None:
    shapes = [
        5,
        {"passed": True, "counts": {"total": 10}},
        None,
        {"passed": True, "cmd": "x"},
        {"passed": True, "counts": {"total": "12"}},
        True,
    ]
    rows = build(tmp_path, deleg(shapes), capsys)
    assert rows[0]["tests"] == 15
    assert isinstance(rows[0]["tests"], int)


def test_old_int_shape_alone(tmp_path: Path, capsys) -> None:
    rows = build(tmp_path, deleg([7, 3]), capsys)
    assert rows[0]["tests"] == 10


def test_fixture_file_real_shape(tmp_path: Path, capsys) -> None:
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    assert len(records) == 1 and len(records[0]["rounds"]) == 2
    records += [run_rec("f1"), run_rec("f2")]
    rows = build(tmp_path, records, capsys)
    assert rows[0]["tests"] == 3462
