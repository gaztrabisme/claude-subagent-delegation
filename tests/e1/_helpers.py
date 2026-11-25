"""Shared helpers for the E1 (pre-dispatch envelope check) acceptance tests.

Everything is offline: providers are claude-driver entries whose process is a
recorded fake, the lane plan is the real "Usage envelopes" section pasted in
tests/e1/fixtures/, and every path lives under tmp_path.
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from subagent import config
from subagent.lane_status import LaneStatusStore
from subagent.runs import Registry

from ..conftest import make_settings

FIXTURE = Path(__file__).parent / "fixtures" / "lane-plan-usage-envelopes.md"
DAY = 86400.0


# --- the module under test, failing loudly instead of erroring at collection ----


def envelope_module():
    try:
        return importlib.import_module("subagent.envelope")
    except ImportError as exc:
        pytest.fail(f"module subagent.envelope is missing: {exc}")


def member(name: str):
    module = envelope_module()
    if not hasattr(module, name):
        pytest.fail(f"subagent.envelope has no member {name!r}")
    return getattr(module, name)


def plan_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def parse(text: str | None = None):
    return member("parse_envelopes")(plan_text() if text is None else text)


def row(label_start: str, text: str | None = None):
    for candidate in parse(text):
        if candidate.label.lower().startswith(label_start.lower()):
            return candidate
    pytest.fail(f"no envelope row whose label starts with {label_start!r}")


def rules_of(label_start: str, text: str | None = None):
    return row(label_start, text).rules


def evaluate(rules, reading):
    return member("evaluate")(rules, reading)


# --- providers, settings, registry -----------------------------------------------


def provider(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "driver": "claude",
        "base_url": "https://api.example.test/anthropic",
        "model": f"{name}-model",
        "api_key_env": "GLM_API_KEY",
        "vendor": "zai",
        **extra,
    }


def providers(*names: str) -> dict[str, dict[str, Any]]:
    return {name: provider(name) for name in names}


def write_plan(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "lane-plan.md"
    path.write_text("# Lane plan\n\nIntro.\n\n" + (plan_text() if text is None else text)
                    + "\n\n## Lane trust promotion\n\nOther text.\n", encoding="utf-8")
    return path


def build_settings(tmp_path: Path, names: tuple[str, ...] = ("codex", "glm"), *,
                   plan: Path | str | None = None, trace: bool = True,
                   provider_tables: dict[str, Any] | None = None):
    """Settings with `[envelope] lane_plan = plan` appended when `plan` is given."""
    tables = provider_tables if provider_tables is not None else providers(*names)
    kwargs: dict[str, Any] = {"trace": str(tmp_path / "trace.jsonl")} if trace else {}
    settings = make_settings(tmp_path, providers=tables, **kwargs)
    if plan is not None:
        path = tmp_path / "subagent.toml"
        path.write_text(path.read_text() + f'\n[envelope]\nlane_plan = {json.dumps(str(plan))}\n')
        settings = config.load(extra=path)
        settings.session_root.mkdir(parents=True, exist_ok=True)
    return settings


def trace_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.jsonl"


def read_trace(tmp_path: Path) -> list[dict[str, Any]]:
    path = trace_path(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def kinds(records: list[dict[str, Any]], prefix: str = "envelope_") -> list[dict[str, Any]]:
    return [r for r in records if str(r.get("kind", "")).startswith(prefix)]


def run_line(ts: float, lane: str, usd: float | None = None, n: int = 0) -> str:
    return json.dumps({
        "schema": 3, "ts": ts, "kind": "run", "run_id": f"seed-{lane}-{n}", "lane": lane,
        "provider": lane, "state": "completed",
        "cost": {"provider_usd": usd, "counterfactual_usd": 0.0, "kind": "per_token",
                 "note": None},
    })


def seed_runs(tmp_path: Path, lane: str, count: int, age: float = 3600.0,
              usd: float | None = None) -> None:
    """`count` finished run records for `lane`, each `age` seconds old."""
    path = trace_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with path.open("a", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(run_line(now - age, lane, usd, i) + "\n")


def seed_percent(settings, lane: str, percent: float) -> None:
    LaneStatusStore(settings.session_root).record_usage(lane, {
        "kind": "percent", "used_percent": percent, "resets_at": 1796743897,
        "observed_at": None,
    })


class FakeProcess:
    def __init__(self, argv, env):
        self.argv = argv
        self.env = env

    def events(self) -> Iterator[dict[str, Any]]:
        yield {"type": "system", "subtype": "init", "session_id": "sess-1"}
        yield {"type": "assistant",
               "message": {"content": [{"type": "text", "text": "done"}]}}
        yield {"type": "result", "subtype": "success", "is_error": False, "result": "done",
               "session_id": "sess-1", "usage": {"input_tokens": 1, "output_tokens": 1},
               "num_turns": 1}

    def kill(self) -> None:
        pass


def patch_spawn(monkeypatch) -> list[FakeProcess]:
    spawned: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        spawned.append(FakeProcess(argv, env))
        return spawned[-1]

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    return spawned


def run_on(registry: Registry, tmp_path: Path, name: str, fallback: str = "none"):
    agent = registry.create_agent("t", tmp_path, provider=name, fallback=fallback)
    agent.wait_ready(5)
    run = agent.delegate("do the thing", "true")
    assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
    return run


def error_text(run) -> str:
    return f"{run.error or ''} {run.error_detail or ''}"


class Env:
    """A registry with recorded spawns over settings written by build_settings."""

    def __init__(self, tmp_path: Path, monkeypatch, names=("codex", "glm"), *, plan="default",
                 trace: bool = True, provider_tables=None, seed=None):
        self.tmp_path = tmp_path
        if plan == "default":
            plan = write_plan(tmp_path)
        self.settings = build_settings(tmp_path, names, plan=plan, trace=trace,
                                       provider_tables=provider_tables)
        if seed:
            seed(self.settings)
        self.spawned = patch_spawn(monkeypatch)
        self.registry = Registry(self.settings, start_reaper=False)

    def run(self, name: str, fallback: str = "none"):
        return run_on(self.registry, self.tmp_path, name, fallback)

    def trace(self) -> list[dict[str, Any]]:
        return read_trace(self.tmp_path)

    def envelope_records(self) -> list[dict[str, Any]]:
        return kinds(self.trace())

    def close(self) -> None:
        self.registry.shutdown()
