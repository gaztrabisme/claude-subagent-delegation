"""Shared test fixtures. Keep Settings construction in one place."""

from __future__ import annotations

from pathlib import Path

from glm_subagent_mcp.config import DEFAULT_BASE_URL, DEFAULT_FLASH_MODEL, DEFAULT_MODEL, Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "api_key": "test-key",
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "flash_model": DEFAULT_FLASH_MODEL,
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
    settings = Settings(**values)
    settings.session_root.mkdir(parents=True, exist_ok=True)
    return settings
