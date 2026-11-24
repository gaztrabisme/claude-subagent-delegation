"""Regression tests for the three new minor findings in the W1 verification report.

Each test is prefixed with ``test_review_<n>_`` matching the finding number under
``New Findings``. All tests run strictly offline using synthetic data.
"""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path
from typing import Any

import pytest

from subagent import health, lane_status
from subagent.runs import FAILED, Registry, RegistryError

from .conftest import make_settings
from .test_lane_status import FAKE_CLAUDE, _fake_claude_provider
from .test_lane_status_review import EPI_FAKE_ENV, FAKE_EPI
from .test_runs import FakeProcess, _wait


def _active_lockfiles(session_root: Path) -> list[Path]:
    return sorted(session_root.glob(f".{lane_status.FILENAME}.active.*.lock"))


# --- Finding 1 (minor): active_run() leaks .active.*.lock files on disk --------


def test_review_1_active_run_unlinks_lockfile_on_exit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)

    with store.active_run("glm"):
        assert store.active_count("glm") == 1
        assert len(_active_lockfiles(settings.session_root)) == 1

    assert store.active_count("glm") == 0
    assert _active_lockfiles(settings.session_root) == [], (
        "active_run() left its .active.*.lock file on disk after exiting normally"
    )

    with pytest.raises(RuntimeError, match="boom"), store.active_run("glm"):
        assert store.active_count("glm") == 1
        raise RuntimeError("boom")

    assert store.active_count("glm") == 0
    assert _active_lockfiles(settings.session_root) == [], (
        "active_run() left its .active.*.lock file on disk after an exception"
    )


def test_review_1_repeated_runs_do_not_accumulate_lockfiles_or_flock_syscalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    store = lane_status.LaneStatusStore(settings.session_root)

    for _ in range(15):
        with store.active_run("glm"):
            pass

    leftover = _active_lockfiles(settings.session_root)
    assert leftover == [], (
        f"repeated active_run() calls accumulated {len(leftover)} unlinked lockfiles"
    )

    flock_calls = 0
    real_flock = fcntl.flock

    def counted_flock(fd: Any, operation: int) -> None:
        nonlocal flock_calls
        flock_calls += 1
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", counted_flock)
    store.snapshot(settings)
    assert flock_calls == 0, (
        f"snapshot() performed {flock_calls} flock syscalls on leftover active lockfiles"
    )


# --- Finding 2 (minor): fallback-hop crash/cancel before HOP_RAN skips run.lane -


def _two_hop_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GLM_API_KEY", "test-glm-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-ds-key")
    monkeypatch.setattr(
        health, "check", lambda cfg: health.Health(ok=True, base_url=cfg.base_url)
    )
    providers = {
        "glm": _fake_claude_provider(model="glm-test"),
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "ds-test",
            "api_key_env": "DEEPSEEK_API_KEY",
            "vendor": "deepseek",
            "binary": str(FAKE_CLAUDE),
        },
    }
    return make_settings(tmp_path, providers=providers, default_provider="glm")


def test_review_2_fallback_hop_crash_records_lane_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _two_hop_settings(tmp_path, monkeypatch)
    calls = 0

    def spawn_refuse_then_crash(
        argv: list[str], env: dict[str, str], cwd: str
    ) -> FakeProcess:
        nonlocal calls
        calls += 1
        if calls == 1:
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
        raise RuntimeError("deepseek stream crashed")

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn_refuse_then_crash)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("two-hop-crash", tmp_path, provider="glm", fallback="full")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("work", "true"), timeout=10)
        assert run.state == FAILED
        assert run.lane == "deepseek"
        assert run.finish_reason == "error"
        assert len(run.hops) == 1 and run.hops[0]["lane"] == "glm"
    finally:
        reg.shutdown()

    snap = lane_status.LaneStatusStore(settings.session_root).snapshot(settings)
    assert snap["glm"]["state"] == "closed", f"Hop 0 refusal was lost: {snap['glm']}"
    assert snap["deepseek"]["reason"] and "deepseek stream crashed" in snap["deepseek"]["reason"], (
        f"fallback hop crash before HOP_RAN skipped recording run.lane: {snap['deepseek']}"
    )


def test_review_2_fallback_hop_cancel_before_hop_ran_records_lane_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _two_hop_settings(tmp_path, monkeypatch)
    hop1_entered = threading.Event()
    release_hop1 = threading.Event()
    calls = 0

    class BlockingHop1Process:
        def __init__(self, argv: list[str], env: dict[str, str]) -> None:
            self.argv = argv
            self.env = env
            self.killed = False

        def events(self):
            hop1_entered.set()
            release_hop1.wait(timeout=5)
            return iter(())

        def kill(self) -> None:
            self.killed = True

    def spawn_refuse_then_block(
        argv: list[str], env: dict[str, str], cwd: str
    ) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
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
        return BlockingHop1Process(argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn_refuse_then_block)
    reg = Registry(settings, start_reaper=False)
    closer: threading.Thread | None = None
    try:
        agent = reg.create_agent("two-hop-cancel", tmp_path, provider="glm", fallback="full")
        assert agent.wait_ready(10) is None
        run = agent.delegate("work", "true")
        assert hop1_entered.wait(timeout=5), "Hop 1 never entered _collect()"

        closer = threading.Thread(
            target=agent.close,
            kwargs={"reason": "cancelled on fallback hop", "kind": "cancel"},
            daemon=True,
        )
        closer.start()
        assert run.done.wait(timeout=5), "run.done was not signalled by close()"
        assert run.status_ready.wait(timeout=5), "run.status_ready was not set by close()"

        snap = lane_status.LaneStatusStore(settings.session_root).snapshot(settings)
        assert snap["glm"]["state"] == "closed", f"Hop 0 refusal was lost: {snap['glm']}"
        assert (
            snap["deepseek"]["reason"]
            and "cancelled on fallback hop" in snap["deepseek"]["reason"]
        ), (
            "fallback hop cancellation before HOP_RAN skipped recording run.lane: "
            f"{snap['deepseek']}"
        )
    finally:
        release_hop1.set()
        if closer is not None:
            closer.join(timeout=5)
        reg.shutdown()


# --- Finding 3 (minor): wait_admission() bypasses _record_lane_status() ---------


def test_review_3_wait_admission_queued_timeout_records_lane_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "caller.token"
    token.write_text("test-review2-epi-canary\n", encoding="utf-8")
    token.chmod(0o600)
    (tmp_path / ".pi").mkdir(exist_ok=True)
    (tmp_path / ".pi" / "board.db").touch()

    record = tmp_path / "epi-calls.jsonl"
    monkeypatch.setenv("FAKE_EPI_RECORD", str(record))
    monkeypatch.setenv("FAKE_EPI_SCOPE", str(tmp_path))
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")

    settings = make_settings(
        tmp_path,
        providers={
            "epi": {
                "driver": "epi",
                "binary": str(FAKE_EPI),
                "token_file": str(token),
                "project_root": str(tmp_path),
                "experimental": True,
                "local": True,
                "run_timeout": 15,
                "idle_timeout": 30,
            }
        },
        child_env_passthrough=[*EPI_FAKE_ENV, "FAKE_EPI_HUMAN_WAIT"],
    )
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("queued-epi", tmp_path, provider="epi", fallback="none")
        assert agent.wait_ready(5) is None
        first = agent.delegate("change", "true")
        assert first.await_ready.wait(timeout=5)
        assert first.needs_human

        second = agent.follow_up("another change", "true")
        with pytest.raises(RegistryError, match="admission"):
            agent.wait_admission(second, timeout=0.1)

        assert second.status_ready.is_set(), (
            "wait_admission() left run.status_ready unset on queued timeout"
        )
        snap = lane_status.LaneStatusStore(settings.session_root).snapshot(settings)
        assert (
            snap["epi"]["reason"]
            and "provider admission timed out before submission" in snap["epi"]["reason"]
        ), f"queued admission timeout was omitted from lane_status.json: {snap['epi']}"
    finally:
        monkeypatch.delenv("FAKE_EPI_HUMAN_WAIT", raising=False)
        reg.shutdown()
