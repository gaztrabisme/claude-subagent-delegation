"""Shared test fixtures. Keep Settings construction in one place."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subagent_mcp.config import Settings
from subagent_mcp.lanes import load_lanes

# Every lane's key variable. Tests never see the machine's real keys.
KEY_ENVS = ("GLM_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY", "SAM_BPPC_API_KEY",
            "SAM_OMLX_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _lane_keys(monkeypatch):
    """The glm lane (the default) gets a fake key; every other key is unset."""
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLM_API_KEY", "test-key")


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings for a test. Lanes resolve their knobs from these values, as
    they would from the matching global SAM_ variables."""
    values = {
        "workspace": tmp_path,
        "session_root": tmp_path / "sessions",
        "claude_bin": "claude",
        "max_agents": 4,
        "transcript_limit": 400,
        "summary_tokens": 2000,
        "idle_timeout": 900.0,
        "run_timeout": 1800.0,
        "turn_token_budget": None,
        "loop_strikes": 3,
        "supervisor": "off",
        "supervisor_cmd": "claude -p --model sonnet",
        "supervisor_timeout": 120.0,
        "approval_socket": str(tmp_path / "approval.sock"),
        "log_level": "info",
        "max_steps": 40,
        "verify_timeout": 300.0,
        "chars_per_token": 3.5,
        "run_archive": 200,
        "trace": "off",
        "compact_window": 1_000_000,
        "rate_limit_retries": 3,
        "rate_limit_backoff": 5.0,
        "throttle_backoff": 60.0,
    }
    values.update(overrides)
    if "lanes" not in values:
        # Only knobs a test overrode become globals, so the local lanes keep
        # their own defaults, as they would with the SAM_ variables unset.
        knobs = ("max_agents", "compact_window", "max_steps", "run_timeout", "idle_timeout")
        values["lanes"] = load_lanes({
            f"SAM_{knob.upper()}": str(overrides[knob]) for knob in knobs if knob in overrides
        })
    settings = Settings(**values)
    settings.session_root.mkdir(parents=True, exist_ok=True)
    return settings


# Ports of the mock HTTP servers tests start. Health probes to anything else
# answer "nothing there", so no test reaches bppc, the real oMLX or tailscale.
MOCK_PORTS: set[int] = set()


@pytest.fixture(autouse=True)
def _no_network_health(monkeypatch):
    from urllib.parse import urlsplit

    from subagent_mcp import health

    real_get = health._http_get

    def guarded(url, headers=None, timeout=health.PROBE_TIMEOUT):
        parts = urlsplit(url)
        if parts.hostname in ("127.0.0.1", "localhost") and parts.port in MOCK_PORTS:
            return real_get(url, headers, timeout)
        return None

    monkeypatch.setattr(health, "_http_get", guarded)
    monkeypatch.setattr(health, "_tailscale_status", lambda: None)


class MockEndpoint:
    """A local HTTP server standing in for Anthropic-compatible endpoints,
    llama.cpp's /health and oMLX's /api/status.

    Paths are namespaced by a prefix, one per lane: /glm/v1/messages,
    /bppc/health, /omlx/api/status. `routes` maps a full path to
    (status, body); unset paths answer 404. `hits` counts requests per path,
    and `headers` keeps the last request's headers per path.
    """

    def __init__(self):
        import threading
        from collections import Counter
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.routes: dict[str, tuple[int, object]] = {}
        self.hits: Counter[str] = Counter()
        self.headers: dict[str, dict[str, str]] = {}
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                endpoint.hits[self.path] += 1
                endpoint.headers[self.path] = dict(self.headers.items())
                status, body = endpoint.routes.get(self.path, (404, {"error": "not found"}))
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _answer
            do_POST = _answer

            def log_message(self, *args):  # keep test output quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def url(self, prefix: str) -> str:
        return f"http://127.0.0.1:{self.port}/{prefix}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock_endpoint():
    endpoint = MockEndpoint()
    MOCK_PORTS.add(endpoint.port)
    yield endpoint
    MOCK_PORTS.discard(endpoint.port)
    endpoint.close()
