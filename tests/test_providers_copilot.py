"""The copilot driver, against a fake `copilot` binary that replays a fixture.

The fake (the `fake_copilot` fixture below) is a small Python script named as
the copilot provider's `binary`. It prints the fixture named by
FAKE_COPILOT_FIXTURE and records its argv and environment to
FAKE_COPILOT_RECORD (one JSON line per call).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from subagent import router
from subagent.providers import copilot as copilot_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import COMPLETED, Registry

from .conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"


def _copilot_cfg(**extra) -> ProviderConfig:
    return ProviderConfig(name="copilot", driver="copilot", vendor="copilot", extra=extra)


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _session_at(argv: list[str]) -> str:
    return argv[argv.index("--session-id") + 1]


# --- argv and the minted session id -------------------------------------------


def test_argv_flags_mint_session_and_pass_model(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = _copilot_cfg(extra_args=["--json", "--experimental-planning"])
    session = Session(provider="copilot")
    ws = _workspace(tmp_path)
    argv = copilot_driver.COPILOT_PROVIDER.argv(
        cfg, settings, "a1", "do it", ws, session, "claude-sonnet-5"
    )
    assert argv[0] == "copilot"
    assert argv[1:3] == ["-p", "do it"]
    assert "--allow-all-tools" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("-C") + 1] == str(ws)
    assert "--disable-builtin-mcps" in argv
    uuid.UUID(_session_at(argv))  # a freshly minted, valid session id
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[-2:] == ["--json", "--experimental-planning"]


def test_argv_reuses_an_existing_session_id(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = _copilot_cfg()
    session = Session(provider="copilot", session_id="copilot-session-1")
    ws = _workspace(tmp_path)
    argv = copilot_driver.COPILOT_PROVIDER.argv(
        cfg, settings, "a1", "do", ws, session, None
    )
    assert _session_at(argv) == "copilot-session-1"
    assert "--model" not in argv


def test_argv_keeps_builtin_mcps_when_configured(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = _copilot_cfg(builtin_mcps=True)
    ws = _workspace(tmp_path)
    argv = copilot_driver.COPILOT_PROVIDER.argv(
        cfg, settings, "a1", "do", ws, Session(provider="copilot"), None
    )
    assert "--disable-builtin-mcps" not in argv


# --- boot: guard gate and the minted session ----------------------------------


def test_guard_label_is_none(tmp_path: Path):
    assert copilot_driver.COPILOT_PROVIDER.guard(make_settings(tmp_path), _copilot_cfg()) == "none"


def test_boot_mints_a_session_id(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/copilot")
    settings = make_settings(tmp_path, allow_unguarded=True)
    error, session = copilot_driver.COPILOT_PROVIDER.boot(settings, "a1", _copilot_cfg())
    assert error is None
    uuid.UUID(session.session_id)  # minted up front, not learned from the stream


def test_boot_refuses_unguarded_unless_loop_or_allow_unguarded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/copilot")
    settings = make_settings(tmp_path)  # allow_unguarded defaults off
    error, _ = copilot_driver.COPILOT_PROVIDER.boot(settings, "a1", _copilot_cfg())
    assert error and "allow_unguarded" in error
    # The delegate loop lifts the gate itself.
    error, _ = copilot_driver.COPILOT_PROVIDER.boot(settings, "a1", _copilot_cfg(loop=True))
    assert error is None
    # So does [guard].allow_unguarded = true.
    permissive = make_settings(tmp_path, allow_unguarded=True)
    error, _ = copilot_driver.COPILOT_PROVIDER.boot(permissive, "a1", _copilot_cfg())
    assert error is None


def test_boot_admits_registered_loop_agents_without_touching_config(
        tmp_path: Path, monkeypatch):
    """The delegate loop lifts the unguarded gate for its own agent ids only:
    no config mutation, and a plain MCP delegation is still refused."""
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/copilot")
    settings = make_settings(tmp_path)  # allow_unguarded defaults off
    cfg = _copilot_cfg()  # no extra["loop"]

    error, _ = copilot_driver.COPILOT_PROVIDER.boot(settings, "a1", cfg)
    assert error and "allow_unguarded" in error

    agent_id = f"loop-{uuid.uuid4().hex[:12]}"
    copilot_driver.allow_loop_agent(agent_id)
    error, _ = copilot_driver.COPILOT_PROVIDER.boot(settings, agent_id, cfg)
    assert error is None
    # The registration is scoped to the loop's ids: nobody else gains anything.
    error, _ = copilot_driver.COPILOT_PROVIDER.boot(settings, "a2", cfg)
    assert error and "allow_unguarded" in error
    # The config was never mutated.
    assert "loop" not in cfg.extra


def test_boot_checks_the_binary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    settings = make_settings(tmp_path, allow_unguarded=True)
    error, _ = copilot_driver.COPILOT_PROVIDER.boot(settings, "a1", _copilot_cfg())
    assert error and "not on PATH" in error


# --- the translator on the fixture --------------------------------------------


def test_translator_maps_events_and_credits():
    translator = copilot_driver.Translator("copilot-session-1")
    out: list[dict] = []
    for event in _load("success.jsonl"):
        out.extend(translator.feed(event))
    out.append(translator.finish(0))

    assert [e["type"] for e in out] == ["system", "assistant", "user", "assistant", "result"]
    assert out[0] == {"type": "system", "subtype": "init", "session_id": "copilot-session-1"}

    tool_use = next(
        b for e in out if e["type"] == "assistant"
        for b in e["message"]["content"] if b["type"] == "tool_use"
    )
    assert tool_use == {"type": "tool_use", "id": "1", "name": "bash",
                        "input": {"command": "uv run pytest -q"}}
    tool_result = next(
        b for e in out if e["type"] == "user"
        for b in e["message"]["content"] if b["type"] == "tool_result"
    )
    assert tool_result == {"type": "tool_result", "tool_use_id": "1", "content": "",
                           "is_error": False}

    result = out[-1]
    assert result["is_error"] is False
    assert result["result"] == "the tests pass"
    assert result["num_turns"] == 1
    assert result["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 20,
        "credits": 2.5,  # 2,500,000,000 nano-AIU
    }


def test_translator_reports_error_exit_and_raw_lines():
    translator = copilot_driver.Translator("s")
    for event in _load("success.jsonl"):
        translator.feed(event)
    result = translator.finish(1, 'Error: Model "gpt-5" from --model flag is not available.')
    assert result["is_error"] is True
    assert result["subtype"] == "error"
    assert "not available" in result["error"]


# --- the model-unavailable refusal --------------------------------------------


def test_model_unavailable_is_a_refusal_that_does_not_close():
    cfg = _copilot_cfg()
    events = [{
        "type": "result", "subtype": "error", "is_error": True, "result": "",
        "error": 'Error: Model "gpt-5.6-sol" from --model flag is not available.',
    }]
    refusal = copilot_driver.COPILOT_PROVIDER.refusal(cfg, events)
    assert refusal is not None
    assert refusal.code == router.COPILOT_MODEL_UNAVAILABLE
    # Copilot's refusal is driver-local: the same provider with another model
    # may still run, so the lane is never closed.
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is None

    assert copilot_driver.COPILOT_PROVIDER.refusal(
        cfg, [{"type": "result", "is_error": False, "error": ""}]
    ) is None


# --- end to end: a fake `copilot` binary --------------------------------------

FAKE_COPILOT = """\
import json, os, sys
argv = sys.argv[1:]
record = {"argv": argv, "env": {k: os.environ.get(k) for k in (
    "ANTHROPIC_AUTH_TOKEN", "GLM_API_KEY")}}
with open(os.environ["FAKE_COPILOT_RECORD"], "a") as fh:
    fh.write(json.dumps(record) + "\\n")
with open(os.environ["FAKE_COPILOT_FIXTURE"]) as fh:
    for line in fh:
        if line.strip():
            sys.stdout.write(line if line.endswith("\\n") else line + "\\n")
sys.stdout.flush()
"""


@pytest.fixture
def fake_copilot(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "copilot"
    binary.write_text(f"#!{sys.executable}\n{FAKE_COPILOT}")
    binary.chmod(0o755)
    record = tmp_path / "copilot-calls.jsonl"
    monkeypatch.setenv("FAKE_COPILOT_RECORD", str(record))
    monkeypatch.setenv("FAKE_COPILOT_FIXTURE", str(FIXTURES / "success.jsonl"))

    class Fake:
        path = str(binary)

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


def test_run_carries_credits_and_resumes_the_minted_session(fake_copilot, tmp_path: Path):
    providers = {"copilot": {"driver": "copilot", "binary": fake_copilot.path}}
    settings = make_settings(tmp_path, providers=providers, allow_unguarded=True, run_timeout=30)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("c", _workspace(tmp_path), provider="copilot")
        assert agent.wait_ready(5) is None
        run = agent.submit("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        # The copilot session id is a minted uuid, not the CLI's own string.
        uuid.UUID(run.session_id)
        assert run.session_id == agent.session_id
        # The usage checkpoint's credits ride through Usage into list output.
        assert run.usage.credits == 2.5
        assert agent.info()["usage"]["credits"] == 2.5

        second = agent.follow_up("keep going")
        assert second.done.wait(10)
        first_argv, second_argv = (c["argv"] for c in fake_copilot.calls()[:2])
        assert _session_at(first_argv) == run.session_id
        assert _session_at(second_argv) == run.session_id
        assert "--model" not in first_argv  # the agent named no model
    finally:
        reg.shutdown()
