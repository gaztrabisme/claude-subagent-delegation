"""R7a lifecycle (d): hitting an agent cap is a named error, never a bare exception."""

from __future__ import annotations

import importlib
import re

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from subagent import config
from subagent.providers import claude as claude_provider
from subagent.runs import Registry, RegistryError

from ..conftest import make_settings
from ..r7a import _helpers as h
from ..test_runs import FakeProcess, _result


def _fill(reg, tmp_path, n):
    return [reg.create_agent(f"a{i}", tmp_path, "glm") for i in range(n)]


def test_d_create_agent_at_the_core_cap_names_cap_count_and_cancel(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())],
                     provider_extra={"max_agents": 9}, max_agents=2)
    try:
        _fill(reg, tmp_path, 2)
        with pytest.raises(RegistryError) as err:
            reg.create_agent("third", tmp_path, "glm")
        text = str(err.value)
        assert text.strip()
        assert re.search(r"\b2\b", text)  # the cap, which is also the live count
        assert "live" in text.lower()
        assert "cancel" in text.lower()
    finally:
        reg.shutdown()


def test_d_create_agent_at_the_provider_cap_names_the_provider(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())],
                     provider_extra={"max_agents": 1}, max_agents=5)
    try:
        _fill(reg, tmp_path, 1)
        with pytest.raises(RegistryError) as err:
            reg.create_agent("second", tmp_path, "glm")
        text = str(err.value)
        assert "glm" in text
        assert re.search(r"\b1\b", text)
        assert "live" in text.lower()
        assert "cancel" in text.lower()
    finally:
        reg.shutdown()


def _server(tmp_path, monkeypatch, **core):
    settings = make_settings(tmp_path, providers=h.provider(max_agents=1), **core)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    registry = Registry(settings, start_reaper=False)
    spawned: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        spawned.append(FakeProcess(_result("done"), argv, env))
        return spawned[-1]

    monkeypatch.setattr(claude_provider, "_spawn_claude", spawn)
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    return module, registry


def _call(module, tmp_path):
    async def go():
        return await module.delegate(
            task="do it", verification="true", workspace=str(tmp_path), provider="glm",
            fallback="none")

    return anyio.run(go)


def test_d_the_mcp_delegate_tool_at_the_provider_cap_returns_a_named_refusal(tmp_path, monkeypatch):
    module, registry = _server(tmp_path, monkeypatch)
    try:
        registry.create_agent("held", tmp_path, "glm")
        with pytest.raises((RegistryError, ToolError)) as err:
            _call(module, tmp_path)
        text = str(err.value)
        assert text.strip()
        assert "glm" in text and re.search(r"\b1\b", text)
        assert "cancel" in text.lower()
    finally:
        registry.shutdown()


def test_d_the_mcp_delegate_tool_at_the_core_cap_names_cap_and_cancel(tmp_path, monkeypatch):
    module, registry = _server(tmp_path, monkeypatch, max_agents=1)
    try:
        registry.create_agent("held", tmp_path, "glm")
        with pytest.raises((RegistryError, ToolError)) as err:
            _call(module, tmp_path)
        text = str(err.value)
        assert text.strip()
        assert re.search(r"\b1\b", text) and "live" in text.lower()
        assert "cancel" in text.lower()
        assert len(registry.agents()) == 1  # nothing half-created
    finally:
        registry.shutdown()
