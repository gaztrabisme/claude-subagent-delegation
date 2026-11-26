#!/usr/bin/env python3
"""Live demo of the durable graph: start a run, restart the Temporal dev server mid-run,
and check that the run still finishes with no duplicate work and matching cost figures.

Real mode (default) runs tasks on a provider. `--dispatcher fake` plays a script offline.
Run from anywhere: uv run --extra graph python scripts/graph_live_demo/run_demo.py [options]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}
_CODE = "import sys; from subagent.cli import main; sys.exit(main(sys.argv[1:]))"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = HERE / ".runs" / stamp
        self.project = self.run_dir / "project"
        self.state = self.run_dir / "graph-state"
        self.trace = self.run_dir / "trace.jsonl"
        self.address = f"127.0.0.1:{free_port()}"
        self.worker_proc: subprocess.Popen | None = None
        self.fake = args.dispatcher == "fake"

    # -- setup -------------------------------------------------------------
    def setup(self) -> None:
        shutil.copytree(HERE / "scratch_template", self.project)
        doc = json.loads((HERE / "demo.json").read_text(encoding="utf-8"))
        doc["defaults"]["provider"] = self.args.provider
        doc["budget_usd"] = self.args.budget
        doc["inputs"] = {"provider": self.args.provider, "budget_usd": self.args.budget}
        (self.run_dir / "graph.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
        cfg = self.project / ".subagent"
        cfg.mkdir()
        (cfg / "config.toml").write_text(
            f'[core]\nsession_root = "{self.run_dir / "sessions"}"\ntrace = "{self.trace}"\n\n'
            f'[graph]\nstate_dir = "{self.state}"\naddress = "{self.address}"\n'
            f'autostart = true\nbudget_usd = {self.args.budget}\n'
            f'default_reserve_usd = 0.1\n', encoding="utf-8")
        self.audit = self.run_dir / "fake-audit.jsonl"
        if self.fake:
            script = json.loads((HERE / "fake_script.json").read_text(encoding="utf-8"))
            script["audit_log"] = str(self.audit)
            self.fake_script = self.run_dir / "fake_script.json"
            self.fake_script.write_text(json.dumps(script), encoding="utf-8")

    # -- subprocess helpers ---------------------------------------------------
    def cli(self, *argv: str, check: bool = True) -> dict:
        proc = subprocess.run([sys.executable, "-P", "-c", _CODE, "graph", *argv, "--json"],
                              cwd=self.project, capture_output=True, text=True, timeout=180)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"ok": False, "error": {"code": "no_json", "message": proc.stdout + proc.stderr}}
        if check and not out.get("ok"):
            raise SystemExit(f"graph {' '.join(argv)} failed: {out.get('error')}")
        return out

    def worker_pids(self) -> list[int]:
        db = self.state / "graph.sqlite3"
        if not db.is_file():
            return []
        try:
            with sqlite3.connect(db, timeout=5) as conn:
                return [int(r[0]) for r in conn.execute("SELECT pid FROM workers")]
        except sqlite3.Error:
            return []

    def start_fake_worker(self) -> None:
        self.cli("server", "start")
        with open(self.run_dir / "worker.log", "ab") as log:
            self.worker_proc = subprocess.Popen(
                [sys.executable, "-P", "-c", _CODE, "graph", "worker", "--dispatcher", "fake",
                 "--fake-script", str(self.fake_script), "--address", self.address,
                 "--state-dir", str(self.state)],
                cwd=self.project, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.worker_proc.pid in self.worker_pids():
                return
            if self.worker_proc.poll() is not None:
                raise SystemExit(f"fake worker exited early; see {self.run_dir / 'worker.log'}")
            time.sleep(0.5)
        raise SystemExit("fake worker did not register")

    def cleanup(self) -> None:
        pids = set(self.worker_pids())
        if self.worker_proc is not None:
            pids.add(self.worker_proc.pid)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 40
        while time.time() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.5)
        for pid in pids:
            if _alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        if self.worker_proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.worker_proc.wait(timeout=5)
        self.cli("server", "stop", check=False)

    # -- the run ---------------------------------------------------------------
    @staticmethod
    def busy(status: dict) -> bool:
        nodes = status.get("nodes") or {}
        running = any(n.get("status") == "running" for n in nodes.values())
        in_flight = any(e.get("phase") in ("queued", "admitted", "dispatched")
                        for e in status.get("journal") or [])
        return running or in_flight or bool((status.get("waiting") or {}).get("admission"))

    @staticmethod
    def build_in_flight(status: dict) -> bool:
        return any(e.get("phase") in ("queued", "admitted", "dispatched")
                   and str(e.get("node_id")).startswith("build_")
                   for e in status.get("journal") or [])

    def run(self) -> int:
        self.setup()
        deadline = time.time() + self.args.max_minutes * 60
        print(f"run dir: {self.run_dir}")
        try:
            if self.fake:
                self.start_fake_worker()
            started = self.cli("start", str(self.run_dir / "graph.json"),
                               "--input", f"provider={self.args.provider}",
                               "--budget", str(self.args.budget))
            root = started["root_run_id"]
            print(f"started {root}")
            snap: dict = {}
            while time.time() < deadline:
                snap = self.cli("status", root, "--detail")
                nodes = snap.get("nodes") or {}
                if nodes.get("plan", {}).get("status") == "completed" and \
                        self.build_in_flight(snap):
                    break
                if snap.get("state") in TERMINAL:
                    break
                time.sleep(1)
            restart_mid_run = snap.get("state") not in TERMINAL and self.busy(snap)
            print(f"restarting dev server; state={snap.get('state')} "
                  f"mid_run={restart_mid_run}")
            self.cli("server", "stop")
            time.sleep(2)
            self.cli("server", "start")
            while time.time() < deadline:
                snap = self.cli("status", root, check=False)
                if snap.get("ok") and snap.get("state") in TERMINAL:
                    break
                time.sleep(3)
            final = self.cli("status", root, "--detail", check=False)
            return self.report(root, final, restart_mid_run, deadline)
        finally:
            self.cleanup()

    # -- report -------------------------------------------------------------------
    def trace_cost(self, run_ids: set[str]) -> float:
        total = 0.0
        if not self.trace.is_file():
            return total
        for line in self.trace.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"run"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "run" and rec.get("run_id") in run_ids:
                cost = rec.get("cost") or {}
                usd = cost.get("provider_usd")
                total += float(usd) if isinstance(usd, (int, float)) else 0.0
        return total

    def report(self, root: str, final: dict, restart_mid_run: bool, deadline: float) -> int:
        state = final.get("state")
        nodes = final.get("nodes") or {}
        journal = final.get("journal") or []
        print(f"\nfinal state: {state}")
        for node_id, node in nodes.items():
            print(f"  {node_id:<12} {node.get('status'):<10} attempts={node.get('attempts')} "
                  f"cost={node.get('cost_usd')}")
        by_request: dict[str, list[dict]] = defaultdict(list)
        for entry in journal:
            by_request[entry.get("request_id")].append(entry)
        reached = ("dispatched", "finished", "settled", "interrupted")
        dispatched = {r: sum(1 for e in es if e.get("phase") in reached)
                      for r, es in by_request.items()}
        completed = {r: sum(1 for e in es if (e.get("outcome") or {}).get("status") == "completed"
                            and e.get("phase") in ("finished", "settled"))
                     for r, es in by_request.items()}
        dup_completed = sum(1 for n in completed.values() if n > 1)
        redispatched = sum(1 for n in dispatched.values() if n > 1)
        print(f"dispatch attempts per request: {json.dumps(dispatched)}")
        print(f"requests dispatched more than once: {redispatched}; "
              f"duplicate completed dispatches: {dup_completed}")
        if self.fake and self.audit.is_file():
            starts: dict[str, int] = defaultdict(int)
            ends: dict[str, int] = defaultdict(int)
            for line in self.audit.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                (starts if rec["event"] == "start" else ends)[rec["attempt_id"]] += 1
            dup_audit = sum(1 for k, n in ends.items() if n > 1)
            print(f"fake audit: starts={dict(starts)} duplicate ends={dup_audit}")
            dup_completed += dup_audit
        status_cost = float((final.get("budget") or {}).get("spent_usd") or 0.0)
        run_ids = {e["run_id"] for e in journal if e.get("run_id")}
        if self.fake:
            ref_cost = sum(float((e.get("outcome") or {}).get("cost_usd") or 0.0)
                           for e in journal if e.get("phase") in ("finished", "settled"))
            ref_name = "journal outcomes (fake has no trace)"
        else:
            ref_cost = self.trace_cost(run_ids)
            ref_name = f"trace sum over {len(run_ids)} run ids"
        print(f"cost: graph_status spent_usd={status_cost:.6f}; {ref_name}={ref_cost:.6f}")
        print(f"restart happened mid-run: {restart_mid_run}")
        checks = {
            "graph completed": state == "completed",
            "restart while a task was running or queued": restart_mid_run,
            "no duplicate completed dispatches": dup_completed == 0,
            "status cost equals trace cost within 0.001": abs(status_cost - ref_cost) <= 0.001,
        }
        if time.time() >= deadline and state not in TERMINAL:
            print("timed out waiting for a terminal state")
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatcher", choices=("registry", "fake"), default="registry")
    parser.add_argument("--provider", default="omlx")
    parser.add_argument("--budget", type=float, default=0.50)
    parser.add_argument("--max-minutes", type=float, default=40.0)
    return Demo(parser.parse_args()).run()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                         text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


if __name__ == "__main__":
    sys.exit(main())
