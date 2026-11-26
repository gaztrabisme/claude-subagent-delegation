"""A real mini-deployment for the crash tests: a DevServer, subprocess workers, a config file.

Everything runs on loopback, in tmp_path, with FakeDispatcher workers
(`subagent graph worker --dispatcher fake`). Every process the cluster starts is
killed in `close()` (also called from the fixture teardown), by process group.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ...conftest import _toml_table, make_settings
from ..conftest import require_temporal

CLI = "import sys; from subagent.cli import main; sys.exit(main(sys.argv[1:]))"
RESERVE = 0.5
CAPS = {"codex": 2, "glm": 1}


def wait_for(predicate, timeout: float, what: str, interval: float = 0.4):
    """Poll `predicate` until it returns something truthy; fail with `what` at the deadline."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last={last!r})")


def task_node(node_id: str, provider: str) -> dict[str, Any]:
    return {"id": node_id, "kind": "task", "provider": provider, "prompt": f"work {node_id}",
            "verification": "true", "reserve_usd": RESERVE, "retry_backoff_s": 0}


def level_tasks(prefix: str) -> list[dict[str, Any]]:
    """Eight tasks per level: four on codex, four on glm."""
    return [task_node(f"{prefix}{i}", "codex" if i % 2 == 0 else "glm") for i in range(8)]


def crash_graph(*, root_usd: float, a_usd: float, b_usd: float) -> dict[str, Any]:
    """24 task nodes over 2 lanes and 2 subgraph levels: root -> sa -> sb, eight tasks each."""
    sb = {"id": "sb", "kind": "subgraph", "budget_usd": b_usd, "body": [
        {"id": "pb", "kind": "parallel", "branches": level_tasks("b")}],
        "output": {"pb": "nodes.pb.output.branches"}}
    sa = {"id": "sa", "kind": "subgraph", "budget_usd": a_usd, "body": [
        {"id": "pa", "kind": "parallel", "branches": level_tasks("a")}, sb],
        "output": {"pa": "nodes.pa.output.branches", "sb": "nodes.sb.output"}}
    return {
        "schema": "subagent.graph/1", "id": "crash", "version": 1, "budget_usd": root_usd,
        "defaults": {"provider": "codex", "reserve_usd": RESERVE},
        "body": [{"id": "pr", "kind": "parallel", "branches": level_tasks("r")}, sa],
        "output": {"pr": "nodes.pr.output.branches", "sa": "nodes.sa.output"},
    }


def task_statuses(output: Any) -> dict[str, str]:
    """Every {task_id: status} map found anywhere in a GraphResult output."""
    found: dict[str, str] = {}
    if isinstance(output, dict):
        if output and all(isinstance(v, str) for v in output.values()) and all(
                len(k) == 2 and k[0] in "rab" and k[1].isdigit() for k in output):
            found.update(output)
        else:
            for value in output.values():
                found.update(task_statuses(value))
    return found


def free_loopback_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Cluster:
    def __init__(self, tmp_path: Path, *, default_step: dict[str, Any] | None = None,
                 root_budget: float = 40.0, lease_ttl_s: float = 6.0):
        self.tmp = tmp_path
        self.state_dir = tmp_path / "graph-state"
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.audit = tmp_path / "audit.jsonl"
        self.audit.touch()
        self.root_budget = root_budget
        self.lease_ttl_s = lease_ttl_s
        step = {"status": "completed", "cost_usd": 0.2, "sleep_s": 1.0}
        step.update(default_step or {})
        self.fake_script = tmp_path / "fake-script.json"
        self.fake_script.write_text(json.dumps({"script": {}, "default": step,
                                                "audit_log": str(self.audit)}))
        self.binary = require_temporal()
        self.server = None
        self.address = ""
        self.procs: list[subprocess.Popen] = []
        self.workers: list[subprocess.Popen] = []
        self.config_path = tmp_path / "cluster.toml"
        self.prefix = "crash"

    # --- lifecycle ---------------------------------------------------------

    def bring_up(self, workers: int = 2):
        info = self.start_server()
        self.write_config()
        for _ in range(workers):
            self.start_worker()
        return info

    def start_server(self):
        from subagent.graph.server import DevServer

        address = self.address or "127.0.0.1:0"
        self.server = DevServer(self.state_dir, binary=self.binary, address=address,
                                startup_timeout_s=30.0)
        info = self.server.start()
        self.address = info.address
        return info

    def write_config(self) -> Path:
        make_settings(self.tmp, workspace=str(self.workspace))
        base = self.tmp / "subagent.toml"
        graph = {
            "state_dir": str(self.state_dir), "address": self.address,
            "task_queue_prefix": self.prefix, "autostart": False, "budget_usd": self.root_budget,
            "default_reserve_usd": RESERVE, "admit_poll_s": 0.3,
            "lease_ttl_s": self.lease_ttl_s, "worker_max_tasks": 8,
            "lanes": {name: {"slots": slots} for name, slots in CAPS.items()},
            "pools": {"default": {"budget_usd": self.root_budget}},
        }
        self.config_path.write_text(base.read_text() + "\n\n" + _toml_table(
            "graph", graph) + "\n")
        return self.config_path

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SUBAGENT_CONFIG"] = str(self.config_path)
        env["TEMPORAL_BIN"] = str(self.binary)
        return env

    def popen(self, args: list[str], log_name: str) -> subprocess.Popen:
        log = open(self.tmp / f"{log_name}.log", "ab")  # noqa: SIM115 - owned by the child
        proc = subprocess.Popen([sys.executable, "-c", CLI, *args], env=self.env(),
                                cwd=str(self.workspace), stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        self.procs.append(proc)
        return proc

    def start_worker(self) -> subprocess.Popen:
        index = len(self.workers) + 1
        proc = self.popen(["graph", "worker", "--dispatcher", "fake", "--fake-script",
                           str(self.fake_script), "--max-tasks", "8"], f"worker{index}")
        self.workers.append(proc)
        return proc

    def run_cli(self, *args: str, timeout: float = 60.0) -> tuple[int, Any]:
        proc = subprocess.run([sys.executable, "-c", CLI, "graph", *args, "--json"],
                              env=self.env(), cwd=str(self.workspace), capture_output=True,
                              text=True, timeout=timeout)
        text = proc.stdout.strip()
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = text
        return proc.returncode, payload

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
        for proc in self.procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        if self.server is not None:
            with contextlib.suppress(Exception):  # teardown must not mask the test result
                self.server.stop(timeout_s=5)
        info_path = self.state_dir / "server.json"
        if info_path.exists():
            try:
                pid = json.loads(info_path.read_text()).get("pid")
                if pid:
                    os.kill(int(pid), signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass

    # --- clients and state --------------------------------------------------

    def service(self):
        from subagent.graph.server import settings_from
        from subagent.graph.service import GraphService

        from subagent import config

        settings = config.load(extra=self.config_path)
        gs = settings_from(settings, raw=config.read_config(extra=self.config_path)["graph"])
        return GraphService(settings, gsettings=gs)

    def call(self, method: str, *args, **kwargs):
        service = self.service()
        return asyncio.run(getattr(service, method)(*args, **kwargs))

    def status(self, root: str, **kwargs):
        return self.call("status", root, **kwargs)

    def store(self):
        from subagent.graph.store import Store

        return Store(self.state_dir / "graph.sqlite3")

    def audit_events(self) -> list[dict[str, Any]]:
        events = []
        for line in self.audit.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def wait_state(self, root: str, states: tuple[str, ...], timeout: float):
        return wait_for(lambda: (lambda s: s if s.get("state") in states else None)(
            self.status(root)), timeout, f"{root} to reach {states}")

    def wait_done(self, root: str, timeout: float = 150.0):
        return self.wait_state(root, ("completed", "failed", "cancelled", "budget_exhausted"),
                               timeout)


def pid_alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                          text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")
