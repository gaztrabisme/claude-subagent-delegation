"""Locked acceptance tests for W10: harness version tracking, status, routing, and check."""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from subagent import config
from subagent.lane_state import LaneState
from subagent.lane_status import LaneStatusStore
from subagent.providers import codex as codex_driver
from subagent.runs import COMPLETED, FAILED, Registry, RegistryError

from .conftest import default_providers, make_settings
from .test_lane_status import REQUIRED_VIEW_KEYS
from .test_runs import FakeProcess, _result, _wait

CONFORMANCE_DIR = Path(__file__).parent / "fixtures" / "conformance"
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"


def _harness():
    """Import subagent.harness lazily so tests collect before W10 is built."""
    return importlib.import_module("subagent.harness")


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _as_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.astimezone()
    assert isinstance(raw, str) and raw, f"expected ISO timestamp or datetime, got {raw!r}"
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.astimezone()


def _harness_providers(**codex_harness: Any) -> dict[str, Any]:
    return {
        "codex": {
            "driver": "codex",
            "binary": "codex",
            **({"harness": codex_harness} if codex_harness else {}),
        },
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "vendor": "zai",
            "binary": str(FAKE_CLAUDE),
        },
    }


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- 1. Version detection -------------------------------------------------------


def test_detect_version_default_args_stdout_and_suffix(tmp_path: Path) -> None:
    harness = _harness()
    settings = make_settings(tmp_path, providers=_harness_providers())
    calls: list[tuple[list[str], float]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append((list(argv), timeout))
        return (0, "codex-cli 0.152.0-beta.1\n", "")

    assert harness.detect_version(settings.provider("codex"), runner) == "0.152.0-beta.1"
    assert calls[0][0] == ["codex", "--version"]


def test_detect_version_override_args_and_stderr_fallback(tmp_path: Path) -> None:
    harness = _harness()
    settings = make_settings(
        tmp_path,
        providers=_harness_providers(version_args=["version", "--short"]),
    )
    seen: list[list[str]] = []

    def runner_stderr(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        seen.append(list(argv))
        return (0, "build info only\n", "Claude Code 2.1.261\n")

    assert harness.detect_version(settings.provider("codex"), runner_stderr) == "2.1.261"
    assert seen == [["codex", "version", "--short"]]

    def runner_both(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        return (0, "codex 3.4.5\n", "warning 0.0.1\n")

    assert harness.detect_version(settings.provider("codex"), runner_both) == "3.4.5"


@pytest.mark.parametrize(
    "outcome",
    [
        (1, "1.2.3", ""),
        (0, "no version token here", "still nothing"),
        TimeoutError("timed out"),
        FileNotFoundError("codex not found"),
    ],
)
def test_detect_version_failures_return_none_without_raising(
    tmp_path: Path, outcome: Any
) -> None:
    harness = _harness()
    settings = make_settings(tmp_path, providers=_harness_providers())

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    assert harness.detect_version(settings.provider("codex"), runner) is None


# --- 2. Verified version memory -------------------------------------------------


def test_lane_state_record_and_read_verified_version(tmp_path: Path) -> None:
    state = LaneState(tmp_path)
    assert state.verified_version("codex") is None

    state.record_verified("codex", "0.152.0")
    assert state.verified_version("codex") == "0.152.0"
    assert LaneState(tmp_path).verified_version("codex") == "0.152.0"


def test_lane_state_old_file_without_verified_field_loads(tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    until = now + timedelta(hours=2)
    (tmp_path / "lane_state.json").write_text(
        json.dumps({
            "glm": {
                "closed_until": until.isoformat(),
                "code": "zai_1308",
                "message": "quota",
                "set_at": now.isoformat(),
            }
        }),
        encoding="utf-8",
    )

    state = LaneState(tmp_path)
    assert state.closed("glm") is not None
    assert state.verified_version("glm") is None

    state.record_verified("glm", "2.1.261")
    reloaded = LaneState(tmp_path)
    assert reloaded.verified_version("glm") == "2.1.261"
    assert reloaded.closed("glm") is not None


# --- 3. Lane status -------------------------------------------------------------


def test_lane_status_harness_fields_unknown_unverified_verified(tmp_path: Path) -> None:
    harness = _harness()
    settings = make_settings(
        tmp_path,
        providers=_harness_providers(update_check=["npm", "view", "codex", "version"]),
    )
    store = LaneStatusStore(settings.session_root)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    snap0 = store.snapshot(settings, now=t0)
    assert set(snap0["codex"]) >= REQUIRED_VIEW_KEYS | {"harness_version", "harness_state"}
    assert snap0["codex"]["harness_version"] is None
    assert snap0["codex"]["harness_state"] == "unknown"

    version = "0.152.0"

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        if "--version" in argv:
            return (0, f"codex {version}\n", "")
        return (0, "0.153.0\n", "")

    harness.check_updates(settings, runner, t0)
    snap1 = store.snapshot(settings, now=t0)
    assert snap1["codex"]["harness_version"] == "0.152.0"
    assert snap1["codex"]["harness_state"] == "unverified"

    harness.verify("codex", settings, runner, CONFORMANCE_DIR)
    snap2 = store.snapshot(settings, now=t0)
    assert snap2["codex"]["harness_version"] == "0.152.0"
    assert snap2["codex"]["harness_state"] == "verified"

    version = "0.153.0"
    harness.check_updates(settings, runner, t0 + timedelta(hours=25))
    snap3 = store.snapshot(settings, now=t0 + timedelta(hours=25))
    assert snap3["codex"]["harness_version"] == "0.153.0"
    assert snap3["codex"]["harness_state"] == "unverified"


def test_lane_status_old_lane_state_file_loads_with_harness_unknown(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, providers=_harness_providers())
    now = datetime.now().astimezone()
    (settings.session_root / "lane_state.json").write_text(
        json.dumps({
            "glm": {
                "closed_until": (now + timedelta(hours=1)).isoformat(),
                "code": "zai_1308",
                "message": "5h quota",
                "set_at": now.isoformat(),
            }
        }),
        encoding="utf-8",
    )
    store = LaneStatusStore(settings.session_root)
    store.record_run("codex", "completed", at=now)

    snap = store.snapshot(settings, now=now)
    assert snap["glm"]["state"] == "closed"
    assert snap["glm"]["harness_version"] is None
    assert snap["glm"]["harness_state"] == "unknown"
    assert snap["codex"]["state"] == "open"
    assert snap["codex"]["harness_version"] is None
    assert snap["codex"]["harness_state"] == "unknown"


# --- 4. Routing -----------------------------------------------------------------


def _seed_unverified(settings: config.Settings, version: str = "0.153.0") -> None:
    harness = _harness()
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    harness.check_updates(settings, lambda argv, timeout=10.0: (0, f"{version}\n", ""), t0)


def _setup_mock_drivers(
    monkeypatch: pytest.MonkeyPatch, *, glm_refuses: bool = False
) -> dict[str, list[list[str]]]:
    spawned: dict[str, list[list[str]]] = {"codex": [], "claude": []}

    def spawn_codex(argv, env, cwd, prompt):
        spawned["codex"].append(list(argv))
        return FakeProcess(_result("codex ok", session_id="thread-1"), argv, env)

    def spawn_claude(argv, env, cwd):
        spawned["claude"].append(list(argv))
        if glm_refuses and "z.ai" in (env.get("ANTHROPIC_BASE_URL") or ""):
            err = {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "API Error: Request rejected (429) · [1310][Weekly Limit Exhausted.]",
                "session_id": "s-glm",
            }
            return FakeProcess(
                [{"type": "system", "subtype": "init", "session_id": "s-glm"}, err], argv, env
            )
        return FakeProcess(_result("claude ok", session_id="s-ok"), argv, env)

    monkeypatch.setattr(codex_driver, "_spawn_codex", spawn_codex)
    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn_claude)
    return spawned


def test_router_skips_unverified_for_auto_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex
) -> None:
    providers = default_providers()
    providers["codex"]["harness"] = {"update_check": ["true"]}
    providers["glm"]["binary"] = str(FAKE_CLAUDE)
    providers["deepseek"]["binary"] = str(FAKE_CLAUDE)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")

    settings_auto = make_settings(
        _subdir(tmp_path, "auto"), providers=providers, default_provider="codex"
    )
    _seed_unverified(settings_auto)
    spawned_a = _setup_mock_drivers(monkeypatch)
    reg_a = Registry(settings_auto, start_reaper=False)
    try:
        agent = reg_a.create_agent("auto", tmp_path, provider=None, fallback="full")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("work", "true"), timeout=15)
        assert run.state == COMPLETED and run.lane != "codex" and spawned_a["codex"] == []
    finally:
        reg_a.shutdown()

    settings_fb = make_settings(
        _subdir(tmp_path, "fb"), providers=providers, default_provider="glm"
    )
    _seed_unverified(settings_fb)
    spawned_b = _setup_mock_drivers(monkeypatch, glm_refuses=True)
    reg_b = Registry(settings_fb, start_reaper=False)
    try:
        agent = reg_b.create_agent("fb", tmp_path, provider="glm", fallback="full")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("work", "true"), timeout=15)
        assert run.state == COMPLETED and run.lane == "deepseek" and spawned_b["codex"] == []
    finally:
        reg_b.shutdown()

    settings_none = make_settings(
        _subdir(tmp_path, "none"), providers={"codex": providers["codex"]}, default_provider="codex"
    )
    _seed_unverified(settings_none)
    spawned_c = _setup_mock_drivers(monkeypatch)
    reg_c = Registry(settings_none, start_reaper=False)
    try:
        try:
            agent = reg_c.create_agent("blocked", tmp_path, provider=None, fallback="full")
            assert agent.wait_ready(10) is None
            run = _wait(agent.delegate("work", "true"), timeout=15)
            assert run.state == FAILED
        except RegistryError:
            pass
        assert spawned_c["codex"] == []
    finally:
        reg_c.shutdown()


def test_router_explicit_unverified_runs_with_warning_and_unknown_unblocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex
) -> None:
    providers = default_providers()
    providers["codex"]["harness"] = {"update_check": ["true"]}

    s_unv = make_settings(_subdir(tmp_path, "explicit"), providers={"codex": providers["codex"]})
    _seed_unverified(s_unv)
    _setup_mock_drivers(monkeypatch)
    reg_unv = Registry(s_unv, start_reaper=False)
    try:
        agent = reg_unv.create_agent("exp", tmp_path, provider="codex", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("work", "true"), timeout=15)
        assert run.state == COMPLETED and run.lane == "codex"
        detail = run.detail()
        warning = detail.get("warning") or " ".join(detail.get("warnings") or ())
        assert isinstance(warning, str) and "codex" in warning and "unverified" in warning.lower()
    finally:
        reg_unv.shutdown()

    s_unk = make_settings(_subdir(tmp_path, "unknown"), providers={"codex": providers["codex"]})
    reg_unk = Registry(s_unk, start_reaper=False)
    try:
        agent = reg_unk.create_agent("unk", tmp_path, provider=None, fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("work", "true"), timeout=15)
        assert run.state == COMPLETED and run.lane == "codex"
    finally:
        reg_unk.shutdown()
