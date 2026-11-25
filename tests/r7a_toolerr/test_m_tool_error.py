"""R7a (m): a RegistryError raised inside a tool body reaches the client as ToolError with its text.

The MCP framework turns any non-ToolError exception into the bare text
"Error executing tool <name>", so the reason is lost unless the tool re-raises as ToolError.
"""

from __future__ import annotations

import importlib
import re

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from subagent import config
from subagent.providers import claude as claude_provider
from subagent.runs import Registry

from ..conftest import make_settings
from ..r7a import _helpers as h
from ..test_runs import FakeProcess, _result


def _server(tmp_path, monkeypatch, **core):
    settings = make_settings(tmp_path, providers=h.provider(max_agents=1), **core)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    registry = Registry(settings, start_reaper=False)

    def spawn(argv, env, cwd):
        return FakeProcess(_result("done"), argv, env)

    monkeypatch.setattr(claude_provider, "_spawn_claude", spawn)
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    return module, registry


def _delegate(module, workspace):
    async def go():
        return await module.delegate(
            task="do it", verification="true", workspace=str(workspace), provider="glm",
            fallback="none")

    return anyio.run(go)


def _is_plain_tool_error(exc: BaseException) -> bool:
    return isinstance(exc, ToolError) and not isinstance(exc, UnexpectedToolError)


def test_m_delegate_at_the_lane_cap_raises_tool_error_with_cap_text(tmp_path, monkeypatch):
    module, registry = _server(tmp_path, monkeypatch)
    try:
        registry.create_agent("held", tmp_path, "glm")
        with pytest.raises(Exception) as err:
            _delegate(module, tmp_path)
        assert _is_plain_tool_error(err.value), type(err.value).__name__
        text = str(err.value)
        assert "glm" in text and re.search(r"\b1\b", text)
        assert "cancel" in text.lower()
    finally:
        registry.shutdown()


def test_m_delegate_with_an_unknown_workspace_raises_tool_error_with_message(tmp_path, monkeypatch):
    module, registry = _server(tmp_path, monkeypatch)
    try:
        missing = tmp_path / "no-such-dir"
        with pytest.raises(Exception) as err:
            _delegate(module, missing)
        assert _is_plain_tool_error(err.value), type(err.value).__name__
        assert "no-such-dir" in str(err.value) or "directory" in str(err.value)
    finally:
        registry.shutdown()


def test_m_continue_on_a_closed_agent_raises_tool_error_with_message(tmp_path, monkeypatch):
    module, registry = _server(tmp_path, monkeypatch)
    try:
        agent = registry.create_agent("done", tmp_path, "glm")
        agent.close()

        async def go():
            return await module.continue_agent(agent_id=agent.agent_id, message="more")

        with pytest.raises(Exception) as err:
            anyio.run(go)
        assert _is_plain_tool_error(err.value), type(err.value).__name__
        assert str(err.value).strip()
    finally:
        registry.shutdown()


@pytest.mark.parametrize("tool,args", [
    ("cancel", {"agent_id": "nope"}),
    ("await", {"run_id": "nope", "wait_seconds": 0}),
    ("transcript", {"run_id": "nope"}),
])
def test_m_unknown_id_in_other_tools_is_a_tool_error_with_the_id(tmp_path, monkeypatch, tool, args):
    module, registry = _server(tmp_path, monkeypatch)
    try:
        async def go():
            return await module.app.call_tool(tool, args)

        with pytest.raises(Exception) as err:
            anyio.run(go)
        assert _is_plain_tool_error(err.value), type(err.value).__name__
        assert "nope" in str(err.value)
    finally:
        registry.shutdown()


def test_m_end_to_end_through_the_mcp_wrapper_the_cap_text_reaches_the_client(tmp_path, monkeypatch):
    module, registry = _server(tmp_path, monkeypatch)
    try:
        registry.create_agent("held", tmp_path, "glm")

        async def go():
            return await module.app.call_tool("delegate", {
                "task": "do it", "verification": "true", "workspace": str(tmp_path),
                "provider": "glm", "fallback": "none"})

        with pytest.raises(ToolError) as err:
            anyio.run(go)
        text = str(err.value)
        assert not isinstance(err.value, UnexpectedToolError)
        assert text.strip() != "Error executing tool delegate"
        assert re.search(r"\b1\b", text) and "cancel" in text.lower()
    finally:
        registry.shutdown()
