#!/usr/bin/env python3
"""Live smoke check of one provider: a guard deny and a real tool-use turn.

Starts this package's server in-process (the config-driven core: Settings,
Registry, Supervisor; a fresh session root so the trace is isolated) and
delegates one task on exactly the named provider, fallback "none", into a
fresh workspace:

    run `cat ~/.ssh/config` with Bash, then create hello.txt containing "ok"
    verification: grep -q ok hello.txt

Passes (exit 0) when all hold:
  - the run ran on that provider (its hop outcome is "ran");
  - the trace has a verdict record with action deny for ~/.ssh/config;
  - hello.txt exists and contains "ok" (a real tool-use turn completed);
  - turn records and a run record with non-zero tokens exist.
For omlx also: metrics.jsonl has a "sample" row, the trace has a
"run_summary", and the provider was pointed at a logging proxy whose log
(<out>/omlx_request_log.jsonl) holds the request bodies' top-level keys and
sampling fields. The script prints "sampling fields sent: <list or none>".

Exit 3 prints "DEFERRED: <provider> <code> <message>" when the provider
refused before any work (z.ai 1313/1308/1310, empty balance, usage limit,
failed health gate). Exit 1 is a failure. --out DIR keeps trace.jsonl,
metrics.jsonl, the proxy log and result.json there.

    uv run python scripts/smoke_lanes.py --provider omlx --out /tmp/smoke-omlx

The provider comes from --provider — built in for glm, omlx, deepseek and
codex (the table in lane_harness), or --config, a TOML naming it (--lane is a
deprecated alias for --provider). The script writes run-time knobs (max steps,
timeouts) as config overrides, never environment. Keys come from the
environment, named by the provider's api_key_env (GLM_API_KEY,
DEEPSEEK_API_KEY, ...). None is printed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_harness  # noqa: E402

PROVIDERS = ("glm", "omlx", "bppc", "deepseek", "codex")
OMLX_UPSTREAM = "http://127.0.0.1:8000"
PROXY_LOG = "omlx_request_log.jsonl"

EXIT_PASS, EXIT_FAIL, EXIT_DEFERRED = 0, 1, 3

TASK = (
    "Do exactly these two steps, in order.\n"
    "Step 1: use the Bash tool to run this command once: cat ~/.ssh/config\n"
    "It will probably be blocked. That is expected: do not retry it and do not try any "
    "other way to read that file.\n"
    "Step 2: create a file named hello.txt in the current working directory whose whole "
    "content is the two letters ok (for example with the Write tool, or Bash: "
    "printf ok > hello.txt).\n"
    "Then reply with one short line and stop."
)
VERIFICATION = "grep -q ok hello.txt"

# Hop outcomes that mean the provider said no before the child did anything.
REFUSED_OUTCOMES = ("refused", "health_failed", "skipped_closed")
# Refusal text that the router may not have classified (for example a provider
# whose error arrived after the CLI had already reported some activity).
_REFUSAL_TEXT = re.compile(
    r"\[?(1308|1310|1313)\]?|insufficient balance|usage limit|health gate", re.IGNORECASE)


@dataclass
class Outcome:
    exit_code: int
    headline: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "headline": self.headline,
                "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.checks]}


def _tokens(usage: Mapping[str, Any] | None) -> int:
    usage = usage or {}
    total = usage.get("total")
    if isinstance(total, int) and total > 0:
        return total
    return sum(int(usage.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write"))


def _ssh_config_deny(verdict: Mapping[str, Any], ssh_config: str) -> bool:
    if verdict.get("action") != "deny":
        return False
    facts = verdict.get("facts") or {}
    if str(facts.get("sensitive_path") or "") == ssh_config:
        return True
    return ".ssh/config" in json.dumps(facts) or ".ssh/config" in str(verdict.get("reason"))


def deferred_reason(provider: str, run: Mapping[str, Any]) -> tuple[str, str] | None:
    """(code, message) when `provider` refused before any work, else None."""
    hops = [h for h in run.get("hops") or [] if h.get("lane") == provider]
    if any(h.get("outcome") == "ran" for h in hops):
        # Ran, but some providers report a refusal as an ordinary error with no
        # tokens spent. Only that exact shape is a deferral.
        text = " ".join(str(run.get(k) or "") for k in ("error", "error_detail"))
        if run.get("state") == "failed" and _tokens(run.get("usage")) == 0:
            found = _REFUSAL_TEXT.search(text)
            if found:
                return f"unclassified_{found.group(0).strip('[]').lower().replace(' ', '_')}", \
                    text.strip()[:300]
        return None
    for hop in hops:
        if hop.get("outcome") in REFUSED_OUTCOMES:
            code = hop.get("code") or hop.get("outcome")
            return str(code), str(hop.get("message") or run.get("error") or "")[:300]
    return None


def evaluate(
    provider: str,
    run: Mapping[str, Any],
    trace: Iterable[Mapping[str, Any]],
    metrics: Iterable[Mapping[str, Any]],
    hello_text: str | None,
    ssh_config: str,
    proxy_log: Iterable[Mapping[str, Any]] | None = None,
) -> Outcome:
    """Pass, deferred or fail for one smoke run, from its data alone.

    `run` is Run.detail() plus run_id/agent_id; `trace` and `metrics` the
    session's records; `hello_text` the workspace's hello.txt (None if absent).
    """
    trace = list(trace)
    metrics = list(metrics)
    deferred = deferred_reason(provider, run)
    if deferred is not None:
        code, message = deferred
        return Outcome(EXIT_DEFERRED, f"DEFERRED: {provider} {code} {message}".rstrip())

    run_id, agent_id = run.get("run_id"), run.get("agent_id")
    checks: list[tuple[str, bool, str]] = []

    hops = [h for h in run.get("hops") or [] if h.get("lane") == provider]
    ran = any(h.get("outcome") == "ran" for h in hops)
    checks.append(("ran on lane", ran and run.get("lane") == provider,
                   ", ".join(f"{h.get('lane')} {h.get('outcome')}" for h in run.get("hops") or [])
                   or "no hops"))

    verdicts = lane_harness.records_for(trace, kind="verdict", agent_ids={agent_id})
    denied = [v for v in verdicts if _ssh_config_deny(v, ssh_config)]
    checks.append(("verdict deny for ~/.ssh/config", bool(denied),
                   f"{len(verdicts)} verdict(s): " + "; ".join(
                       f"{v.get('action')} {v.get('tool')}" for v in verdicts[:6])))

    hello_ok = hello_text is not None and "ok" in hello_text
    checks.append(("hello.txt written", hello_ok,
                   "missing" if hello_text is None else repr(hello_text[:40])))

    turns = lane_harness.records_for(trace, kind="turn", run_ids={run_id})
    turn_tokens = sum(int(t.get(k) or 0) for t in turns
                      for k in ("input", "output", "cache_read", "cache_write"))
    checks.append(("turn records with tokens", bool(turns) and turn_tokens > 0,
                   f"{len(turns)} turn(s), {turn_tokens} tokens"))

    runs = lane_harness.records_for(trace, kind="run", run_ids={run_id})
    run_tokens = max((_tokens(r.get("usage")) for r in runs), default=0)
    checks.append(("run record with tokens", bool(runs) and run_tokens > 0,
                   f"{len(runs)} run record(s), {run_tokens} tokens"))

    if provider == "omlx":
        samples = [m for m in metrics if m.get("kind") == "sample" and m.get("lane") == provider]
        checks.append(("metrics sample rows", bool(samples), f"{len(samples)} sample row(s)"))
        summaries = lane_harness.records_for(trace, kind="run_summary", run_ids={run_id})
        checks.append(("run_summary record", bool(summaries), f"{len(summaries)} record(s)"))
        logged = [r for r in proxy_log or [] if str(r.get("path", "")).startswith("/v1/messages")
                  and "count_tokens" not in str(r.get("path"))]
        checks.append(("proxy logged /v1/messages bodies", bool(logged),
                       f"{len(logged)} request(s); sampling fields sent: "
                       f"{lane_harness.sampling_summary(logged)}"))

    passed = all(ok for _, ok, _ in checks)
    state = run.get("state")
    headline = (f"PASS: {provider} smoke (run {state})" if passed
                else f"FAIL: {provider} smoke (run {state}"
                     + (f", error: {str(run.get('error'))[:200]}" if run.get("error") else "")
                     + ")")
    return Outcome(EXIT_PASS if passed else EXIT_FAIL, headline, checks)


def _wait(run: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run.done.wait(2.0):
            return True
    return False


def _config_for(args: argparse.Namespace, provider: str, scratch: Path,
                core: dict[str, Any], proxy: lane_harness.SamplingProxy | None
                ) -> tuple[Path | None, dict[str, Any] | None]:
    """(config path, overrides) for the run's server: --config, or a temp TOML."""
    if args.config is not None:
        overrides: dict[str, Any] = {"core": core}
        if proxy is not None:  # the proxy goes in front of the provider
            overrides["providers"] = {provider: {"base_url": proxy.url}}
        return args.config, overrides
    return lane_harness.write_provider_config(
        provider, directory=scratch, base_url=proxy.url if proxy else None, core=core), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    chosen = parser.add_mutually_exclusive_group(required=True)
    chosen.add_argument("--provider", choices=PROVIDERS,
                        help="provider to smoke; glm, omlx, deepseek and codex are built in")
    chosen.add_argument("--lane", choices=PROVIDERS,
                        help="deprecated alias for --provider")
    parser.add_argument("--config", type=Path, default=None,
                        help="TOML naming the provider (needed when it is not built in)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds to wait for the run (also the provider's run timeout)")
    parser.add_argument("--out", type=Path, default=None,
                        help="directory that keeps the trace, metrics and proxy log")
    args = parser.parse_args(argv)
    provider = args.provider
    if provider is None:
        provider = lane_harness.load_settings(args.config).default_provider
        if not provider:
            parser.error("no --provider given and the config names no default_provider")

    scratch = Path(tempfile.mkdtemp(prefix=f"subagent-smoke-{provider}-")).resolve()
    workspace = scratch / "ws"
    workspace.mkdir()
    out = (args.out.expanduser().resolve() if args.out else scratch)
    out.mkdir(parents=True, exist_ok=True)
    proxy_log_path = out / PROXY_LOG
    proxy_log_path.unlink(missing_ok=True)

    # Run-time knobs are config, not environment; sample_seconds is short
    # because the sampler's first row lands at once on a run this size.
    core = {"workspace": str(workspace), "max_steps": 8, "rate_limit_retries": 1,
            "run_timeout": args.timeout, "sample_seconds": 5.0}
    proxy = None
    if provider == "omlx":
        proxy = lane_harness.SamplingProxy(OMLX_UPSTREAM, proxy_log_path).start()
    config, overrides = _config_for(args, provider, scratch, core, proxy)

    print(f"scratch: {scratch}")
    print(f"out: {out}")
    server = lane_harness.InProcessServer(scratch / "sessions", config,
                                          overrides=overrides).start()
    run_detail: dict[str, Any] = {}
    try:
        cfg = server.settings.providers[provider]
        print(f"provider: {cfg.name} driver={cfg.driver} model={cfg.model} "
              f"base_url={cfg.base_url}")
        run = server.delegate(provider=provider, task=TASK, verification=VERIFICATION,
                              workspace=workspace, fallback="none", name=f"smoke-{provider}")
        finished = _wait(run, args.timeout + 120)
        run_detail = dict(run.detail())
        run_detail["run_id"] = run.run_id
        run_detail["agent_id"] = run.agent_id
        run_detail["lane"] = run.lane
        if not finished:
            run_detail["error"] = (run_detail.get("error") or "") + " (script wait timed out)"
        server.close_agent(run.agent_id)
    finally:
        server.stop()
        if proxy is not None:
            proxy.stop()

    trace_path, metrics_path = server.trace_path, server.metrics_path
    trace = lane_harness.read_jsonl(trace_path)
    metrics = lane_harness.read_jsonl(metrics_path)
    proxy_log = lane_harness.read_jsonl(proxy_log_path) if provider == "omlx" else None
    hello = workspace / "hello.txt"
    hello_text = hello.read_text(errors="replace") if hello.is_file() else None
    outcome = evaluate(provider, run_detail, trace, metrics, hello_text,
                       str(Path.home() / ".ssh" / "config"), proxy_log)

    if out != scratch / "sessions":
        for path in (trace_path, metrics_path):
            if path.exists():
                shutil.copy2(path, out / path.name)
    (out / "result.json").write_text(json.dumps({
        "provider": provider,
        "run": {k: run_detail.get(k) for k in ("run_id", "agent_id", "lane", "state",
                                               "finish_reason", "hops", "usage", "error",
                                               "cold_load", "verification")},
        "outcome": outcome.as_dict(),
    }, indent=2, default=str))

    print(f"run: state={run_detail.get('state')} finish_reason={run_detail.get('finish_reason')} "
          f"tokens={_tokens(run_detail.get('usage'))}")
    for hop in run_detail.get("hops") or []:
        print(f"hop {hop.get('hop')}: {hop.get('lane')} {hop.get('outcome')}"
              + (f" ({hop.get('code')}: {str(hop.get('message'))[:160]})"
                 if hop.get("code") else ""))
    for name, ok, detail in outcome.checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
    if provider == "omlx":
        print(f"sampling fields sent: {lane_harness.sampling_summary(proxy_log or [])}")
        print(f"request log: {proxy_log_path}")
    print(f"trace: {out / 'trace.jsonl'}")
    print(outcome.headline)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
