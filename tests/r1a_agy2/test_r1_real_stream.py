"""R1: the real agy stream of a bypassing turn, translated by the antigravity Translator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subagent.providers.antigravity import Translator
from subagent.providers.base import Session

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/antigravity/real-bypass.stdout.jsonl"
RAW = ROOT / "wiki/briefs-2026-11-25/r1a-agy-raw-bypass.stdout.jsonl"
SCRATCH = "/private/var/folders/yy/s3_y_d5x2_34qb0k9f6qvjqr0000gn/T/subagent-agy-smoke-nogk2s01"


def _translate() -> tuple[Translator, list[dict]]:
    translator = Translator(Session(provider="gemini"))
    out: list[dict] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.extend(translator.feed(json.loads(line)))
    return translator, out


def _blocks(out: list[dict], kind: str) -> list[dict]:
    blocks = []
    for ev in out:
        content = ev.get("message", {}).get("content", [])
        blocks.extend(b for b in content if isinstance(b, dict) and b.get("type") == kind)
    return blocks


@pytest.mark.skip(reason="needs a private raw capture that is not part of the public repository")
def test_r1_fixture_is_the_raw_stream_with_only_paths_replaced() -> None:
    """R1: the fixture equals the saved raw stream except <SCRATCH> and <WORKSPACE> for the scratch paths."""
    fixture = FIXTURE.read_text(encoding="utf-8")
    raw = RAW.read_text(encoding="utf-8")
    assert SCRATCH not in fixture
    assert "<WORKSPACE>" in fixture and "<SCRATCH>" in fixture
    restored = fixture.replace("<WORKSPACE>", SCRATCH + "/workspace").replace("<SCRATCH>", SCRATCH)
    assert restored == raw


def test_r1_translates_write_file_and_run_command_without_bypass_key() -> None:
    """R1: write_file and run_command tool_use appear; run_command input has CommandLine and no BypassSandbox key."""
    translator, out = _translate()
    uses = _blocks(out, "tool_use")
    names = [u["name"] for u in uses]
    assert names == ["write_file", "run_command"], names
    run_command = uses[1]
    assert "CommandLine" in run_command["input"]
    assert all(str(k).lower() != "bypasssandbox" for k in run_command["input"])
    assert translator.bypass is None
    assert len(_blocks(out, "tool_result")) == 2


def test_r1_run_command_result_says_operation_not_permitted() -> None:
    """R1: the run_command tool_result text contains 'operation not permitted'."""
    _, out = _translate()
    results = _blocks(out, "tool_result")
    assert "operation not permitted" in str(results[-1]["content"])


def test_r1_final_result_is_success_with_stream_usage() -> None:
    """R1: the final result is a success and its usage is taken from the stream."""
    translator, out = _translate()
    final = out[-1]
    assert final["type"] == "result"
    assert final["subtype"] == "success" and final["is_error"] is False
    assert final["usage"] == {
        "input_tokens": 38193 - 16218,
        "cache_read_input_tokens": 16218,
        "output_tokens": 1936 + 1526,
    }
    assert translator.terminal is True
