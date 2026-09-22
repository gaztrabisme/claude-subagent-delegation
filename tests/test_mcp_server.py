"""The MCP wrapper: server instructions name the configured providers only,
and `lane=` is a deprecated alias for `provider=`."""

from __future__ import annotations

import importlib

import pytest

from subagent import config

from .conftest import make_settings
from .test_config import _delegate, server  # noqa: F401 - fixture


@pytest.fixture
def instructions_module(tmp_path, monkeypatch):
    """mcp_server imported against a config with only three providers."""
    settings = make_settings(tmp_path, providers={
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
        },
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "llamacpp": {
            "driver": "claude",
            "model": "qwen3.8-27b",
            "api_key_env": "LOCAL_API_KEY",
            "api_key": "local",
            "local": True,
        },
    })
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    monkeypatch.setattr(module, "settings", settings)
    return module


def test_instructions_name_only_the_configured_providers(instructions_module):
    text = instructions_module._instructions()
    # The configured three are named, with driver and local flag.
    assert "glm (driver claude)" in text
    assert "deepseek (driver claude)" in text
    assert "llamacpp (driver claude, local)" in text
    # Providers that are not configured are not named.
    for other in ("codex", "copilot", "grok", "gemini", "omlx", "vllm"):
        assert other not in text


def test_lane_alias_yields_the_same_result_as_provider(server, tmp_path):  # noqa: F811
    by_provider = _delegate(server, tmp_path, provider="deepseek", fallback="none", wait_seconds=5)
    by_lane = _delegate(server, tmp_path, lane="deepseek", fallback="none", wait_seconds=5)
    assert by_provider["lane"] == by_lane["lane"] == "deepseek"
    assert by_provider["state"] == by_lane["state"]
    assert by_provider["model"] == by_lane["model"]
    # Only the deprecated spelling warns.
    assert "warning" not in by_provider
    assert by_lane["warning"] == "`lane` is deprecated; use `provider`"
