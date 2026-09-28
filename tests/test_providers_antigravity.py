"""Antigravity's CLI protocol, translated from captured offline fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from subagent.providers import antigravity as agy_driver
from subagent.providers.base import ProviderConfig, Session

from .conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
FAKE_AGY = Path(__file__).parent / "fakes" / "fake_agy.py"


def _cfg(**kw) -> ProviderConfig:
    return ProviderConfig(name="gemini", driver="antigravity", vendor="gemini", **kw)


def _load(name: str) -> list[dict]:
    path = FIXTURES / f"{name}.stdout.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


def _translate(name: str, session: Session | None = None) -> tuple[agy_driver.Translator, list[dict]]:
    translator = agy_driver.Translator(session or Session(provider="gemini"))
    out: list[dict] = []
    for event in _load(name):
        out.extend(translator.feed(event))
    return translator, out


def test_argv_includes_model_effort_skip_permissions_and_sandbox(tmp_path: Path):
    settings = make_settings(tmp_path)
    argv = agy_driver.ANTIGRAVITY_PROVIDER.argv(
        _cfg(model="gemini-3.8-flash-high", effort="high"),
        settings,
        "a1",
        "do it",
        _workspace(tmp_path),
        Session(provider="gemini"),
        "gemini-3.8-flash-low",
    )
    assert argv == [
        "agy", "-p", "do it", "--output-format", "stream-json",
        "--model", "gemini-3.8-flash-low", "--effort", "high",
        "--dangerously-skip-permissions", "--sandbox",
    ]


def test_argv_uses_config_model_and_resumes_conversation(tmp_path: Path):
    settings = make_settings(tmp_path)
    argv = agy_driver.ANTIGRAVITY_PROVIDER.argv(
        _cfg(model="gemini-3.8-flash-high", effort="medium"),
        settings,
        "a1",
        "continue",
        _workspace(tmp_path),
        Session(provider="gemini", session_id="conversation-1"),
        None,
    )
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--conversation") + 1] == "conversation-1"
    assert "--sandbox" in argv and "--dangerously-skip-permissions" in argv


def test_guard_is_none_and_stays_sandboxed(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = _cfg()
    assert agy_driver.ANTIGRAVITY_PROVIDER.guard(settings, cfg) == "none"
    argv = agy_driver.ANTIGRAVITY_PROVIDER.argv(
        cfg, settings, "a1", "task", _workspace(tmp_path), Session(provider="gemini"), None
    )
    assert "--sandbox" in argv


def test_boot_requires_experimental_and_an_available_binary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda binary: "/fake/agy")
    settings = make_settings(tmp_path)
    error, _ = agy_driver.ANTIGRAVITY_PROVIDER.boot(settings, "a1", _cfg())
    assert error and "experimental = true" in error
    error, session = agy_driver.ANTIGRAVITY_PROVIDER.boot(
        settings, "a1", _cfg(experimental=True)
    )
    assert error is None
    assert session.provider == "gemini"

    monkeypatch.setattr(shutil, "which", lambda binary: None)
    error, _ = agy_driver.ANTIGRAVITY_PROVIDER.boot(
        settings, "a1", _cfg(experimental=True)
    )
    assert error and "not on PATH" in error


def test_probe_fixture_translation_and_usage():
    translator, out = _translate("probe")
    assert out[0] == {
        "type": "system",
        "subtype": "init",
        "session_id": "013c3dfb-618f-44cf-8000-d9a9dd0182fc",
    }
    assert translator.session.data["conversation_id"] == "013c3dfb-618f-44cf-8000-d9a9dd0182fc"
    assert [event["type"] for event in out] == [
        "system", "assistant", "user", "assistant", "user", "assistant", "user",
        "assistant", "assistant", "result",
    ]
    tool_uses = [
        block for event in out if event["type"] == "assistant"
        for block in event["message"]["content"] if block["type"] == "tool_use"
    ]
    tool_use = next(block for block in tool_uses if block["name"] == "replace_file_content")
    assert tool_use == {
        "type": "tool_use",
        "id": "013c3dfb-618f-44cf-8000-d9a9dd0182fc:4",
        "name": "replace_file_content",
        "input": {
            "TargetFile": "<WORKSPACE>/a.py",
        },
    }
    tool_result = next(
        block for event in out if event["type"] == "user"
        for block in event["message"]["content"]
        if block["type"] == "tool_result" and block["tool_use_id"] == tool_use["id"]
    )
    assert tool_result["tool_use_id"] == tool_use["id"]
    assert tool_result["content"] == ""
    assert tool_result["is_error"] is False

    result = out[-1]
    assert result["type"] == "result" and result["is_error"] is False
    assert result["result"] == "OK\n"
    assert result["usage"] == {
        "input_tokens": 17510,
        "cache_read_input_tokens": 24406,
        "output_tokens": 430,
    }
    assert result["num_turns"] == 1
    assert translator.finish(0) is result


def test_failing_command_fixture_keeps_successful_turn_status():
    translator, out = _translate("failing")
    tool_use = next(
        block for event in out if event["type"] == "assistant"
        for block in event["message"]["content"] if block["type"] == "tool_use"
    )
    assert tool_use["input"] == {
        "CommandLine": "python3 -c 'from pathlib import Path; Path(\"nonzero-ran\").write_text(\"yes\"); raise SystemExit(7)'"
    }
    result = out[-1]
    assert result["type"] == "result" and result["is_error"] is False
    assert result["result"] == "DONE\n"
    assert translator.finish(0) is result


def test_bad_model_fixture_is_process_error_and_model_refusal():
    translator, out = _translate("bad-model")
    assert len(out) == 1
    result = translator.finish(1, (FIXTURES / "bad-model.stderr.txt").read_text())
    assert result["is_error"] is True
    assert result["subtype"] == "error"
    assert "not recognized as a known model" in result["error"]
    refusal = agy_driver.ANTIGRAVITY_PROVIDER.refusal(_cfg(), [result])
    assert refusal and refusal.code == "antigravity_model_unavailable"


def test_nonzero_agy_exit_overrides_success_result():
    translator, out = _translate("probe")
    result = translator.finish(2, "process failed")
    assert result is out[-1]
    assert result["is_error"] is True
    assert result["subtype"] == "error"
    assert "agy exited with code 2" in result["error"]
    assert "process failed" in result["error"]


def test_resumed_fixture_reports_per_turn_usage_delta():
    session = Session(provider="gemini")
    first, first_out = _translate("probe", session)
    first.finish(0)
    assert session.data["agy_num_turns"] == 1

    resumed, out = _translate("resumed", session)
    result = out[-1]
    assert result["session_id"] == "013c3dfb-618f-44cf-8000-d9a9dd0182fc"
    assert result["result"] == "OK\n"
    assert result["usage"] == {
        "input_tokens": 19646,
        "cache_read_input_tokens": 0,
        "output_tokens": 1,
    }
    assert result["num_turns"] == 1
    assert session.data["conversation_id"] == result["session_id"]
    assert resumed.finish(0) is result
    assert first_out[-1]["usage"]["input_tokens"] == 17510


def test_refusal_classifies_quota_and_auth_errors():
    classify = agy_driver.ANTIGRAVITY_PROVIDER.refusal
    assert classify(_cfg(), [{"type": "result", "is_error": True, "error": "HTTP 429 quota exceeded"}]).code == "antigravity_quota"
    assert classify(_cfg(), [{"type": "result", "is_error": True, "error": "401 Unauthorized: sign in required"}]).code == "antigravity_auth"
    assert classify(_cfg(), [{"type": "result", "is_error": False, "result": "The answer mentions quota"}]) is None


def test_fake_agy_replays_a_fixture(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_STDOUT", str(FIXTURES / "probe.stdout.jsonl"))
    record = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_AGY_RECORD", str(record))
    cfg = _cfg(binary=str(FAKE_AGY), experimental=True)
    settings = make_settings(tmp_path, providers={"gemini": {"driver": "antigravity", "binary": str(FAKE_AGY), "experimental": True}})
    session = Session(provider="gemini")
    process = agy_driver.ANTIGRAVITY_PROVIDER.spawn(
        cfg, settings, "a1", "reply ok", _workspace(tmp_path), session, "gemini-3.8-flash-low"
    )
    out = list(process.events())
    assert out[0]["type"] == "system"
    assert out[-1]["type"] == "result" and out[-1]["is_error"] is False
    argv = json.loads(record.read_text().splitlines()[0])
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-low"
    assert "--sandbox" in argv
