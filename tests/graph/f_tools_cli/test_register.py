"""register(app): the nine tools, their parameter names and defaults (SPEC 9)."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from .conftest import TOOL_NAMES, FakeApp

REQUIRED = object()

# name -> {parameter: default or REQUIRED}, straight from the table in SPEC 9.
EXPECTED = {
    "graph_validate": {"graph": REQUIRED},
    "graph_start": {"graph": REQUIRED, "inputs": ({}, None), "pool": "default",
                    "budget_usd": None, "priority": 3, "request_id": None, "workspace": None},
    "graph_status": {"root_run_id": REQUIRED, "detail": False},
    "graph_pause": {"root_run_id": REQUIRED, "reason": None},
    "graph_resume": {"root_run_id": REQUIRED},
    "graph_cancel": {"root_run_id": REQUIRED, "reason": REQUIRED},
    "graph_list": {"state": None, "limit": 50},
    "graph_exceptions": {"root_run_id": None, "state": "open", "kind": None, "limit": 100},
    "graph_answer": {"exception_id": REQUIRED, "answer": REQUIRED, "note": None,
                     "answered_by": "orchestrator"},
}


@pytest.fixture
def registered():
    from subagent.graph.mcp_tools import register

    app = FakeApp()
    names = register(app, service_factory=lambda: object())
    return app, names


def test_register_returns_the_nine_tool_names(registered):
    app, names = registered
    assert sorted(names) == sorted(TOOL_NAMES)
    assert sorted(app.tools) == sorted(TOOL_NAMES)


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_parameter_names_and_defaults_match_the_table(registered, tool):
    app, _ = registered
    params = dict(inspect.signature(app.tools[tool]).parameters)
    params.pop("ctx", None)  # an MCP Context parameter is allowed, never required of a caller
    expected = EXPECTED[tool]
    assert list(params) == list(expected), f"{tool} parameters"
    for name, want in expected.items():
        param = params[name]
        if want is REQUIRED:
            assert param.default is inspect.Parameter.empty, f"{tool}.{name} must be required"
        elif isinstance(want, tuple):
            assert param.default in want, f"{tool}.{name} default"
        else:
            assert param.default == want, f"{tool}.{name} default"


def test_register_without_a_factory_still_returns_the_names():
    from subagent.graph.mcp_tools import register

    assert sorted(register(FakeApp())) == sorted(TOOL_NAMES)


def test_tools_are_registered_on_a_real_mcp_server():
    from mcp.server import MCPServer

    from subagent.graph.mcp_tools import register

    app = MCPServer("graph-test")
    register(app, service_factory=lambda: object())
    listed = asyncio.run(app.list_tools())
    names = {t.name for t in listed}
    assert set(TOOL_NAMES) <= names
    start = next(t for t in listed if t.name == "graph_start")
    props = start.input_schema["properties"]
    assert {"graph", "inputs", "pool", "budget_usd", "priority", "request_id", "workspace"} <= set(props)
    assert start.input_schema["required"] == ["graph"]


def test_the_subagent_server_registers_the_graph_tools():
    from subagent import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.app.list_tools())}
    assert set(TOOL_NAMES) <= names
    assert {"delegate", "await", "continue", "list", "cancel", "transcript"} <= names
