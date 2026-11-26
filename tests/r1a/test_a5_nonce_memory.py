"""A5 acceptance tests: per-agent nonce memory cleanup and 100,000-entry fail-closed cap."""

from __future__ import annotations

import secrets
from pathlib import Path

from subagent.guard.classify import ALLOW, DENY
from subagent.guard.supervisor import Supervisor
from subagent.runs import Registry
from tests.conftest import make_settings

from ._helpers import send_guard_request, wire_sign


def _signed_read(ws: Path, agent_id: str, key: bytes, nonce: str) -> dict:
    return wire_sign(
        key,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "workspace": str(ws),
            "cwd": str(ws),
            "agent_id": agent_id,
            "nonce": nonce,
        },
    )


def test_a5_used_nonces_dropped_on_close_cancel_and_evict(tmp_path: Path) -> None:
    """A5: an agent's used-nonce set is dropped when the agent is closed, cancelled or evicted."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# readme\n", encoding="utf-8")
    settings = make_settings(tmp_path, workspace=str(ws), supervisor="off")
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)
    try:
        a1 = registry.create_agent("a1", ws, provider="glm")
        a2 = registry.create_agent("a2", ws, provider="glm")
        assert send_guard_request(
            supervisor, _signed_read(ws, a1.agent_id, a1.approval_key, secrets.token_hex(32))
        )["action"] == ALLOW
        assert send_guard_request(
            supervisor, _signed_read(ws, a2.agent_id, a2.approval_key, secrets.token_hex(32))
        )["action"] == ALLOW
        assert a1.agent_id in supervisor._used_nonces
        assert a2.agent_id in supervisor._used_nonces

        # 1. Close a1, then send a request (denied) so either hook-based or lazy pruning runs.
        a1.close("done")
        assert send_guard_request(
            supervisor, _signed_read(ws, a1.agent_id, a1.approval_key, secrets.token_hex(32))
        )["action"] == DENY
        assert a1.agent_id not in supervisor._used_nonces

        # 2. Cancel + evict a2, then check its nonce set is dropped.
        registry.cancel(a2.agent_id)
        registry._evict_closed()
        assert send_guard_request(
            supervisor, _signed_read(ws, a2.agent_id, a2.approval_key, secrets.token_hex(32))
        )["action"] == DENY
        assert a2.agent_id not in supervisor._used_nonces
    finally:
        registry.shutdown()


def test_a5_nonce_cap_100000_fails_closed_without_evicting_old_nonces(tmp_path: Path) -> None:
    """A5: past 100,000 nonces, requests are denied (fail closed) and old nonces are never forgotten."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# readme\n", encoding="utf-8")
    settings = make_settings(tmp_path, workspace=str(ws), supervisor="off")
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)
    try:
        agent = registry.create_agent("cap", ws, provider="glm")
        first_nonce = f"{0:064x}"
        supervisor._used_nonces[agent.agent_id] = {f"{i:064x}" for i in range(100_000)}

        fresh_nonce = f"{100_000:064x}"
        reply_new = send_guard_request(
            supervisor, _signed_read(ws, agent.agent_id, agent.approval_key, fresh_nonce)
        )
        assert reply_new["action"] == DENY
        assert len(supervisor._used_nonces[agent.agent_id]) <= 100_000

        reply_old = send_guard_request(
            supervisor, _signed_read(ws, agent.agent_id, agent.approval_key, first_nonce)
        )
        assert reply_old["action"] == DENY
    finally:
        registry.shutdown()
