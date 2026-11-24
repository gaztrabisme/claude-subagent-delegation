"""Locked acceptance tests for W10 Blocker 4: drift detection across all fixture drivers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from subagent import providers as providers_pkg
from subagent.providers.base import Session

from .conftest import make_settings
from .test_harness_maintenance import CONFORMANCE_DIR, _field, _harness, _subdir
from .test_harness_review import _driver_provider

REMAINING_DRIVERS = ("omp", "antigravity", "grok", "gemini")
ALL_FIXTURE_DRIVERS = ("claude", "codex", "copilot", *REMAINING_DRIVERS)


def _conformance_fixture_pair(driver: str) -> tuple[Path, Path]:
    paths = sorted((CONFORMANCE_DIR / driver).glob("*.jsonl"))
    assert paths, f"missing conformance fixture for {driver}"
    jsonl = paths[0]
    expected = jsonl.with_suffix(".expected.json")
    assert expected.is_file(), f"missing expected output for {jsonl}"
    return jsonl, expected


def _unknown_event_line(driver: str) -> str:
    if driver == "antigravity":
        return json.dumps({"event": "response.future_v2", "data": {}})
    return json.dumps({"type": "response.future_v2", "data": {}})


# --- 1. Drift detection for omp, antigravity, grok, gemini --------------------


@pytest.mark.parametrize("driver", REMAINING_DRIVERS)
def test_unmodified_conformance_fixtures_pass_for_remaining_drivers(
    tmp_path: Path, driver: str
) -> None:
    harness = _harness()
    settings = make_settings(
        _subdir(tmp_path, driver),
        providers={"lane": _driver_provider(driver)},
    )
    res = harness.verify(
        "lane",
        settings,
        lambda argv, timeout=10.0: (0, "1.0.0\n", ""),
        CONFORMANCE_DIR,
    )
    assert _field(res, "ok", "passed") is True, res


@pytest.mark.parametrize("driver", REMAINING_DRIVERS)
@pytest.mark.parametrize("position", ["append", "insert"])
def test_drift_unknown_event_appended_or_inserted_fails_verify_for_remaining_drivers(
    tmp_path: Path, driver: str, position: str
) -> None:
    harness = _harness()
    cdir = tmp_path / "conformance" / driver
    cdir.mkdir(parents=True)

    src_jsonl, src_expected = _conformance_fixture_pair(driver)
    src_lines = [
        line for line in src_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    unknown_line = _unknown_event_line(driver)
    if position == "append":
        mutated = [*src_lines, unknown_line]
    else:
        mutated = [src_lines[0], unknown_line, *src_lines[1:]]

    (cdir / "sample.jsonl").write_text("\n".join(mutated) + "\n", encoding="utf-8")
    (cdir / "sample.expected.json").write_text(
        src_expected.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    settings = make_settings(
        _subdir(tmp_path, f"{driver}-{position}"),
        providers={"lane": _driver_provider(driver)},
    )
    res = harness.verify(
        "lane",
        settings,
        lambda argv, timeout=10.0: (0, "1.0.0\n", ""),
        tmp_path / "conformance",
    )
    assert _field(res, "ok", "passed") is False, res
    report = json.dumps(res, default=str)
    assert "response.future_v2" in report
    assert "sample" in report


# --- 2. Fail closed when driver declares no known-event list ------------------


class _EchoTranslator:
    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        return [event]


class _DoublePassthroughNoKnown:
    name = "codex"
    needs_api_key = False
    prompt_on_stdin = False

    def translator(self, session: Session) -> None:
        return None


class _DoubleTranslatorNoKnown:
    name = "codex"
    needs_api_key = False
    prompt_on_stdin = False

    def translator(self, session: Session) -> _EchoTranslator:
        return _EchoTranslator()


class _DoubleKnownNone:
    name = "codex"
    needs_api_key = False
    prompt_on_stdin = False
    known_event_types = None

    def translator(self, session: Session) -> None:
        return None


@pytest.mark.parametrize(
    ("label", "double"),
    [
        ("omitted_passthrough", _DoublePassthroughNoKnown()),
        ("omitted_translator", _DoubleTranslatorNoKnown()),
        ("none_passthrough", _DoubleKnownNone()),
    ],
)
def test_verify_fails_closed_when_driver_declares_no_known_event_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    double: Any,
) -> None:
    harness = _harness()
    cdir = tmp_path / "conformance" / "codex"
    cdir.mkdir(parents=True)

    event = {"type": "system", "subtype": "init", "session_id": "s1"}
    (cdir / "sample.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (cdir / "sample.expected.json").write_text(json.dumps([event], indent=2), encoding="utf-8")

    monkeypatch.setitem(providers_pkg._CACHE, "codex", double)
    orig_for_driver = providers_pkg.for_driver

    def fake_for_driver(name: str) -> Any:
        if name == "codex":
            return double
        return orig_for_driver(name)

    monkeypatch.setattr(providers_pkg, "for_driver", fake_for_driver)
    if hasattr(harness, "for_driver"):
        monkeypatch.setattr(harness, "for_driver", fake_for_driver)

    settings = make_settings(
        _subdir(tmp_path, label),
        providers={"lane": {"driver": "codex", "binary": "codex"}},
    )
    res = harness.verify(
        "lane",
        settings,
        lambda argv, timeout=10.0: (0, "1.0.0\n", ""),
        tmp_path / "conformance",
    )
    assert _field(res, "ok", "passed") is False, res
    report = json.dumps(res, default=str).lower()
    assert "known_event_types" in report or ("known" in report and "event" in report)


# --- 3. Extra trailing events fail verify for every driver with fixtures ------


@pytest.mark.parametrize("driver", ALL_FIXTURE_DRIVERS)
def test_extra_trailing_events_fail_verify_for_every_driver(
    tmp_path: Path, driver: str
) -> None:
    assert set(ALL_FIXTURE_DRIVERS) == {
        p.name for p in CONFORMANCE_DIR.iterdir() if p.is_dir()
    }
    harness = _harness()
    cdir = tmp_path / "conformance" / driver
    cdir.mkdir(parents=True)

    src_jsonl, src_expected = _conformance_fixture_pair(driver)
    expected = json.loads(src_expected.read_text(encoding="utf-8"))
    assert len(expected) >= 2

    (cdir / "trailing.jsonl").write_text(
        src_jsonl.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (cdir / "trailing.expected.json").write_text(
        json.dumps(expected[:-1], indent=2),
        encoding="utf-8",
    )

    settings = make_settings(
        _subdir(tmp_path, driver),
        providers={"lane": _driver_provider(driver)},
    )
    res = harness.verify(
        "lane",
        settings,
        lambda argv, timeout=10.0: (0, "1.0.0\n", ""),
        tmp_path / "conformance",
    )
    assert _field(res, "ok", "passed") is False, res
    assert "trailing" in json.dumps(res, default=str)
