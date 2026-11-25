"""E1-7 and E1-8: record shapes, schema version, and the repository rules."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from subagent.telemetry.trace import SCHEMA

from ..test_trace_schema import REQUIRED
from ._helpers import kinds, seed_percent, seed_runs
from .test_override import SENTINEL, call, server  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]
# A record's own "kind" is its record kind, so the decided rule travels as "rule".
DECISION = {"level", "lane", "rule", "used", "limit", "unit", "reason"}
BASE = {"schema", "ts", "kind"}


def test_schema_version_is_unchanged():
    assert SCHEMA == 3


def test_existing_record_kinds_keep_their_required_fields():
    assert {"run", "hop", "turn", "sample", "run_summary", "verdict", "calibration",
            "delegation"} <= set(REQUIRED)
    assert "code" in REQUIRED["hop"]


def test_hop_record_with_envelope_code_keeps_the_hop_shape(make_env):
    env = make_env(seed=lambda s: seed_percent(s, "codex", 93.0))
    env.run("codex")
    hop = next(r for r in env.trace() if r["kind"] == "hop" and r["lane"] == "codex")
    assert (REQUIRED["hop"] | BASE) <= set(hop)
    assert hop["code"] == "envelope" and "93" in (hop.get("message") or json.dumps(hop))


def test_envelope_warning_fields(make_env):
    env = make_env(("deepseek", "glm"))
    seed_runs(env.tmp_path, "deepseek", 1, age=60.0, usd=6.0)
    run = env.run("deepseek")
    (rec,) = kinds(env.trace(), "envelope_warning")
    assert (DECISION | {"run_id"} | BASE) <= set(rec)
    assert rec["run_id"] == run.run_id and rec["level"] == "warn"
    assert rec["lane"] == "deepseek" and rec["limit"] == 10.0 and rec["rule"] == "usd_month"


def test_envelope_no_reading_fields(make_env):
    env = make_env()
    run = env.run("codex")
    (rec,) = kinds(env.trace(), "envelope_no_reading")
    assert ({"lane", "missing", "run_id"} | BASE) <= set(rec)
    assert rec["run_id"] == run.run_id and rec["lane"] == "codex"
    assert "used_percent" in rec["missing"]


def test_envelope_config_warning_fields(make_env, tmp_path: Path):
    env = make_env(plan=tmp_path / "absent.md")
    run = env.run("codex")
    (rec,) = kinds(env.trace(), "envelope_config_warning")
    assert ({"run_id", "message"} | BASE) <= set(rec)
    assert rec["run_id"] == run.run_id and isinstance(rec["message"], str) and rec["message"]


def test_envelope_override_fields(server, tmp_path: Path):  # noqa: F811
    from ._helpers import read_trace

    call(server, tmp_path, override_envelope=SENTINEL)
    (rec,) = kinds(read_trace(tmp_path), "envelope_override")
    assert (DECISION | {"entry", "override_reason", "run_id"} | BASE) <= set(rec)
    assert rec["override_reason"] == SENTINEL and rec["entry"] == "mcp"


def test_no_machine_specific_values_under_src():
    pattern = re.compile(r"100\.106|192\.168|Tailscale\.app|\.omlx/settings|/Users/")
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
            if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_envelope_module_hardcodes_no_plan_path():
    path = ROOT / "src" / "subagent" / "envelope.py"
    if not path.exists():
        pytest.fail("src/subagent/envelope.py is missing")
    text = path.read_text(encoding="utf-8")
    for needle in ("workflow-config", "lane-plan.md", "src", "/Users/", "/home/"):
        assert needle not in text


def test_an_example_config_carries_a_commented_envelope_block():
    found = []
    for path in sorted((ROOT / "examples").glob("config.*.toml")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if any(re.match(r"#\s*\[envelope\]", ln) for ln in lines) and any(
            re.match(r"#\s*lane_plan\s*=", ln) for ln in lines
        ):
            found.append(path.name)
    assert found, "no examples/config.*.toml has a commented [envelope] block with lane_plan"
    for name in found:
        text = (ROOT / "examples" / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*\[envelope\]", text, re.M), "the block must stay commented"
