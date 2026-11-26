"""Run the scale gate end to end and print the six audit lines.

    uv run --extra graph python scripts/graph_scale_gate/orchestrate.py --dispatch fake

Choreography: dev server (own sqlite db) -> singleton worker -> two client processes ->
wait for the owner pause and 30 completed children -> SIGKILL client 1 -> stop and restart
the server on the same db -> restart client 1 (it re-attaches) -> owner signal -> drain ->
shut everything down -> ingest runtime errors -> audit_gate.py report.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from gate_common import (  # noqa: E402
    PAUSED_WORKFLOW,
    ROOTS,
    audit,
    build_plan,
)

DYNAMIC_CONFIG = (
    "matching.useNewMatcher=true", "matching.enableFairness=true",
    "matching.numTaskqueueReadPartitions=1", "matching.numTaskqueueWritePartitions=1",
)
SERVER_PATTERN = re.compile(
    r"database is locked|history corruption|corrupt(?:ed)? history|"
    r"matcher.{0,80}(?:error|failed|panic)|(?:error|failed|panic).{0,80}matcher",
    re.IGNORECASE)
PYTHON_PATTERN = re.compile(r"Traceback \(most recent call last\)|^(ERROR|CRITICAL)\b")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def descendants(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True,
                         check=False).stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def stop(proc: subprocess.Popen[Any] | None, grace: float = 30.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)


def read_events(log: Path) -> list[dict[str, Any]]:
    events = []
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def wait_for(cond: Any, timeout: float, what: str, procs: list[subprocess.Popen[Any]]) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        for proc in procs:
            if proc.poll() not in (None, 0):
                raise RuntimeError(f"a process exited with {proc.returncode} while waiting "
                                   f"for {what}")
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {what}")


class Gate:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = (Path(args.run_root) / f"gate-{args.dispatch}-{stamp}").resolve()
        self.run_dir.mkdir(parents=True)
        self.audit_log = self.run_dir / "audit.jsonl"
        self.port = args.port or free_port()
        self.temporal = os.environ.get("TEMPORAL_BIN", "/opt/homebrew/bin/temporal")
        self.server: subprocess.Popen[Any] | None = None
        self.worker: subprocess.Popen[Any] | None = None
        self.clients: dict[str, subprocess.Popen[Any]] = {}
        self.tracked: set[int] = set()
        self.env = {**os.environ, "GATE_AUDIT_LOG": str(self.audit_log),
                    "GATE_CONFIG": str(self.run_dir / "config.json"),
                    "PYTHONUNBUFFERED": "1"}
        os.environ.update(GATE_AUDIT_LOG=self.env["GATE_AUDIT_LOG"])
        self.cfg: dict[str, Any] = {
            "address": f"127.0.0.1:{self.port}", "run_dir": str(self.run_dir),
            "ledger_db": str(self.run_dir / "ledger.sqlite3"),
            "lane_caps": {"omlx": 1, "deepseek": 1},
            "estimates": {"omlx": args.omlx_estimate, "deepseek": args.deepseek_estimate},
            "budget_cap_usd": args.budget_cap, "max_attempts": 2, "dispatch": args.dispatch,
        }

    # -- processes -------------------------------------------------------
    def log(self, name: str) -> Any:
        return open(self.run_dir / name, "ab")  # noqa: SIM115 - held by the child

    def start_server(self, restart: bool) -> None:
        started = time.monotonic()
        cmd = [self.temporal, "server", "start-dev", "--ip", "127.0.0.1",
               "--port", str(self.port), "--headless",
               "--db-filename", str(self.run_dir / "temporal.db")]
        for item in DYNAMIC_CONFIG:
            cmd += ["--dynamic-config-value", item]
        self.server = subprocess.Popen(cmd, stdout=self.log("server.log"),
                                       stderr=subprocess.STDOUT, env=self.env)
        self.tracked.add(self.server.pid)
        asyncio.run(self._health(started))
        audit("server_ready", pid=self.server.pid, restart=restart,
              cold_start_seconds=round(time.monotonic() - started, 3))

    async def _health(self, started: float, timeout: float = 60.0) -> None:
        from temporalio.client import Client
        while time.monotonic() - started < timeout:
            if self.server is not None and self.server.poll() is not None:
                raise RuntimeError("the Temporal dev server exited while starting")
            try:
                await asyncio.wait_for(Client.connect(self.cfg["address"]), timeout=0.5)
                return
            except Exception:  # noqa: BLE001 - not up yet
                await asyncio.sleep(0.1)
        raise RuntimeError("Temporal dev server not reachable")

    def stop_server(self) -> None:
        if self.server is None:
            return
        pid = self.server.pid
        self.tracked.update(descendants(pid))
        stop(self.server)
        self.server = None
        audit("server_stopped", pid=pid)

    def spawn(self, name: str, *argv: str) -> subprocess.Popen[Any]:
        proc = subprocess.Popen([sys.executable, str(HERE / argv[0]), *argv[1:]],
                                cwd=REPO, env=self.env, stdout=self.log(f"{name}.log"),
                                stderr=subprocess.STDOUT)
        self.tracked.add(proc.pid)
        return proc

    # -- the run ---------------------------------------------------------
    def completed(self) -> int:
        return sum(1 for e in read_events(self.audit_log)
                   if e.get("type") == "child_end" and e.get("outcome") == "completed")

    def seen(self, kind: str) -> bool:
        return any(e.get("type") == kind for e in read_events(self.audit_log))

    def run(self) -> int:
        a = self.args
        entries, specs = build_plan(a.deepseek_total)
        (self.run_dir / "specs.json").write_text(json.dumps(specs), encoding="utf-8")
        (self.run_dir / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        trace = Path(a.trace) if a.trace else self.run_dir / "fake_trace.jsonl"
        audit("config", lane_caps=self.cfg["lane_caps"], budget_cap_usd=a.budget_cap,
              planned_children=len(entries), planned_roots=len(ROOTS))
        for root in ROOTS:
            audit("plan", requests=[e for e in entries if e["root_id"] == root])
        print(f"run dir: {self.run_dir}", flush=True)
        try:
            self.start_server(restart=False)
            self.worker = self.spawn("worker", "gate_worker.py")
            wait_for(lambda: self.seen("worker_started"), 120, "worker_started", [self.worker])
            self.clients["root-1"] = self.spawn("client-root-1", "gate_client.py", "submit",
                                                "--root-id", "root-1")
            self.clients["root-2"] = self.spawn("client-root-2", "gate_client.py", "submit",
                                                "--root-id", "root-2")
            wait_for(lambda: self.seen("owner_paused") and self.completed() >= 30,
                     a.timeout, "owner_paused and 30 completed children",
                     [self.worker, *self.clients.values()])
            victim = self.clients["root-1"]
            victim.kill()
            victim.wait()
            audit("client_killed", client_pid=victim.pid, signal="SIGKILL")
            self.stop_server()
            self.start_server(restart=True)
            restarted = self.spawn("client-root-1-restarted", "gate_client.py", "submit",
                                   "--root-id", "root-1", "--restarted")
            self.clients["root-1"] = restarted
            wait_for(lambda: self.seen("client_restarted"), 120, "client_restarted",
                     [self.worker, restarted])
            subprocess.run([sys.executable, str(HERE / "gate_client.py"), "signal",
                            "--workflow-id", PAUSED_WORKFLOW], cwd=REPO, env=self.env,
                           check=True, stdout=self.log("signal.log"), stderr=subprocess.STDOUT)
            for name, proc in self.clients.items():
                try:
                    proc.wait(a.timeout)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"client {name} did not finish") from exc
            wait_for(lambda: sum(1 for e in read_events(self.audit_log)
                                 if e.get("type") == "root_done") >= len(ROOTS), 60,
                     "root_done x2", [])
        except Exception as exc:  # noqa: BLE001 - report, then still shut down and audit
            print(f"ABORT: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        finally:
            self.shutdown()
        return self.report(trace)

    def shutdown(self) -> None:
        for proc in self.clients.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if self.worker is not None:
            self.tracked.update(descendants(self.worker.pid))
            stop(self.worker, 120.0)
        if self.server is not None:
            self.tracked.update(descendants(self.server.pid))
            stop(self.server)
        end = time.monotonic() + 10
        orphans = sorted(p for p in self.tracked if alive(p))
        while orphans and time.monotonic() < end:
            time.sleep(0.5)
            orphans = sorted(p for p in self.tracked if alive(p))
        for pid in orphans:  # reported as orphans, then removed so none are left behind
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        audit("shutdown", clean=not orphans, orphan_pids=orphans)

    def ingest_errors(self) -> None:
        for name, pattern in (("server.log", SERVER_PATTERN), ("worker.log", PYTHON_PATTERN),
                              ("client-root-1.log", PYTHON_PATTERN),
                              ("client-root-2.log", PYTHON_PATTERN),
                              ("client-root-1-restarted.log", PYTHON_PATTERN),
                              ("signal.log", PYTHON_PATTERN)):
            path = self.run_dir / name
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    audit("runtime_error", source=name, line=number, detail=line[:300])

    def report(self, trace: Path) -> int:
        self.ingest_errors()
        print(f"audit log: {self.audit_log}\ntrace: {trace}", flush=True)
        proc = subprocess.run([sys.executable, str(HERE / "audit_gate.py"), "report",
                               "--log", str(self.audit_log), "--trace", str(trace)],
                              check=False)
        return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dispatch", choices=["fake", "real"], default="fake")
    parser.add_argument("--trace", help="subagent trace to audit (real: the Settings trace; "
                        "fake: the fake trace in the run dir)")
    parser.add_argument("--run-root", default=str(HERE / ".runs"))
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--deepseek-total", type=int, default=20)
    parser.add_argument("--budget-cap", type=float, default=0.30)
    parser.add_argument("--deepseek-estimate", type=float, default=0.05)
    parser.add_argument("--omlx-estimate", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args()
    if args.timeout is None:
        args.timeout = 600.0 if args.dispatch == "fake" else 14400.0
    if args.dispatch == "real" and not args.trace:
        from gate_dispatch import real_trace_path
        args.trace = str(real_trace_path())
    return Gate(args).run()


if __name__ == "__main__":
    sys.exit(main())
