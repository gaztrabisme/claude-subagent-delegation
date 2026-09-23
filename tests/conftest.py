"""Shared test fixtures. Keep Settings construction in one place."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from subagent import config
from subagent.config import Settings

# Every provider's key variable. Tests never see the machine's real keys.
KEY_ENVS = ("GLM_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY", "BPPC_API_KEY",
            "OMLX_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# The fake `codex` binary a test installs; the default provider table reads it
# so no test can reach the real Codex CLI.
CODEX_BIN_ENV = "SUBAGENT_TEST_CODEX_BIN"
NO_CODEX = "/nonexistent/codex-disabled-in-tests"
# Stands in for that binary in a provider table, resolved when the config is
# written: a fixture that installs a fake `codex` may run after the table was
# built.
CODEX_BIN_TOKEN = "@codex-bin@"

# The bppc probe, as the old lane hardcoded it.
GPU_CMD = [
    "ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", "bppc@{host}", "nvidia-smi",
    "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu",
    "--format=csv,noheader,nounits",
]


@pytest.fixture(autouse=True)
def _provider_keys(monkeypatch, tmp_path_factory):
    """The glm provider (the default) gets a fake key; every other is unset.

    The machine's own config files are out of reach too: XDG_CONFIG_HOME
    points at an empty directory and SUBAGENT_CONFIG is unset, so `config.load`
    sees only what a test writes.
    """
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv(CODEX_BIN_ENV, NO_CODEX)
    monkeypatch.setenv("CODEX_HOME", "/nonexistent/codex-home-disabled-in-tests")
    empty = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)


def default_providers() -> dict[str, dict[str, Any]]:
    """The five backends this project ran on before the config file landed.

    Same drivers, models, keys, adapters and limits the old lane registry
    hardcoded, so a test that does not care about providers keeps its old
    behaviour by saying nothing.
    """
    return {
        "codex": {
            "driver": "codex",
            "binary": CODEX_BIN_TOKEN,
        },
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
            # Stated, not derived: these tests point base_url at a local mock.
            "vendor": "deepseek",
        },
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": ["GLM_API_KEY", "ZAI_API_KEY"],
            "vendor": "zai",
        },
        "bppc": {
            "driver": "claude",
            "model": "qwen3.8-27b",
            "api_key_env": "BPPC_API_KEY",
            "api_key": "local",
            "local": True,
            "max_agents": 1,
            "compact_window": 40960,
            # llama.cpp's Qwen3.8 template refuses a system message after the
            # first user turn, which Claude Code sends.
            "adapter": "fold_system",
            "health": {
                "kind": "llamacpp",
                "candidates": ["http://192.168.1.17:8080", "http://100.106.185.34:8080"],
                "warm": True,
                "cold_load_seconds": 180.0,
            },
            "probe": {
                "gpu_cmd": GPU_CMD,
                "slots_url": "http://{host}:8081/slots",
                "metrics_url": "http://{host}:8081/metrics",
            },
        },
        "omlx": {
            "driver": "claude",
            "base_url": "http://127.0.0.1:8000",
            "model": "Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B",
            "api_key_env": "OMLX_API_KEY",
            "local": True,
            "send_sampling": False,
            # Measured on this Mac: aggregate output tok/s 20.1 at 1 child,
            # 18.3 at 2, 7.6 at 4; p90 TTFT 3.9 s -> 181 s at 4.
            "max_agents": 1,
            "compact_window": 98304,
            "health": {
                "kind": "omlx",
                "url": "http://127.0.0.1:8000/api/status",
                "cold_load_seconds": 120.0,
            },
            "probe": {
                "host_cmd": ["mac"],
                "metrics_url": "http://127.0.0.1:8000/api/status",
            },
        },
    }


# Overrides make_settings routes to a table other than [core].
GUARD_KEYS = ("supervisor", "supervisor_cmd", "supervisor_timeout", "approval_socket",
              "allow_unguarded")
TELEMETRY_KEYS = ("sample_seconds",)

# Fake CLI controls and record paths are explicit child inputs in tests. Keep
# these here, in the test fixture, instead of baking test-only names into src.
TEST_CHILD_ENV_PASSTHROUGH = (
    "FAKE_SCRIPT", "FAKE_MODEL", "FAKE_PROMPT", "FAKE_FAIL_MODEL",
    "FAKE_CODEX_RECORD", "FAKE_CODEX_FIXTURE",
    "FAKE_GROK_RECORD", "FAKE_GROK_FIXTURE",
    "FAKE_COPILOT_RECORD", "FAKE_COPILOT_FIXTURE",
)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))


def _toml_table(name: str, table: dict[str, Any]) -> str:
    """One table and its sub-tables, deepest last. Values only, no comments."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    lines = [f"[{name}]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in scalars.items()]
    out = ["\n".join(lines)]
    for key, value in table.items():
        if isinstance(value, dict):
            out.append(_toml_table(f"{name}.{key}", value))
    return "\n\n".join(out)


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(CODEX_BIN_TOKEN, os.environ.get(CODEX_BIN_ENV, NO_CODEX))
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


def write_config(path: Path, tables: dict[str, Any]) -> Path:
    """Write `tables` as TOML and return the path."""
    tables = _resolve(tables)
    path.write_text("\n\n".join(
        _toml_table(name, table) for name, table in tables.items() if table
    ) + "\n")
    return path


def make_settings(tmp_path: Path, providers: dict[str, Any] | None = None,
                  **overrides) -> Settings:
    """Settings for a test, written as a config file and loaded back.

    `providers` is the `[providers]` table; None declares the five backends
    this project used to hardcode. Any other keyword goes to [core], [guard]
    or [telemetry], whichever owns it.
    """
    tables = default_providers() if providers is None else providers
    core: dict[str, Any] = {
        "workspace": str(tmp_path),
        "session_root": str(tmp_path / "sessions"),
        "default_provider": "glm" if "glm" in tables else next(iter(tables), ""),
        "loop_strikes": 3,
        "trace": "off",
        "child_env_passthrough": list(TEST_CHILD_ENV_PASSTHROUGH),
    }
    guard: dict[str, Any] = {
        "supervisor": "off",
        "supervisor_cmd": "claude -p --model sonnet",
        "approval_socket": str(tmp_path / "approval.sock"),
    }
    telemetry: dict[str, Any] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if key in GUARD_KEYS:
            guard[key] = value
        elif key in TELEMETRY_KEYS:
            telemetry[key] = value
        else:
            core[key] = value
    chain = [n for n in ("codex", "deepseek", "glm", "bppc", "omlx") if n in tables]
    path = write_config(tmp_path / "subagent.toml", {
        "core": core,
        "guard": guard,
        "telemetry": telemetry,
        "fallback": {"chain": chain},
        "providers": tables,
    })
    settings = config.load(extra=path)
    settings.session_root.mkdir(parents=True, exist_ok=True)
    return settings


def provider_cfg(name: str, **patch):
    """One default provider as a ProviderConfig, with `patch` merged in."""
    table = default_providers()[name]
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(table.get(key), dict):
            table[key] = {**table[key], **value}
        else:
            table[key] = value
    from subagent.config import _provider

    table = _resolve(table)
    defaults = {"max_agents": 4, "compact_window": 1_000_000, "max_steps": 40,
                "run_timeout": 1800.0, "idle_timeout": 900.0}
    return _provider(name, table, defaults)


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
    # No resolve command runs either: an unresolved provider has no candidates
    # beyond the ones its config names.
    monkeypatch.setattr(health, "_run", lambda argv, timeout=health.RESOLVE_TIMEOUT: "")


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
        "CODEX_HOME", "SUBAGENT_APPROVAL_SOCKET", "GLM_API_KEY",
        "ANTHROPIC_AUTH_TOKEN")},
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
    monkeypatch.setenv(CODEX_BIN_ENV, str(binary))
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
