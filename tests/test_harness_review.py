"""Locked acceptance tests for the four W10 harness maintenance review blockers."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest

from subagent import lane_state as lane_state_mod
from subagent.lane_state import LaneState
from subagent.lane_status import LaneStatusStore
from subagent.providers import claude as claude_driver
from subagent.providers import codex as codex_driver
from subagent.runs import COMPLETED, FAILED, Registry, RegistryError

from .conftest import default_providers, make_settings
from .test_harness_maintenance import (
    CONFORMANCE_DIR,
    FAKE_CLAUDE,
    _field,
    _harness,
    _harness_providers,
    _subdir,
)
from .test_runs import FakeProcess, _result

SUDO_COMMANDS: list[tuple[str, list[str]]] = [
    ("direct", ["sudo", "npm", "install", "-g", "@openai/codex"]),
    ("abs_direct", ["/usr/bin/sudo", "npm", "install", "-g", "@openai/codex"]),
    ("env", ["env", "sudo", "npm", "install", "-g", "@openai/codex"]),
    ("bash_c", ["bash", "-c", "sudo npm install -g @openai/codex"]),
    ("sh_lc", ["sh", "-lc", "/usr/bin/sudo npm install -g @openai/codex"]),
    ("zsh_c", ["zsh", "-c", "echo ready && sudo npm install -g @openai/codex"]),
]


# --- Blocker 1: sudo anywhere is refused without invoking runner ----------------


@pytest.mark.parametrize(("label", "bad_cmd"), SUDO_COMMANDS)
def test_update_refuses_sudo_anywhere_without_invoking_runner(
    tmp_path: Path, label: str, bad_cmd: list[str]
) -> None:
    harness = _harness()
    settings = make_settings(
        _subdir(tmp_path, label),
        providers=_harness_providers(update_cmd=bad_cmd),
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append(list(argv))
        return (0, "0.152.0\n", "")

    res = harness.update("codex", settings, runner)
    assert calls == []
    assert _field(res, "ok", "passed") is False
    assert _field(res, "refused") is True
    assert "sudo" in json.dumps(res, default=str).lower()


@pytest.mark.parametrize(("label", "bad_cmd"), SUDO_COMMANDS)
def test_rollback_refuses_sudo_anywhere_without_invoking_runner(
    tmp_path: Path, label: str, bad_cmd: list[str]
) -> None:
    harness = _harness()
    settings = make_settings(
        _subdir(tmp_path, label),
        providers=_harness_providers(
            update_cmd=["npm", "install", "-g", "@openai/codex@latest"],
            smoke=["codex", "smoke-fail"],
            rollback_cmd=bad_cmd,
        ),
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append(list(argv))
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        if "install" in argv:
            return (0, "updated\n", "")
        if "smoke-fail" in argv:
            return (1, "", "smoke failed")
        return (0, "", "")

    res = harness.update("codex", settings, runner)
    assert bad_cmd not in calls
    assert _field(res, "ok", "passed") is False
    assert "sudo" in json.dumps(res, default=str).lower()


@pytest.mark.parametrize(("label", "bad_cmd"), SUDO_COMMANDS)
def test_check_updates_refuses_sudo_anywhere_without_invoking_runner(
    tmp_path: Path, label: str, bad_cmd: list[str]
) -> None:
    harness = _harness()
    settings = make_settings(
        _subdir(tmp_path, label),
        providers=_harness_providers(update_check=bad_cmd),
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append(list(argv))
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        return (0, "9.9.9\n", "")

    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    res = harness.check_updates(settings, runner, t0)
    assert bad_cmd not in calls
    assert res["codex"]["available"] is None


@pytest.mark.parametrize(("label", "bad_cmd"), SUDO_COMMANDS)
def test_verify_smoke_refuses_sudo_anywhere_without_invoking_runner(
    tmp_path: Path, label: str, bad_cmd: list[str]
) -> None:
    harness = _harness()
    settings = make_settings(
        _subdir(tmp_path, label),
        providers=_harness_providers(smoke=bad_cmd),
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append(list(argv))
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        return (0, "ok\n", "")

    res = harness.verify("codex", settings, runner, CONFORMANCE_DIR)
    assert bad_cmd not in calls
    assert _field(res, "ok", "passed") is False
    assert "sudo" in json.dumps(res, default=str).lower()


def test_plain_commands_still_run_in_check_verify_update_and_rollback(tmp_path: Path) -> None:
    harness = _harness()
    check_cmd = ["npm", "view", "pseudo-pkg", "version"]
    smoke_cmd = ["bash", "-c", "echo pseudo-ok"]
    update_cmd = ["npm", "install", "-g", "pseudo-pkg@latest"]
    rollback_cmd = ["bash", "-c", "echo rolled-back"]

    s_ok = make_settings(
        _subdir(tmp_path, "ok"),
        providers=_harness_providers(
            update_check=check_cmd,
            smoke=smoke_cmd,
            update_cmd=update_cmd,
        ),
    )
    ran_ok: list[list[str]] = []

    def runner_ok(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        ran_ok.append(list(argv))
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        if argv == check_cmd:
            return (0, "0.153.0\n", "")
        return (0, "ok\n", "")

    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    chk = harness.check_updates(s_ok, runner_ok, t0)
    assert check_cmd in ran_ok and chk["codex"]["available"] == "0.153.0"

    upd = harness.update("codex", s_ok, runner_ok)
    assert _field(upd, "ok", "passed") is True
    assert update_cmd in ran_ok and smoke_cmd in ran_ok

    s_rb = make_settings(
        _subdir(tmp_path, "rb"),
        providers=_harness_providers(
            update_cmd=update_cmd,
            smoke=["codex", "smoke-fail"],
            rollback_cmd=rollback_cmd,
        ),
    )
    ran_rb: list[list[str]] = []

    def runner_rb(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        ran_rb.append(list(argv))
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        if argv == ["codex", "smoke-fail"]:
            return (1, "", "failed")
        return (0, "ok\n", "")

    upd_rb = harness.update("codex", s_rb, runner_rb)
    assert _field(upd_rb, "ok", "passed") is False
    assert rollback_cmd in ran_rb


# --- Blocker 2: MCP default selection skips an unverified lane ------------------


def _setup_mcp_server(
    subdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, Any],
    *,
    default_provider: str,
) -> tuple[Any, dict[str, list[list[str]]], Registry]:
    settings = make_settings(subdir, providers=providers, default_provider=default_provider)
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    _harness().check_updates(
        settings, lambda argv, timeout=10.0: (0, "0.153.0\n", ""), t0
    )

    spawned: dict[str, list[list[str]]] = {"codex": [], "claude": []}

    def spawn_codex(argv, env, cwd, prompt):
        spawned["codex"].append(list(argv))
        return FakeProcess(_result("codex ok", session_id="thread-1"), argv, env)

    def spawn_claude(argv, env, cwd):
        spawned["claude"].append(list(argv))
        return FakeProcess(_result("claude ok", session_id="s-ok"), argv, env)

    monkeypatch.setattr(codex_driver, "_spawn_codex", spawn_codex)
    monkeypatch.setattr(claude_driver, "_spawn_claude", spawn_claude)

    module = importlib.import_module("subagent.mcp_server")
    registry = Registry(settings, start_reaper=False)
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    return module, spawned, registry


def _mcp_delegate(module: Any, workspace: Path, **kwargs: Any) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        return await module.delegate(
            task="do it",
            verification="true",
            workspace=str(workspace),
            wait_seconds=5,
            **kwargs,
        )

    return anyio.run(call)


def test_mcp_delegate_default_skips_unverified_when_other_lane_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Any
) -> None:
    providers = default_providers()
    providers["codex"]["harness"] = {"update_check": ["true"]}
    providers["glm"]["binary"] = str(FAKE_CLAUDE)

    module, spawned, registry = _setup_mcp_server(
        _subdir(tmp_path, "auto"),
        monkeypatch,
        {"codex": providers["codex"], "glm": providers["glm"]},
        default_provider="codex",
    )
    try:
        out = _mcp_delegate(module, tmp_path)
        assert out["state"] == COMPLETED
        assert out["lane"] == "glm"
        assert spawned["codex"] == []
        assert len(spawned["claude"]) == 1
    finally:
        registry.shutdown()


def test_mcp_delegate_default_refuses_when_only_unverified_lane_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Any
) -> None:
    providers = default_providers()
    providers["codex"]["harness"] = {"update_check": ["true"]}

    module, spawned, registry = _setup_mcp_server(
        _subdir(tmp_path, "only"),
        monkeypatch,
        {"codex": providers["codex"]},
        default_provider="codex",
    )
    try:
        try:
            out = _mcp_delegate(module, tmp_path)
            assert out["state"] in (FAILED, "rejected")
        except RegistryError:
            pass
        assert spawned["codex"] == []
    finally:
        registry.shutdown()


def test_mcp_delegate_explicit_unverified_runs_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Any
) -> None:
    providers = default_providers()
    providers["codex"]["harness"] = {"update_check": ["true"]}
    providers["glm"]["binary"] = str(FAKE_CLAUDE)

    module, spawned, registry = _setup_mcp_server(
        _subdir(tmp_path, "explicit"),
        monkeypatch,
        {"codex": providers["codex"], "glm": providers["glm"]},
        default_provider="codex",
    )
    try:
        out = _mcp_delegate(module, tmp_path, provider="codex")
        assert out["state"] == COMPLETED
        assert out["lane"] == "codex"
        assert len(spawned["codex"]) == 1
        warning = out.get("warning") or ""
        assert "codex" in warning and "unverified" in warning.lower()
    finally:
        registry.shutdown()


# --- Blocker 3: harness_unverified closures do not expire on their own ----------


def test_harness_unverified_closure_stays_closed_until_passing_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness()
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(lane_state_mod, "_now", lambda: t0)

    settings = make_settings(
        tmp_path,
        providers=_harness_providers(
            update_cmd=["npm", "install", "-g", "@openai/codex@latest"],
            smoke=["codex", "smoke"],
        ),
    )
    smoke_ok = False

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        if "install" in argv:
            return (0, "installed\n", "")
        return (0, "ok\n", "") if smoke_ok else (1, "", "smoke failed")

    upd = harness.update("codex", settings, runner)
    assert _field(upd, "ok", "passed") is False

    state = LaneState(settings.session_root)
    store = LaneStatusStore(settings.session_root)

    later = t0 + timedelta(days=10)  # past both 24h and MAX_CLOSE (7 days)
    monkeypatch.setattr(lane_state_mod, "_now", lambda: later)

    entry = state.closed("codex", now=later)
    assert entry is not None and entry["code"] == "harness_unverified"
    assert "codex" in state.closed_entries(now=later)
    closures, _ = state.status_entries(now=later)
    assert "codex" in closures
    assert store.snapshot(settings, now=later)["codex"]["state"] == "closed"

    # A failing verify must keep the closure closed.
    bad_verify = harness.verify("codex", settings, runner, CONFORMANCE_DIR)
    assert _field(bad_verify, "ok", "passed") is False
    assert state.closed("codex", now=later) is not None
    assert store.snapshot(settings, now=later)["codex"]["state"] == "closed"

    # Only a passing verify reopens it.
    smoke_ok = True
    good_verify = harness.verify("codex", settings, runner, CONFORMANCE_DIR)
    assert _field(good_verify, "ok", "passed") is True
    assert state.closed("codex", now=later) is None
    assert store.snapshot(settings, now=later)["codex"]["state"] == "open"


def test_other_closure_codes_keep_existing_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(lane_state_mod, "_now", lambda: t0)

    state = LaneState(tmp_path)
    state.close("glm", t0 + timedelta(hours=2), "zai_1308", "5h quota")
    state.close("deepseek", t0 + timedelta(days=30), "balance_empty", "empty")
    state.close("codex", t0 + timedelta(hours=24), "harness_unverified", "drift")

    assert state.closed("glm", now=t0 + timedelta(hours=1)) is not None
    assert state.closed("glm", now=t0 + timedelta(hours=3)) is None

    assert state.closed("deepseek", now=t0 + timedelta(days=6)) is not None
    assert state.closed("deepseek", now=t0 + timedelta(days=8)) is None

    assert state.closed("codex", now=t0 + timedelta(days=10)) is not None


# --- Blocker 4: drift detection across every driver with fixtures ---------------


def _driver_provider(driver: str) -> dict[str, Any]:
    if driver == "claude":
        return {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-test",
            "api_key_env": "GLM_API_KEY",
            "binary": str(FAKE_CLAUDE),
        }
    return {"driver": driver, "binary": driver}


@pytest.mark.parametrize("driver", ["claude", "codex", "copilot"])
def test_unmodified_conformance_fixtures_pass_for_every_driver(
    tmp_path: Path, driver: str
) -> None:
    harness = _harness()
    lane = "lane"
    settings = make_settings(
        _subdir(tmp_path, driver),
        providers={lane: _driver_provider(driver)},
    )
    res = harness.verify(
        lane,
        settings,
        lambda argv, timeout=10.0: (0, "1.0.0\n", ""),
        CONFORMANCE_DIR,
    )
    assert _field(res, "ok", "passed") is True, res


def test_codex_known_error_and_reasoning_events_do_not_trigger_false_drift(
    tmp_path: Path,
) -> None:
    harness = _harness()
    cdir = tmp_path / "conformance" / "codex"
    cdir.mkdir(parents=True)

    events = [
        {"type": "thread.started", "thread_id": "t-known"},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "item.completed", "item": {"id": "item_r", "type": "reasoning", "text": "step"}},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    translator = codex_driver.Translator()
    expected: list[dict[str, Any]] = []
    for ev in events:
        expected.extend(translator.feed(ev))

    (cdir / "known_events.jsonl").write_text(
        "\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8"
    )
    (cdir / "known_events.expected.json").write_text(
        json.dumps(expected, indent=2), encoding="utf-8"
    )

    settings = make_settings(tmp_path, providers={"codex": _driver_provider("codex")})
    res = harness.verify(
        "codex",
        settings,
        lambda argv, timeout=10.0: (0, "0.152.0\n", ""),
        tmp_path / "conformance",
    )
    assert _field(res, "ok", "passed") is True, res


@pytest.mark.parametrize("driver", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("position", ["append", "insert"])
def test_drift_unknown_event_appended_or_inserted_fails_verify(
    tmp_path: Path, driver: str, position: str
) -> None:
    harness = _harness()
    cdir = tmp_path / "conformance" / driver
    cdir.mkdir(parents=True)

    src_lines = [
        line
        for line in (CONFORMANCE_DIR / driver / "success.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    unknown_line = json.dumps({"type": "response.future_v2", "data": {}})
    if position == "append":
        mutated = [*src_lines, unknown_line]
    else:
        mutated = [src_lines[0], unknown_line, *src_lines[1:]]

    (cdir / "sample.jsonl").write_text("\n".join(mutated) + "\n", encoding="utf-8")
    (cdir / "sample.expected.json").write_text(
        (CONFORMANCE_DIR / driver / "success.expected.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    lane = "lane"
    settings = make_settings(
        _subdir(tmp_path, f"{driver}-{position}"),
        providers={lane: _driver_provider(driver)},
    )
    res = harness.verify(
        lane,
        settings,
        lambda argv, timeout=10.0: (0, "1.0.0\n", ""),
        tmp_path / "conformance",
    )
    assert _field(res, "ok", "passed") is False
    report = json.dumps(res, default=str)
    assert "response.future_v2" in report
    assert "sample" in report
