"""A6 acceptance tests: approval.key removed when agent closes and closed agent denied."""

from __future__ import annotations

import secrets
from pathlib import Path

from subagent.guard.classify import ALLOW, DENY
from subagent.guard.supervisor import Supervisor
from subagent.runs import Registry
from tests.conftest import make_settings

from ._helpers import send_guard_request, wire_sign


def test_a6_approval_key_deleted_on_close_cancel_and_evict(tmp_path: Path) -> None:
    """A6: agents/<id>/approval.key is deleted when the agent is closed, cancelled or evicted."""
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = make_settings(tmp_path, workspace=str(ws), max_agents=4, supervisor="off")
    registry = Registry(settings, start_reaper=False)
    try:
        a_close = registry.create_agent("close", ws, provider="glm")
        key_close = settings.session_root / "agents" / a_close.agent_id / "approval.key"
        assert key_close.is_file()
        a_close.close("done")
        assert not key_close.exists()

        a_cancel = registry.create_agent("cancel", ws, provider="glm")
        key_cancel = settings.session_root / "agents" / a_cancel.agent_id / "approval.key"
        assert key_cancel.is_file()
        registry.cancel(a_cancel.agent_id)
        assert not key_cancel.exists()

        a_evict = registry.create_agent("evict", ws, provider="glm")
        key_evict = settings.session_root / "agents" / a_evict.agent_id / "approval.key"
        a_evict.close("done")
        # Simulate a stale key file still present at eviction time.
        key_evict.write_text(secrets.token_bytes(32).hex() + "\n", encoding="utf-8")
        key_evict.chmod(0o600)
        registry._evict_closed()
        assert not key_evict.exists()
    finally:
        registry.shutdown()


def test_a6_closed_agent_can_no_longer_obtain_allow(tmp_path: Path) -> None:
    """A6: a closed agent's signed request can no longer obtain an allow."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# readme\n", encoding="utf-8")
    settings = make_settings(tmp_path, workspace=str(ws), supervisor="off")
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)
    try:
        agent = registry.create_agent("a6", ws, provider="glm")
        saved_key = agent.approval_key
        saved_id = agent.agent_id

        req_live = wire_sign(
            saved_key,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": saved_id,
                "nonce": secrets.token_hex(32),
            },
        )
        assert send_guard_request(supervisor, req_live)["action"] == ALLOW

        agent.close("done")
        key_file = settings.session_root / "agents" / saved_id / "approval.key"
        assert not key_file.exists()

        req_after = wire_sign(
            saved_key,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": saved_id,
                "nonce": secrets.token_hex(32),
            },
        )
        assert send_guard_request(supervisor, req_after)["action"] == DENY
    finally:
        registry.shutdown()
