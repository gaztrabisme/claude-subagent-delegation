"""Regression tests for review findings on commit 6f486fb (W1 lane status).

Each test is prefixed with ``test_review_<n>_`` matching the finding number in
``wiki/review-w1-2026-11-24.md``. All tests run strictly offline.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import importlib
import json
import multiprocessing
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest

from subagent import cli, config, health, lane_status
from subagent.guard.supervisor import Supervisor
from subagent.lane_state import LaneState
from subagent.providers.epi import EPI_PROVIDER
from subagent.runs import COMPLETED, FAILED, Registry

from .conftest import make_settings, write_config
from .test_lane_status import (
    FAKE_CLAUDE,
    SCENARIOS,
    _assert_lane_view_shape,
    _fake_claude_provider,
    _trace_run_line,
)
from .test_runs import FakeProcess, _wait

FAKE_EPI = Path(__file__).parent / "fakes" / "fake_epi"
HARNESS_FIXTURES = Path(__file__).parent / "fixtures" / "harness"
CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "codex"
EPI_CANARY = "test-review-epi-canary"
EPI_FAKE_ENV = ["FAKE_EPI_RECORD", "FAKE_EPI_SCENARIO", "FAKE_EPI_SCOPE"]


def _make_epi_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, local: bool = True
) -> tuple[config.Settings, Registry, Path]:
    token = tmp_path / "caller.token"
    token.write_text(EPI_CANARY + "\n", encoding="utf-8")
    token.chmod(0o600)
    (tmp_path / ".pi").mkdir(exist_ok=True)
    (tmp_path / ".pi" / "board.db").touch()
    record = tmp_path / "epi-calls.jsonl"
    monkeypatch.setenv("FAKE_EPI_RECORD", str(record))
    monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path))
    settings = make_settings(
        tmp_path,
        providers={
            "epi": {
                "driver": "epi",
                "binary": str(FAKE_EPI),
                "token_file": str(token),
                "project_root": str(tmp_path),
                "experimental": True,
                "local": local,
                "run_timeout": 15,
                "idle_timeout": 30,
            }
        },
        child_env_passthrough=EPI_FAKE_ENV,
    )
    reg = Registry(settings, start_reaper=False)
    return settings, reg, record


def _bind_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: config.Settings, reg: Registry
):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    mcp_server = importlib.import_module("subagent.mcp_server")
    monkeypatch.setattr(mcp_server, "settings", settings)
    monkeypatch.setattr(mcp_server, "registry", reg)
    return mcp_server


def _mp_probe_worker(
    cfg_path_str: str,
    log_path_str: str,
    barrier: Any,
) -> None:
    """Top-level picklable worker for Finding 5 multi-process probe de-duplication."""
    settings = config.load(extra=Path(cfg_path_str))
    store = lane_status.LaneStatusStore(settings.session_root)
    log_path = Path(log_path_str)

    def fake_prompt_turn(_settings: Any, cfg: Any) -> dict[str, Any]:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}:{cfg.name}\n")
            fh.flush()
        time.sleep(0.35)
        return {
            "ok": True,
            "state": "completed",
            "error": None,
            "output_tokens": 1,
            "ttft_seconds": 0.05,
            "wall_seconds": 0.35,
        }

    barrier.wait(timeout=10)
    lane_status.probe_stale_lanes(settings, store, fake_prompt_turn)


def _write_codex_usage_fake(tmp_path: Path, *, used_percent: float) -> tuple[Path, Path]:
    """Create an executable fake codex binary that writes rate_limits into $CODEX_HOME."""
    user_home = tmp_path / "source-codex-home"
    user_home.mkdir(parents=True, exist_ok=True)
    (user_home / "auth.json").write_text('{"fake": "login"}\n', encoding="utf-8")

    bindir = tmp_path / "fake-codex-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    binary = bindir / "codex"
    fixture_path = CODEX_FIXTURES / "success.jsonl"
    script = f"""#!{sys.executable}
import json, os, pathlib, sys
_ = sys.stdin.read()
codex_home = pathlib.Path(os.environ["CODEX_HOME"])
sess_dir = codex_home / "sessions" / "2026" / "11" / "24"
sess_dir.mkdir(parents=True, exist_ok=True)
rollout = sess_dir / "rollout-2026-11-24T12-00-00.jsonl"
event = {{
    "timestamp": "2026-11-24T12:00:00Z",
    "type": "event_msg",
    "payload": {{
        "type": "token_count",
        "rate_limits": {{
            "primary": {{"used_percent": {used_percent!r}, "resets_at": 1796743897}}
        }},
    }},
}}
rollout.write_text(json.dumps(event) + "\\n", encoding="utf-8")
for line in pathlib.Path({str(fixture_path)!r}).read_text(encoding="utf-8").splitlines():
    if line.strip():
        sys.stdout.write(line + "\\n")
sys.stdout.flush()
sys.exit(0)
"""
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary, user_home


# --- Finding 1 (blocker): deleted driver.metadata breaks epi & list contract ----


def test_review_1_epi_list_preserves_extra_and_status(tmp_path: Path, monkeypatch) -> None:
    settings, reg, record = _make_epi_setup(tmp_path, monkeypatch)
    mcp_server = _bind_mcp_server(tmp_path, monkeypatch, settings, reg)
    try:
        out = anyio.run(mcp_server.list_agents)
        assert len(out["providers"]) == 1
        entry = out["providers"][0]
        assert "extra" in entry, "list_agents() dropped epi provider metadata ('extra')"
        fixture_list = json.loads(
            (HARNESS_FIXTURES / "list.json").read_text(encoding="utf-8").splitlines()[0]
        )
        expected_list = copy.deepcopy(fixture_list)
        resolved_root = str(tmp_path.resolve())
        expected_list["providers"][0]["extra"]["allowed_projects"] = [resolved_root]
        expected_list["providers"][0]["extra"]["default_project"] = resolved_root
        assert entry["extra"]["profile"] == "fake"
        assert entry["extra"]["epi"] == expected_list
        assert "status" in entry
        _assert_lane_view_shape(entry["status"])
        if record.exists():
            calls = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
            assert all(call["argv"][1] == "list" for call in calls)
    finally:
        reg.shutdown()


def test_review_1_epi_list_does_not_block_event_loop(tmp_path: Path, monkeypatch) -> None:
    settings, reg, _ = _make_epi_setup(tmp_path, monkeypatch)
    mcp_server = _bind_mcp_server(tmp_path, monkeypatch, settings, reg)

    def slow_metadata(_settings: Any, _cfg: Any) -> dict[str, Any]:
        time.sleep(0.25)
        return {"profile": "fake", "epi": {"providers": []}}

    monkeypatch.setattr(EPI_PROVIDER, "metadata", slow_metadata)

    ticks = 0
    result: dict[str, Any] = {}

    async def scenario() -> None:
        nonlocal ticks
        done = anyio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await anyio.sleep(0.02)

        async def caller() -> None:
            result["out"] = await mcp_server.list_agents()
            done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            tg.start_soon(caller)

    try:
        anyio.run(scenario)
        entry = result["out"]["providers"][0]
        assert entry.get("extra", {}).get("profile") == "fake"
        assert ticks >= 3, (
            f"list_agents() blocked the anyio event loop while fetching epi metadata (ticks={ticks})"
        )
    finally:
        reg.shutdown()


# --- Finding 2 (blocker): unbounded flock & _finish_locked lock ordering --------


def test_review_2_store_write_bounded_when_lock_held(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)
    lock_path = settings.session_root / f"{lane_status.FILENAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys; "
                f"f = open({str(lock_path)!r}, 'a'); "
                "fcntl.flock(f, fcntl.LOCK_EX); "
                "sys.stdout.write('LOCKED\\n'); sys.stdout.flush(); "
                "sys.stdin.read()"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "LOCKED"

        errors: list[BaseException] = []

        def writer() -> None:
            try:
                store.record_run("glm", "completed")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        thread.join(timeout=4.0)
        wedged = thread.is_alive()
        if wedged:
            holder.kill()
            holder.wait(timeout=5)
            thread.join(timeout=2.0)
            pytest.fail(
                "LaneStatusStore.record_run blocked for >4s on a held flock instead of timing out"
            )
        assert not errors, f"record_run raised unexpectedly on lock timeout: {errors[0]!r}"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_review_2_finish_signals_done_outside_dispatch_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    settings = make_settings(tmp_path, providers={"glm": _fake_claude_provider()})
    reg = Registry(settings, start_reaper=False)

    entered = threading.Event()
    release = threading.Event()
    real_record_run = reg.lane_status.record_run

    def slow_record_run(*args: Any, **kwargs: Any) -> None:
        entered.set()
        release.wait(timeout=4.0)
        return real_record_run(*args, **kwargs)

    monkeypatch.setattr(reg.lane_status, "record_run", slow_record_run)
    try:
        agent = reg.create_agent("done-order", tmp_path, provider="glm", fallback="none")
        assert agent.wait_ready(10) is None
        run = agent.delegate("do work", "true")
        assert entered.wait(timeout=10), "lane_status.record_run was not called on run finish"

        done_before_status = run.done.is_set()
        await_before_status = run.await_ready.is_set()
        lock_free = agent._dispatch_lock.acquire(blocking=False)
        if lock_free:
            agent._dispatch_lock.release()

        assert done_before_status and await_before_status, (
            "run.done / run.await_ready were not signalled before lane_status update"
        )
        assert lock_free, "agent._dispatch_lock is still held during lane_status update"
    finally:
        release.set()
        reg.shutdown()


# --- Finding 3 (blocker): secrets, home paths, and oversized details ------------


def test_review_3_secrets_home_paths_and_length_redacted_in_store_and_list(
    tmp_path: Path, monkeypatch
) -> None:
    env_secret = "zai-canary-secret-token-987654"
    bearer_probe = "bearer-probe-secret-xyz987"
    bearer_direct = "bearer-direct-secret-111222"
    sk_direct = "sk-directcanary1234567890abcd"
    bearer_local = "bearer-local-secret-333444"
    home_str = str(Path.home())

    monkeypatch.setenv("GLM_API_KEY", env_secret)
    providers = {
        "glm": _fake_claude_provider(),
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            "vendor": "deepseek",
        },
        "omlx": {
            "driver": "claude",
            "base_url": "http://127.0.0.1:8000",
            "model": "qwen-test",
            "api_key_env": "OMLX_API_KEY",
            "local": True,
            "health": {"kind": "omlx", "url": "http://127.0.0.1:8000/api/status"},
        },
    }
    settings = make_settings(tmp_path, providers=providers)
    store = lane_status.LaneStatusStore(settings.session_root)
    t0 = datetime.now(UTC)

    # 1. Direct record_probe with bearer token, sk- key, $HOME path, and huge detail.
    store.record_probe(
        "deepseek",
        False,
        detail=(
            f"Authorization: Bearer {bearer_direct} key={sk_direct} "
            f"at {home_str}/.codex/auth.json " + ("B" * 1200)
        ),
        at=t0,
    )

    # 2. probe_stale_lanes on remote (env secret + bearer + $HOME + huge detail)
    #    and local (health failure raising bearer + $HOME).
    def failing_prompt(_settings: Any, cfg: Any) -> dict[str, Any]:
        assert cfg.name == "glm"
        return {
            "ok": False,
            "state": "failed",
            "error": (
                f"Authorization: Bearer {bearer_probe} key={env_secret} "
                f"no login at {home_str}/.codex/auth.json " + ("A" * 1200)
            ),
        }

    def failing_http_get(_url: str, _headers: Any = None, _timeout: Any = None) -> Any:
        raise RuntimeError(f"Bearer {bearer_local} failed reading {home_str}/.omlx/settings.json")

    monkeypatch.setattr(health, "_http_get", failing_http_get)
    lane_status.probe_stale_lanes(settings, store, failing_prompt)

    raw_disk = (settings.session_root / lane_status.FILENAME).read_text(encoding="utf-8")
    reg = Registry(settings, start_reaper=False)
    mcp_server = _bind_mcp_server(tmp_path, monkeypatch, settings, reg)
    try:
        list_out = anyio.run(mcp_server.list_agents)
    finally:
        reg.shutdown()
    raw_list = json.dumps(list_out)

    for label, payload in (("lane_status.json", raw_disk), ("MCP list", raw_list)):
        for leaked in (env_secret, bearer_probe, bearer_direct, sk_direct, bearer_local):
            assert leaked not in payload, f"unredacted secret {leaked!r} leaked into {label}"
        assert home_str not in payload, f"user home path {home_str!r} leaked into {label}"

    by_name = {entry["name"]: entry["status"] for entry in list_out["providers"]}
    for name in ("glm", "deepseek"):
        reason = by_name[name]["reason"] or ""
        assert len(reason) <= 500, f"reason for {name!r} was not clipped (len={len(reason)})"


# --- Finding 4 (blocker): unexpired closures re-probed on every tick ------------


def test_review_4_unexpired_closure_not_selected_or_probed(tmp_path: Path) -> None:
    providers = {"glm": _fake_claude_provider()}
    settings = make_settings(tmp_path, providers=providers, lane_status_ttl_seconds=1800)
    now = datetime.now(UTC)
    set_at = now - timedelta(hours=2)
    closed_until = now + timedelta(hours=3)

    state = LaneState(settings.session_root)
    state.close("glm", closed_until, "zai_1308", "5h quota exhausted")
    # Backdate set_at past TTL (1800s) while keeping closed_until unexpired.
    raw = json.loads(state.path.read_text(encoding="utf-8"))
    raw["glm"]["set_at"] = set_at.isoformat()
    state.path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    store = lane_status.LaneStatusStore(settings.session_root)
    snap = store.snapshot(settings, now=now)
    assert snap["glm"]["state"] == "closed"
    assert snap["glm"]["source"] == "closure"
    assert snap["glm"]["closed_until"] is not None
    assert "glm" not in lane_status.lanes_to_probe(snap), (
        "lanes_to_probe() selected a lane with an unexpired closure"
    )

    prompted: list[str] = []

    def fake_prompt(_settings: Any, cfg: Any) -> dict[str, Any]:
        prompted.append(cfg.name)
        return {"ok": False, "state": "refused", "error": "zai_1308"}

    lane_status.probe_stale_lanes(settings, store, fake_prompt)
    lane_status.probe_stale_lanes(settings, store, fake_prompt)
    assert prompted == [], f"unexpired closure was probed repeatedly: {prompted}"


# --- Finding 5 (major): multi-process dedup, skip login/epi, no trace, shutdown -


def test_review_5_multiprocess_probe_deduplication(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, providers={"glm": _fake_claude_provider()})
    cfg_path = tmp_path / "subagent.toml"
    log_path = tmp_path / "probe-calls.log"

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    proc_a = ctx.Process(
        target=_mp_probe_worker, args=(str(cfg_path), str(log_path), barrier)
    )
    proc_b = ctx.Process(
        target=_mp_probe_worker, args=(str(cfg_path), str(log_path), barrier)
    )
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=20)
    proc_b.join(timeout=20)

    try:
        assert proc_a.exitcode == 0, f"proc_a failed: exitcode={proc_a.exitcode}"
        assert proc_b.exitcode == 0, f"proc_b failed: exitcode={proc_b.exitcode}"
        calls = (
            [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if log_path.exists()
            else []
        )
        assert len(calls) == 1, (
            f"expected exactly 1 prompt probe across 2 concurrent processes, got {len(calls)}: {calls}"
        )
        snap = lane_status.LaneStatusStore(settings.session_root).snapshot(settings)
        assert snap["glm"]["state"] == "open"
    finally:
        for proc in (proc_a, proc_b):
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)


def test_review_5_probes_skip_login_and_epi_lanes(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "caller.token"
    token.write_text(EPI_CANARY + "\n", encoding="utf-8")
    token.chmod(0o600)
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".pi" / "board.db").touch()

    providers = {
        "glm": _fake_claude_provider(),
        "claude_login": {
            "driver": "claude",
            "model": "sonnet",
            "auth": "login",
            "binary": str(FAKE_CLAUDE),
        },
        "epi_remote": {
            "driver": "epi",
            "binary": str(FAKE_EPI),
            "token_file": str(token),
            "project_root": str(tmp_path),
            "experimental": True,
            "local": False,
        },
        "epi_local": {
            "driver": "epi",
            "binary": str(FAKE_EPI),
            "token_file": str(token),
            "project_root": str(tmp_path),
            "experimental": True,
            "local": True,
        },
    }
    settings = make_settings(tmp_path, providers=providers)
    store = lane_status.LaneStatusStore(settings.session_root)
    prompted: list[str] = []

    def fake_prompt(_settings: Any, cfg: Any) -> dict[str, Any]:
        prompted.append(cfg.name)
        return {"ok": True, "state": "completed", "error": None}

    lane_status.probe_stale_lanes(settings, store, fake_prompt)
    assert "glm" in prompted
    assert "claude_login" not in prompted, "automatic probe must not prompt an auth='login' lane"
    assert "epi_remote" not in prompted, "automatic probe must not prompt an epi lane"
    assert "epi_local" not in prompted, "automatic probe must not prompt a local epi lane"


def test_review_5_probe_turn_writes_nothing_to_configured_trace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    trace_file = tmp_path / "configured-trace.jsonl"
    settings = make_settings(
        tmp_path,
        providers={"glm": _fake_claude_provider()},
        trace=str(trace_file),
    )

    outcome = cli._prompt_turn(settings, settings.providers["glm"])
    assert outcome["ok"] is True, f"probe turn unexpectedly failed: {outcome}"
    content = trace_file.read_text(encoding="utf-8") if trace_file.exists() else ""
    assert content == "", (
        f"background/CLI probe turn polluted configured trace file with {len(content.splitlines())} lines"
    )


def test_review_5_probe_stops_promptly_on_shutdown(tmp_path: Path, monkeypatch) -> None:
    pid_file = tmp_path / "slow-probe-child.pid"
    slow_cli = tmp_path / "slow_claude.py"
    slow_cli.write_text(
        f"""#!{sys.executable}
import os, pathlib, sys, time
if "--version" in sys.argv[1:]:
    print("1.0.0 (slow-fake)")
    sys.exit(0)
pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
""",
        encoding="utf-8",
    )
    slow_cli.chmod(0o755)

    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "vendor": "zai",
            "binary": str(slow_cli),
        },
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            "vendor": "deepseek",
            "binary": str(slow_cli),
        },
    }
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-ds-key")
    sock = config._approval_socket_path(tmp_path / "a.sock")
    settings = make_settings(
        tmp_path,
        providers=providers,
        lane_status_ttl_seconds=0.05,
        approval_socket=sock,
    )
    reg = Registry(settings, start_reaper=False)
    sup = Supervisor(settings, reg, trace=reg.trace)
    mcp_server = _bind_mcp_server(tmp_path, monkeypatch, settings, reg)
    monkeypatch.setattr(mcp_server, "supervisor", sup)

    async def run_lifespan() -> None:
        async with mcp_server._lifespan(mcp_server.app):
            deadline = time.monotonic() + 8.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await anyio.sleep(0.05)
            assert pid_file.exists(), "background probe never spawned its child CLI"

    child_pid: int | None = None
    try:
        started_shutdown_window = time.monotonic()
        anyio.run(run_lifespan)
        _ = started_shutdown_window
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())

        # Give a short grace period for thread/process teardown after lifespan exit.
        deadline = time.monotonic() + 2.0
        probe_threads = []
        child_alive = True
        while time.monotonic() < deadline:
            probe_threads = [t for t in threading.enumerate() if t.name == "sam-lane-probe" and t.is_alive()]
            try:
                os.kill(child_pid, 0)
                child_alive = True
            except ProcessLookupError:
                child_alive = False
            if not probe_threads and not child_alive:
                break
            time.sleep(0.05)

        assert not probe_threads, "sam-lane-probe thread stayed alive after server shutdown"
        assert not child_alive, f"probe child pid {child_pid} leaked after server shutdown"
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
        reg.shutdown()


# --- Finding 6 (major): missed finish paths, hop clobbering, codes, staleness ---


def test_review_6_worker_exception_records_lane_status(tmp_path: Path, monkeypatch) -> None:
    ws_a = tmp_path / "crash"
    ws_a.mkdir()
    settings_a = make_settings(ws_a, providers={"glm": _fake_claude_provider()})
    reg_a = Registry(settings_a, start_reaper=False)
    try:
        agent_a = reg_a.create_agent("crasher", ws_a, provider="glm", fallback="none")
        assert agent_a.wait_ready(10) is None
        monkeypatch.setattr(
            agent_a.driver,
            "spawn",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom from driver spawn")),
        )
        run_a = _wait(agent_a.delegate("work", "true"), timeout=10)
        assert run_a.state == FAILED
    finally:
        reg_a.shutdown()

    snap_a = lane_status.LaneStatusStore(settings_a.session_root).snapshot(settings_a)
    assert snap_a["glm"]["reason"] and "boom from driver spawn" in snap_a["glm"]["reason"], (
        f"worker crash bypassed lane_status recording: {snap_a['glm']}"
    )


def test_review_6_continue_missing_key_records_lane_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    ws_b = tmp_path / "missing-key"
    ws_b.mkdir()
    settings_b = make_settings(ws_b, providers={"glm": _fake_claude_provider()})
    reg_b = Registry(settings_b, start_reaper=False)
    try:
        agent_b = reg_b.create_agent("cont", ws_b, provider="glm", fallback="none")
        assert agent_b.wait_ready(10) is None
        run_1 = _wait(agent_b.delegate("first", "true"), timeout=15)
        assert run_1.state == COMPLETED
        assert (
            lane_status.LaneStatusStore(settings_b.session_root).snapshot(settings_b)["glm"][
                "state"
            ]
            == "open"
        )

        monkeypatch.delenv("GLM_API_KEY", raising=False)
        run_2 = _wait(agent_b.follow_up("second"), timeout=10)
        assert run_2.state == FAILED
    finally:
        reg_b.shutdown()

    snap_b = lane_status.LaneStatusStore(settings_b.session_root).snapshot(settings_b)
    assert snap_b["glm"]["state"] == "closed", (
        f"missing API key on continue left lane open: {snap_b['glm']}"
    )
    assert "api key" in (snap_b["glm"]["reason"] or "").lower()


def test_review_6_hop_health_failed_marks_lane_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OMLX_API_KEY", "test-omlx-key")
    ws_health = tmp_path / "health-fail"
    ws_health.mkdir()
    providers_health = {
        "omlx": {
            "driver": "claude",
            "base_url": "http://127.0.0.1:8000",
            "model": "qwen-test",
            "api_key_env": "OMLX_API_KEY",
            "binary": str(FAKE_CLAUDE),
            "local": True,
            "health": {"kind": "omlx", "url": "http://127.0.0.1:8000/api/status"},
        }
    }
    settings_h = make_settings(ws_health, providers=providers_health)
    monkeypatch.setattr(
        health,
        "check",
        lambda _cfg: health.Health(ok=False, base_url=None, message="omlx unreachable"),
    )
    reg_h = Registry(settings_h, start_reaper=False)
    try:
        agent_h = reg_h.create_agent("hfail", ws_health, provider="omlx", fallback="none")
        assert agent_h.wait_ready(10) is None
        run_h = _wait(agent_h.delegate("work", "true"), timeout=10)
        assert run_h.finish_reason == "no_lane"
        assert run_h.hops and run_h.hops[0]["outcome"] == "health_failed"
    finally:
        reg_h.shutdown()

    snap_h = lane_status.LaneStatusStore(settings_h.session_root).snapshot(settings_h)
    assert snap_h["omlx"]["state"] == "closed", (
        f"HOP_HEALTH_FAILED did not mark lane closed: {snap_h['omlx']}"
    )
    assert "unreachable" in (snap_h["omlx"]["reason"] or "").lower()


def test_review_6_hop_refusal_code_not_clobbered_by_run_lane(
    tmp_path: Path, monkeypatch
) -> None:
    ws_ref = tmp_path / "refusal"
    ws_ref.mkdir()
    settings_r = make_settings(ws_ref, providers={"glm": _fake_claude_provider()})
    monkeypatch.setattr(
        health, "check", lambda _cfg: health.Health(ok=True, base_url=_cfg.base_url)
    )

    def spawn_zai_1310(argv: list[str], env: dict[str, str], cwd: str) -> FakeProcess:
        return FakeProcess(
            [
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": 'API Error: {"code": "1310", "message": "weekly cap reached"}',
                }
            ],
            argv,
            env,
        )

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn_zai_1310)
    reg_r = Registry(settings_r, start_reaper=False)
    try:
        agent_r = reg_r.create_agent("ref", ws_ref, provider="glm", fallback="none")
        assert agent_r.wait_ready(10) is None
        run_r = _wait(agent_r.delegate("work", "true"), timeout=10)
        assert run_r.finish_reason == "refused"
    finally:
        reg_r.shutdown()

    raw_store = json.loads(
        (settings_r.session_root / lane_status.FILENAME).read_text(encoding="utf-8")
    )
    assert raw_store["glm"]["state"] == "closed", (
        f"refusal write was clobbered or failed to close lane: {raw_store['glm']}"
    )
    assert "zai_1310" in (raw_store["glm"].get("reason") or ""), (
        f"refusal code 'zai_1310' was overwritten by run.lane write: {raw_store['glm']}"
    )


def test_review_6_refusal_codes_zai_1310_and_1313_close_lane(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    store.record_run("glm", "refused", code="zai_1310", detail="weekly plan cap reached", at=t0)
    store.record_run(
        "deepseek", "refused", code="zai_1313_exhausted", detail="fair use throttle", at=t0
    )

    snap = store.snapshot(settings, now=t0)
    assert snap["glm"]["state"] == "closed", f"zai_1310 did not close lane: {snap['glm']}"
    assert snap["deepseek"]["state"] == "closed", (
        f"zai_1313_exhausted did not close lane: {snap['deepseek']}"
    )


@pytest.mark.parametrize("outcome", ["error", "cancel", "timeout", "no_lane"])
def test_review_6_non_closing_outcomes_do_not_reset_staleness(
    tmp_path: Path, outcome: str
) -> None:
    settings = make_settings(tmp_path, lane_status_ttl_seconds=1800)
    store = lane_status.LaneStatusStore(settings.session_root)
    old = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    now = old + timedelta(hours=2)

    # glm was open 2h ago (stale); deepseek has no record (unknown & stale).
    store.record_run("glm", "completed", at=old)
    store.record_run("glm", outcome, detail=f"simulated {outcome}", at=now)
    store.record_run("deepseek", outcome, detail=f"simulated {outcome}", at=now)

    snap = store.snapshot(settings, now=now)
    assert snap["glm"]["state"] == "open"
    assert snap["glm"]["stale"] is True, (
        f"non-closing outcome {outcome!r} falsely cleared staleness on stale open lane"
    )
    assert snap["deepseek"]["state"] == "unknown"
    assert snap["deepseek"]["stale"] is True, (
        f"non-closing outcome {outcome!r} falsely cleared staleness on unknown lane"
    )


# --- Finding 7 (major): Codex usage reads agent/probe CODEX_HOME ----------------


def test_review_7_codex_run_reads_usage_from_agent_codex_home(
    tmp_path: Path, monkeypatch
) -> None:
    binary, user_home = _write_codex_usage_fake(tmp_path, used_percent=42.5)
    # Put a stale, different value in source_codex_home() to prove the child home wins.
    stale_dir = user_home / "sessions" / "2026" / "11" / "20"
    stale_dir.mkdir(parents=True)
    (stale_dir / "rollout-old.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-11-20T00:00:00Z",
            "payload": {"rate_limits": {"primary": {"used_percent": 99.0, "resets_at": 1796000000}}},
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    settings = make_settings(
        tmp_path,
        providers={"codex": {"driver": "codex", "binary": str(binary)}},
    )
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("codex-u", tmp_path, provider="codex", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("do work", "true"), timeout=15)
        assert run.state == COMPLETED
    finally:
        reg.shutdown()

    usage = lane_status.LaneStatusStore(settings.session_root).snapshot(settings)["codex"]["usage"]
    assert usage is not None, "codex run failed to record usage from child CODEX_HOME"
    assert usage["used_percent"] == pytest.approx(42.5), (
        f"expected usage from child CODEX_HOME (42.5), got {usage}"
    )


def test_review_7_codex_probe_reads_usage_from_probe_codex_home(
    tmp_path: Path, monkeypatch
) -> None:
    binary, user_home = _write_codex_usage_fake(tmp_path, used_percent=31.0)
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    settings = make_settings(
        tmp_path,
        providers={"codex": {"driver": "codex", "binary": str(binary)}},
    )
    store = lane_status.LaneStatusStore(settings.session_root)
    snap = lane_status.probe_stale_lanes(settings, store, cli._prompt_turn)
    assert snap["codex"]["state"] == "open"
    usage = snap["codex"]["usage"]
    assert usage is not None, "codex probe failed to capture usage from probe's isolated CODEX_HOME"
    assert usage["used_percent"] == pytest.approx(31.0)


# --- Finding 8 (major): list offloads snapshot, spend cached, lane_state once ---


def test_review_8_list_does_not_block_event_loop_on_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    reg = Registry(settings, start_reaper=False)
    mcp_server = _bind_mcp_server(tmp_path, monkeypatch, settings, reg)
    real_snapshot = reg.lane_status.snapshot

    def slow_snapshot(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        time.sleep(0.25)
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(reg.lane_status, "snapshot", slow_snapshot)

    ticks = 0

    async def scenario() -> None:
        nonlocal ticks
        done = anyio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await anyio.sleep(0.02)

        async def caller() -> None:
            await mcp_server.list_agents()
            done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            tg.start_soon(caller)

    try:
        anyio.run(scenario)
        assert ticks >= 3, (
            f"list_agents() ran lane_status.snapshot() synchronously on the event loop (ticks={ticks})"
        )
    finally:
        reg.shutdown()


def test_review_8_spend_cached_and_invalidated_on_growth(tmp_path: Path) -> None:
    trace_path = tmp_path / "sessions" / "trace.jsonl"
    # The fixture's session root is no longer tmp_path/sessions (R1a round 4).
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    providers = {
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            "pricing": {"kind": "per_token", "input": 1.0, "output": 2.0, "cache_read": 0.1},
        }
    }
    settings = make_settings(tmp_path, providers=providers, trace=str(trace_path))
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()

    n_lines = 15_000
    one_line = _trace_run_line(ts=ts, lane="deepseek", run_id="r", provider_usd=0.01)
    trace_path.write_text((one_line + "\n") * n_lines, encoding="utf-8")

    store = lane_status.LaneStatusStore(settings.session_root)
    t0 = time.perf_counter()
    first = store.snapshot(settings, now=now)
    cold_s = time.perf_counter() - t0
    assert first["deepseek"]["spend"]["month_usd"] == pytest.approx(n_lines * 0.01)

    t1 = time.perf_counter()
    for _ in range(10):
        cached = store.snapshot(settings, now=now)
        assert cached["deepseek"]["spend"]["month_usd"] == pytest.approx(n_lines * 0.01)
    warm_10_s = time.perf_counter() - t1

    assert warm_10_s < cold_s * 2.0, (
        f"10 unchanged snapshots took {warm_10_s:.3f}s vs cold scan {cold_s:.3f}s "
        "(trace is being rescanned from scratch on every snapshot)"
    )

    # Growth must invalidate/update the cache.
    with trace_path.open("a", encoding="utf-8") as fh:
        fh.write(_trace_run_line(ts=ts, lane="deepseek", run_id="r-new", provider_usd=3.5) + "\n")
    after_append = store.snapshot(settings, now=now)
    assert after_append["deepseek"]["spend"]["month_usd"] == pytest.approx(n_lines * 0.01 + 3.5)

    # Truncation/rotation must reset the cache.
    trace_path.write_text(
        _trace_run_line(ts=ts, lane="deepseek", run_id="r-rot", provider_usd=1.25) + "\n",
        encoding="utf-8",
    )
    after_truncate = store.snapshot(settings, now=now)
    assert after_truncate["deepseek"]["spend"]["month_usd"] == pytest.approx(1.25)


def test_review_8_snapshot_reads_lane_state_once_per_call(
    tmp_path: Path, monkeypatch
) -> None:
    providers = {
        f"lane_{i}": _fake_claude_provider(model=f"m-{i}") for i in range(10)
    }
    settings = make_settings(tmp_path, providers=providers)
    now = datetime.now(UTC)
    LaneState(settings.session_root).close("lane_0", now + timedelta(hours=1), "zai_1308", "quota")

    reads = 0
    real_load = LaneState._load

    def counted_load(self: LaneState) -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return real_load(self)

    monkeypatch.setattr(LaneState, "_load", counted_load)
    lane_status.LaneStatusStore(settings.session_root).snapshot(settings, now=now)
    assert reads <= 1, f"snapshot() read lane_state.json {reads} times for 10 providers"


# --- Finding 9 (major): transient read/lock errors & clock skew -----------------


def test_review_9_transient_read_error_preserves_other_lanes(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)

    store.record_run("glm", "completed", at=t0)
    store.record_run("codex", "completed", at=t0)

    real_read_text = Path.read_text

    def failing_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == store.path:
            raise OSError(errno.EMFILE, "too many open files")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    store.record_run("deepseek", "completed", at=t1)
    monkeypatch.setattr(Path, "read_text", real_read_text)

    snap_after_read_err = store.snapshot(settings, now=t1)
    assert snap_after_read_err["glm"]["state"] == "open", "transient read error wiped glm"
    assert snap_after_read_err["codex"]["state"] == "open", "transient read error wiped codex"


def test_review_9_failed_flock_aborts_unlocked_write(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)

    store.record_run("glm", "completed", at=t0)
    store.record_run("codex", "completed", at=t0)

    real_flock = fcntl.flock

    def failing_flock(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", failing_flock)
    store.record_probe("deepseek", False, detail="should not be written unlocked", at=t1)
    monkeypatch.setattr(fcntl, "flock", real_flock)

    snap_after_lock_err = store.snapshot(settings, now=t1)
    assert snap_after_lock_err["deepseek"]["state"] == "unknown", (
        "store performed an unlocked write after flock failed"
    )
    assert snap_after_lock_err["glm"]["state"] == "open"
    assert snap_after_lock_err["codex"]["state"] == "open"


def test_review_9_future_clock_skew_does_not_poison_freshness_or_drop_updates(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, lane_status_ttl_seconds=1800)
    store = lane_status.LaneStatusStore(settings.session_root)
    now = datetime.now(UTC)
    far_future = now + timedelta(days=30)

    store.path.write_text(
        json.dumps({
            "glm": {
                "state": "open",
                "source": "run",
                "checked_at": far_future.isoformat(),
                "reason": None,
            }
        }),
        encoding="utf-8",
    )

    snap_before = store.snapshot(settings, now=now)
    assert snap_before["glm"]["stale"] is True, (
        "future-skewed checked_at suppressed staleness instead of being treated as stale"
    )

    store.record_run("glm", "auth", detail="401 Unauthorized")
    snap_after = store.snapshot(settings, now=datetime.now(UTC))
    assert snap_after["glm"]["state"] == "closed", (
        "future-skewed checked_at dropped a real wall-clock auth failure"
    )


# --- Finding 10 (major): CLI lanes env loading & --root / project config --------


def test_review_10_cli_lanes_probe_loads_provider_env(
    tmp_path: Path, monkeypatch, mock_endpoint, capsys
) -> None:
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)

    env_file = tmp_path / "subagent.env"
    env_file.write_text(
        "GLM_API_KEY=env-file-glm-key\nOMLX_API_KEY=env-file-omlx-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(cli.PROVIDER_ENV_FILE, str(env_file))

    omlx_base = mock_endpoint.url("omlx")
    mock_endpoint.routes["/omlx/api/status"] = (
        200,
        {"status": "ok", "models_loaded": 1, "loaded_models": ["qwen-test"]},
    )

    def check_auth() -> None:
        auth_hdr = mock_endpoint.headers.get("/omlx/api/status", {}).get("Authorization")
        if auth_hdr != "Bearer env-file-omlx-key":
            mock_endpoint.routes["/omlx/api/status"] = (401, {"error": "unauthorized"})
        else:
            mock_endpoint.routes["/omlx/api/status"] = (
                200,
                {"status": "ok", "models_loaded": 1, "loaded_models": ["qwen-test"]},
            )

    mock_endpoint.on_hit["/omlx/api/status"] = check_auth

    cfg_path = write_config(
        tmp_path / "lanes-env.toml",
        {
            "core": {
                "workspace": str(tmp_path),
                "session_root": str(tmp_path / "sessions"),
                "default_provider": "glm",
                "trace": "off",
            },
            "providers": {
                "glm": {
                    "driver": "claude",
                    "base_url": "https://api.z.ai/api/anthropic",
                    "model": "glm-test",
                    "api_key_env": "GLM_API_KEY",
                    "vendor": "zai",
                },
                "omlx": {
                    "driver": "claude",
                    "base_url": omlx_base,
                    "model": "qwen-test",
                    "api_key_env": "OMLX_API_KEY",
                    "local": True,
                    "health": {"kind": "omlx", "url": f"{omlx_base}/api/status"},
                },
            },
        },
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg_path))

    def fake_prompt(_settings: Any, cfg: Any) -> dict[str, Any]:
        if cfg.api_key() == "env-file-glm-key":
            return {"ok": True, "state": "completed", "error": None}
        return {"ok": False, "state": "failed", "error": "401 missing key"}

    monkeypatch.setattr(cli, "_prompt_turn", fake_prompt)

    rc = cli.main(["lanes", "--probe", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["glm"]["state"] == "open", f"lanes --probe did not load GLM_API_KEY: {data['glm']}"
    assert data["omlx"]["state"] == "open", (
        f"lanes --probe did not load OMLX_API_KEY: {data['omlx']}"
    )


def test_review_10_cli_lanes_respects_root_and_project_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    proj = tmp_path / "myproj"
    (proj / config.PROJECT_DIR).mkdir(parents=True)
    write_config(
        proj / config.PROJECT_DIR / config.CONFIG_NAME,
        {
            "core": {
                "workspace": str(proj),
                "session_root": str(proj / "sessions"),
                "default_provider": "proj_lane",
                "trace": "off",
            },
            "providers": {
                "proj_lane": {
                    "driver": "claude",
                    "base_url": "https://api.z.ai/api/anthropic",
                    "model": "glm-test",
                    "api_key_env": "GLM_API_KEY",
                }
            },
        },
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)

    rc = cli.main(["--root", str(proj), "lanes", "--json"])
    out = capsys.readouterr().out
    assert rc == 0, f"lanes --root failed (rc={rc}): {out}"
    data = json.loads(out)
    assert "proj_lane" in data


# --- Finding 11 (minor): flat_plan spend & native_usage cash_usd ----------------


def test_review_11_month_spend_flat_plan_not_zero(tmp_path: Path) -> None:
    trace_path = tmp_path / "sessions" / "trace.jsonl"
    # The fixture's session root is no longer tmp_path/sessions (R1a round 4).
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    providers = {
        "flat_lane": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "pricing": {"kind": "flat_plan", "monthly_usd": 200.0},
        },
    }
    settings = make_settings(tmp_path, providers=providers, trace=str(trace_path))
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()

    flat_line = _trace_run_line(
        ts=ts, lane="flat_lane", run_id="r-flat", provider_usd=None, cost_kind="flat_plan"
    )
    trace_path.write_text(flat_line + "\n", encoding="utf-8")

    snap = lane_status.LaneStatusStore(settings.session_root).snapshot(settings, now=now)
    assert snap["flat_lane"]["spend"]["month_usd"] != 0.0, (
        "flat_plan provider must not report $0.00 monthly spend"
    )


def test_review_11_month_spend_reads_native_usage_cash_usd(tmp_path: Path) -> None:
    trace_path = tmp_path / "sessions" / "trace.jsonl"
    # The fixture's session root is no longer tmp_path/sessions (R1a round 4).
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    providers = {
        "native_lane": {
            "driver": "claude",
            "base_url": "https://api.example.test",
            "model": "m-test",
            "api_key_env": "GLM_API_KEY",
        },
    }
    settings = make_settings(tmp_path, providers=providers, trace=str(trace_path))
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    ts = datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()

    native_record = json.loads(
        _trace_run_line(ts=ts, lane="native_lane", run_id="r-native", provider_usd=None)
    )
    native_record["native_usage"] = True
    native_record["cash_usd"] = 0.42
    native_record["cost"] = None
    trace_path.write_text(json.dumps(native_record) + "\n", encoding="utf-8")

    snap = lane_status.LaneStatusStore(settings.session_root).snapshot(settings, now=now)
    assert snap["native_lane"]["spend"]["month_usd"] == pytest.approx(0.42), (
        f"native_usage cash_usd was ignored: {snap['native_lane']['spend']}"
    )


# --- Finding 12 (minor): durability, health=none, bool/fallback, single agents --


def test_review_12_store_writes_private_mode_and_fsyncs(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)
    fsync_calls = 0
    real_fsync = os.fsync

    def counted_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counted_fsync)
    store.record_run("glm", "completed", at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))

    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600, f"expected lane_status.json mode 0o600, got {oct(mode)}"
    assert fsync_calls >= 1, "lane_status.json write did not call os.fsync before os.replace"


def test_review_12_local_health_none_not_marked_open_by_probe(tmp_path: Path) -> None:
    providers = {
        "local_none": {
            "driver": "claude",
            "base_url": "http://127.0.0.1:9999",
            "model": "qwen-test",
            "api_key_env": "OMLX_API_KEY",
            "local": True,
            "health": {"kind": "none"},
        }
    }
    settings = make_settings(tmp_path, providers=providers)
    store = lane_status.LaneStatusStore(settings.session_root)
    snap = lane_status.probe_stale_lanes(
        settings,
        store,
        lambda *_a, **_k: pytest.fail("local provider must never be prompt-probed"),
    )
    assert snap["local_none"]["state"] == "unknown", (
        f"local provider with health.kind='none' was marked open without a probe: {snap['local_none']}"
    )


def test_review_12_usage_parsers_reject_bool(tmp_path: Path) -> None:
    assert (
        lane_status.deepseek_balance(
            {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": True}]}
        )
        is None
    )

    bool_only = tmp_path / "codex-bool-only"
    bdir = bool_only / "sessions" / "2026" / "11" / "24"
    bdir.mkdir(parents=True)
    (bdir / "rollout-bool.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-11-24T12:00:00Z",
            "payload": {"rate_limits": {"primary": {"used_percent": True, "resets_at": 1796743897}}},
        }) + "\n",
        encoding="utf-8",
    )
    assert lane_status.codex_usage(bool_only) is None


def test_review_12_codex_usage_falls_back_to_older_session(tmp_path: Path) -> None:
    fallback_home = tmp_path / "codex-fallback"
    fdir = fallback_home / "sessions" / "2026" / "11" / "24"
    fdir.mkdir(parents=True)
    older = fdir / "rollout-older.jsonl"
    newer = fdir / "rollout-newer.jsonl"
    older.write_text(
        json.dumps({
            "timestamp": "2026-11-24T10:00:00Z",
            "payload": {"rate_limits": {"primary": {"used_percent": 33.0, "resets_at": 1796743897}}},
        }) + "\n",
        encoding="utf-8",
    )
    newer.write_text(
        json.dumps({"timestamp": "2026-11-24T12:00:00Z", "payload": {"type": "other"}}) + "\n",
        encoding="utf-8",
    )
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_100_000, 1_700_100_000))

    usage = lane_status.codex_usage(fallback_home)
    assert usage is not None and usage["used_percent"] == pytest.approx(33.0), (
        f"codex_usage did not fall back to older session with rate_limits: {usage}"
    )


def test_review_12_list_agents_reads_registry_agents_once(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    reg = Registry(settings, start_reaper=False)
    mcp_server = _bind_mcp_server(tmp_path, monkeypatch, settings, reg)
    calls = 0
    real_agents = reg.agents

    def counted_agents() -> list[Any]:
        nonlocal calls
        calls += 1
        return real_agents()

    monkeypatch.setattr(reg, "agents", counted_agents)
    try:
        anyio.run(mcp_server.list_agents)
        assert calls == 1, f"list_agents() called registry.agents() {calls} times instead of once"
    finally:
        reg.shutdown()
