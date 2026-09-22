"""Shared test fixtures. Keep Settings construction in one place."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from subagent.config import Settings
from subagent.lanes import load_lanes

# Every lane's key variable. Tests never see the machine's real keys.
KEY_ENVS = ("GLM_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY", "SAM_BPPC_API_KEY",
            "SAM_OMLX_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _lane_keys(monkeypatch):
    """The glm lane (the default) gets a fake key; every other key is unset."""
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    # No test may reach the real Codex CLI or the user's Codex login.
    monkeypatch.setenv("SAM_CODEX_BIN", "/nonexistent/codex-disabled-in-tests")
    monkeypatch.setenv("CODEX_HOME", "/nonexistent/codex-home-disabled-in-tests")


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

    from subagent import health

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
        # path -> callable run after the request is counted, before the answer.
        self.on_hit: dict[str, object] = {}
        self.bodies: dict[str, bytes] = {}
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                endpoint.bodies[self.path] = self.rfile.read(length) if length else b""
                endpoint.hits[self.path] += 1
                endpoint.headers[self.path] = dict(self.headers.items())
                hook = endpoint.on_hit.get(self.path)
                if callable(hook):
                    hook()
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


# --- a fake `codex` binary replaying tests/fixtures/codex -----------------------

CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "codex"

FAKE_CODEX = """\
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read()
record = {
    "argv": argv,
    "stdin": stdin,
    "env": {k: os.environ.get(k) for k in (
        "CODEX_HOME", "SAM_APPROVAL_SOCKET", "GLM_API_KEY", "ANTHROPIC_AUTH_TOKEN")},
    "cwd": os.getcwd(),
}
with open(os.environ["FAKE_CODEX_RECORD"], "a") as fh:
    fh.write(json.dumps(record) + "\\n")
fixture = os.environ["FAKE_CODEX_FIXTURE"]
failed = False
with open(fixture) as fh:
    for line in fh:
        if line.strip():
            sys.stdout.write(line if line.endswith("\\n") else line + "\\n")
            failed = failed or json.loads(line).get("type") == "turn.failed"
sys.stdout.flush()
sys.exit(1 if failed else 0)
"""


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "codex"
    binary.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
    binary.chmod(0o755)
    record = tmp_path / "codex-calls.jsonl"
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    (user_home / "auth.json").write_text('{"fake": "login"}')
    (user_home / "config.toml").write_text(
        'notify = ["/bin/echo"]\nmodel = "gpt-6-astra"\nmodel_reasoning_effort = "xhigh"\n'
        '[mcp_servers.x]\ncommand = "x"\n'
    )
    (user_home / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "/bin/sh /user/own-hook.sh"}]}]}}))
    monkeypatch.setenv("SAM_CODEX_BIN", str(binary))
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    monkeypatch.setenv("FAKE_CODEX_FIXTURE", str(CODEX_FIXTURES / "success.jsonl"))

    class Fake:
        home = user_home

        def use(self, fixture: str) -> None:
            monkeypatch.setenv("FAKE_CODEX_FIXTURE", str(CODEX_FIXTURES / fixture))

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


# --- telemetry probes and trace-record validation ----------------------------------


@pytest.fixture(autouse=True)
def _no_telemetry_probes(monkeypatch):
    """No test reaches oMLX, bppc (ssh, :8081) or sysctl/pmset through telemetry.

    Tests that exercise the probe parsers patch these again with canned output.
    """
    from subagent.telemetry import sampler as telemetry

    def no_http(url, headers=None, timeout=telemetry.PROBE_TIMEOUT):
        raise OSError("network probes are disabled in tests")

    def no_run(argv, timeout=telemetry.PROBE_TIMEOUT):
        raise OSError("subprocess probes are disabled in tests")

    monkeypatch.setattr(telemetry, "_http_get", no_http)
    monkeypatch.setattr(telemetry, "_run", no_run)


@pytest.fixture(autouse=True)
def trace_records(monkeypatch):
    """Every record any Trace builds during a test, enabled or not, as written.

    Checked against tests/test_trace_schema.py when the test ends, so every fake
    run in the suite validates the schema of what it wrote.
    """
    from subagent.telemetry import trace as trace_module

    records: list[dict] = []
    real_write = trace_module.Trace.write

    def write(self, kind, **fields):
        record = {"schema": trace_module.SCHEMA, "ts": 0.0, "kind": kind, **fields}
        records.append(json.loads(json.dumps(record, default=str)))
        real_write(self, kind, **fields)

    monkeypatch.setattr(trace_module.Trace, "write", write)
    yield records
    from .test_trace_schema import problems

    found = [p for record in list(records) for p in problems(record)]
    assert not found, "trace records off schema:\n" + "\n".join(found[:20])
