"""A2 acceptance tests: no fallback to Supervisor.default_guard_context."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from subagent.guard import supervisor as sup_mod
from subagent.guard.classify import DENY
from subagent.guard.supervisor import Supervisor
from subagent.runs import Registry
from tests.conftest import make_settings

from ._helpers import send_guard_request, wire_sign


@pytest.mark.parametrize("mode", ["missing", "none", "empty"])
def test_a2_live_agent_without_guard_context_is_denied(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    """A2: a live agent whose guard_context is missing, None or empty is denied without default_guard_context."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# readme\n", encoding="utf-8")
    settings = make_settings(tmp_path, workspace=str(ws), supervisor="off")
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)

    seen_contexts: list[object] = []
    orig_classify = sup_mod._classify

    def spy_classify(tool_name, tool_input, workspace, cwd=None, guard_context=None):
        seen_contexts.append(guard_context)
        return orig_classify(tool_name, tool_input, workspace, cwd, guard_context=guard_context)

    monkeypatch.setattr(sup_mod, "_classify", spy_classify)
    try:
        agent = registry.create_agent("a2", ws, provider="glm")
        if mode == "missing":
            delattr(agent, "guard_context")
        elif mode == "none":
            agent.guard_context = None  # type: ignore[assignment]
        else:
            agent.guard_context = {}

        req = wire_sign(
            agent.approval_key,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": agent.agent_id,
                "nonce": secrets.token_hex(32),
            },
        )
        reply = send_guard_request(supervisor, req)
        assert reply["action"] == DENY
        assert supervisor.default_guard_context not in seen_contexts
    finally:
        registry.shutdown()


def test_a2_workspace_for_does_not_return_default_guard_context(tmp_path: Path) -> None:
    """A2: Supervisor.workspace_for never falls back to default_guard_context when guard_context is empty or None."""
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = make_settings(tmp_path, workspace=str(ws), supervisor="off")
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)
    try:
        agent = registry.create_agent("a2b", ws, provider="glm")
        for bad in (None, {}):
            agent.guard_context = bad  # type: ignore[assignment]
            with pytest.raises(ValueError):
                supervisor.workspace_for(agent.agent_id, None)
    finally:
        registry.shutdown()
