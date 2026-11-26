"""graph_validate and the `unavailable` path without temporalio (SPEC 9, 12.2 f)."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from tests.conftest import make_settings

from .conftest import SIMPLE_DOC, FakeApp, call, import_graph

INSTALL_HINT = "uv sync --extra graph"


def _tools(tmp_path):
    from subagent.graph.mcp_tools import register
    from subagent.graph.server import GraphSettings
    from subagent.graph.service import GraphService

    service = GraphService(make_settings(tmp_path),
                           gsettings=GraphSettings(state_dir=tmp_path / "state"))
    app = FakeApp()
    register(app, service_factory=lambda: service)
    return app.tools


def _unavailable_tools(tmp_path):
    """Tools wired to a real GraphService, imported while temporalio is absent."""
    server = import_graph("server")
    service = import_graph("service")
    settings = make_settings(tmp_path)
    gsettings = server.GraphSettings(state_dir=tmp_path / "state")
    return import_graph("mcp_tools"), service.GraphService(settings, gsettings=gsettings)


def test_validate_success_shape(tmp_path):
    from subagent.graph import graph_hash

    result = asyncio.run(call(_tools(tmp_path)["graph_validate"], graph=SIMPLE_DOC))
    assert result["ok"] is True
    assert result["graph_id"] == "simple"
    assert result["version"] == 1
    assert result["graph_hash"] == graph_hash(SIMPLE_DOC)
    assert re.fullmatch(r"[0-9a-f]{64}", result["graph_hash"])
    assert result["nodes"] == 2
    assert result["problems"] == []


def test_validate_accepts_json_text_and_a_file_path(tmp_path):
    tool = _tools(tmp_path)["graph_validate"]
    as_text = asyncio.run(call(tool, graph=json.dumps(SIMPLE_DOC)))
    path = tmp_path / "g.json"
    path.write_text(json.dumps(SIMPLE_DOC), encoding="utf-8")
    as_path = asyncio.run(call(tool, graph=str(path)))
    assert as_text["ok"] is True and as_path["ok"] is True
    assert as_text["graph_hash"] == as_path["graph_hash"]


def test_validate_invalid_graph_returns_the_problems(tmp_path):
    bad = {**SIMPLE_DOC, "bogus": 1}
    result = asyncio.run(call(_tools(tmp_path)["graph_validate"], graph=bad))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_graph"
    problems = result["error"]["problems"]
    assert {"path": "$", "code": "unknown_key", "message": "unknown key 'bogus'"} in [
        {k: p[k] for k in ("path", "code", "message")} for p in problems
    ]
    json.dumps(result)  # the whole envelope is JSON-safe


def test_validate_reports_every_problem_not_just_the_first(tmp_path):
    bad = {"schema": "subagent.graph/1", "id": "Bad Id", "version": 1,
           "body": [{"id": "x", "kind": "nope"}], "extra": True}
    result = asyncio.run(call(_tools(tmp_path)["graph_validate"], graph=bad))
    codes = {p["code"] for p in result["error"]["problems"]}
    assert {"bad_id", "unknown_key", "unknown_kind"} <= codes


def test_validate_unparseable_text_is_invalid_graph(tmp_path):
    result = asyncio.run(call(_tools(tmp_path)["graph_validate"], graph="{not json"))
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_graph"
    assert result["error"]["problems"][0]["code"] == "parse_error"


def test_validate_works_without_temporalio(no_temporalio, tmp_path):
    mcp_tools, service = _unavailable_tools(tmp_path)
    from subagent.graph import temporal_available

    assert temporal_available() is False
    app = FakeApp()
    mcp_tools.register(app, service_factory=lambda: service)
    ok = asyncio.run(call(app.tools["graph_validate"], graph=SIMPLE_DOC))
    assert ok["ok"] is True and ok["nodes"] == 2
    bad = asyncio.run(call(app.tools["graph_validate"], graph={"id": "x"}))
    assert bad["error"]["code"] == "invalid_graph"


CALLS = {
    "graph_start": {"graph": SIMPLE_DOC},
    "graph_status": {"root_run_id": "gr-000000000000"},
    "graph_pause": {"root_run_id": "gr-000000000000", "reason": "x"},
    "graph_resume": {"root_run_id": "gr-000000000000"},
    "graph_cancel": {"root_run_id": "gr-000000000000", "reason": "stop"},
    "graph_list": {},
    "graph_exceptions": {},
    "graph_answer": {"exception_id": "gx-000000000000", "answer": "go"},
}


@pytest.mark.parametrize("tool", sorted(CALLS))
def test_every_other_tool_returns_unavailable_without_temporalio(no_temporalio, tmp_path, tool):
    mcp_tools, service = _unavailable_tools(tmp_path)
    app = FakeApp()
    mcp_tools.register(app, service_factory=lambda: service)
    result = asyncio.run(call(app.tools[tool], **CALLS[tool]))
    assert result["ok"] is False
    assert result["error"]["code"] == "unavailable"
    assert INSTALL_HINT in result["error"]["message"]
    assert "install the graph extra" in result["error"]["message"]


def test_graph_unavailable_is_exported_and_a_runtime_error(no_temporalio):
    graph = __import__("subagent.graph", fromlist=["GraphUnavailable"])
    assert issubclass(graph.GraphUnavailable, RuntimeError)
    assert graph.temporal_available() is False


def test_missing_temporal_cli_is_unavailable_with_its_own_message(tmp_path, monkeypatch):
    pytest.importorskip("temporalio")
    from subagent.graph import GraphUnavailable
    from subagent.graph.server import find_temporal_cli

    monkeypatch.delenv("TEMPORAL_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GraphUnavailable) as err:
        find_temporal_cli()
    assert "Temporal CLI not found: put temporal on PATH or set TEMPORAL_BIN" in str(err.value)
