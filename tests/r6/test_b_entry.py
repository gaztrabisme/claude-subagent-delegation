"""Acceptance tests for R6(b): `entry` on every trace run record."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import anyio

from subagent import cli, config
from subagent.runs import COMPLETED, Registry
from subagent.telemetry.trace import SCHEMA

from ..conftest import make_settings, write_config
from ..test_runs import _wait
from ..test_trace_schema import problems
from ._helpers import FAKE_CLAUDE, SCENARIOS, fake_claude_provider, read_trace, short_socket_path

VALID_ENTRIES = frozenset({"mcp", "cli", "exec-wrapper"})


def _assert_entry(record: dict, expected: str) -> None:
    assert "entry" in record, f"missing 'entry' on run record: {sorted(record)}"
    assert isinstance(record["entry"], str)
    assert record["entry"] in VALID_ENTRIES
    assert record["entry"] == expected
    assert SCHEMA == 3
    assert problems(record) == []


def test_b_mcp_delegate_records_entry_mcp(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    trace_path = tmp_path / "trace.jsonl"

    with short_socket_path() as sock:
        settings = make_settings(
            tmp_path,
            providers={"glm": fake_claude_provider()},
            workspace=str(project),
            trace=str(trace_path),
            approval_socket=str(sock),
        )
        monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
        module = importlib.import_module("subagent.mcp_server")
        reg = Registry(settings, start_reaper=False)
        monkeypatch.setattr(module, "settings", settings)
        monkeypatch.setattr(module, "registry", reg)
        try:
            out = anyio.run(
                lambda: module.delegate(
                    task="implement add",
                    verification="true",
                    workspace=str(project),
                    provider="glm",
                    fallback="none",
                    wait_seconds=10,
                )
            )
            assert out["state"] == COMPLETED
        finally:
            reg.shutdown()

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    _assert_entry(run_records[0], "mcp")


def test_b_cli_subagent_run_records_entry_cli(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    monkeypatch.setenv("SUBAGENT_LIVE_VIEW", "off")
    project = tmp_path / "project"
    project.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text("Implement add.\n", encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"

    with short_socket_path() as sock:
        cfg_path = write_config(
            tmp_path / "subagent.toml",
            {
                "core": {
                    "workspace": str(project),
                    "session_root": str(tmp_path / "sessions"),
                    "default_provider": "glm",
                    "trace": str(trace_path),
                    "child_env_passthrough": [
                        "FAKE_SCRIPT", "FAKE_MODEL", "FAKE_PROMPT", "FAKE_FAIL_MODEL"
                    ],
                },
                "guard": {
                    "supervisor": "off",
                    "approval_socket": str(sock),
                },
                "providers": {
                    "glm": fake_claude_provider(),
                },
                "loop": {
                    "test_cmd": "true",
                    "review_tests": False,
                    "auto_review": False,
                    "tiers": {
                        "normal": {"provider": "glm", "model": "glm-test"},
                        "hard": {"provider": "glm", "model": "glm-test"},
                    },
                    "review": {"provider": "glm", "model": "glm-test"},
                    "review_hard": {"provider": "glm", "model": "glm-test"},
                    "test_writer": {"provider": "glm", "model": "glm-test"},
                },
            },
        )
        monkeypatch.setenv(config.CONFIG_ENV, str(cfg_path))
        rc = cli.main(["--root", str(project), "run", "--plan", str(plan)])
        assert rc == 0

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    _assert_entry(run_records[0], "cli")


def test_b_exec_wrapper_records_entry_exec_wrapper(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    make_settings(
        tmp_path,
        providers={"glm": fake_claude_provider()},
        workspace=str(project),
        trace=str(trace_path),
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))

    rc = cli.main([
        "--root", str(project),
        "exec", "glm",
        "--", sys.executable, "-c", "print('ok')",
    ])
    assert rc == 0

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    _assert_entry(run_records[0], "exec-wrapper")


def test_b_registry_direct_defaults_entry_to_mcp_and_schema_unchanged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", str(SCENARIOS / "good.sh"))
    monkeypatch.delenv("FAKE_FAIL_MODEL", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    trace_path = tmp_path / "trace.jsonl"

    assert FAKE_CLAUDE.is_file()
    settings = make_settings(
        tmp_path,
        providers={"glm": fake_claude_provider()},
        workspace=str(project),
        trace=str(trace_path),
    )
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("direct", project, provider="glm", fallback="none")
        assert agent.wait_ready(10) is None
        run = _wait(agent.delegate("do work", "true"), timeout=15)
        assert run.state == COMPLETED
    finally:
        reg.shutdown()

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    _assert_entry(run_records[0], "mcp")
