"""The gemini driver, fixture-tested only (the Gemini CLI is not installed).

The tests check the argv shape, the experimental gate that keeps the driver
from running before an operator opts in, and the translator's usage math on
the hand-written fixture: `prompt - cached` -> input, `cached` -> cache_read,
`candidates + thoughts` -> output.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from subagent.providers import gemini as gemini_driver
from subagent.providers.base import ProviderConfig, Session

from .conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"


def _gemini_cfg(**kw) -> ProviderConfig:
    return ProviderConfig(name="gemini", driver="gemini", vendor="gemini", **kw)


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


# --- argv ----------------------------------------------------------------------


def test_argv_is_yolo_stream_json(tmp_path: Path):
    settings = make_settings(tmp_path)
    argv = gemini_driver.GEMINI_PROVIDER.argv(
        _gemini_cfg(), settings, "a1", "do it", _workspace(tmp_path),
        Session(provider="gemini"), None,
    )
    assert argv == ["gemini", "-p", "do it", "--output-format", "stream-json", "--yolo"]


def test_guard_label_is_none(tmp_path: Path):
    assert gemini_driver.GEMINI_PROVIDER.guard(make_settings(tmp_path), _gemini_cfg()) == "none"


# --- the experimental gate ------------------------------------------------------


def test_boot_refuses_unless_experimental(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/gemini")
    settings = make_settings(tmp_path)
    error, _ = gemini_driver.GEMINI_PROVIDER.boot(settings, "a1", _gemini_cfg())
    assert error and "experimental" in error
    error, _ = gemini_driver.GEMINI_PROVIDER.boot(settings, "a1", _gemini_cfg(experimental=True))
    assert error is None


# --- the translator's usage math on the fixture ---------------------------------


def test_translator_usage_math():
    translator = gemini_driver.Translator()
    out: list[dict] = []
    for event in _load("success.jsonl"):
        out.extend(translator.feed(event))

    assert [e["type"] for e in out] == ["system", "assistant", "assistant", "user",
                                        "assistant", "result"]
    assert out[0] == {"type": "system", "subtype": "init", "session_id": "gemini-sess-1"}
    tool_use = next(
        b for e in out if e["type"] == "assistant"
        for b in e["message"]["content"] if b["type"] == "tool_use"
    )
    assert tool_use == {"type": "tool_use", "id": "t1", "name": "Bash",
                        "input": {"command": "uv run pytest -q"}}

    result = out[-1]
    assert result["type"] == "result" and result["is_error"] is False
    assert result["result"] == "the tests pass"
    # prompt 120, cached 20, candidates 30, thoughts 10 (tool 5 is not counted).
    assert result["usage"] == {
        "input_tokens": 100,
        "cache_read_input_tokens": 20,
        "output_tokens": 40,
    }


def test_translator_failure_fallback():
    translator = gemini_driver.Translator()
    failure = translator.failure("boom")
    assert failure["type"] == "result" and failure["is_error"] is True
    assert failure["error"] == "boom"
