"""Shared test fixtures. Keep Settings construction in one place."""

from __future__ import annotations

from pathlib import Path

import pytest

from subagent_mcp.config import Settings
from subagent_mcp.lanes import load_lanes

# Every lane's key variable. Tests never see the machine's real keys.
KEY_ENVS = ("GLM_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY", "SAM_BPPC_API_KEY",
            "SAM_OMLX_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _lane_keys(monkeypatch):
    """The glm lane (the default) gets a fake key; every other key is unset."""
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    # No test may reach the real Codex CLI or the user's Codex login.
    monkeypatch.setenv("SAM_CODEX_BIN", "/nonexistent/codex-disabled-in-tests")
    monkeypatch.setenv("CODEX_HOME", "/nonexistent/codex-home-disabled-in-tests")


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings for a test. Lanes resolve their knobs from these values, as
    they would from the matching global SAM_ variables."""
    values = {
        "workspace": tmp_path,
        "session_root": tmp_path / "sessions",
        "claude_bin": "claude",
        "max_agents": 4,
        "transcript_limit": 400,
        "summary_tokens": 2000,
        "idle_timeout": 900.0,
        "run_timeout": 1800.0,
        "turn_token_budget": None,
        "loop_strikes": 3,
        "supervisor": "off",
        "supervisor_cmd": "claude -p --model sonnet",
        "supervisor_timeout": 120.0,
        "approval_socket": str(tmp_path / "approval.sock"),
        "log_level": "info",
        "max_steps": 40,
        "verify_timeout": 300.0,
        "chars_per_token": 3.5,
        "run_archive": 200,
        "trace": "off",
        "compact_window": 1_000_000,
        "rate_limit_retries": 3,
        "rate_limit_backoff": 5.0,
        "throttle_backoff": 60.0,
    }
    values.update(overrides)
    if "lanes" not in values:
        # Only knobs a test overrode become globals, so the local lanes keep
        # their own defaults, as they would with the SAM_ variables unset.
        knobs = ("max_agents", "compact_window", "max_steps", "run_timeout", "idle_timeout")
        values["lanes"] = load_lanes({
            f"SAM_{knob.upper()}": str(overrides[knob]) for knob in knobs if knob in overrides
        })
    settings = Settings(**values)
    settings.session_root.mkdir(parents=True, exist_ok=True)
    return settings
