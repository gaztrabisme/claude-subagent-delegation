"""Shared pieces for the live lane scripts (smoke_lanes.py, bench_concurrency.py).

`InProcessServer` is this package's server without the MCP transport: the
same Settings, Registry and Supervisor objects `server.py` builds, with the
approval socket served on a background event loop. Environment overrides are
applied before Settings is read, and the session root is a fresh directory, so
the trace and metrics of one script run are isolated from every other.

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
        text = path.read_text(encoding="utf-8")
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
                                        name="sam-sampling-proxy")

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


# --- the in-process server --------------------------------------------------------


class InProcessServer:
    """Settings + Registry + Supervisor from the package, no MCP transport.

    `env` is applied to os.environ before Settings.from_env() runs, and the
    session root (SAM_SESSION_ROOT) is `session_root`. The supervisor is never
    bound to an MCP session, so an escalation denies (deterministic tier);
    the policy's own deny verdicts are unaffected.
    """

    def __init__(self, session_root: Path, env: Mapping[str, str] | None = None):
        self.session_root = session_root
        session_root.mkdir(parents=True, exist_ok=True)
        os.environ["SAM_SESSION_ROOT"] = str(session_root)
        # A unix socket path must stay under ~104 bytes on macOS.
        os.environ.setdefault("SAM_APPROVAL_SOCKET", f"/tmp/sam-live-{os.getpid()}.sock")
        os.environ.setdefault("SAM_SUPERVISOR", "auto")
        for key, value in (env or {}).items():
            os.environ[key] = value
        from subagent.config import Settings, configure_logging
        from subagent.guard.supervisor import Supervisor
        from subagent.runs import Registry

        self.settings = Settings.from_env()
        configure_logging(os.environ.get("SAM_LOG_LEVEL") or "warning")
        self.registry = Registry(self.settings)
        self.supervisor = Supervisor(self.settings, self.registry, trace=self.registry.trace)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True, name="sam-approval")

    @property
    def trace_path(self) -> Path:
        path = self.registry.trace.path
        return path if path is not None else self.session_root / "trace.jsonl"

    @property
    def metrics_path(self) -> Path:
        return self.session_root / "metrics.jsonl"

    def _serve(self) -> None:
        import anyio

        async def main() -> None:
            async with anyio.create_task_group() as tg:
                await tg.start(self.supervisor.serve)
                self._ready.set()
                while not self._stop.is_set():
                    await anyio.sleep(0.2)
                tg.cancel_scope.cancel()

        try:
            anyio.run(main)
        except BaseException as exc:  # noqa: BLE001 - reported by start()
            self._error = exc
            self._ready.set()

    def start(self) -> InProcessServer:
        self._thread.start()
        if not self._ready.wait(10) or self._error is not None:
            raise RuntimeError(f"approval socket did not start: {self._error!r}")
        return self

    def delegate(self, *, lane: str, task: str, verification: str, workspace: Path,
                 fallback: str = "none", name: str | None = None) -> Any:
        """Create an agent on `lane` and submit its delegate run. Returns the Run."""
        agent = self.registry.create_agent(name, workspace.resolve(), lane=lane,
                                           fallback=fallback)
        error = agent.wait_ready(60.0)
        if error is not None:
            agent.close("runtime failed to start")
            raise RuntimeError(f"runtime failed to start on lane {lane}: {error}")
        return agent.delegate(task, verification)

    def close_agent(self, agent_id: str) -> None:
        agent = self.registry.find_agent(agent_id)
        if agent is not None and not agent.closed:
            agent.close("script finished")

    def stop(self) -> None:
        try:
            self.registry.shutdown()
            self.registry.telemetry.shutdown()
        finally:
            self._stop.set()
            self._thread.join(timeout=5)
            self.supervisor.cleanup()


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
