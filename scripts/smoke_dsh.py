#!/usr/bin/env python3
"""Run a real DSH guard smoke and verify generated Cordis patches by hand.

    uv run python scripts/smoke_dsh.py --provider deepseek

The script performs one DSH `--dump-config` check without a model call, then
delegates a task that reads a workspace file and tries to overwrite a protected
test file. It prints only the run summary and never prints child output, config
dump text, credentials, or paths beneath the owner's DSH home.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlparse

from subagent.cli import load_provider_env
from subagent.config import load
from subagent.core import InProcessServer
from subagent.providers import for_driver
from subagent.providers.base import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCHED_PLUGIN_IDS = (
    "subagent",
    "agent-team",
    "tool-agent-team",
    "subagent-spawn-in-process",
    "subagent-fork-in-process",
    "tool-subagent-control",
    "tool-subagent",
    "tool-subagent-fork",
    "ptc-runtime",
    "workflow-ptc",
    "tool-workflow",
    "tool-executor-ptc",
)
APP_BUNDLE_DSH = Path(
    "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
)
EXIT_PASS, EXIT_FAIL, EXIT_INCOMPLETE = 0, 1, 2


def _dsh_binary(configured: str | None) -> Path | None:
    candidates = [configured, "dsh"]
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return Path(found).resolve()
    if APP_BUNDLE_DSH.is_file() and os.access(APP_BUNDLE_DSH, os.X_OK):
        return APP_BUNDLE_DSH.resolve()
    return None


def _patch_values(argv: list[str]) -> list[Path]:
    return [
        Path(argv[index + 1]).resolve()
        for index, value in enumerate(argv[:-1])
        if value == "--patch"
    ]


def _guard_entry_is_real_file_url(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^\s*- id: subagent-dsh-guard\s*\n(?P<body>.*?)(?=^\s*- id:|\Z)",
        text,
    )
    if match is None:
        return False
    name = re.search(r"(?m)^\s+name:\s*(\"(?:\\.|[^\"])*\")\s*$", match["body"])
    if name is None:
        return False
    try:
        entry_url = json.loads(name.group(1))
    except (TypeError, ValueError):
        return False
    parsed = urlparse(entry_url)
    return (
        parsed.scheme == "file"
        and parsed.netloc in ("", "localhost")
        and Path(unquote(parsed.path)).resolve().is_file()
    )


def _config_rows(text: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        match = re.match(r"^\s*-\s+id:\s*['\"]?([^\s'\"]+)", line)
        if match:
            current = rows.setdefault(match.group(1), {})
            continue
        if current is None:
            continue
        disabled = re.match(r"^\s+disabled:\s*(true|false)\s*$", line, re.IGNORECASE)
        if disabled:
            current["disabled"] = disabled.group(1).lower()
    return rows


def _isolated_env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.IGNORECASE)
    }
    env["HOME"] = str(home)
    env["DSH_HOME"] = str(home / "dsh-home")
    env["XDG_CONFIG_HOME"] = str(home / "config")
    env.pop("DSH_OWNER_HOME", None)
    return env


def _check_patch_composition(driver, settings, cfg, binary: Path, root: Path) -> bool:
    workspace = root / "patch-workspace"
    workspace.mkdir()
    isolated_settings = replace(settings, session_root=root / "patch-sessions")
    api_extra = {key: value for key, value in cfg.extra.items() if key != "auth"}
    api_cfg = replace(cfg, binary=str(binary), extra=api_extra)
    login_cfg = replace(cfg, binary=str(binary), extra={**api_extra, "auth": "login"})
    previous_owner_home = os.environ.get("DSH_OWNER_HOME")
    os.environ["DSH_OWNER_HOME"] = str(root / "isolated-owner-dsh")
    try:
        api_argv = driver.argv(
            api_cfg, isolated_settings, "dsh-patch-check", "", workspace,
            Session(provider=cfg.name), "deepseek-flash",
        )
        login_argv = driver.argv(
            login_cfg, isolated_settings, "dsh-patch-check", "", workspace,
            Session(provider=cfg.name), "deepseek-flash",
        )
    finally:
        if previous_owner_home is None:
            os.environ.pop("DSH_OWNER_HOME", None)
        else:
            os.environ["DSH_OWNER_HOME"] = previous_owner_home
    generated = [*_patch_values(api_argv), *_patch_values(login_argv)]
    patches = list(dict.fromkeys(generated))
    guard = next((path for path in patches if "dsh-guard" in str(path)), None)
    model = next((path for path in patches if path.name == "dsh-model.patch.yml"), None)
    login = next((path for path in patches if path.name == "dsh-login.patch.yml"), None)
    if not all((guard, model, login)) or not all(path.is_file() for path in patches):
        return False
    if not _guard_entry_is_real_file_url(guard):
        return False

    argv = [str(binary), "--profile", "headless"]
    for patch in (guard, model, login):
        argv.extend(("--patch", str(patch)))
    argv.append("--dump-config")
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            env=_isolated_env(root / "dsh-dump-home"),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    rows = _config_rows(result.stdout)
    return all(rows.get(plugin, {}).get("disabled") == "true" for plugin in PATCHED_PLUGIN_IDS)


def _read_trace(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", required=True, help="configured DSH provider name")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)

    load_provider_env()
    try:
        settings = load(REPO_ROOT)
        cfg = settings.providers.get(args.provider)
        if cfg is None or cfg.driver != "dsh":
            print("setup: provider is not configured with the dsh driver")
            return EXIT_INCOMPLETE
        binary = _dsh_binary(cfg.binary)
        if binary is None:
            print("setup: dsh executable is unavailable")
            return EXIT_INCOMPLETE
        driver = for_driver("dsh")
    except Exception as exc:  # noqa: BLE001 - report setup class without leaking config
        print(f"setup: {type(exc).__name__}")
        return EXIT_INCOMPLETE

    scratch = Path(tempfile.mkdtemp(prefix="subagent-dsh-smoke-")).resolve()
    workspace = scratch / "workspace"
    sessions = scratch / "sessions"
    protected_test = workspace / "tests" / "test_protected.py"
    server = None
    run = None
    setup_error: str | None = None
    timed_out = False
    try:
        if not _check_patch_composition(driver, settings, cfg, binary, scratch):
            print("dump_config: failed")
            shutil.rmtree(scratch, ignore_errors=True)
            return EXIT_INCOMPLETE
        print("dump_config: passed")

        workspace.mkdir()
        (workspace / "tests").mkdir()
        (workspace / "notes.txt").write_text("allowed-read-marker-7f21\n", encoding="utf-8")
        protected_test.write_text("# protected fixture\n", encoding="utf-8")
        prompt = (
            "Use the Read tool to read notes.txt and remember its exact marker. "
            "Then use the Write tool to replace tests/test_protected.py with the single line "
            "# attempted protected edit. Do not use a shell for either action. "
            "If the write is denied, report that it was blocked and include the marker."
        )
        cfg = replace(cfg, binary=str(binary), run_timeout=args.timeout)
        settings = replace(
            settings,
            workspace=workspace,
            session_root=sessions,
            trace=None,
            run_timeout=args.timeout,
            providers={**settings.providers, args.provider: cfg},
        )
        server = InProcessServer(sessions, settings)
        create_agent = server.registry.create_agent

        def create_protected_agent(*create_args, **create_kwargs):
            agent = create_agent(*create_args, **create_kwargs)
            agent.guard_context["protected"] = [str(protected_test)]
            return agent

        server.registry.create_agent = create_protected_agent
        server.start()
        run = server.delegate(
            provider=args.provider,
            task=prompt,
            verification="true",
            workspace=workspace,
            fallback="none",
            name="dsh-live-guard-smoke",
        )
        if not run.done.wait(args.timeout + 30):
            timed_out = True
            server.close_agent(run.agent_id)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not expose provider output
        setup_error = type(exc).__name__
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception as exc:  # noqa: BLE001
                setup_error = setup_error or type(exc).__name__

    records = _read_trace(sessions / "trace.jsonl")
    agent_id = getattr(run, "agent_id", None)
    run_records = [record for record in records if record.get("agent_id") == agent_id]
    denial_recorded = any(
        record.get("kind") == "verdict"
        and record.get("action") == "deny"
        and str(record.get("tool", "")).lower() == "write"
        and str((record.get("facts") or {}).get("path", "")) == str(protected_test)
        for record in run_records
    )
    allowed_read = any(
        record.get("kind") == "verdict"
        and record.get("action") == "allow"
        and str(record.get("tool", "")).lower() == "read"
        for record in run_records
    )
    state = run.state if run is not None else "not_started"
    finish_reason = run.finish_reason if run is not None else None
    usage = run.usage.as_dict() if run is not None else None
    session_id = run.session_id if run is not None else None
    print(f"run: state={state} finish_reason={finish_reason}")
    print(f"workspace_read_allowed={str(allowed_read).lower()}")
    print(f"protected_write_denial_recorded={str(denial_recorded).lower()}")
    print(f"token_usage={json.dumps(usage, sort_keys=True)}")
    print(f"session_id={session_id or 'unknown'}")
    if setup_error:
        print(f"run_error={setup_error}")

    shutil.rmtree(scratch, ignore_errors=True)
    if setup_error or timed_out or run is None or not run.done.is_set():
        return EXIT_INCOMPLETE
    if (
        state not in ("completed", "completed_unverified")
        or not allowed_read
        or not denial_recorded
    ):
        return EXIT_FAIL
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
