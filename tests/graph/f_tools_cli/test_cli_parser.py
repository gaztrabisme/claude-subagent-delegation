"""`subagent graph ...` parsing and the exit codes that need no Temporal (SPEC 9)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

import pytest

from tests.conftest import make_settings

from .conftest import CLI_SNIPPET, SIMPLE_DOC, cli_json, run_cli, run_cli_inprocess

VALID = [
    ["graph", "validate", "g.json"],
    ["graph", "validate", "g.json", "--json"],
    ["graph", "start", "g.json"],
    ["graph", "start", "g.json", "--input", "a=1", "--input", "b=x", "--pool", "p",
     "--budget", "2.5", "--priority", "2", "--request-id", "k1", "--workspace", "ws", "--json"],
    ["graph", "status", "gr-abc"],
    ["graph", "status", "gr-abc", "--detail", "--json"],
    ["graph", "pause", "gr-abc"],
    ["graph", "pause", "gr-abc", "--reason", "lunch"],
    ["graph", "resume", "gr-abc"],
    ["graph", "cancel", "gr-abc", "--reason", "wrong graph"],
    ["graph", "list"],
    ["graph", "list", "--state", "completed", "--limit", "5", "--json"],
    ["graph", "exceptions"],
    ["graph", "exceptions", "--root", "gr-abc", "--state", "all", "--kind", "node_failed",
     "--limit", "10", "--json"],
    ["graph", "answer", "gx-abc", "merge"],
    ["graph", "answer", "gx-abc", "merge", "--note", "fine", "--by", "the owner", "--json"],
    ["graph", "server", "start"],
    ["graph", "server", "stop"],
    ["graph", "server", "status"],
    ["graph", "worker"],
    ["graph", "worker", "--dispatcher", "registry", "--max-tasks", "4"],
    ["graph", "worker", "--dispatcher", "fake", "--fake-script", "s.json"],
]

INVALID = [
    ["graph"],
    ["graph", "nonsense"],
    ["graph", "cancel", "gr-abc"],  # --reason is required
    ["graph", "answer", "gx-abc"],
    ["graph", "validate"],
    ["graph", "server", "restart"],
    ["graph", "status", "gr-abc", "--limit", "5"],
]


def _parser():
    from subagent.graph.cli import add_graph_parser

    parser = argparse.ArgumentParser(prog="subagent")
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command")
    add_graph_parser(sub)
    return parser


@pytest.mark.parametrize("argv", VALID, ids=[" ".join(a[1:]) for a in VALID])
def test_every_documented_form_parses(argv):
    _parser().parse_args(argv)


@pytest.mark.parametrize("argv", INVALID, ids=[" ".join(a) for a in INVALID])
def test_malformed_forms_are_rejected(argv):
    with pytest.raises(SystemExit) as err:
        _parser().parse_args(argv)
    assert err.value.code == 2


def test_cmd_graph_is_exposed():
    from subagent.graph import cli

    assert callable(cli.cmd_graph)
    assert callable(cli.add_graph_parser)


def test_the_real_cli_dispatches_graph_verbs(tmp_path, graph_cfg):
    graph_cfg()
    path = tmp_path / "g.json"
    path.write_text(json.dumps(SIMPLE_DOC), encoding="utf-8")
    proc = run_cli(["graph", "validate", str(path), "--json"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = cli_json(proc)
    assert out["ok"] is True and out["graph_id"] == "simple" and out["nodes"] == 2
    assert out["problems"] == []


def test_validate_exit_codes_zero_and_two(tmp_path, graph_cfg):
    graph_cfg()
    good = tmp_path / "good.json"
    good.write_text(json.dumps(SIMPLE_DOC), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**SIMPLE_DOC, "bogus": True}), encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")

    ok = run_cli(["graph", "validate", str(good)], tmp_path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.strip()  # a short table, not silence

    invalid = run_cli(["graph", "validate", str(bad), "--json"], tmp_path)
    assert invalid.returncode == 2
    body = cli_json(invalid)
    assert body["ok"] is False and body["error"]["code"] == "invalid_graph"
    assert any(p["code"] == "unknown_key" for p in body["error"]["problems"])

    parse = run_cli(["graph", "validate", str(broken), "--json"], tmp_path)
    assert parse.returncode == 2
    assert cli_json(parse)["error"]["problems"][0]["code"] == "parse_error"


def test_validate_output_equals_the_tool_dict(tmp_path, graph_cfg):
    import asyncio

    graph_cfg()

    from .conftest import FakeApp, call

    path = tmp_path / "g.json"
    path.write_text(json.dumps(SIMPLE_DOC), encoding="utf-8")
    from subagent.graph.mcp_tools import register

    from subagent.graph.server import GraphSettings
    from subagent.graph.service import GraphService

    service = GraphService(make_settings(tmp_path),
                           gsettings=GraphSettings(state_dir=tmp_path / "state"))
    app = FakeApp()
    register(app, service_factory=lambda: service)
    tool = asyncio.run(call(app.tools["graph_validate"], graph=json.loads(path.read_text())))
    cli = cli_json(run_cli(["graph", "validate", str(path), "--json"], tmp_path))
    assert cli == json.loads(json.dumps(tool))


def test_worker_with_the_fake_dispatcher_requires_a_script(tmp_path, graph_cfg):
    graph_cfg()
    proc = subprocess.Popen(
        [sys.executable, "-c", CLI_SNIPPET, "graph", "worker", "--dispatcher", "fake"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("worker started instead of rejecting --dispatcher fake") from None
    assert proc.returncode != 0
    assert re.search(r"fake-script", out + err)


@pytest.mark.parametrize("argv", [
    ["graph", "start", "{file}", "--json"],
    ["graph", "status", "gr-000000000000", "--json"],
    ["graph", "pause", "gr-000000000000", "--json"],
    ["graph", "resume", "gr-000000000000", "--json"],
    ["graph", "cancel", "gr-000000000000", "--reason", "stop", "--json"],
    ["graph", "list", "--json"],
    ["graph", "exceptions", "--json"],
    ["graph", "answer", "gx-000000000000", "go", "--json"],
], ids=lambda a: a[1])
def test_cli_exits_three_without_temporalio(no_temporalio, graph_cfg, tmp_path, capsys, argv):
    graph_cfg()
    path = tmp_path / "g.json"
    path.write_text(json.dumps(SIMPLE_DOC), encoding="utf-8")
    argv = [str(path) if a == "{file}" else a for a in argv]

    code = run_cli_inprocess(argv)

    assert code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == "unavailable"
    assert "uv sync --extra graph" in out["error"]["message"]


def test_cli_validate_still_works_without_temporalio(no_temporalio, graph_cfg, tmp_path, capsys):
    graph_cfg()
    path = tmp_path / "g.json"
    path.write_text(json.dumps(SIMPLE_DOC), encoding="utf-8")
    assert run_cli_inprocess(["graph", "validate", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_server_start_without_the_temporal_cli_exits_three(tmp_path, monkeypatch, graph_cfg):
    pytest.importorskip("temporalio")
    graph_cfg(address="127.0.0.1:0")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("TEMPORAL_BIN", str(tmp_path / "not-a-file"))
    proc = run_cli(["graph", "server", "start"], tmp_path)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "Temporal CLI not found" in proc.stdout + proc.stderr
