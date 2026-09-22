"""The omp driver, against recorded `omp -p --mode json` streams and a fake
`omp` binary that replays them.

The fixtures under tests/fixtures/omp were recorded live from omp 18.0.11 on
a local oMLX model (see the README there). The fake (tests/fakes/fake_omp.py,
installed by the `fake_omp` fixture below under the provider's `binary` name)
prints the fixture named by FAKE_OMP_FIXTURE and records its argv, cwd, env
and what it read from stdin to FAKE_OMP_RECORD (one JSON line per call).
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from subagent import router
from subagent.providers import omp as omp_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import COMPLETED, Registry

from .conftest import default_providers, make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "omp"
FAKE_OMP = Path(__file__).parent / "fakes" / "fake_omp.py"

MODEL = "Qwen3.8-Flash-Next-REAP-384-oQ4e-BF16-MTP-PLE"


def _omp_cfg(**extra) -> ProviderConfig:
    return ProviderConfig(name="local", driver="omp", vendor="omp", extra=extra)


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _translate(name: str, exit_code: int = 0, extra_text: str = "") -> list[dict]:
    translator = omp_driver.Translator("omp-session-1")
    out: list[dict] = []
    for event in _load(name):
        out.extend(translator.feed(event))
    out.append(translator.finish(exit_code, extra_text))
    return out


def _blocks(events: list[dict], kind: str, block_type: str) -> list[dict]:
    return [
        b for e in events if e["type"] == kind
        for b in e["message"]["content"] if b["type"] == block_type
    ]


# --- argv ---------------------------------------------------------------------


def test_argv_first_turn_shape(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = _omp_cfg(extra_args=["--thinking", "low"])
    ws = _workspace(tmp_path)
    argv = omp_driver.OMP_PROVIDER.argv(
        cfg, settings, "a1", "do it", ws, Session(provider="local"), f"omlx/{MODEL}"
    )
    assert argv[0] == "omp"
    assert "-p" in argv
    assert argv[argv.index("--mode") + 1] == "json"
    assert "--no-session" in argv
    assert argv[argv.index("--cwd") + 1] == str(ws)
    assert argv[argv.index("--model") + 1] == f"omlx/{MODEL}"
    assert argv[-3:] == ["--thinking", "low", "do it"]  # extras, then the prompt verbatim
    assert "--resume" not in argv and "--continue" not in argv


def test_argv_without_a_model_leaves_the_choice_to_omp(tmp_path: Path):
    settings = make_settings(tmp_path)
    argv = omp_driver.OMP_PROVIDER.argv(
        _omp_cfg(), settings, "a1", "do", _workspace(tmp_path), Session(provider="local"), None
    )
    assert "--model" not in argv
    assert argv[-1] == "do"


def test_argv_resume_turn_prepends_the_continue_line_and_previous_text(tmp_path: Path):
    settings = make_settings(tmp_path)
    session = Session(provider="local", session_id="s1", data={"last_message": "wrote hello.txt"})
    argv = omp_driver.OMP_PROVIDER.argv(
        _omp_cfg(), settings, "a1", "now delete it", _workspace(tmp_path), session, None
    )
    prompt = argv[-1]
    assert prompt.startswith(omp_driver.RESUME_PREFIX)
    assert "wrote hello.txt" in prompt
    assert prompt.endswith("now delete it")
    # omp runs --no-session: the continue line is the whole mechanism.
    assert "--resume" not in argv and "--continue" not in argv
    assert "--no-session" in argv


def test_argv_binary_comes_from_config(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = ProviderConfig(name="local", driver="omp", vendor="omp", binary="/opt/bin/omp")
    argv = omp_driver.OMP_PROVIDER.argv(
        cfg, settings, "a1", "do", _workspace(tmp_path), Session(provider="local"), None
    )
    assert argv[0] == "/opt/bin/omp"


# --- boot: guard gate and the minted session ----------------------------------


def test_guard_label_is_none(tmp_path: Path):
    assert omp_driver.OMP_PROVIDER.guard(make_settings(tmp_path), _omp_cfg()) == "none"


def test_boot_mints_a_session_id(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/omp")
    settings = make_settings(tmp_path, allow_unguarded=True)
    error, session = omp_driver.OMP_PROVIDER.boot(settings, "a1", _omp_cfg())
    assert error is None
    uuid.UUID(session.session_id)  # tags the records; omp never sees it


def test_boot_refuses_unguarded_unless_loop_or_allow_unguarded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/omp")
    settings = make_settings(tmp_path)  # allow_unguarded defaults off
    error, _ = omp_driver.OMP_PROVIDER.boot(settings, "a1", _omp_cfg())
    assert error and "allow_unguarded" in error
    error, _ = omp_driver.OMP_PROVIDER.boot(settings, "a1", _omp_cfg(loop=True))
    assert error is None
    permissive = make_settings(tmp_path, allow_unguarded=True)
    error, _ = omp_driver.OMP_PROVIDER.boot(permissive, "a1", _omp_cfg())
    assert error is None


def test_boot_checks_the_binary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    settings = make_settings(tmp_path, allow_unguarded=True)
    error, _ = omp_driver.OMP_PROVIDER.boot(settings, "a1", _omp_cfg())
    assert error and "not on PATH" in error


# --- the translator on the recorded streams -----------------------------------


def test_translator_simple_reply():
    out = _translate("simple.jsonl")
    assert out[0] == {"type": "system", "subtype": "init", "session_id": "omp-session-1"}
    assert [e["type"] for e in out] == ["system", "assistant", "assistant", "assistant", "result"]
    # Two thinking deltas, then the text delta, in stream order.
    thinking = _blocks(out, "assistant", "thinking")
    assert [b["thinking"] for b in thinking] == [
        "\nThe user is asking me to reply", ' with exactly "OK".\n',
    ]
    assert [b["text"] for b in _blocks(out, "assistant", "text")] == ["\n\nOK"]
    assert not _blocks(out, "assistant", "tool_use")

    result = out[-1]
    assert result["type"] == "result" and result["subtype"] == "success"
    assert result["is_error"] is False
    assert result["result"] == "OK"
    assert result["session_id"] == "omp-session-1"
    assert result["num_turns"] == 1
    assert "error" not in result
    # Usage is read from the assistant message agent_end carries, once.
    assert result["usage"] == {
        "input_tokens": 17933,
        "output_tokens": 16,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_translator_tool_call_stream():
    out = _translate("tool_call.jsonl")
    kinds = [e["type"] for e in out]
    assert kinds[0] == "system" and kinds[-1] == "result"
    assert "user" in kinds

    tool_use = _blocks(out, "assistant", "tool_use")
    assert tool_use == [{
        "type": "tool_use",
        "id": "call_2b66c830",
        "name": "write",
        "input": {"path": "hello.txt", "content": "hi"},
    }]
    tool_result = _blocks(out, "user", "tool_result")
    assert tool_result == [{
        "type": "tool_result",
        "tool_use_id": "call_2b66c830",
        "content": "[hello.txt#A564]\nSuccessfully wrote 2 bytes to hello.txt",
        "is_error": False,
    }]
    # The tool_use precedes its result, which precedes the second turn's text.
    assert kinds.index("user") > next(
        i for i, e in enumerate(out)
        if e["type"] == "assistant"
        and any(b["type"] == "tool_use" for b in e["message"]["content"])
    )

    result = out[-1]
    assert result["is_error"] is False
    # The final turn's text, not the first turn's "\n\n" prefixed to it.
    assert result["result"] == "Created `hello.txt` containing `hi`."
    assert result["num_turns"] == 2
    # Two assistant messages, two model calls: their usage is summed.
    assert result["usage"] == {
        "input_tokens": 1564 + 1678,
        "output_tokens": 80 + 40,
        "cache_read_input_tokens": 16384 * 2,
        "cache_creation_input_tokens": 0,
    }


def test_translator_every_event_type_is_handled():
    """Each type the recorded streams carry maps to a known shape or is
    dropped on purpose; nothing leaks through as an unknown claude event."""
    seen: set[str] = set()
    translator = omp_driver.Translator("s")
    for name in ("simple.jsonl", "tool_call.jsonl", "refused.jsonl"):
        for event in _load(name):
            seen.add(event.get("type"))
            for emitted in translator.feed(event):
                assert emitted["type"] in ("system", "assistant", "user")
    assert seen >= {
        "session", "agent_start", "turn_start", "message_start", "message_update",
        "message_end", "tool_execution_start", "tool_execution_update",
        "tool_execution_end", "turn_end", "agent_end", "auto_retry_start",
    }


def test_translator_tool_result_shapes():
    translator = omp_driver.Translator("s")
    string_result = translator.feed({
        "type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
        "result": "plain text", "isError": True,
    })[-1]
    assert string_result["message"]["content"][0] == {
        "type": "tool_result", "tool_use_id": "c1", "content": "plain text", "is_error": True,
    }
    other = translator.feed({
        "type": "tool_execution_end", "toolCallId": "c2", "toolName": "x",
        "result": {"rows": [1, 2]},
    })[-1]
    assert json.loads(other["message"]["content"][0]["content"]) == {"rows": [1, 2]}
    assert other["message"]["content"][0]["is_error"] is False


def test_translator_reports_error_exit_and_raw_lines():
    translator = omp_driver.Translator("s")
    for event in _load("simple.jsonl"):
        translator.feed(event)
    result = translator.finish(1, "Error: model not found: omlx/nope")
    assert result["is_error"] is True
    assert result["subtype"] == "error"
    assert "model not found" in result["error"]


def test_translator_message_update_error_marks_the_result():
    translator = omp_driver.Translator("s")
    translator.feed({"type": "agent_start"})
    translator.feed({
        "type": "message_update",
        "assistantMessageEvent": {"type": "error", "reason": "provider omlx not configured"},
    })
    result = translator.finish(0)
    assert result["is_error"] is True
    assert "not configured" in result["error"]


# --- refusals: a server or model that cannot be reached ----------------------


def test_refused_connection_stream_is_a_refusal_that_does_not_close():
    """What omp actually prints when the server is down: an assistant message
    that stopped on `error` with `errorMessage` "Unable to connect...", then
    auto retries. Not ECONNREFUSED."""
    out = _translate("refused.jsonl")
    result = out[-1]
    assert result["is_error"] is True
    assert "Unable to connect" in result["error"]
    # No agent_end, so no usage: only the two defaults.
    assert result["usage"] == {"input_tokens": 0, "output_tokens": 0}

    refusal = omp_driver.OMP_PROVIDER.refusal(_omp_cfg(), out)
    assert refusal is not None
    assert refusal.code == router.COPILOT_MODEL_UNAVAILABLE
    # Driver-local: the lane is never closed for it.
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is None


@pytest.mark.parametrize("text", [
    "connect ECONNREFUSED 127.0.0.1:8000",
    "Error: model not found: omlx/nope",
    "Unable to connect. Is the computer able to access the url?",
])
def test_refusal_patterns(text: str):
    events = [{"type": "result", "subtype": "error", "is_error": True, "result": "",
               "error": text}]
    refusal = omp_driver.OMP_PROVIDER.refusal(_omp_cfg(), events)
    assert refusal is not None and refusal.code == router.COPILOT_MODEL_UNAVAILABLE


def test_no_refusal_on_success_or_unrelated_error():
    cfg = _omp_cfg()
    assert omp_driver.OMP_PROVIDER.refusal(cfg, [{"type": "result", "is_error": False}]) is None
    assert omp_driver.OMP_PROVIDER.refusal(
        cfg, [{"type": "result", "is_error": True, "error": "tests failed"}]
    ) is None


# --- end to end: a fake `omp` binary ------------------------------------------


@pytest.fixture
def fake_omp(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "omp"
    binary.write_text(f"#!{sys.executable}\n{FAKE_OMP.read_text()}")
    binary.chmod(0o755)
    record = tmp_path / "omp-calls.jsonl"
    monkeypatch.setenv("FAKE_OMP_RECORD", str(record))
    monkeypatch.setenv("FAKE_OMP_FIXTURE", str(FIXTURES / "tool_call.jsonl"))

    class Fake:
        path = str(binary)

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


def test_run_completes_and_resumes_through_the_prompt(fake_omp, tmp_path: Path):
    # The stock providers stay declared so their keys count as leaked.
    providers = {**default_providers(), "local": {"driver": "omp", "binary": fake_omp.path}}
    settings = make_settings(tmp_path, providers=providers, allow_unguarded=True, run_timeout=30)
    reg = Registry(settings, start_reaper=False)
    try:
        ws = _workspace(tmp_path)
        agent = reg.create_agent("o", ws, provider="local")
        assert agent.wait_ready(5) is None
        run = agent.submit("make hello.txt", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        uuid.UUID(run.session_id)  # minted, never omp's own session id
        assert run.session_id == agent.session_id
        assert run.usage.input == 1564 + 1678
        assert run.usage.output == 120
        assert run.usage.cache_read == 32768
        assert run.usage.steps == 1  # the one write call
        assert agent.info()["usage"]["input"] == 1564 + 1678

        second = agent.follow_up("now remove it", verification="true")
        assert second.done.wait(10)
        assert second.state == COMPLETED, second.error
        first, then = fake_omp.calls()[:2]
        assert first["argv"][-1] == "make hello.txt"
        assert first["argv"][first["argv"].index("--cwd") + 1] == str(ws)
        assert "--model" not in first["argv"]  # the agent named no model
        # The resume turn carries the previous turn's final text in the prompt.
        assert then["argv"][-1].startswith(omp_driver.RESUME_PREFIX)
        assert "Created `hello.txt` containing `hi`." in then["argv"][-1]
        assert then["argv"][-1].endswith("now remove it")
        # The child's stdin is closed, not the server's: omp would otherwise
        # sit reading it to EOF.
        assert first["stdin"] == "" and then["stdin"] == ""
        # The server's own keys never reach the child.
        assert first["env"]["GLM_API_KEY"] is None
    finally:
        reg.shutdown()


def test_run_refused_connection_fails_without_closing_the_lane(fake_omp, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OMP_FIXTURE", str(FIXTURES / "refused.jsonl"))
    monkeypatch.setenv("FAKE_OMP_EXIT", "1")
    providers = {"local": {"driver": "omp", "binary": fake_omp.path}}
    settings = make_settings(tmp_path, providers=providers, allow_unguarded=True, run_timeout=30)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("o", _workspace(tmp_path), provider="local")
        assert agent.wait_ready(5) is None
        run = agent.submit("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state != COMPLETED
        assert run.error and "Unable to connect" in run.error
    finally:
        reg.shutdown()
