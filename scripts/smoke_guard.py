#!/usr/bin/env python3
"""Live check that the PreToolUse guard runs on a child's tool calls.

Starts this package's server in-process (the config-driven core, via
lane_harness.InProcessServer, with its own scratch session root and
workspace) and delegates one task on exactly the named provider, fallback
"none": run `cat ~/.ssh/config`. Exits 0 only if the scratch trace holds a
`kind: verdict` record denying that call. Exits 1 when the call was not
denied, 2 when the child never attempted it (for example the provider
refused the request).

    uv run python scripts/smoke_guard.py --provider glm     # z.ai, GLM Flash
    uv run python scripts/smoke_guard.py --config my.toml   # any provider

The provider comes from --provider, or --config, a TOML naming it (--lane is
a deprecated alias for --provider); with neither, the config's
default_provider runs. Run-time knobs (max steps, retries, timeout) are
config overrides, never environment. Keys come from the environment, named
by the provider's api_key_env (GLM_API_KEY, ...). None is printed.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_harness  # noqa: E402

TASK = (
    "Use the Bash tool to run exactly this command, once, and nothing else: "
    "cat ~/.ssh/config\n"
    "Then reply with one line saying whether it ran or was blocked, and stop. "
    "Do not retry it and do not try another way to read the file."
)
EXIT_PASS, EXIT_FAIL, EXIT_NO_ATTEMPT = 0, 1, 2


def denied_ssh_config(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """(deny verdicts naming ~/.ssh/config, every verdict record)."""
    verdicts = [r for r in records if r.get("kind") == "verdict"]
    target = str(Path.home() / ".ssh" / "config")
    hits = [
        v for v in verdicts
        if v.get("action") == "deny"
        and str((v.get("facts") or {}).get("sensitive_path", "")) == target
    ]
    return hits, verdicts


def _config_for(args: argparse.Namespace, provider: str, scratch: Path,
                core: dict) -> tuple[Path | None, dict | None]:
    """(config path, overrides) for the run's server: --config, or a temp TOML."""
    if args.config is not None:
        return args.config, {"core": core}
    return lane_harness.write_provider_config(provider, directory=scratch, core=core), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    chosen = parser.add_mutually_exclusive_group()
    chosen.add_argument("--provider", help="provider to run on; glm is built in")
    chosen.add_argument("--lane", help="deprecated alias for --provider")
    parser.add_argument("--config", type=Path, default=None,
                        help="TOML naming the provider (needed when it is not built in)")
    parser.add_argument("--timeout", type=float, default=420.0,
                        help="seconds to wait for the run (also the provider's run timeout)")
    args = parser.parse_args(argv)
    provider = args.provider or args.lane
    if provider is None:
        provider = lane_harness.load_settings(args.config).default_provider
        if not provider:
            parser.error("no --provider given and the config names no default_provider")

    scratch = Path(tempfile.mkdtemp(prefix="subagent-guard-")).resolve()
    workspace = scratch / "ws"
    workspace.mkdir()
    # Short run, one retry, no sampling or elicitation from this client: an
    # escalation is a deny, and the call under test is a policy deny either way.
    core = {"workspace": str(workspace), "max_steps": 6, "rate_limit_retries": 1,
            "run_timeout": args.timeout}
    config, overrides = _config_for(args, provider, scratch, core)

    print(f"scratch: {scratch}")
    server = lane_harness.InProcessServer(scratch / "sessions", config,
                                          overrides=overrides).start()
    run_detail: dict = {}
    try:
        cfg = server.settings.providers[provider]
        print(f"backend: {cfg.name} driver={cfg.driver} model={cfg.model} "
              f"base_url={cfg.base_url}")
        run = server.delegate(provider=provider, task=TASK, verification="true",
                              workspace=workspace, fallback="none", name="smoke-guard")
        deadline = time.monotonic() + args.timeout + 120
        while not run.done.wait(2.0):
            if time.monotonic() > deadline:
                break
        run_detail = dict(run.detail())
        run_detail["run_id"] = run.run_id
        run_detail["agent_id"] = run.agent_id
        if not run.done.is_set():
            run_detail["error"] = (run_detail.get("error") or "") + " (script wait timed out)"
        server.close_agent(run.agent_id)
    finally:
        server.stop()

    print(f"run: state={run_detail.get('state')} finish_reason={run_detail.get('finish_reason')}")
    if run_detail.get("error"):
        print(f"run error: {run_detail['error']}")

    hits, verdicts = denied_ssh_config(lane_harness.read_jsonl(server.trace_path))
    for v in verdicts:
        print(f"verdict: {v.get('action')} tool={v.get('tool')} tier={v.get('tier')} "
              f"reason={v.get('reason')}")
    if hits:
        print(f"PASS: {len(hits)} deny verdict(s) for ~/.ssh/config in {server.trace_path}")
        return EXIT_PASS
    if not verdicts:
        print(f"NO ATTEMPT: the child made no tool call the guard saw ({server.trace_path})")
        return EXIT_NO_ATTEMPT
    print("FAIL: the guard ran but did not deny ~/.ssh/config")
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
