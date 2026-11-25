"""E1-2: evaluate(rules, reading) -> Decision."""

from __future__ import annotations

import dataclasses
import math

import pytest

from ._helpers import evaluate, member, rules_of

LEVELS = {"ok", "warn", "refuse", "unknown"}


def full_codex(percent=10.0, week=0):
    return {"used_percent": percent, "runs_week": week}


@pytest.mark.parametrize(("percent", "level"), [
    (0, "ok"), (50, "ok"), (84.9, "ok"), (85, "refuse"), (93, "refuse"), (100, "refuse"),
])
def test_codex_percent_boundary(percent, level):
    assert evaluate(rules_of("Codex"), full_codex(percent)).level == level


@pytest.mark.parametrize(("usd", "level"), [
    (0, "ok"), (4.99, "ok"), (5, "warn"), (9.99, "warn"), (10, "refuse"), (55, "refuse"),
])
def test_deepseek_money_boundary(usd, level):
    assert evaluate(rules_of("DeepSeek"), {"month_usd": usd}).level == level


@pytest.mark.parametrize(("count", "level"), [
    (0, "ok"), (9, "ok"), (10, "refuse"), (11, "refuse"),
])
def test_gemini_runs_day_boundary(count, level):
    assert evaluate(rules_of("Gemini"), {"runs_day": count}).level == level


@pytest.mark.parametrize(("count", "level"), [(3, "ok"), (4, "refuse"), (5, "refuse")])
def test_codex_runs_week_boundary(count, level):
    assert evaluate(rules_of("Codex"), full_codex(10.0, count)).level == level


def test_worst_rule_decides_and_is_reported():
    d = evaluate(rules_of("Codex"), full_codex(10.0, 4))
    assert d.level == "refuse" and d.kind == "runs_week"
    assert d.used == 4 and d.limit == 4


def test_closed_lane_always_refuses_and_says_zero():
    d = evaluate(rules_of("glm"), {})
    assert d.level == "refuse" and d.kind == "closed" and d.limit == 0
    assert "0" in d.reason
    assert evaluate(rules_of("glm"), {"runs_day": 0, "month_usd": 0}).level == "refuse"


def test_absent_reading_is_unknown():
    d = evaluate(rules_of("Codex"), {})
    assert d.level == "unknown"


def test_unknown_outranks_ok_and_refuse_outranks_unknown():
    codex = rules_of("Codex")
    assert evaluate(codex, {"used_percent": 10.0}).level == "unknown"
    assert evaluate(codex, {"runs_week": 4}).level == "refuse"


def test_warn_outranks_unknown():
    rule = member("Rule")
    rules = (
        rule(kind="usd_month", limit=10.0, warn_at=5.0, unit="usd", source="s"),
        rule(kind="runs_day", limit=10.0, warn_at=None, unit="runs", source="s"),
    )
    assert evaluate(rules, {"month_usd": 6}).level == "warn"


def test_no_rules_is_unknown_not_a_crash():
    assert evaluate((), {"used_percent": 99}).level == "unknown"


@pytest.mark.parametrize("bad", ["93", True, False, math.nan, -5, None, [93], {"x": 1}])
def test_odd_readings_count_as_absent_and_never_raise(bad):
    d = evaluate(rules_of("Codex"), {"used_percent": bad, "runs_week": bad})
    assert d.level == "unknown"


@pytest.mark.parametrize("reading", [{}, {"observed_at": None}, {"observed_at": "not a date"}])
def test_observed_at_is_optional_and_harmless(reading):
    assert evaluate(rules_of("DeepSeek"), {"month_usd": 1, **reading}).level == "ok"


def test_decision_shape_is_frozen_with_named_fields():
    d = evaluate(rules_of("Codex"), full_codex(93))
    assert dataclasses.is_dataclass(d)
    names = {f.name for f in dataclasses.fields(d)}
    assert {"level", "lane", "kind", "used", "limit", "unit", "reason"} <= names
    assert d.level in LEVELS and isinstance(d.lane, str)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.level = "ok"  # type: ignore[misc]


@pytest.mark.parametrize(("label", "reading", "used", "limit"), [
    ("Codex", {"used_percent": 93.0, "runs_week": 0}, "93", "85"),
    ("DeepSeek", {"month_usd": 10}, "10", "10"),
    ("DeepSeek", {"month_usd": 6}, "6", "10"),
    ("Gemini", {"runs_day": 10}, "10", "10"),
])
def test_reason_carries_used_limit_and_unit(label, reading, used, limit):
    d = evaluate(rules_of(label), reading)
    assert d.level in {"refuse", "warn"}
    assert used in d.reason and limit in d.reason
    assert d.unit and d.unit in d.reason
    assert d.used is not None and d.limit is not None


def test_ok_decision_has_no_alarm_text():
    d = evaluate(rules_of("Codex"), full_codex(10))
    assert d.level == "ok" and isinstance(d.reason, str)
