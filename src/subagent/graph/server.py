"""Temporal dev-server lifecycle, graph state dir and the `[graph]` settings (spec 8.2).

Pure module: `temporalio` is imported lazily and only for the gRPC health check.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as _config
from ..config import ConfigError, Settings
from . import CLI_HINT, GraphUnavailable

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
DEFAULT_ADDRESS = "127.0.0.1:7233"


# --- state dir and CLI discovery -------------------------------------------------


def _private_dir(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def private_file(path: Path) -> Path:
    """Create `path` if missing and make it owner-only (0600)."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)  # on the descriptor we hold: no path swap between create and chmod
    finally:
        os.close(fd)
    return path


def graph_state_dir(settings: Settings) -> Path:
    """`[graph].state_dir`, else `<session_root>/graph`; created with mode 0700."""
    raw = _config.read_config(settings.workspace).get("graph") or {}
    configured = raw.get("state_dir") if isinstance(raw, Mapping) else None
    path = Path(str(configured)).expanduser() if configured else settings.session_root / "graph"
    return _private_dir(path)


def find_temporal_cli(explicit: str | Path | None = None) -> Path:
    """explicit, else $TEMPORAL_BIN, else `temporal` on PATH; never downloads."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
    env = os.environ.get("TEMPORAL_BIN")
    if env and Path(env).expanduser().is_file():
        return Path(env).expanduser()
    found = shutil.which("temporal")
    if found:
        return Path(found)
    raise GraphUnavailable(CLI_HINT)


# --- the dev server --------------------------------------------------------------


@dataclass
class ServerInfo:
    address: str
    pid: int | None
    db_path: str
    binary: str
    started_by_us: bool
    started_at: float | None


def _split_address(address: str) -> tuple[str, int]:
    host, sep, port = address.rpartition(":")
    if not sep:
        raise ValueError(f"graph server address must be host:port, got {address!r}")
    host = host.strip("[]")
    if host not in _LOOPBACK:
        raise ValueError("graph server address must be loopback")
    try:
        return host, int(port)
    except ValueError:
        raise ValueError(f"graph server address has a bad port: {address!r}") from None


def _free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                         text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _command_line(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                          text=True).stdout.strip()


async def _grpc_health(address: str, timeout_s: float) -> None:
    import asyncio

    from temporalio.client import Client

    client = await asyncio.wait_for(Client.connect(address), timeout_s)
    await asyncio.wait_for(client.service_client.check_health(), timeout_s)


def _grpc_check(address: str, timeout_s: float) -> None:
    """gRPC health through temporalio when importable (in a thread: callers may have a loop)."""
    try:
        import temporalio  # noqa: F401
    except ImportError:
        return
    import asyncio

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(_grpc_health(address, timeout_s))).result(
            timeout=timeout_s + 5)


class DevServer:
    def __init__(self, state_dir: Path, *, binary: Path | None = None,
                 address: str = DEFAULT_ADDRESS, startup_timeout_s: float = 10.0):
        _split_address(address)
        self.state_dir = Path(state_dir)
        self.binary = Path(binary) if binary is not None else None
        self.address = address
        self.startup_timeout_s = startup_timeout_s
        self._proc: subprocess.Popen | None = None

    # -- files
    @property
    def _json(self) -> Path:
        return self.state_dir / "server.json"

    @property
    def _db(self) -> Path:
        return self.state_dir / "temporal.db"

    @property
    def _log(self) -> Path:
        return self.state_dir / "server.log"

    def _read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _info(self, data: Mapping[str, Any], *, by_us: bool) -> ServerInfo:
        return ServerInfo(address=str(data.get("address") or self.address),
                          pid=data.get("pid"), db_path=str(data.get("db_path") or self._db),
                          binary=str(data.get("binary") or ""), started_by_us=by_us,
                          started_at=data.get("started_at"))

    # -- queries
    def health(self, *, timeout_s: float = 2.0) -> dict[str, Any]:
        data = self._read() or {}
        address = str(data.get("address") or self.address)
        out: dict[str, Any] = {"ok": False, "address": address, "pid": data.get("pid"),
                               "db_path": str(data.get("db_path") or self._db), "error": None}
        try:
            host, port = _split_address(address)
            if port == 0:
                raise ConnectionError("no port: the server has not been started")
            with socket.create_connection((host, port), timeout=timeout_s):
                pass
            _grpc_check(f"[{host}]:{port}" if ":" in host else f"{host}:{port}", timeout_s)
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001 - health reports, never raises
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def info(self) -> ServerInfo | None:
        data = self._read()
        if not data or not isinstance(data.get("pid"), int) or not _pid_alive(data["pid"]):
            return None
        return self._info(data, by_us=self._proc is not None and self._proc.pid == data["pid"])

    # -- lifecycle
    def start(self) -> ServerInfo:
        host, port = _split_address(self.address)
        _private_dir(self.state_dir)
        lock = open(private_file(self.state_dir / "server.lock"), "a+")  # noqa: SIM115 - held across the body
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = self._read()
            if existing and isinstance(existing.get("pid"), int) and _pid_alive(existing["pid"]):
                self.address = str(existing.get("address") or self.address)
                if self.health()["ok"]:
                    return self._info(existing, by_us=False)
            binary = find_temporal_cli(self.binary)
            if port == 0:
                port = _free_port(host)
            self.address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            env = {k: v for k, v in os.environ.items()
                   if k in ("PATH", "HOME", "LANG", "TERM", "TMPDIR")}
            with open(private_file(self._log), "ab") as log:
                proc = subprocess.Popen(
                    [str(binary), "server", "start-dev", "--ip", host, "--port", str(port),
                     "--headless", "--db-filename", str(self._db), "--log-level", "warn"],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, env=env)
            self._proc = proc
            deadline = time.monotonic() + self.startup_timeout_s
            while True:
                if proc.poll() is None and self._probe(host, port):
                    break
                if proc.poll() is not None or time.monotonic() >= deadline:
                    self._kill(proc)
                    raise GraphUnavailable(
                        "Temporal dev server did not become healthy in "
                        f"{self.startup_timeout_s:g}s; see {self._log}")
                time.sleep(0.2)
            record = {"pid": proc.pid, "address": self.address, "db_path": str(self._db),
                      "binary": str(binary), "started_at": time.time()}
            tmp = self._json.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:  # born 0600, never widened by umask
                os.fchmod(fh.fileno(), 0o600)
                fh.write(json.dumps(record))
            os.replace(tmp, self._json)
            return self._info(record, by_us=True)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _probe(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
            _grpc_check(f"[{host}]:{port}" if ":" in host else f"{host}:{port}", 2.0)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                continue

    def stop(self, *, timeout_s: float = 10.0) -> bool:
        data = self._read()
        if not data or not isinstance(data.get("pid"), int):
            return False
        pid = data["pid"]
        db_path = str(data.get("db_path") or "")
        if not _pid_alive(pid) or not db_path or db_path not in _command_line(pid):
            if not _pid_alive(pid):
                with contextlib.suppress(FileNotFoundError):
                    self._json.unlink()
            return False  # never signal a pid that is not our dev server
        proc = self._proc if self._proc is not None and self._proc.pid == pid else None

        def gone(wait: float) -> bool:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if proc is not None:
                    if proc.poll() is not None:
                        return True
                elif not _pid_alive(pid):
                    return True
                time.sleep(0.1)
            return proc.poll() is not None if proc is not None else not _pid_alive(pid)

        def signal_group(sig: int) -> None:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

        signal_group(signal.SIGTERM)
        if not gone(timeout_s):
            signal_group(signal.SIGKILL)
            gone(5.0)
        with contextlib.suppress(FileNotFoundError):
            self._json.unlink()
        return True


# --- [graph] settings ------------------------------------------------------------


@dataclass(frozen=True)
class GraphSettings:
    state_dir: Path
    address: str = DEFAULT_ADDRESS
    task_queue_prefix: str = "subagent"
    autostart: bool = True
    budget_usd: float = 10.0
    default_reserve_usd: float = 0.50
    max_depth: int = 3
    admit_poll_s: float = 2.0
    lease_ttl_s: float = 60.0
    worker_max_tasks: int = 8
    lane_slots: Mapping[str, int] = field(default_factory=dict)
    pools: Mapping[str, float] = field(default_factory=dict)

    def caps(self, settings: Settings) -> dict[str, int]:
        """Slots per lane: `[graph.lanes.<p>].slots` overrides `ProviderConfig.max_agents`."""
        out = {name: int(cfg.max_agents) for name, cfg in settings.providers.items()}
        out.update({name: int(n) for name, n in self.lane_slots.items()})
        return out


def _num(table: Mapping[str, Any], key: str, default: float, *, minimum: float,
         integer: bool = False, path: str = "[graph]") -> Any:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            integer and not isinstance(value, int)):
        raise ConfigError(f"{path}.{key} must be {'an integer' if integer else 'a number'}, "
                          f"got {value!r}")
    if value < minimum:
        raise ConfigError(f"{path}.{key} must be at least {minimum:g}, got {value!r}")
    return value if integer else float(value)


def _subtable(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"[graph].{name} must be a table, got {type(value).__name__}")
    return value


def settings_from(settings: Settings, raw: Mapping[str, Any] | None = None) -> GraphSettings:
    """Parse the `[graph]` table; a bad value raises ConfigError naming its key."""
    if raw is None:
        raw = _config.read_config(settings.workspace).get("graph") or {}
    if not isinstance(raw, Mapping):
        raise ConfigError("[graph] must be a table")
    state = raw.get("state_dir")
    if state is not None and (not isinstance(state, str) or not state):
        raise ConfigError(f"[graph].state_dir must be a non-empty string, got {state!r}")
    address = raw.get("address", DEFAULT_ADDRESS)
    if not isinstance(address, str) or ":" not in address:
        raise ConfigError(f"[graph].address must be 'host:port', got {address!r}")
    prefix = raw.get("task_queue_prefix", "subagent")
    if not isinstance(prefix, str) or not prefix:
        raise ConfigError(f"[graph].task_queue_prefix must be a non-empty string, got {prefix!r}")
    autostart = raw.get("autostart", True)
    if not isinstance(autostart, bool):
        raise ConfigError(f"[graph].autostart must be true or false, got {autostart!r}")
    slots: dict[str, int] = {}
    for lane, table in _subtable(raw, "lanes").items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"[graph.lanes.{lane}] must be a table")
        slots[lane] = _num(table, "slots", 1, minimum=1, integer=True,
                           path=f"[graph.lanes.{lane}]")
    pools: dict[str, float] = {}
    for name, table in _subtable(raw, "pools").items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"[graph.pools.{name}] must be a table")
        pools[name] = _num(table, "budget_usd", 0.0, minimum=0.0, path=f"[graph.pools.{name}]")
    return GraphSettings(
        state_dir=(Path(state).expanduser() if state else settings.session_root / "graph"),
        address=address, task_queue_prefix=prefix, autostart=autostart,
        budget_usd=_num(raw, "budget_usd", 10.0, minimum=0.0),
        default_reserve_usd=_num(raw, "default_reserve_usd", 0.50, minimum=0.0),
        max_depth=_num(raw, "max_depth", 3, minimum=1, integer=True),
        admit_poll_s=_num(raw, "admit_poll_s", 2.0, minimum=0.01),
        lease_ttl_s=_num(raw, "lease_ttl_s", 60.0, minimum=1.0),
        worker_max_tasks=_num(raw, "worker_max_tasks", 8, minimum=1, integer=True),
        lane_slots=slots, pools=pools)
