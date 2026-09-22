"""Shared pieces for the live scripts (smoke_lanes.py, bench/concurrency.py).

The server is `subagent.core.InProcessServer`: the Settings, Registry and
Supervisor `mcp_server` builds, with the approval socket served on a
background event loop. Settings come from the config-driven core
(`subagent.config.load`): the TOML the caller names, the usual layers beneath
it (`$SUBAGENT_CONFIG`, the user's own config.toml), or — when a script is
pointed at a provider by name alone — a temporary file
`write_provider_config` writes declaring exactly that one provider. The
session root is a fresh directory, so the trace and metrics of one script run
are isolated from every other.

`SamplingProxy` is a logging reverse proxy: it forwards every request to an
upstream base URL, streams the answer back unchanged, and appends one JSON
line per request with the body's top-level keys and any sampling fields. It
never logs headers, so the Authorization key does not reach the log.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from subagent.config import Settings, configure_logging, load, read_config  # noqa: E402
from subagent.core import InProcessServer as _CoreServer  # noqa: E402

# Request-body fields that change how the model samples.
SAMPLING_FIELDS = (
    "temperature", "top_p", "top_k", "min_p", "typical_p", "presence_penalty",
    "frequency_penalty", "repetition_penalty", "repeat_penalty", "seed",
)


# --- reading JSONL back ---------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every JSON object line in `path`; missing file or bad lines are skipped."""
    out: list[dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


# --- the sampling proxy -----------------------------------------------------------


def extract_sampling(body: Any) -> dict[str, Any]:
    """The sampling fields present at the top level of a request body."""
    if not isinstance(body, Mapping):
        return {}
    return {name: body[name] for name in SAMPLING_FIELDS if name in body}


def request_record(method: str, path: str, raw: bytes) -> dict[str, Any]:
    """One log line for a proxied request. Body keys and sampling only, no headers."""
    record: dict[str, Any] = {"ts": round(time.time(), 3), "method": method, "path": path,
                              "bytes": len(raw)}
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    if isinstance(body, dict):
        record["keys"] = sorted(body)
        record["model"] = body.get("model")
        record["stream"] = body.get("stream")
        record["sampling"] = extract_sampling(body)
    else:
        record["keys"] = None
        record["sampling"] = {}
    return record


def sampling_summary(records: Iterable[Mapping[str, Any]]) -> str:
    """"none", or each sampling field sent with the distinct values seen."""
    seen: dict[str, list[str]] = {}
    for record in records:
        for name, value in (record.get("sampling") or {}).items():
            text = json.dumps(value)
            values = seen.setdefault(name, [])
            if text not in values:
                values.append(text)
    if not seen:
        return "none"
    return ", ".join(f"{name}={'/'.join(values)}" for name, values in sorted(seen.items()))


_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailer", "trailers", "transfer-encoding", "upgrade", "host"}


class SamplingProxy:
    """127.0.0.1:<free port> -> `upstream`, logging request bodies to `log_path`."""

    def __init__(self, upstream: str, log_path: Path):
        parts = urlsplit(upstream)
        self.upstream_host = parts.hostname or "127.0.0.1"
        self.upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        self.upstream_https = parts.scheme == "https"
        self.upstream_prefix = parts.path.rstrip("/")
        self.log_path = log_path
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0: one request per connection, so a streamed answer
            # ends when the connection closes and needs no length up front.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: Any) -> None:
                return

            def _body(self) -> bytes:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    data = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _forward(self) -> None:
                raw = self._body()
                if self.command in ("POST", "PUT", "PATCH"):
                    proxy.record(request_record(self.command, self.path, raw))
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"}
                cls = (http.client.HTTPSConnection if proxy.upstream_https
                       else http.client.HTTPConnection)
                conn = cls(proxy.upstream_host, proxy.upstream_port, timeout=3600)
                try:
                    conn.request(self.command, proxy.upstream_prefix + self.path,
                                 body=raw if raw or self.command in ("POST", "PUT", "PATCH")
                                 else None, headers=headers)
                    resp = conn.getresponse()
                except OSError as exc:
                    conn.close()
                    self.send_error(502, f"upstream unreachable: {type(exc).__name__}")
                    return
                try:
                    self.send_response(resp.status, resp.reason)
                    for key, value in resp.getheaders():
                        if key.lower() not in _HOP_BY_HOP:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    pass  # the client went away mid-stream
                finally:
                    conn.close()

            do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                        name="subagent-sampling-proxy")

    def record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def start(self) -> SamplingProxy:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --- the config-driven core -------------------------------------------------------


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """`over` on top of `base`, tables merged key by key (as config.load does)."""
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            out[key] = _merge(current, value)
        else:
            out[key] = value
    return out


def toml_value(value: Any) -> str:
    """One inline TOML value. A JSON string is a TOML basic string."""
    if isinstance(value, Mapping):
        raise TypeError("nested tables become [sections], not inline values")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


def toml_dump(data: Mapping[str, Any]) -> str:
    """A TOML document for `data`: nested tables become [a.b] sections.

    None values are dropped: TOML has no null, and an absent key is what a
    "leave the default alone" override means.
    """
    lines: list[str] = []

    def emit(table: Mapping[str, Any], header: str) -> None:
        scalars: list[tuple[str, Any]] = []
        sections: list[tuple[str, Any]] = []
        for key, value in table.items():
            if value is None:
                continue
            (sections if isinstance(value, Mapping) else scalars).append((str(key), value))
        if header and (scalars or not sections):
            lines.extend([f"[{header}]", ""])
        lines.extend(f"{key} = {toml_value(value)}" for key, value in scalars)
        if scalars:
            lines.append("")
        for key, value in sections:
            emit(value, f"{header}.{key}" if header else key)

    emit(dict(data), "")
    return "\n".join(lines)


def write_toml(path: Path, data: Mapping[str, Any]) -> Path:
    """`toml_dump(data)` into `path`, returning the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(toml_dump(data), encoding="utf-8")
    return path


def config_table(config: Path | str | None = None) -> dict[str, Any]:
    """The merged config table the scripts' settings come from."""
    return read_config(None, Path(config) if config is not None else None)


def load_settings(config: Path | str | None = None,
                  overrides: Mapping[str, Any] | None = None) -> Settings:
    """Settings for the live scripts: the config layers, `overrides` on top.

    `config` is passed as the extra (top) config layer; None means just
    `$SUBAGENT_CONFIG` and the user's own config.toml. `overrides` is a config
    table ({"core": {...}, "providers": {...}}) merged over everything below,
    via a temporary file, because `load` takes exactly one extra layer.
    """
    extra = Path(config) if config is not None else None
    if overrides is None:
        return load(project_root=None, extra=extra)
    merged = _merge(config_table(extra), overrides)
    handle, name = tempfile.mkstemp(prefix="subagent-live-", suffix=".toml")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(toml_dump(merged))
    return load(project_root=None, extra=Path(name))


# The providers the live scripts name without a config file, from
# examples/config.*.toml: the least a delegation needs to reach them. A
# provider none of them knows needs a config naming it.
KNOWN_PROVIDERS: dict[str, dict[str, Any]] = {
    "glm": {
        "driver": "claude", "vendor": "zai",
        "base_url": "https://api.z.ai/api/anthropic",
        "model": "glm-5.3-flash[1m]",
        "api_key_env": ["GLM_API_KEY", "ZAI_API_KEY"],
    },
    "omlx": {
        "driver": "claude", "vendor": "omlx", "local": True,
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B",
        "api_key_env": "OMLX_API_KEY",
        "health": {"kind": "omlx", "url": "http://127.0.0.1:8000/api/status",
                   "cold_load_seconds": 120},
        "probe": {"host_cmd": "mac"},
    },
    "deepseek": {
        "driver": "claude", "vendor": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-pro",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "codex": {"driver": "codex"},
}


def write_provider_config(provider: str, *, directory: Path, base_url: str | None = None,
                          model: str | None = None, api_key_env: str | list[str] | None = None,
                          driver: str | None = None,
                          core: Mapping[str, Any] | None = None) -> Path:
    """A TOML in `directory` declaring exactly `provider`, and its path.

    The given base_url, model, api_key_env and driver win over the built-in
    table for the known providers; an unknown provider needs at least a
    driver, or a config file naming it instead of this call. `core` is the
    [core] table written beside it; default_provider is always `provider`.
    """
    table = dict(KNOWN_PROVIDERS.get(provider) or {})
    table.update({name: value for name, value in (
        ("driver", driver), ("base_url", base_url), ("model", model),
        ("api_key_env", api_key_env)) if value is not None})
    if not table.get("driver"):
        raise SystemExit(f"no built-in definition for provider {provider!r}; "
                         "pass --config naming it")
    return write_toml(Path(directory) / "provider.toml", {
        "core": {"default_provider": provider, **dict(core or {})},
        "providers": {provider: table},
    })


class InProcessServer(_CoreServer):
    """The core server, built from config files instead of a Settings object.

    `config` is an extra TOML layer above the usual ones — a path, or the file
    `write_provider_config` writes — and `overrides` (a table like
    {"core": {...}, "providers": {"omlx": {...}}}) goes on top of everything,
    so a script can raise max_agents or set a provider's run timeout without
    editing any file. delegate, close_agent, stop and the trace are the core
    class's own; `metrics_path` is the one thing the scripts read beyond it.
    """

    def __init__(self, session_root: Path, config: Path | str | None = None, *,
                 overrides: Mapping[str, Any] | None = None,
                 approval_socket: str | None = None):
        settings = load_settings(config, overrides)
        configure_logging(settings.log_level)
        super().__init__(Path(session_root), settings, approval_socket=approval_socket)

    @property
    def metrics_path(self) -> Path:
        """metrics.jsonl beside the trace, under this run's session root."""
        return self.session_root / "metrics.jsonl"


def records_for(records: Iterable[Mapping[str, Any]], *, run_ids: set[str] | None = None,
                agent_ids: set[str] | None = None, kind: str | None = None
                ) -> list[Mapping[str, Any]]:
    """Trace records filtered by kind, run_id or agent_id."""
    out = []
    for record in records:
        if kind is not None and record.get("kind") != kind:
            continue
        if run_ids is not None and record.get("run_id") not in run_ids:
            continue
        if agent_ids is not None and record.get("agent_id") not in agent_ids:
            continue
        out.append(record)
    return out
