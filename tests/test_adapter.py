"""The per-provider request adapter (U-B1) and the local wake-up (U-B2)."""

from __future__ import annotations

import http.client
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from subagent import adapter, health

from .conftest import make_settings, provider_cfg

# The shape Claude Code 2.1.276 sends, cut down: a system entry after the
# first user message, as the last message.
CLAUDE_CODE_BODY = {
    "model": "qwen3.8-27b",
    "stream": True,
    "system": [{"type": "text", "text": "You are Claude Code."},
               {"type": "text", "text": "tools...", "cache_control": {"type": "ephemeral"}}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x"},
                                     {"type": "text", "text": "say hi"}]},
        {"role": "system", "content": [{"type": "text", "text": "# Environment\ncwd: /ws"}]},
    ],
}


@pytest.fixture(autouse=True)
def _stop_adapters():
    yield
    adapter.shutdown()


def test_adapter_fold_system_moves_trailing_system_into_the_user_turn():
    out = adapter.fold_system(CLAUDE_CODE_BODY)
    assert [m["role"] for m in out["messages"]] == ["user"]
    texts = [b["text"] for b in out["messages"][0]["content"]]
    assert texts == ["<system-reminder>x", "say hi", "# Environment\ncwd: /ws"]
    assert out["system"] == CLAUDE_CODE_BODY["system"]  # top-level system first, untouched
    assert CLAUDE_CODE_BODY["messages"][1]["role"] == "system"  # input not mutated


def test_adapter_fold_system_goes_to_the_next_user_turn_after_tool_results():
    body = {"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                           "input": {}}]},
        {"role": "system", "content": "reminder"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ok"}, {"type": "text", "text": "more"}]},
    ]}
    out = adapter.fold_system(body)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in out["messages"][2]["content"]] == ["tool_result", "text", "text"]
    assert out["messages"][2]["content"][1]["text"] == "reminder"
    assert out["messages"][0]["content"] == "go"


def test_adapter_fold_system_without_a_user_turn_joins_top_level_system():
    out = adapter.fold_system({"system": "base", "messages": [
        {"role": "system", "content": "late"}]})
    assert out["messages"] == []
    assert [b["text"] for b in out["system"]] == ["base", "late"]


def test_adapter_passes_other_bodies_byte_for_byte():
    plain = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    assert adapter.adapt_body("fold_system", "/v1/messages", plain) is plain
    raw = json.dumps(CLAUDE_CODE_BODY).encode()
    assert adapter.adapt_body("fold_system", "/v1/models", raw) is raw
    assert adapter.adapt_body("fold_system", "/v1/messages", b"not json") == b"not json"
    folded = json.loads(adapter.adapt_body("fold_system", "/v1/messages?beta=true", raw))
    assert all(m["role"] != "system" for m in folded["messages"])


def test_adapter_proxy_rewrites_and_forwards_headers(mock_endpoint):
    mock_endpoint.routes["/bppc/v1/messages?beta=true"] = (200, {"type": "message"})
    url = adapter.proxy_url("fold_system", mock_endpoint.url("bppc"))
    assert url.startswith("http://127.0.0.1:")
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("POST", "/v1/messages?beta=true", body=json.dumps(CLAUDE_CODE_BODY),
                 headers={"x-api-key": "local", "content-type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200 and json.loads(resp.read()) == {"type": "message"}
    conn.close()
    sent = json.loads(mock_endpoint.bodies["/bppc/v1/messages?beta=true"])
    assert [m["role"] for m in sent["messages"]] == ["user"]
    headers = {k.lower(): v for k, v in mock_endpoint.headers["/bppc/v1/messages?beta=true"].items()}
    assert headers["x-api-key"] == "local"
    # One proxy per (adapter, upstream).
    assert adapter.proxy_url("fold_system", mock_endpoint.url("bppc") + "/") == url


def test_adapter_bppc_provider_defaults_to_fold_system_and_can_be_turned_off(tmp_path: Path):
    settings = make_settings(tmp_path)
    assert settings.provider("bppc").adapter == "fold_system"
    assert provider_cfg("bppc", adapter="none").adapter is None
    assert all(cfg.adapter is None for name, cfg in settings.providers.items()
               if name != "bppc")


def test_adapter_child_gets_the_proxy_url_while_the_lane_keeps_the_backend(tmp_path: Path):
    settings = make_settings(tmp_path)
    lane = replace(settings.provider("bppc"), base_url="http://192.0.2.10:8080")
    env = settings.child_env("a1", lane)
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    written = json.loads(settings.hooks_config("a1", lane).read_text())
    assert written["env"]["ANTHROPIC_BASE_URL"] == env["ANTHROPIC_BASE_URL"]
    assert lane.base_url == "http://192.0.2.10:8080"  # health and telemetry still see bppc
    glm = settings.provider("glm")
    assert settings.child_env("a2", glm)["ANTHROPIC_BASE_URL"] == glm.base_url


def test_adapter_warm_bppc_wakes_through_the_proxy(mock_endpoint):
    base = mock_endpoint.url("bppc")
    mock_endpoint.routes["/bppc/health"] = (200, {"status": "ok", "backend": "stopped"})
    mock_endpoint.on_hit["/bppc/v1/models"] = lambda: mock_endpoint.routes.__setitem__(
        "/bppc/health", (200, {"status": "ok", "backend": "running"}))
    gate = health.warm(provider_cfg("bppc"), base, 10.0, poll=0.05)
    assert gate.ok and gate.base_url == base
    assert mock_endpoint.hits["/bppc/v1/models"] == 1


def test_adapter_warm_bppc_is_bounded(mock_endpoint):
    base = mock_endpoint.url("bppc")
    mock_endpoint.routes["/bppc/health"] = (200, {"status": "ok", "backend": "stopped"})
    gate = health.warm(provider_cfg("bppc"), base, 0.3, poll=0.05)
    assert not gate.ok and "not running" in gate.message
