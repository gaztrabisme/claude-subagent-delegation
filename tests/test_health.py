"""Health gates: base-URL resolution for a llama.cpp provider and the oMLX
status check.

No test reaches the network: conftest answers every probe outside the local
mock endpoint with "nothing there", and stubs the resolve command.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from subagent import health

from .conftest import provider_cfg
from .test_router import _delegate, _registry, lanes_on_mock  # noqa: F401 - fixture

TAILNET = "http://100.106.185.34:8080"
LAN = "http://192.168.1.17:8080"


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
    return provider_cfg("bppc")


def _resolving(monkeypatch, printed: str) -> None:
    """The provider's resolve command prints `printed` (one URL per line)."""
    monkeypatch.setattr(health, "_run", lambda argv, timeout=health.RESOLVE_TIMEOUT: printed)


def test_health_resolve_cmd_comes_first_then_the_candidates(monkeypatch):
    cfg = provider_cfg("bppc", health={"resolve_cmd": ["echo", "found"]})
    _resolving(monkeypatch, "http://192.168.1.50:8080\n")
    assert health.resolve_urls(cfg) == ["http://192.168.1.50:8080", LAN, TAILNET]
    # Deduplicated when the command prints a listed candidate, and a command
    # that prints nothing leaves the candidates alone.
    _resolving(monkeypatch, f"{LAN}\n")
    assert health.resolve_urls(cfg) == [LAN, TAILNET]
    _resolving(monkeypatch, "")
    assert health.resolve_urls(cfg) == [LAN, TAILNET]


def test_health_no_resolve_cmd_is_the_candidates_in_order(bppc_lane):
    assert health.resolve_urls(bppc_lane) == [LAN, TAILNET]
    assert health.resolve_urls(provider_cfg("bppc", health={"candidates": []})) == []


def test_health_llamacpp_picks_the_first_that_answers(monkeypatch, probes):
    seen, up = probes
    cfg = provider_cfg("bppc", health={"resolve_cmd": ["true"]})
    _resolving(monkeypatch, "http://192.168.1.50:8080\n")
    up.update({"192.168.1.50", "100.106.185.34"})
    gate = health.check_llamacpp(cfg)
    assert gate.ok and gate.base_url == "http://192.168.1.50:8080"
    assert seen == ["http://192.168.1.50:8080/health"]


def test_health_llamacpp_falls_back_to_the_last_candidate(monkeypatch, probes):
    seen, up = probes
    cfg = provider_cfg("bppc", health={"resolve_cmd": ["true"]})
    _resolving(monkeypatch, "http://192.168.1.50:8080\n")
    up.add("100.106.185.34")
    gate = health.check_llamacpp(cfg)
    assert gate.ok and gate.base_url == TAILNET
    assert seen == ["http://192.168.1.50:8080/health", f"{LAN}/health", f"{TAILNET}/health"]


def test_health_llamacpp_nothing_answers_is_health_failed(probes, bppc_lane):
    seen, _ = probes
    gate = health.check_llamacpp(bppc_lane)
    assert not gate.ok and gate.base_url is None
    assert "no host answered" in gate.message
    assert len(seen) == 2  # the LAN candidate, then the tailnet one


def test_health_llamacpp_resolves_against_a_live_mock(mock_endpoint):
    mock_endpoint.routes["/bppc/health"] = (200, {"status": "ok"})
    cfg = provider_cfg("bppc", health={"candidates": [mock_endpoint.url("bppc")]})
    gate = health.check_llamacpp(cfg)
    assert gate.ok and gate.base_url == mock_endpoint.url("bppc")
    assert mock_endpoint.hits["/bppc/health"] == 1


def test_health_base_url_skips_resolution(monkeypatch, mock_endpoint):
    def no_resolve(argv, timeout=health.RESOLVE_TIMEOUT):
        raise AssertionError("resolution must be skipped")

    monkeypatch.setattr(health, "_run", no_resolve)
    cfg = provider_cfg("bppc", base_url=mock_endpoint.url("bppc"),
                       health={"resolve_cmd": ["true"]})
    mock_endpoint.routes["/bppc/health"] = (200, {})
    assert health.check(cfg) == health.Health(True, mock_endpoint.url("bppc"))
    mock_endpoint.routes["/bppc/health"] = (503, {})
    assert not health.check(cfg).ok


def test_health_backend_stopped_is_healthy_cold_load(mock_endpoint):
    """A llama.cpp proxy answers /health with backend "stopped" while idle."""
    cfg = provider_cfg("bppc", health={"candidates": [mock_endpoint.url("bppc")]})
    mock_endpoint.routes["/bppc/health"] = (200, {"status": "ok", "backend": "stopped"})
    gate = health.check_llamacpp(cfg)
    assert gate.ok and gate.cold_load
    mock_endpoint.routes["/bppc/health"] = (200, {"status": "ok", "backend": "running"})
    assert not health.check_llamacpp(cfg).cold_load


def test_health_bppc_cold_load_extends_deadline_and_marks_hop(
        tmp_path: Path, lanes_on_mock, trace_records):  # noqa: F811
    ep = lanes_on_mock
    ep.routes["/bppc/health"] = (200, {"status": "ok", "backend": "stopped"})
    # The owner's proxy starts its backend on the first proxied request.
    ep.on_hit["/bppc/v1/models"] = lambda: ep.routes.__setitem__(
        "/bppc/health", (200, {"status": "ok", "backend": "running"}))
    ep.providers["bppc"]["health"]["cold_load_seconds"] = 700.0
    reg = _registry(tmp_path, ep, trace=str(tmp_path / "t.jsonl"))
    try:
        before = time.time()
        _, run = _delegate(reg, tmp_path, "bppc", "none")
        assert ep.hits["/bppc/v1/models"] == 1
        assert run.cold_load and run.hops[0]["cold_load"] is True
        timeout = reg.settings.provider("bppc").run_timeout
        assert run.deadline - before >= timeout + 700 - 1
        hops = [r for r in trace_records if r["kind"] == "hop"]
        assert hops and hops[-1]["cold_load"] is True
    finally:
        reg.shutdown()


def _omlx(mock_endpoint):
    return provider_cfg("omlx", base_url=mock_endpoint.url("omlx"),
                        health={"url": f"{mock_endpoint.url('omlx')}/api/status"})


def test_health_omlx_ok_sends_bearer_key(monkeypatch, mock_endpoint):
    monkeypatch.setenv("OMLX_API_KEY", "omlx-test")
    lane = _omlx(mock_endpoint)
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 2})
    gate = health.check(lane)
    assert gate.ok and not gate.cold_load and gate.base_url == mock_endpoint.url("omlx")
    sent = {k.lower(): v for k, v in mock_endpoint.headers["/omlx/api/status"].items()}
    assert sent["authorization"] == "Bearer omlx-test"


def test_health_omlx_cold_load_flag(monkeypatch, mock_endpoint):
    monkeypatch.setenv("OMLX_API_KEY", "omlx-test")
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
    monkeypatch.setenv("OMLX_API_KEY", "omlx-test")
    if route is not None:
        mock_endpoint.routes["/omlx/api/status"] = route
    gate = health.check(_omlx(mock_endpoint))
    assert not gate.ok and gate.message


def test_health_cloud_providers_have_no_gate(probes):
    seen, _ = probes
    for name in ("deepseek", "glm", "codex"):
        assert health.check(provider_cfg(name)).ok
    assert seen == []


def test_health_omlx_cold_load_extends_the_run_deadline(tmp_path: Path, lanes_on_mock):  # noqa: F811
    ep = lanes_on_mock
    ep.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 0})
    ep.providers["omlx"]["health"]["cold_load_seconds"] = 500.0
    reg = _registry(tmp_path, ep)
    try:
        before = time.time()
        _, run = _delegate(reg, tmp_path, "omlx", "none")
        assert run.cold_load and run.detail()["cold_load"] is True
        timeout = reg.settings.provider("omlx").run_timeout
        assert run.deadline - before >= timeout + 500 - 1
    finally:
        reg.shutdown()


def test_health_cold_load_seconds_comes_from_each_provider(tmp_path: Path):
    from .conftest import make_settings

    s = make_settings(tmp_path)
    assert (s.balance_close_hours, s.throttle_close_minutes) == (6.0, 15.0)
    assert (s.cold_load_seconds("bppc"), s.cold_load_seconds("omlx")) == (180.0, 120.0)
    # A provider with no health block (and an unknown name) extends nothing.
    assert (s.cold_load_seconds("glm"), s.cold_load_seconds("nope")) == (0.0, 0.0)
