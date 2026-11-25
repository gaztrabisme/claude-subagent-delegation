"""E1-5 (CLI flag) and E1-6: `subagent envelope check` and `run --override-envelope`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subagent import cli, config

from ._helpers import providers, seed_runs, write_plan


def project(tmp_path: Path, *, envelope: bool = True, percent=None, usd=None, runs_lane="codex"):
    root = tmp_path / "proj"
    (root / config.PROJECT_DIR).mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    plan = write_plan(tmp_path)
    lines = [
        "[core]", 'default_provider = "codex"', f'session_root = "{sessions}"',
        f'trace = "{tmp_path / "trace.jsonl"}"', "",
        "[guard]", 'supervisor = "off"', f'approval_socket = "{tmp_path / "a.sock"}"', "",
    ]
    for name, table in providers("codex", "deepseek", "glm").items():
        lines += [f"[providers.{name}]"] + [f'{k} = "{v}"' for k, v in table.items()] + [""]
    if envelope:
        lines += ["[envelope]", f'lane_plan = "{plan}"', ""]
    (root / config.PROJECT_DIR / config.CONFIG_NAME).write_text("\n".join(lines))
    (tmp_path / "trace.jsonl").write_text("")
    from subagent.lane_status import LaneStatusStore

    store = LaneStatusStore(sessions)
    if percent is not None:
        store.record_usage("codex", {"kind": "percent", "used_percent": percent})
    if usd is not None:
        seed_runs(tmp_path, "deepseek", 1, age=60.0, usd=usd)
    return root


def check(root: Path, provider: str, capsys, *extra: str):
    try:
        code = cli.main(["--root", str(root), "envelope", "check", "--provider", provider,
                         *extra])
    except SystemExit as exc:
        pytest.fail(f"`subagent envelope check` is not a command (exit {exc.code})")
    return code, capsys.readouterr()


@pytest.mark.parametrize(("percent", "code", "level"), [
    (93.0, 3, "refuse"), (85.0, 3, "refuse"), (84.9, 0, "ok"),
])
def test_codex_exit_code_and_level(tmp_path, capsys, percent, code, level):
    root = project(tmp_path, percent=percent)
    got, out = check(root, "codex", capsys, "--json")
    assert got == code
    data = json.loads(out.out)
    assert data["level"] == level and data["lane"] == "codex"


def test_json_object_has_decision_rules_and_reading(tmp_path, capsys):
    root = project(tmp_path, percent=93.0)
    _, out = check(root, "codex", capsys, "--json")
    data = json.loads(out.out)
    for key in ("level", "lane", "kind", "used", "limit", "unit", "reason", "rules", "reading"):
        assert key in data
    assert data["used"] == 93.0 and data["limit"] == 85.0
    assert data["reading"]["used_percent"] == 93.0
    assert {r["kind"] for r in data["rules"]} == {"percent_used", "runs_week"}


def test_warn_exits_zero(tmp_path, capsys):
    root = project(tmp_path, usd=6.0)
    got, out = check(root, "deepseek", capsys, "--json")
    assert got == 0 and json.loads(out.out)["level"] == "warn"


def test_unknown_exits_zero(tmp_path, capsys):
    root = project(tmp_path)
    got, out = check(root, "codex", capsys, "--json")
    assert got == 0 and json.loads(out.out)["level"] == "unknown"


def test_plain_output_names_lane_use_and_limit(tmp_path, capsys):
    root = project(tmp_path, percent=93.0)
    got, out = check(root, "codex", capsys)
    assert got == 3
    text = out.out + out.err
    assert "codex" in text and "93" in text and "85" in text


def test_check_off_says_so_and_exits_zero(tmp_path, capsys):
    root = project(tmp_path, envelope=False, percent=93.0)
    got, out = check(root, "codex", capsys)
    assert got == 0
    text = (out.out + out.err).lower()
    assert "envelope" in text and any(w in text for w in ("off", "not configured", "disabled"))


def test_check_starts_nothing_and_writes_no_trace(tmp_path, capsys, monkeypatch):
    def no_spawn(*args, **kwargs):
        raise AssertionError("envelope check must not start a worker")

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", no_spawn)
    root = project(tmp_path, percent=93.0)
    check(root, "codex", capsys, "--json")
    assert (tmp_path / "trace.jsonl").read_text() == ""


@pytest.fixture
def loop_stub(monkeypatch):
    calls: list = []

    class Server:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return self

        def stop(self):
            pass

    def handler(root, server, args, raw):
        calls.append(args)
        return {"ok": True}, 0

    monkeypatch.setattr("subagent.core.InProcessServer", Server)
    monkeypatch.setitem(cli.loop.LOOP_COMMANDS, "run", handler)
    return calls


def run_cli(root: Path, *flags: str) -> int:
    try:
        return cli.main(["--root", str(root), "run", "--plan", "plan.md", *flags])
    except SystemExit as exc:
        pytest.fail(f"`subagent run` rejects the flag (exit {exc.code})")


@pytest.mark.parametrize("reason", ["", "   "])
def test_cli_empty_reason_is_an_error_before_dispatch(tmp_path, capsys, loop_stub, reason):
    root = project(tmp_path)
    assert run_cli(root, "--override-envelope", reason) != 0
    assert loop_stub == []
    err = capsys.readouterr().err.lower()
    assert "override" in err or "reason" in err


def test_cli_reason_reaches_the_run_handler(tmp_path, loop_stub):
    root = project(tmp_path)
    assert run_cli(root, "--override-envelope", "release freeze") == 0
    assert loop_stub[0].override_envelope == "release freeze"
