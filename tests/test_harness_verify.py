"""Locked acceptance tests for W10: update check, update/rollback, verify, drift, CLI."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from subagent import cli, config
from subagent.lane_state import LaneState
from subagent.providers import codex as codex_driver

from .conftest import make_settings, write_config
from .test_harness_maintenance import (
    CONFORMANCE_DIR,
    _as_dt,
    _field,
    _harness,
    _harness_providers,
    _subdir,
)

CODEX_CONFORMANCE = CONFORMANCE_DIR / "codex"


def _cli_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    for key in ("lanes", "providers"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return [
        {"lane": k, **v} if "lane" not in v else v
        for k, v in payload.items()
        if isinstance(v, dict)
    ]


def _write_fake_bin(tmp_path: Path, name: str, script: str) -> Path:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- 5. Update check, never install ---------------------------------------------


def test_check_updates_skips_unconfigured_caches_and_refreshes_after_interval(
    tmp_path: Path,
) -> None:
    harness = _harness()
    providers = {
        "codex": {"driver": "codex", "binary": "codex", "harness": {
            "update_check": ["npm", "view", "@openai/codex", "version"],
            "update_cmd": ["npm", "install", "-g", "@openai/codex@latest"],
            "check_interval_hours": 12,
        }},
        "claude_default": {"driver": "claude", "base_url": "https://api.z.ai/api/anthropic",
                           "model": "m", "api_key_env": "GLM_API_KEY",
                           "harness": {"update_check": ["npm", "view", "@anthropic-ai/claude-code", "version"]}},
        "glm_no_check": {"driver": "claude", "base_url": "https://api.z.ai/api/anthropic",
                         "model": "m", "api_key_env": "GLM_API_KEY"},
    }
    settings = make_settings(tmp_path, providers=providers)
    calls: list[list[str]] = []
    avail = {"codex": "0.153.0", "claude_default": "2.1.262"}

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append(list(argv))
        assert "install" not in argv  # never installs
        if argv[:2] == ["codex", "--version"]:
            return (0, "0.152.0\n", "")
        if "@openai/codex" in argv:
            return (0, f"{avail['codex']}\n", "")
        if argv[:2] == ["claude", "--version"]:
            return (0, "2.1.261\n", "")
        if "@anthropic-ai/claude-code" in argv:
            return (0, f"{avail['claude_default']}\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    res1 = harness.check_updates(settings, runner, t0)
    assert set(res1) == {"codex", "claude_default"}
    assert res1["codex"]["current"] == "0.152.0"
    assert res1["codex"]["available"] == "0.153.0"
    assert _as_dt(res1["codex"]["checked_at"]) == t0

    calls.clear()
    avail["codex"] = "0.999.0"
    res2 = harness.check_updates(settings, runner, t0 + timedelta(hours=6))
    assert calls == []
    assert res2["codex"]["available"] == "0.153.0"
    assert _as_dt(res2["codex"]["checked_at"]) == t0

    res3 = harness.check_updates(settings, runner, t0 + timedelta(hours=13))
    assert any("@openai/codex" in c for c in calls)
    assert not any("@anthropic-ai/claude-code" in c for c in calls)
    assert res3["codex"]["available"] == "0.999.0"
    assert _as_dt(res3["codex"]["checked_at"]) == t0 + timedelta(hours=13)


def test_check_updates_failing_command_returns_none_available(tmp_path: Path) -> None:
    harness = _harness()
    settings = make_settings(
        tmp_path,
        providers=_harness_providers(update_check=["npm", "view", "codex", "version"]),
    )
    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        raise TimeoutError("network down")

    res = harness.check_updates(settings, runner, t0)
    assert res["codex"]["current"] == "0.152.0"
    assert res["codex"]["available"] is None


# --- 6. Update with rollback ----------------------------------------------------


def test_update_passes_records_previous_and_verifies_new_version(tmp_path: Path) -> None:
    harness = _harness()
    settings = make_settings(
        tmp_path,
        providers=_harness_providers(
            update_cmd=["npm", "install", "-g", "@openai/codex@latest"],
            smoke=["codex", "smoke-ok"],
        ),
    )
    version = "0.151.0"
    ran: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        nonlocal version
        ran.append(list(argv))
        if "--version" in argv:
            return (0, f"{version}\n", "")
        if "install" in argv:
            version = "0.152.0"
            return (0, "installed 0.152.0\n", "")
        if "smoke-ok" in argv:
            return (0, "ok\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    res = harness.update("codex", settings, runner)
    assert _field(res, "ok", "passed") is True
    assert any("install" in cmd for cmd in ran)
    assert any("smoke-ok" in cmd for cmd in ran)
    assert LaneState(settings.session_root).verified_version("codex") == "0.152.0"
    prev = _field(res, "previous_version", "previous")
    if prev is not None:
        assert prev == "0.151.0"


@pytest.mark.parametrize("rollback_mode", ["passes", "absent", "fails"])
def test_update_verify_failure_rollback_and_closure(
    tmp_path: Path, rollback_mode: str
) -> None:
    harness = _harness()
    cfg_extra: dict[str, Any] = {
        "update_cmd": ["npm", "install", "-g", "@openai/codex@latest"],
        "smoke": ["codex", "smoke-fail"],
    }
    if rollback_mode != "absent":
        cfg_extra["rollback_cmd"] = ["npm", "install", "-g", "@openai/codex@0.151.0"]
    settings = make_settings(_subdir(tmp_path, rollback_mode), providers=_harness_providers(**cfg_extra))
    ran: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        ran.append(list(argv))
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        if "@openai/codex@0.151.0" in argv:
            return (0, "rolled back\n", "") if rollback_mode == "passes" else (1, "", "rb failed")
        if "install" in argv:
            return (0, "updated\n", "")
        if "smoke-fail" in argv:
            return (1, "", "smoke failed")
        raise AssertionError(f"unexpected argv: {argv}")

    res = harness.update("codex", settings, runner)
    assert _field(res, "ok", "passed") is False
    closed = LaneState(settings.session_root).closed("codex")
    if rollback_mode == "passes":
        assert any("@openai/codex@0.151.0" in cmd for cmd in ran)
        assert closed is None
    else:
        if rollback_mode == "fails":
            assert any("@openai/codex@0.151.0" in cmd for cmd in ran)
        assert closed is not None and closed["code"] == "harness_unverified"


def test_update_refuses_sudo_and_kind_app_without_running(tmp_path: Path) -> None:
    harness = _harness()
    providers = {
        "codex_sudo": {
            "driver": "codex",
            "harness": {"update_cmd": ["sudo", "npm", "install", "-g", "@openai/codex"]},
        },
        "agy_app": {
            "driver": "antigravity",
            "harness": {"kind": "app", "update_cmd": ["agy", "update"]},
        },
    }
    settings = make_settings(tmp_path, providers=providers)
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        calls.append(list(argv))
        return (0, "", "")

    sudo_res = harness.update("codex_sudo", settings, runner)
    assert calls == []
    assert _field(sudo_res, "refused") is True or _field(sudo_res, "ok", "passed") is False
    assert "sudo" in json.dumps(sudo_res, default=str).lower()

    app_res = harness.update("agy_app", settings, runner)
    assert calls == []
    assert _field(app_res, "refused") is True or _field(app_res, "ok", "passed") is False
    assert "owner" in json.dumps(app_res, default=str).lower()


# --- 7. Verify ------------------------------------------------------------------


def test_verify_passes_records_version_and_reopens_only_harness_unverified(
    tmp_path: Path,
) -> None:
    harness = _harness()
    settings = make_settings(tmp_path, providers=_harness_providers(smoke=["codex", "smoke-ok"]))
    state = LaneState(settings.session_root)
    future = datetime.now().astimezone() + timedelta(hours=3)
    state.close("codex", future, "harness_unverified", "drift")
    state.close("glm", future, "zai_1308", "5h quota")

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        return (0, "ok\n", "")

    res_codex = harness.verify("codex", settings, runner, CONFORMANCE_DIR)
    assert _field(res_codex, "ok", "passed") is True
    assert state.verified_version("codex") == "0.152.0"
    assert state.closed("codex") is None

    res_glm = harness.verify("glm", settings, runner, CONFORMANCE_DIR)
    assert _field(res_glm, "ok", "passed") is True
    assert state.closed("glm") is not None and state.closed("glm")["code"] == "zai_1308"


def test_verify_fails_on_smoke_failure_without_recording_or_reopening(tmp_path: Path) -> None:
    harness = _harness()
    settings = make_settings(tmp_path, providers=_harness_providers(smoke=["codex", "smoke-bad"]))
    state = LaneState(settings.session_root)
    state.close("codex", datetime.now().astimezone() + timedelta(hours=3), "harness_unverified", "x")

    def runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        if "--version" in argv:
            return (0, "0.152.0\n", "")
        return (1, "", "smoke failed")

    res = harness.verify("codex", settings, runner, CONFORMANCE_DIR)
    assert _field(res, "ok", "passed") is False
    assert state.verified_version("codex") is None
    assert state.closed("codex") is not None


# --- 8. Event-schema drift ------------------------------------------------------


def test_drift_unknown_event_type_never_crashes_and_fails_verify(tmp_path: Path) -> None:
    harness = _harness()
    translator = codex_driver.Translator()
    assert isinstance(translator.feed({"type": "response.future_v2", "data": {}}), list)

    cdir = tmp_path / "conformance" / "codex"
    cdir.mkdir(parents=True)
    (cdir / "unknown_ev.jsonl").write_text(
        json.dumps({"type": "thread.started", "thread_id": "t1"})
        + "\n"
        + json.dumps({"type": "response.future_v2", "data": {}})
        + "\n",
        encoding="utf-8",
    )
    (cdir / "unknown_ev.expected.json").write_text(
        json.dumps([{"type": "system", "subtype": "init", "session_id": "t1"}]),
        encoding="utf-8",
    )
    settings = make_settings(tmp_path, providers=_harness_providers())
    res = harness.verify(
        "codex", settings, lambda argv, timeout=10: (0, "0.152.0\n", ""), tmp_path / "conformance"
    )
    assert _field(res, "ok", "passed") is False
    report = json.dumps(res, default=str)
    assert "response.future_v2" in report
    assert "unknown_ev" in report


def test_drift_missing_expected_field_fails_verify_with_type_and_fixture(tmp_path: Path) -> None:
    harness = _harness()
    cdir = tmp_path / "conformance" / "codex"
    cdir.mkdir(parents=True)
    expected = json.loads((CODEX_CONFORMANCE / "success.expected.json").read_text(encoding="utf-8"))
    (cdir / "missing_usage.jsonl").write_text(
        json.dumps({"type": "thread.started", "thread_id": "01a0a0de-91f8-7441-a178-a154caba9282"})
        + "\n"
        + json.dumps({"type": "turn.completed"})
        + "\n",
        encoding="utf-8",
    )
    (cdir / "missing_usage.expected.json").write_text(
        json.dumps([expected[0], expected[-1]]), encoding="utf-8"
    )
    settings = make_settings(tmp_path, providers=_harness_providers())
    res = harness.verify(
        "codex", settings, lambda argv, timeout=10: (0, "0.152.0\n", ""), tmp_path / "conformance"
    )
    assert _field(res, "ok", "passed") is False
    report = json.dumps(res, default=str)
    assert "missing_usage" in report
    assert "result" in report or "turn.completed" in report


# --- 9. CLI ---------------------------------------------------------------------


def test_cli_help_lists_harness(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "harness" in capsys.readouterr().out


def test_cli_harness_status_check_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    codex_bin = _write_fake_bin(
        tmp_path,
        "fake-codex",
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n    print('codex-cli 0.152.0')\n"
        "elif 'check' in args:\n    print('0.153.0')\n"
        "sys.exit(0)\n",
    )
    bad_bin = _write_fake_bin(
        tmp_path,
        "fake-bad",
        "import sys\n"
        "if '--version' in sys.argv[1:]:\n    print('1.0.0')\n    sys.exit(0)\n"
        "sys.exit(1)\n",
    )
    cfg = write_config(
        tmp_path / "subagent.toml",
        {
            "core": {"workspace": str(tmp_path), "session_root": str(tmp_path / "sessions"),
                     "default_provider": "codex", "trace": "off"},
            "providers": {
                "codex": {"driver": "codex", "binary": str(codex_bin),
                          "harness": {"update_check": [str(codex_bin), "check"],
                                      "smoke": [str(codex_bin), "smoke"]}},
                "glm": {"driver": "claude", "base_url": "https://api.z.ai/api/anthropic",
                        "model": "glm-test", "api_key_env": "GLM_API_KEY",
                        "binary": str(bad_bin), "harness": {"smoke": [str(bad_bin), "smoke"]}},
            },
        },
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg))

    assert cli.main(["harness", "check"]) == 0
    capsys.readouterr()

    assert cli.main(["harness", "verify", "codex"]) == 0
    capsys.readouterr()

    assert cli.main(["harness", "status", "--json"]) == 0
    rows = _cli_rows(json.loads(capsys.readouterr().out))
    by_lane = {r["lane"]: r for r in rows}
    assert set(by_lane) == {"codex", "glm"}
    for row in rows:
        assert {"lane", "harness_version", "harness_state", "verified_version"} <= set(row)
    assert by_lane["codex"]["harness_version"] == "0.152.0"
    assert by_lane["codex"]["verified_version"] == "0.152.0"
    assert by_lane["codex"]["harness_state"] == "verified"

    assert cli.main(["harness", "verify", "glm"]) != 0
    assert cli.main(["harness", "verify", "nonexistent"]) != 0

    empty_cfg = write_config(tmp_path / "empty.toml", {"core": {"workspace": str(tmp_path)}})
    monkeypatch.setenv(config.CONFIG_ENV, str(empty_cfg))
    assert cli.main(["harness", "check"]) != 0
