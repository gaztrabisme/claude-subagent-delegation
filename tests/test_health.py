"""Health gates: bppc address resolution and the oMLX status check.

No test reaches the network: conftest answers every probe outside the local
mock endpoint with "nothing there", and stubs `tailscale status`.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from subagent_mcp import health
from subagent_mcp.lanes import load_lanes

from .test_router import _delegate, _registry, lanes_on_mock  # noqa: F401 - fixture


def _tailscale(lan: str | None = "192.168.1.50", host: str = "bppc") -> dict:
    addrs = [f"{lan}:41641"] if lan else []
    return {"Peer": {
        "nodekey:aaa": {"HostName": "mcbob", "CurAddr": "192.168.1.216:41641"},
        "nodekey:bbb": {"HostName": host, "CurAddr": "",
                        "TailscaleIPs": ["100.106.185.34", "fd7a:115c:a1e0::1"],
                        "Addrs": ["100.106.185.34:41641", *addrs],
                        "Endpoints": ["[fd7a::1]:41641"]},
    }}


@pytest.fixture
def probes(monkeypatch):
    """Record every probed URL; `up` is the set of hosts that answer 200."""
    seen: list[str] = []
    up: set[str] = set()

    def fake_get(url, headers=None, timeout=health.PROBE_TIMEOUT):
        seen.append(url)
        host = url.split("//", 1)[1].split(":", 1)[0]
        return (200, b"{}") if host in up else None

    monkeypatch.setattr(health, "_http_get", fake_get)
    return seen, up


@pytest.fixture
def bppc_lane():
    return load_lanes({})["bppc"]


def test_health_bppc_lan_from_tailscale_json():
    assert health.bppc_lan_from_tailscale(_tailscale("192.168.1.50")) == "192.168.1.50"
    assert health.bppc_lan_from_tailscale(_tailscale(None)) is None
    assert health.bppc_lan_from_tailscale(_tailscale("192.168.1.9", host="bppc-2")) == "192.168.1.9"
    assert health.bppc_lan_from_tailscale({"Peer": {"x": {"HostName": "other",
                                                          "CurAddr": "10.0.0.2:1"}}}) is None
    assert health.bppc_lan_from_tailscale(None) is None
    assert health.bppc_lan_from_tailscale({"Peer": "garbage"}) is None


def test_health_bppc_host_order_lan_then_list_then_tailscale(monkeypatch):
    monkeypatch.delenv("SAM_BPPC_LAN_HOSTS", raising=False)
    assert health.bppc_hosts({}, _tailscale("192.168.1.50")) == [
        "192.168.1.50", "192.168.1.17", "100.106.185.34"]
    # Deduplicated when tailscale reports the listed host.
    assert health.bppc_hosts({}, _tailscale("192.168.1.17")) == [
        "192.168.1.17", "100.106.185.34"]
    env = {"SAM_BPPC_LAN_HOSTS": "10.0.0.5, 10.0.0.6"}
    assert health.bppc_hosts(env, None) == ["10.0.0.5", "10.0.0.6", "100.106.185.34"]


def test_health_bppc_picks_lan_first(monkeypatch, probes, bppc_lane):
    seen, up = probes
    monkeypatch.setattr(health, "_tailscale_status", lambda: _tailscale("192.168.1.50"))
    up.update({"192.168.1.50", "100.106.185.34"})
    gate = health.check_bppc(bppc_lane, {})
    assert gate.ok and gate.base_url == "http://192.168.1.50:8080"
    assert seen == ["http://192.168.1.50:8080/health"]


def test_health_bppc_falls_back_to_tailscale(monkeypatch, probes, bppc_lane):
    seen, up = probes
    monkeypatch.setattr(health, "_tailscale_status", lambda: _tailscale("192.168.1.50"))
    up.add("100.106.185.34")
    gate = health.check_bppc(bppc_lane, {})
    assert gate.ok and gate.base_url == "http://100.106.185.34:8080"
    assert seen == ["http://192.168.1.50:8080/health", "http://192.168.1.17:8080/health",
                    "http://100.106.185.34:8080/health"]


def test_health_bppc_no_tailscale_peer_default_lan_host_answers(monkeypatch, probes, bppc_lane):
    seen, up = probes
    monkeypatch.setattr(health, "_tailscale_status", lambda: {"Peer": {}})
    up.add("192.168.1.17")
    gate = health.check_bppc(bppc_lane, {})
    assert gate.ok and gate.base_url == "http://192.168.1.17:8080"
    assert seen == ["http://192.168.1.17:8080/health"]


def test_health_bppc_nothing_answers_is_health_failed(probes, bppc_lane):
    seen, _ = probes
    gate = health.check_bppc(bppc_lane, {})
    assert not gate.ok and gate.base_url is None
    assert "no host answered" in gate.message
    assert len(seen) == 2  # 192.168.1.17, then the Tailscale address


def test_health_bppc_resolves_against_a_live_mock(mock_endpoint, bppc_lane):
    mock_endpoint.routes["/health"] = (200, {"status": "ok"})
    env = {"SAM_BPPC_LAN_HOSTS": "127.0.0.1", "SAM_BPPC_PORT": str(mock_endpoint.port),
           "SAM_BPPC_TAILSCALE_HOST": "127.0.0.1"}
    gate = health.check_bppc(bppc_lane, env)
    assert gate.ok and gate.base_url == f"http://127.0.0.1:{mock_endpoint.port}"
    assert mock_endpoint.hits["/health"] == 1


def test_health_bppc_base_url_override_skips_resolution(monkeypatch, mock_endpoint):
    def no_tailscale():
        raise AssertionError("resolution must be skipped")

    monkeypatch.setattr(health, "_tailscale_status", no_tailscale)
    lane = load_lanes({"SAM_BPPC_BASE_URL": mock_endpoint.url("bppc")})["bppc"]
    mock_endpoint.routes["/bppc/health"] = (200, {})
    assert health.check(lane) == health.Health(True, mock_endpoint.url("bppc"))
    mock_endpoint.routes["/bppc/health"] = (503, {})
    assert not health.check(lane).ok


def test_health_bppc_backend_stopped_is_healthy_cold_load(mock_endpoint, bppc_lane):
    """bppc's proxy answers /health with backend "stopped" while idle."""
    mock_endpoint.routes["/health"] = (200, {"status": "ok", "backend": "stopped"})
    env = {"SAM_BPPC_LAN_HOSTS": "127.0.0.1", "SAM_BPPC_PORT": str(mock_endpoint.port),
           "SAM_BPPC_TAILSCALE_HOST": "127.0.0.1"}
    gate = health.check_bppc(bppc_lane, env)
    assert gate.ok and gate.cold_load
    mock_endpoint.routes["/health"] = (200, {"status": "ok", "backend": "running"})
    assert not health.check_bppc(bppc_lane, env).cold_load


def test_health_bppc_cold_load_extends_deadline_and_marks_hop(
        tmp_path: Path, lanes_on_mock, trace_records):  # noqa: F811
    ep = lanes_on_mock
    ep.routes["/bppc/health"] = (200, {"status": "ok", "backend": "stopped"})
    # The owner's proxy starts its backend on the first proxied request.
    ep.on_hit["/bppc/v1/models"] = lambda: ep.routes.__setitem__(
        "/bppc/health", (200, {"status": "ok", "backend": "running"}))
    reg = _registry(tmp_path, ep, bppc_cold_load_seconds=700.0, trace=str(tmp_path / "t.jsonl"))
    try:
        before = time.time()
        _, run = _delegate(reg, tmp_path, "bppc", "none")
        assert ep.hits["/bppc/v1/models"] == 1
        assert run.cold_load and run.hops[0]["cold_load"] is True
        timeout = reg.settings.lanes["bppc"].run_timeout
        assert run.deadline - before >= timeout + 700 - 1
        hops = [r for r in trace_records if r["kind"] == "hop"]
        assert hops and hops[-1]["cold_load"] is True
    finally:
        reg.shutdown()


def _omlx(mock_endpoint, key="omlx-test"):
    lane = load_lanes({"SAM_OMLX_BASE_URL": mock_endpoint.url("omlx"),
                       "SAM_OMLX_API_KEY": key})["omlx"]
    return replace(lane, health_url=f"{mock_endpoint.url('omlx')}/api/status")


def test_health_omlx_ok_sends_bearer_key(monkeypatch, mock_endpoint):
    monkeypatch.setenv("SAM_OMLX_API_KEY", "omlx-test")
    lane = _omlx(mock_endpoint)
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 2})
    gate = health.check(lane)
    assert gate.ok and not gate.cold_load and gate.base_url == mock_endpoint.url("omlx")
    sent = {k.lower(): v for k, v in mock_endpoint.headers["/omlx/api/status"].items()}
    assert sent["authorization"] == "Bearer omlx-test"


def test_health_omlx_cold_load_flag(monkeypatch, mock_endpoint):
    monkeypatch.setenv("SAM_OMLX_API_KEY", "omlx-test")
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 0})
    gate = health.check(_omlx(mock_endpoint))
    assert gate.ok and gate.cold_load


@pytest.mark.parametrize("route", [
    (200, {"status": "loading"}),
    (401, {"error": "bad key"}),
    (200, b"not json"),
    None,
])
def test_health_omlx_unhealthy(monkeypatch, mock_endpoint, route):
    monkeypatch.setenv("SAM_OMLX_API_KEY", "omlx-test")
    if route is not None:
        mock_endpoint.routes["/omlx/api/status"] = route
    gate = health.check(_omlx(mock_endpoint))
    assert not gate.ok and gate.message


def test_health_cloud_lanes_have_no_gate(probes):
    seen, _ = probes
    lanes = load_lanes({})
    for name in ("deepseek", "glm", "codex"):
        assert health.check(lanes[name]).ok
    assert seen == []


def test_health_omlx_cold_load_extends_the_run_deadline(tmp_path: Path, lanes_on_mock):  # noqa: F811
    ep = lanes_on_mock
    ep.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 0})
    reg = _registry(tmp_path, ep, omlx_cold_load_seconds=500.0)
    try:
        before = time.time()
        _, run = _delegate(reg, tmp_path, "omlx", "none")
        assert run.cold_load and run.detail()["cold_load"] is True
        timeout = reg.settings.lanes["omlx"].run_timeout
        assert run.deadline - before >= timeout + 500 - 1
    finally:
        reg.shutdown()


def test_health_cold_load_seconds_default(monkeypatch):
    from subagent_mcp.config import Settings

    for name in ("SAM_OMLX_COLD_LOAD_SECONDS", "SAM_BALANCE_CLOSE_HOURS",
                 "SAM_THROTTLE_CLOSE_MINUTES", "SAM_BPPC_COLD_LOAD_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert (s.omlx_cold_load_seconds, s.balance_close_hours, s.throttle_close_minutes) == (
        120.0, 6.0, 15.0)
    assert s.bppc_cold_load_seconds == 180.0
    assert (s.cold_load_seconds("bppc"), s.cold_load_seconds("omlx")) == (180.0, 120.0)
