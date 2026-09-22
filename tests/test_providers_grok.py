"""The grok driver, against a fake `grok` binary that replays real fixtures.

Grok Build's streaming-messages-json is already Claude stream-json, so this
driver has no translator; the tests check the argv shape, the resume flag, the
guard label, the XAI_API_KEY handoff, and that a spent-balance result (the
402 fixture) is a refusal that closes the lane.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from subagent import router
from subagent.providers import grok as grok_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import FAILED, Registry

from .conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "grok"


def _grok_cfg(**extra) -> ProviderConfig:
    return ProviderConfig(name="grok", driver="grok", vendor="grok", extra=extra)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


# --- argv and resume ----------------------------------------------------------


def test_argv_flags_and_model(tmp_path: Path):
    settings = make_settings(tmp_path)
    ws = _workspace(tmp_path)
    argv = grok_driver.GROK_PROVIDER.argv(
        _grok_cfg(), settings, "a1", "do it", ws, Session(provider="grok"), "grok-4.6"
    )
    assert argv[:2] == ["grok", "-p"]
    assert argv[2] == "do it"
    assert argv[argv.index("--output-format") + 1] == "streaming-messages-json"
    assert "--always-approve" in argv
    assert "--no-auto-update" in argv
    assert argv[argv.index("--cwd") + 1] == str(ws)
    assert argv[argv.index("-m") + 1] == "grok-4.6"
    assert "-r" not in argv  # a first turn has no session to resume


def test_argv_resume_passes_session_id(tmp_path: Path):
    settings = make_settings(tmp_path)
    ws = _workspace(tmp_path)
    argv = grok_driver.GROK_PROVIDER.argv(
        _grok_cfg(), settings, "a1", "do", ws,
        Session(provider="grok", session_id="grok-sess-1"), None,
    )
    assert "-m" not in argv  # no model named
    assert argv[argv.index("-r") + 1] == "grok-sess-1"


# --- guard label ---------------------------------------------------------------


def test_guard_label_follows_hooks_extra(tmp_path: Path):
    settings = make_settings(tmp_path)
    assert grok_driver.GROK_PROVIDER.guard(settings, _grok_cfg()) == grok_driver.GUARD_SANDBOX
    assert grok_driver.GROK_PROVIDER.guard(settings, _grok_cfg(hooks=True)) == grok_driver.GUARD_HOOK


# --- env: XAI_API_KEY handoff --------------------------------------------------


def test_env_sets_xai_key_and_strips_leaked(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-token")
    providers = {"grok": {"driver": "grok", "api_key_env": "XAI_API_KEY"}}
    settings = make_settings(tmp_path, providers=providers)
    cfg = settings.provider("grok")
    session = Session(provider="grok")

    env = grok_driver.GROK_PROVIDER.env(settings, "a1", cfg, session)
    assert "XAI_API_KEY" not in env  # no key configured
    assert "ANTHROPIC_AUTH_TOKEN" not in env  # the server's own key is leaked out

    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    env = grok_driver.GROK_PROVIDER.env(settings, "a1", cfg, session)
    assert env["XAI_API_KEY"] == "xai-test"


# --- end to end: the 402 fixture closes the lane -------------------------------

FAKE_GROK = """\
import json, os, sys
argv = sys.argv[1:]
record = {"argv": argv, "env": {k: os.environ.get(k) for k in (
    "XAI_API_KEY", "ANTHROPIC_AUTH_TOKEN")}}
with open(os.environ["FAKE_GROK_RECORD"], "a") as fh:
    fh.write(json.dumps(record) + "\\n")
with open(os.environ["FAKE_GROK_FIXTURE"]) as fh:
    for line in fh:
        if line.strip():
            sys.stdout.write(line if line.endswith("\\n") else line + "\\n")
sys.stdout.flush()
"""


@pytest.fixture
def fake_grok(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "grok"
    binary.write_text(f"#!{sys.executable}\n{FAKE_GROK}")
    binary.chmod(0o755)
    record = tmp_path / "grok-calls.jsonl"
    monkeypatch.setenv("FAKE_GROK_RECORD", str(record))
    monkeypatch.setenv("FAKE_GROK_FIXTURE", str(FIXTURES / "402.jsonl"))

    class Fake:
        path = str(binary)

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


def test_402_balance_is_a_refusal_that_closes_the_lane(fake_grok, tmp_path: Path):
    providers = {"grok": {"driver": "grok", "binary": fake_grok.path}}
    settings = make_settings(tmp_path, providers=providers, run_timeout=30)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("g", _workspace(tmp_path), provider="grok")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", "true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert run.finish_reason == "refused"
        hop = run.hops[-1]
        assert hop["outcome"] == router.HOP_REFUSED
        assert hop["code"] == router.GROK_BALANCE
        closed = reg.lane_state.closed("grok")
        assert closed is not None and closed["code"] == router.GROK_BALANCE
        assert len(fake_grok.calls()) == 1  # not retried, no fallback lane
    finally:
        reg.shutdown()
