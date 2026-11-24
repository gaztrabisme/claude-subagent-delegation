"""Regression tests for findings from the real DSH launcher smoke."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session

from .conftest import make_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
DELEGATION_PLUGIN_IDS = (
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
)
BRIDGE_DENIED_TOOL_NAMES = (*DELEGATION_PLUGIN_IDS, "tool-executor-ptc")


def _dsh_settings(tmp_path: Path):
    return make_settings(tmp_path)


def _dsh_config(*, login: bool = False) -> ProviderConfig:
    return ProviderConfig(
        name="deepseek",
        driver="dsh",
        vendor="deepseek",
        model="deepseek-flash",
        binary="/fake/bin/dsh",
        api_key_envs=() if login else ("DEEPSEEK_API_KEY",),
        extra={"auth": "login"} if login else {},
        experimental=True,
    )


def _patch_values(argv: list[str]) -> list[Path]:
    return [
        Path(argv[index + 1]).resolve()
        for index, value in enumerate(argv[:-1])
        if value == "--patch"
    ]


def _patch_rows(text: str) -> dict[str, dict[str, str]]:
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


@pytest.mark.parametrize("login", (False, True), ids=("api-key", "login"))
def test_all_dsh_patches_follow_profile_before_headless_app_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: bool
) -> None:
    monkeypatch.setenv("DSH_OWNER_HOME", str(tmp_path / "isolated-owner-dsh"))
    settings = _dsh_settings(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = for_driver("dsh")
    cfg = _dsh_config(login=login)

    argv = driver.argv(
        cfg,
        settings,
        "live-fixes-order",
        "task",
        workspace,
        Session(provider=cfg.name, session_id="session-order-check"),
        cfg.model,
    )

    assert argv[1:3] == ["--profile", "headless"]
    patch_count = len(_patch_values(argv))
    assert patch_count == 2
    assert argv[3 : 3 + 2 * patch_count : 2] == ["--patch"] * patch_count
    first_app_option = 3 + 2 * patch_count
    assert argv[first_app_option] == "--json"
    assert "--patch" not in argv[first_app_option:]
    assert argv[-1] == "-"
    assert argv[argv.index("--session-id") + 1] == "session-order-check"


def test_generated_guard_patch_disables_every_headless_delegation_plugin(
    tmp_path: Path,
) -> None:
    settings = _dsh_settings(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = _dsh_config()
    argv = for_driver("dsh").argv(
        cfg, settings, "live-fixes-patch", "task", workspace,
        Session(provider=cfg.name), cfg.model,
    )
    guard_patch = next(path for path in _patch_values(argv) if "dsh-guard" in str(path))
    rows = _patch_rows(guard_patch.read_text(encoding="utf-8"))

    for plugin_id in (*DELEGATION_PLUGIN_IDS, "tool-executor-ptc"):
        assert rows.get(plugin_id, {}).get("disabled") == "true", (
            f"guard patch must disable {plugin_id!r}: {rows.get(plugin_id)}"
        )
    assert "__SUBAGENT_DSH_GUARD_ENTRY__" not in guard_patch.read_text(encoding="utf-8")
    assert re.search(
        r'(?m)^\s+name:\s*"file://[^"\n]+"\s*$',
        guard_patch.read_text(encoding="utf-8"),
    )


def test_guard_bridge_explicitly_denies_dsh_delegation_tools(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; DSH bridge behavioral check needs Node")
    entry = REPO_ROOT / "integrations" / "dsh-guard" / "index.mjs"
    script = """
import { pathToFileURL } from 'node:url';
const bridge = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const names = JSON.parse(process.env.TEST_TOOL_NAMES);
const results = await Promise.all(names.map(name => bridge.decide({ name }, {})));
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_TOOL_NAMES": json.dumps(BRIDGE_DENIED_TOOL_NAMES),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    decisions = json.loads(result.stdout)
    assert len(decisions) == len(BRIDGE_DENIED_TOOL_NAMES)
    assert all(decision.get("allow") is False for decision in decisions)
    assert all("disabled by subagent" in decision.get("reason", "") for decision in decisions)
