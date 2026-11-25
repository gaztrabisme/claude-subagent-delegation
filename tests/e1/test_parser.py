"""E1-1: parse_envelopes reads the real "Usage envelopes" table."""

from __future__ import annotations

import dataclasses

import pytest

from ._helpers import member, parse, plan_text, row, rules_of

LABELS = [
    "Claude (Ultra plan)",
    "Codex",
    "DeepSeek (DSH)",
    "Gemini (Hydrogen + Neon share one quota)",
    "omlx (local)",
    "glm, gpubox",
]


def kinds_of(label: str) -> list[str]:
    return sorted(rule.kind for rule in rules_of(label))


def test_fixture_is_the_real_section():
    text = plan_text()
    assert text.startswith("## Usage envelopes")
    assert "at most 4 runs a week" in text and "$10 a month; report at $5" in text


def test_six_rows_in_table_order_with_markdown_stripped():
    assert [r.label for r in parse()] == LABELS


def test_providers_hint_is_lower_case_words_of_the_label():
    hints = {r.label: r.providers_hint for r in parse()}
    assert all(isinstance(h, tuple) for h in hints.values())
    assert "codex" in hints["Codex"]
    assert {"deepseek", "dsh"} <= set(hints["DeepSeek (DSH)"])
    assert {"gemini", "hydrogen", "neon"} <= set(hints["Gemini (Hydrogen + Neon share one quota)"])
    assert {"glm", "gpubox"} <= set(hints["glm, gpubox"])
    assert "omlx" in hints["omlx (local)"]
    assert all(w == w.lower() for h in hints.values() for w in h)


def test_codex_has_percent_and_weekly_run_rules():
    assert kinds_of("Codex") == ["percent_used", "runs_week"]
    by_kind = {r.kind: r for r in rules_of("Codex")}
    assert by_kind["percent_used"].limit == 85
    assert by_kind["runs_week"].limit == 4
    assert by_kind["percent_used"].warn_at is None


def test_deepseek_money_rule_has_limit_and_report_line():
    (rule,) = rules_of("DeepSeek")
    assert (rule.kind, rule.limit, rule.warn_at) == ("usd_month", 10, 5)
    assert isinstance(rule.unit, str) and rule.unit
    assert "$10" in rule.source


def test_gemini_daily_run_cap_ignores_the_neon_subcap():
    (rule,) = rules_of("Gemini")
    assert (rule.kind, rule.limit) == ("runs_day", 10)


def test_closed_lane_and_gpu_hours():
    (closed,) = rules_of("glm")
    assert (closed.kind, closed.limit) == ("closed", 0)
    (gpu,) = rules_of("omlx")
    assert (gpu.kind, gpu.limit) == ("gpu_hours_day", 8)


def test_claude_row_has_no_recognisable_rule():
    assert rules_of("Claude") == ()


def test_row_and_rule_are_frozen_dataclasses():
    codex = row("Codex")
    assert dataclasses.is_dataclass(codex) and dataclasses.is_dataclass(codex.rules[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        codex.label = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        codex.rules[0].limit = 1.0  # type: ignore[misc]


def test_rule_fields_are_typed():
    for r in parse():
        for rule in r.rules:
            assert isinstance(rule.kind, str)
            assert isinstance(rule.limit, (int, float))
            assert rule.warn_at is None or isinstance(rule.warn_at, (int, float))
            assert isinstance(rule.unit, str) and isinstance(rule.source, str)


@pytest.mark.parametrize("text", [
    "",
    "no table anywhere",
    "## Usage envelopes\n\nonly prose here\n",
    "## Usage envelopes\n\n| Lane | Measured so far |\n|---|\n| broken",
    "## Usage envelopes\n\n| a | b | c | d |\n|---|---|---|---|\n| x | y | z | w |\n",
    "\x00\x01\x02 | | |",
])
def test_malformed_or_missing_table_gives_empty_list_never_raises(text):
    assert parse(text) == []


def test_table_under_another_heading_is_ignored():
    text = plan_text().replace("## Usage envelopes", "## Something else", 1)
    assert parse(text) == []


def test_section_inside_a_longer_document_is_found():
    doc = "# Lane plan\n\nIntro\n\n" + plan_text() + "\n## Next section\n\n| a | b |\n|---|---|\n"
    assert [r.label for r in parse(doc)] == LABELS


@pytest.mark.parametrize(("old", "new", "label", "kind", "expected"), [
    ("$10 a month", "$12 a month", "DeepSeek", "usd_month", 12),
    ("15% of the window", "20% of the window", "Codex", "percent_used", 80),
    ("at most 4 runs a week", "at most 6 runs a week", "Codex", "runs_week", 6),
    ("at most 10 runs a day", "at most 12 runs a day", "Gemini", "runs_day", 12),
])
def test_editing_the_numbers_changes_the_rules(old, new, label, kind, expected):
    text = plan_text()
    assert old in text
    rules = {r.kind: r for r in rules_of(label, text.replace(old, new))}
    assert rules[kind].limit == expected


def test_parse_is_the_only_public_entry_the_dispatch_needs():
    for name in ("parse_envelopes", "evaluate", "reading_for", "EnvelopeRow", "Rule", "Decision"):
        member(name)
