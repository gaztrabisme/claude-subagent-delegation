"""The pre-dispatch usage-envelope check.

The lane plan is a hand-maintained markdown document. Its "Usage envelopes"
table states, per lane, what has been measured, the envelope the owner set,
and the reserve that must never be planned. This module reads that table and
turns it into a small decision: may a run start on a lane, must it warn, or
must it be refused before any vendor call.

The check is configured explicitly with ``[envelope] lane_plan``; unset means
off. Nothing here reads a machine-specific path.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HEADING = "## Usage envelopes"
_SEPARATOR = re.compile(r"[\s|:\-]+")
_WORD = re.compile(r"[a-z][a-z-]*")

# Which reading key each rule consults. GPU hours deliberately have no source
# yet: the rule exists in the plan, so it reports unknown rather than ok.
_READING_KEY = {
    "percent_used": "used_percent",
    "usd_month": "month_usd",
    "runs_day": "runs_day",
    "runs_week": "runs_week",
    "gpu_hours_day": "gpu_hours_day",
}

_RANK = {"ok": 0, "unknown": 1, "warn": 2, "refuse": 3}


@dataclass(frozen=True)
class Rule:
    """One limit read from a row's Envelope or Reserve cell."""

    kind: str
    limit: float
    warn_at: float | None
    unit: str
    source: str


@dataclass(frozen=True)
class EnvelopeRow:
    """One lane's envelope, and the lower-case words that name it."""

    label: str
    providers_hint: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Decision:
    """The outcome of checking a row against one reading."""

    level: str
    lane: str
    kind: str | None
    used: float | None
    limit: float | None
    unit: str | None
    reason: str


# --- parsing ------------------------------------------------------------------


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_separator(line: str) -> bool:
    return bool(_SEPARATOR.fullmatch(line.strip())) and "-" in line


def _clean(cell: str) -> str:
    """A first cell with the common markdown decoration removed."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(text.split())


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _rules(envelope: str, reserve: str) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*of\s*the\s*window", reserve, re.I)
    if match is not None:
        percentage = _number(match.group(1))
        if percentage is not None:
            rules.append(Rule("percent_used", 100.0 - percentage, None, "% of window",
                              reserve))
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:a|per)\s*month", envelope, re.I)
    if match is not None:
        limit = _number(match.group(1))
        if limit is not None:
            report = re.search(r"report\s+at\s*\$\s*([0-9]+(?:\.[0-9]+)?)", envelope, re.I)
            warn_at = _number(report.group(1)) if report is not None else None
            rules.append(Rule("usd_month", limit, warn_at, "USD", envelope))
    match = re.search(r"at\s+most\s+([0-9]+(?:\.[0-9]+)?)\s+runs\s+a\s+day", envelope, re.I)
    if match is not None:
        limit = _number(match.group(1))
        if limit is not None:
            rules.append(Rule("runs_day", limit, None, "runs per day", envelope))
    match = re.search(r"at\s+most\s+([0-9]+(?:\.[0-9]+)?)\s+runs\s+a\s+week", envelope, re.I)
    if match is not None:
        limit = _number(match.group(1))
        if limit is not None:
            rules.append(Rule("runs_week", limit, None, "runs per week", envelope))
    if re.fullmatch(r"0(?:\.0+)?", envelope.strip()):
        rules.append(Rule("closed", 0.0, None, "runs", envelope))
    match = re.search(r"at\s+most\s+([0-9]+(?:\.[0-9]+)?)\s+GPU\s+hours\s+a\s+day",
                      envelope, re.I)
    if match is not None:
        limit = _number(match.group(1))
        if limit is not None:
            rules.append(Rule("gpu_hours_day", limit, None, "GPU hours per day", envelope))
    return tuple(rules)


def _parse(text: str) -> list[EnvelopeRow]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(HEADING)), None)
    if start is None:
        return []
    header: int | None = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("## "):
            break
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if (len(cells) >= 4 and cells[0] == "Lane"
                and cells[1].startswith("Measured so far")
                and cells[2] == "Envelope"
                and cells[3].startswith("Reserve")):
            header = i
            break
    if header is None:
        return []
    rows: list[EnvelopeRow] = []
    for line in lines[header + 1:]:
        if not line.lstrip().startswith("|"):
            break
        if _is_separator(line):
            continue
        cells = _cells(line)
        if len(cells) < 4:
            continue
        label = _clean(cells[0])
        if not label:
            continue
        rows.append(EnvelopeRow(
            label=label,
            providers_hint=tuple(_WORD.findall(label.lower())),
            rules=_rules(cells[2], cells[3]),
        ))
    return rows


def parse_envelopes(text: str) -> list[EnvelopeRow]:
    """Read the table under "## Usage envelopes"; [] when it is missing or broken."""
    try:
        return _parse(text)
    except Exception:  # noqa: BLE001 - a hand-edited plan must never break dispatch
        return []


def load_plan(path: str | Path) -> list[EnvelopeRow]:
    """Parse the configured plan file; [] when it cannot be read."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    return parse_envelopes(text)


def row_for(rows: list[EnvelopeRow], provider: str) -> EnvelopeRow | None:
    """The row a provider name matches, by the lane plan's own rule."""
    name = provider.lower()
    for row in rows:
        if any(name == hint or name.startswith(hint + "-") for hint in row.providers_hint):
            return row
    return None


def _providers_for(row: EnvelopeRow, snapshot: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for name in snapshot:
        if not isinstance(name, str):
            continue
        if any(name == hint or name.startswith(hint + "-") for hint in row.providers_hint):
            names.append(name)
    return names


# --- readings -----------------------------------------------------------------


def _reading_value(reading: Any, key: str) -> float | None:
    """A finite, non-negative number; anything else (bool, str, NaN) is absent."""
    if not isinstance(reading, Mapping):
        return None
    value = reading.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def reading_for(row: EnvelopeRow, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The numbers this row's providers report in one lane-status snapshot."""
    reading: dict[str, Any] = {}
    percent = None
    observed = None
    month = None
    day = None
    week = None
    for name in _providers_for(row, snapshot):
        view = snapshot.get(name)
        if not isinstance(view, Mapping):
            continue
        usage = view.get("usage")
        if isinstance(usage, Mapping) and usage.get("kind") == "percent" and percent is None:
            value = _reading_value(usage, "used_percent")
            if value is not None:
                percent = value
                observed = usage.get("observed_at")
        spend = view.get("spend")
        if isinstance(spend, Mapping):
            value = _reading_value(spend, "month_usd")
            if value is not None:
                month = value if month is None else month + value
        runs = view.get("runs")
        if isinstance(runs, Mapping):
            value = _reading_value(runs, "day")
            if value is not None:
                day = value if day is None else day + value
            value = _reading_value(runs, "week")
            if value is not None:
                week = value if week is None else week + value
    if percent is not None:
        reading["used_percent"] = percent
    if month is not None:
        reading["month_usd"] = month
    if day is not None:
        reading["runs_day"] = day
    if week is not None:
        reading["runs_week"] = week
    if observed is not None:
        reading["observed_at"] = observed
    return reading


def missing_keys(rules: tuple[Rule, ...], reading: Mapping[str, Any] | None) -> list[str]:
    """Reading keys the rules need but the reading does not carry."""
    missing: list[str] = []
    for rule in rules or ():
        key = _READING_KEY.get(rule.kind)
        if key is None or _reading_value(reading, key) is not None:
            continue
        if key not in missing:
            missing.append(key)
    return missing


# --- decision -----------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:g}"


def _reason(lane: str, rule: Rule, used: float, why: str) -> str:
    return (
        f"envelope: lane {lane or '?'} used {_fmt(used)} of limit "
        f"{_fmt(rule.limit)} ({rule.unit}): {why}"
    )


def _unknown(lane: str, rule: Rule) -> Decision:
    key = _READING_KEY.get(rule.kind, rule.kind)
    reason = (
        f"envelope: lane {lane or '?'} has no reading for {key} "
        f"({rule.unit})"
    )
    return Decision("unknown", lane, rule.kind, None, rule.limit, rule.unit, reason)


def _judge(lane: str, rule: Rule, reading: Mapping[str, Any] | None) -> Decision:
    if rule.kind == "closed":
        reason = (
            f"envelope: lane {lane or '?'} is closed: "
            f"the envelope for the lane is 0 ({rule.unit})"
        )
        return Decision("refuse", lane, rule.kind, 0.0, rule.limit, rule.unit, reason)
    key = _READING_KEY.get(rule.kind)
    used = _reading_value(reading, key) if key is not None else None
    if used is None:
        return _unknown(lane, rule)
    if used >= rule.limit:
        why = "at or past the limit; the reserve is not available"
        if rule.kind == "percent_used":
            why = "inside the reserve; the limit is not available"
        return Decision("refuse", lane, rule.kind, used, rule.limit, rule.unit,
                        _reason(lane, rule, used, why))
    if rule.warn_at is not None and used >= rule.warn_at:
        return Decision("warn", lane, rule.kind, used, rule.limit, rule.unit,
                        _reason(lane, rule, used, "at the report line"))
    return Decision("ok", lane, rule.kind, used, rule.limit, rule.unit,
                    _reason(lane, rule, used, "ok"))


def evaluate(
    rules: tuple[Rule, ...], reading: Mapping[str, Any] | None, lane: str = ""
) -> Decision:
    """The worst decision over `rules`; unknown beats ok, refuse beats everything."""
    if not isinstance(reading, Mapping):
        reading = {}
    decisions = [_judge(lane, rule, reading) for rule in rules or ()]
    if not decisions:
        reason = f"envelope: lane {lane or '?'} has no rules to check"
        return Decision("unknown", lane, None, None, None, None, reason)
    return max(decisions, key=lambda decision: _RANK[decision.level])
