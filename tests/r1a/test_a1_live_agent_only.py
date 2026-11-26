"""A1 acceptance tests: live agent only and non-string agent_id hardening."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from subagent.guard import supervisor as sup_mod
from subagent.guard.classify import ALLOW, DENY, Verdict
from subagent.guard.supervisor import Supervisor
from subagent.runs import Registry
from tests.conftest import make_settings

from ._helpers import send_guard_request, wire_sign


def _setup(tmp_path: Path) -> tuple[Path, Path, Registry, Supervisor]:
    server_ws = tmp_path / "server-ws"
    agent_ws = tmp_path / "agent-ws"
    server_ws.mkdir()
    agent_ws.mkdir()
    (agent_ws / "README.md").write_text("# agent\n", encoding="utf-8")
    settings = make_settings(tmp_path, workspace=str(server_ws), supervisor="off")
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)
    return server_ws, agent_ws, registry, supervisor


@pytest.mark.parametrize(
    "bad_id",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("unknown-agent", id="unknown"),
        pytest.param("closed", id="closed"),
        pytest.param(123, id="int"),
        pytest.param(["a"], id="list"),
        pytest.param({"id": "a"}, id="dict"),
        pytest.param(True, id="bool"),
    ],
)
def test_a1_handle_denies_non_live_and_non_string_agent_ids(
    tmp_path: Path, monkeypatch, bad_id: Any
) -> None:
    """A1: Supervisor._handle denies missing, empty, unknown, closed, and non-string agent_id."""
    server_ws, agent_ws, registry, supervisor = _setup(tmp_path)
    seen_workspaces: list[Path] = []
    orig_classify = sup_mod._classify

    def spy_classify(tool_name, tool_input, workspace, cwd=None, guard_context=None):
        seen_workspaces.append(workspace)
        return orig_classify(tool_name, tool_input, workspace, cwd, guard_context=guard_context)

    monkeypatch.setattr(sup_mod, "_classify", spy_classify)
    try:
        closed_agent = registry.create_agent("c", agent_ws, provider="glm")
        closed_key = closed_agent.approval_key
        closed_agent.close("done")
        target_id = closed_agent.agent_id if bad_id == "closed" else bad_id
        key = closed_key if bad_id == "closed" else secrets.token_bytes(32)

        caller_ws = tmp_path / "caller-ws"
        caller_ws.mkdir()
        body: dict[str, Any] = {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "workspace": str(caller_ws),
            "cwd": str(agent_ws),
            "nonce": secrets.token_hex(32),
        }
        if target_id is not None:
            body["agent_id"] = target_id
        reply = send_guard_request(supervisor, wire_sign(key, body))
        assert reply["action"] == DENY
        assert seen_workspaces == []
        assert server_ws not in seen_workspaces
        assert caller_ws not in seen_workspaces
    finally:
        registry.shutdown()


@pytest.mark.parametrize(
    "bad_id",
    [
        pytest.param(["a"], id="list"),
        pytest.param({"id": "a"}, id="dict"),
        pytest.param(123, id="int"),
        pytest.param(True, id="bool"),
    ],
)
def test_a1_workspace_for_raises_value_error_for_non_string_before_lookup(
    tmp_path: Path, monkeypatch, bad_id: Any
) -> None:
    """A1: workspace_for raises ValueError for non-string agent_id before any registry lookup."""
    _, agent_ws, registry, supervisor = _setup(tmp_path)
    lookup_calls: list[Any] = []
    orig_find = registry.find_agent

    def spy_find(agent_id: Any) -> Any:
        lookup_calls.append(agent_id)
        return orig_find(agent_id)

    monkeypatch.setattr(registry, "find_agent", spy_find)
    try:
        with pytest.raises(ValueError, match="live agent"):
            supervisor.workspace_for(bad_id, str(agent_ws))
        assert lookup_calls == []
    finally:
        registry.shutdown()


def test_a1_supervisor_uses_agent_workspace_never_server_or_caller_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """A1: live agent requests are judged against agent.workspace, never server or caller workspace."""
    server_ws, agent_ws, registry, supervisor = _setup(tmp_path)
    caller_ws = tmp_path / "caller-ws"
    caller_ws.mkdir()
    seen_workspaces: list[Path] = []

    def spy_classify(tool_name, tool_input, workspace, cwd=None, guard_context=None) -> Verdict:
        seen_workspaces.append(workspace)
        return Verdict(ALLOW, "ok", {})

    monkeypatch.setattr(sup_mod, "_classify", spy_classify)
    try:
        agent = registry.create_agent("live", agent_ws, provider="glm")
        req = wire_sign(
            agent.approval_key,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
                "workspace": str(caller_ws),
                "cwd": str(agent_ws),
                "agent_id": agent.agent_id,
                "nonce": secrets.token_hex(32),
            },
        )
        reply = send_guard_request(supervisor, req)
        assert reply["action"] == ALLOW
        assert seen_workspaces == [agent_ws]
        assert seen_workspaces[0] != server_ws
        assert seen_workspaces[0] != caller_ws
    finally:
        registry.shutdown()
