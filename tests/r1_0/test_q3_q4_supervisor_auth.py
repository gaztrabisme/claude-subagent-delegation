"""Q3 and Q4 acceptance tests: Supervisor request authentication and signed replies."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import stat
from pathlib import Path
from typing import Any

import pytest

from subagent.guard import supervisor as sup_mod
from subagent.guard.classify import ALLOW, DENY
from subagent.guard.supervisor import Supervisor
from subagent.runs import Registry
from tests.conftest import make_settings

from ._helpers import send_guard_request, wire_sign, wire_verify


def _setup_registry_and_supervisor(
    tmp_path: Path, *, supervisor_mode: str = "agent"
) -> tuple[Path, Registry, Supervisor]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "README.md").write_text("# readme\n", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        supervisor=supervisor_mode,
        supervisor_cmd="claude -p --model sonnet",
    )
    registry = Registry(settings, start_reaper=False)
    supervisor = Supervisor(settings, registry=registry)
    return ws, registry, supervisor


def test_q3_agent_gets_32_byte_approval_key_and_0600_key_file(tmp_path: Path) -> None:
    """Q3: Registry.create_agent mints a 32-byte approval_key and writes 0600 approval.key."""
    ws, registry, _ = _setup_registry_and_supervisor(tmp_path)
    try:
        a1 = registry.create_agent("a1", ws, provider="glm")
        a2 = registry.create_agent("a2", ws, provider="glm")

        assert isinstance(a1.approval_key, bytes)
        assert len(a1.approval_key) == 32
        assert isinstance(a2.approval_key, bytes)
        assert len(a2.approval_key) == 32
        assert a1.approval_key != a2.approval_key

        key_file = registry.settings.session_root / "agents" / a1.agent_id / "approval.key"
        assert key_file.is_file()
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        raw_hex = key_file.read_text(encoding="utf-8").strip()
        assert len(raw_hex) == 64
        assert bytes.fromhex(raw_hex) == a1.approval_key
    finally:
        registry.shutdown()


def test_q3_approval_auth_sign_and_verify_contract() -> None:
    """Q3/Q4: subagent.guard.approval_auth.sign and verify match canonical HMAC-SHA256."""
    from subagent.guard.approval_auth import sign, verify

    key = secrets.token_bytes(32)
    other_key = secrets.token_bytes(32)
    payload = {"action": "allow", "reason": "ok", "nonce": "a" * 64}
    signed = sign(key, payload)

    assert "mac" not in payload  # sign returns a copy without mutating input
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    expected_mac = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    assert signed["mac"] == expected_mac
    assert verify(key, signed) is True
    assert verify(other_key, signed) is False

    tampered = {**signed, "reason": "tampered"}
    assert verify(key, tampered) is False
    assert verify(key, payload) is False
    assert verify(key, {**payload, "mac": 12345}) is False


def test_q3_valid_signed_request_matches_decide(tmp_path: Path) -> None:
    """Q3: a correctly signed request for a live agent gets the exact verdict of decide()."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)
    try:
        agent = registry.create_agent("live", ws, provider="glm")
        key = agent.approval_key

        for tool_name, tool_input, expected_action in (
            ("Read", {"file_path": "README.md"}, ALLOW),
            ("Read", {"file_path": "~/.ssh/id_rsa"}, DENY),
        ):
            req = wire_sign(
                key,
                {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "workspace": str(ws),
                    "cwd": str(ws),
                    "agent_id": agent.agent_id,
                    "nonce": secrets.token_hex(32),
                },
            )
            reply = send_guard_request(supervisor, req)
            direct = asyncio.run(
                supervisor.decide(
                    tool_name, tool_input, agent.workspace, agent.agent_id, str(ws), agent.guard_context
                )
            )
            assert reply["action"] == expected_action == direct.action
            assert reply["reason"] == direct.reason
    finally:
        registry.shutdown()


def test_q3_missing_or_wrong_mac_denied_without_escalation_or_denials(
    tmp_path: Path, monkeypatch
) -> None:
    """Q3: missing mac or wrong mac gets deny without calling _escalate or adding denials."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)

    async def fail_escalate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_escalate must not be called on unauthenticated request")

    monkeypatch.setattr(supervisor, "_escalate", fail_escalate)
    try:
        agent = registry.create_agent("live", ws, provider="glm")
        for base_call in (
            {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
        ):
            missing_mac = {
                **base_call,
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": agent.agent_id,
                "nonce": secrets.token_hex(32),
            }
            reply_missing = send_guard_request(supervisor, missing_mac)
            assert reply_missing["action"] == DENY

            wrong_mac = {
                **missing_mac,
                "nonce": secrets.token_hex(32),
                "mac": "0" * 64,
            }
            reply_wrong = send_guard_request(supervisor, wrong_mac)
            assert reply_wrong["action"] == DENY

        assert supervisor.denials == {}
    finally:
        registry.shutdown()


def test_q3_mac_with_another_agents_key_is_denied(tmp_path: Path, monkeypatch) -> None:
    """Q3: a request signed with another agent's key is denied without escalation or denials."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)

    async def fail_escalate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_escalate must not be called for cross-agent key")

    monkeypatch.setattr(supervisor, "_escalate", fail_escalate)
    try:
        a1 = registry.create_agent("a1", ws, provider="glm")
        a2 = registry.create_agent("a2", ws, provider="glm")
        for tool_call in (
            {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
        ):
            forged = wire_sign(
                a2.approval_key,
                {
                    **tool_call,
                    "workspace": str(ws),
                    "cwd": str(ws),
                    "agent_id": a1.agent_id,
                    "nonce": secrets.token_hex(32),
                },
            )
            reply = send_guard_request(supervisor, forged)
            assert reply["action"] == DENY
        assert supervisor.denials == {}
    finally:
        registry.shutdown()


@pytest.mark.parametrize(
    "bad_nonce",
    [
        pytest.param(None, id="missing-nonce"),
        pytest.param("", id="empty-nonce"),
        pytest.param("abc", id="short-nonce"),
        pytest.param("a" * 63, id="63-hex"),
        pytest.param("a" * 65, id="65-hex"),
        pytest.param("g" * 64, id="non-hex-64"),
        pytest.param(12345, id="int-nonce"),
    ],
)
def test_q3_missing_or_malformed_nonce_is_denied(
    tmp_path: Path, monkeypatch, bad_nonce: Any
) -> None:
    """Q3: missing or malformed nonce is denied without calling _escalate or adding denials."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)

    async def fail_escalate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_escalate must not be called on invalid nonce")

    monkeypatch.setattr(supervisor, "_escalate", fail_escalate)
    try:
        agent = registry.create_agent("live", ws, provider="glm")
        key = getattr(agent, "approval_key", b"fallback-32-byte-key-for-base---")
        for tool_call in (
            {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
        ):
            body: dict[str, Any] = {
                **tool_call,
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": agent.agent_id,
            }
            if bad_nonce is not None:
                body["nonce"] = bad_nonce
            reply = send_guard_request(supervisor, wire_sign(key, body))
            assert reply["action"] == DENY
        assert supervisor.denials == {}
    finally:
        registry.shutdown()


def test_q3_replayed_nonce_is_denied_without_escalation_or_denials(
    tmp_path: Path, monkeypatch
) -> None:
    """Q3: reusing a previously accepted nonce is denied as a replay."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)

    async def fail_escalate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_escalate must not be called on replayed nonce")

    monkeypatch.setattr(supervisor, "_escalate", fail_escalate)
    try:
        agent = registry.create_agent("live", ws, provider="glm")
        nonce = secrets.token_hex(32)
        first = wire_sign(
            agent.approval_key,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": agent.agent_id,
                "nonce": nonce,
            },
        )
        assert send_guard_request(supervisor, first)["action"] == ALLOW

        replay_allow = wire_sign(
            agent.approval_key,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": agent.agent_id,
                "nonce": nonce,
            },
        )
        assert send_guard_request(supervisor, replay_allow)["action"] == DENY

        replay_escalate = wire_sign(
            agent.approval_key,
            {
                "tool_name": "WebFetch",
                "tool_input": {"url": "https://example.com"},
                "workspace": str(ws),
                "cwd": str(ws),
                "agent_id": agent.agent_id,
                "nonce": nonce,
            },
        )
        assert send_guard_request(supervisor, replay_escalate)["action"] == DENY
        assert supervisor.denials == {}
    finally:
        registry.shutdown()


def test_q3_empty_missing_unknown_evicted_agent_id_denied_without_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    """Q3: empty, missing, unknown, or evicted agent_id is denied without escalation or denials."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)

    async def fail_escalate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_escalate must not be called for non-live agent_id")

    monkeypatch.setattr(supervisor, "_escalate", fail_escalate)
    try:
        evicted = registry.create_agent("evicted", ws, provider="glm")
        evicted_id = evicted.agent_id
        evicted.close("done")
        registry._evict_closed()
        assert registry.find_agent(evicted_id) is None

        random_key = secrets.token_bytes(32)
        for bad_agent_id in (None, "", "unknown-agent-id", evicted_id):
            for tool_call in (
                {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
                {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
            ):
                body: dict[str, Any] = {
                    **tool_call,
                    "workspace": str(ws),
                    "cwd": str(ws),
                    "nonce": secrets.token_hex(32),
                }
                if bad_agent_id is not None:
                    body["agent_id"] = bad_agent_id
                reply = send_guard_request(supervisor, wire_sign(random_key, body))
                assert reply["action"] == DENY, (
                    f"expected deny for agent_id={bad_agent_id!r}, got {reply!r}"
                )
        assert supervisor.denials == {}
    finally:
        registry.shutdown()


def test_q4_supervisor_signs_allow_deny_and_error_replies_for_known_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """Q4: every reply for a known agent (allow, deny, error) echoes nonce and has valid mac."""
    ws, registry, supervisor = _setup_registry_and_supervisor(tmp_path)
    try:
        agent = registry.create_agent("known", ws, provider="glm")
        key = getattr(agent, "approval_key", b"\x00" * 32)

        # 1. allow
        allow_nonce = secrets.token_hex(32)
        allow_reply = send_guard_request(
            supervisor,
            wire_sign(
                key,
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "README.md"},
                    "workspace": str(ws),
                    "cwd": str(ws),
                    "agent_id": agent.agent_id,
                    "nonce": allow_nonce,
                },
            ),
        )
        assert allow_reply["action"] == ALLOW
        assert allow_reply.get("nonce") == allow_nonce
        assert wire_verify(agent.approval_key, allow_reply)

        # 2. deny
        deny_nonce = secrets.token_hex(32)
        deny_reply = send_guard_request(
            supervisor,
            wire_sign(
                key,
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "~/.ssh/id_rsa"},
                    "workspace": str(ws),
                    "cwd": str(ws),
                    "agent_id": agent.agent_id,
                    "nonce": deny_nonce,
                },
            ),
        )
        assert deny_reply["action"] == DENY
        assert deny_reply.get("nonce") == deny_nonce
        assert wire_verify(agent.approval_key, deny_reply)

        # 3. error inside Supervisor._handle
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("injected classifier failure")

        monkeypatch.setattr(sup_mod, "_classify", boom)
        error_nonce = secrets.token_hex(32)
        error_reply = send_guard_request(
            supervisor,
            wire_sign(
                key,
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "README.md"},
                    "workspace": str(ws),
                    "cwd": str(ws),
                    "agent_id": agent.agent_id,
                    "nonce": error_nonce,
                },
            ),
        )
        assert error_reply["action"] == DENY
        assert error_reply.get("nonce") == error_nonce
        assert wire_verify(agent.approval_key, error_reply)
    finally:
        registry.shutdown()
