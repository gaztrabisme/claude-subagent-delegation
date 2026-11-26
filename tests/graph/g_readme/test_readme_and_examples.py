"""Spec 11 and 12.2 g: the README "Durable graphs" section and examples/graph/ stay true.

Text checks only: no Temporal, no network. The section's commands are parsed by the
real argparse parser and its JSON examples by the real validator, so a README that
drifts from the code fails here.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
EXAMPLES = ROOT / "examples" / "graph"

SUBSECTIONS = [
    "What it is",
    "Quick start",
    "The document",
    "Budgets",
    "Lanes",
    "Exceptions",
    "Operating it",
    "From another project",
    "Limits and known gaps",
]
TOOL_NAMES = {"graph_validate", "graph_start", "graph_status", "graph_pause", "graph_resume",
              "graph_cancel", "graph_list", "graph_exceptions", "graph_answer"}
FIELD_NAMES_NOT_TOOLS = {"graph_id", "graph_hash", "graph_version"}


def section_text() -> str:
    """The README text from `## Durable graphs` to the next `## ` heading outside code fences."""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = None
    in_fence = False
    out: list[str] = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if start is None:
            if line.strip() == "## Durable graphs" and not in_fence:
                start = index
                out.append(line)
            continue
        if not in_fence and line.startswith("## "):
            break
        out.append(line)
    assert start is not None, "README.md has no '## Durable graphs' section"
    return "\n".join(out)


def headings(text: str, prefix: str) -> list[str]:
    found, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith(prefix) and not line.startswith(prefix + "#"):
            found.append(line[len(prefix):].strip())
    return found


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    blocks, lang, buf, inside = [], "", [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if inside:
                blocks.append((lang, "\n".join(buf)))
                buf, inside = [], False
            else:
                lang, inside = stripped[3:].strip().lower(), True
            continue
        if inside:
            buf.append(line)
    return blocks


def test_section_exists_once_with_nine_subsections_in_order():
    text = section_text()
    subs = headings(text, "### ")
    assert len(subs) == len(SUBSECTIONS), subs
    for expected, actual in zip(SUBSECTIONS, subs, strict=True):
        assert actual.lower().startswith(expected.lower()), (expected, actual)
    all_h2 = headings(README.read_text(encoding="utf-8"), "## ")
    assert all_h2.count("Durable graphs") == 1


def test_names_the_install_command_the_cli_variable_and_the_state_dir_rule():
    text = section_text()
    assert "uv sync --extra graph" in text
    assert "TEMPORAL_BIN" in text
    assert "state_dir" in text
    assert "session_root" in text, "the state dir rule is `session_root / graph` unless overridden"


def test_section_has_no_host_address_home_path_or_key_like_string():
    text = section_text()
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), "no IP address in the section"
    for needle in ("localhost", "tailscale", ".ts.net", "/Users/", "/home/", "~/"):
        assert needle.lower() not in text.lower(), needle
    key_like = [
        r"sk-[A-Za-z0-9_-]{10,}", r"AKIA[0-9A-Z]{12,}", r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"xox[abprs]-", r"eyJ[A-Za-z0-9_-]{20,}",
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{16,}",
    ]
    for pattern in key_like:
        assert not re.search(pattern, text), pattern


def test_feature_x_example_validates_with_load_graph():
    from subagent.graph import load_graph

    graph = load_graph(EXAMPLES / "feature-x.json")
    assert graph.id == "feature-x"
    assert graph.body


def test_feature_x_example_is_the_trimmed_plan_build_review_flow():
    document = json.loads((EXAMPLES / "feature-x.json").read_text(encoding="utf-8"))
    assert document["schema"] == "subagent.graph/1"
    kinds = [n["kind"] for n in document["body"]]
    assert "task" in kinds and "parallel" in kinds


def test_fake_script_example_loads_and_names_real_nodes():
    from subagent.graph import load_graph
    from subagent.graph.dispatch import FakeDispatcher

    path = EXAMPLES / "fake-script.json"
    dispatcher = FakeDispatcher.from_file(path)
    assert dispatcher.calls == []
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "script" in raw
    graph = load_graph(EXAMPLES / "feature-x.json")
    node_ids = {n.id for n in graph.body}

    def collect(nodes):
        for node in nodes:
            node_ids.add(node.id)
            for child in (getattr(node, "branches", None) or ()):
                collect([child])
            collect(getattr(node, "body", None) or ())

    collect(graph.body)
    for key in raw["script"]:
        assert key in node_ids or "/" in key, f"fake-script key {key!r} is not a node of feature-x"


def test_every_graph_json_block_in_the_section_validates():
    from subagent.graph import SCHEMA_ID, load_graph

    graph_blocks = []
    for lang, body in fenced_blocks(section_text()):
        if re.search(r'"schema"\s*:\s*"subagent\.graph/1"', body):
            graph_blocks.append((lang, body))
    assert graph_blocks, "the Quick start must show a graph document"
    for _lang, body in graph_blocks:
        document = json.loads(body)
        assert document["schema"] == SCHEMA_ID
        load_graph(document)


def commands_in(text: str) -> list[str]:
    """Every `subagent graph ...` command line in code fences and inline code, joined across `\\`."""
    found: list[str] = []
    for lang, body in fenced_blocks(text):
        if lang in ("json", "toml"):
            continue
        joined = body.replace("\\\n", " ")
        for line in joined.splitlines():
            line = line.strip().lstrip("$").strip()
            if "subagent graph" in line:
                found.append(line)
    for match in re.finditer(r"`([^`\n]*subagent graph[^`\n]*)`", text):
        found.append(match.group(1).strip())
    return found


def parser() -> argparse.ArgumentParser:
    from subagent.graph.cli import add_graph_parser

    top = argparse.ArgumentParser(prog="subagent")
    sub = top.add_subparsers(dest="command")
    add_graph_parser(sub)
    return top


def tokens(command: str) -> list[str] | None:
    command = command.split(" #")[0].strip()
    if any(mark in command for mark in ("|", "[", "]", "<", ">", "...", "…")):
        return None  # a synopsis with alternatives or placeholders, not a runnable line
    parts = shlex.split(command)
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        parts.pop(0)  # leading VAR=value
    if parts[:2] == ["uv", "run"]:
        parts = parts[2:]
    if not parts or parts[0] != "subagent":
        return None
    return parts[1:]


def test_every_subagent_graph_command_parses_with_the_real_parser():
    commands = commands_in(section_text())
    runnable = [(c, tokens(c)) for c in commands]
    runnable = [(c, t) for c, t in runnable if t]
    assert len(runnable) >= 5, f"too few runnable commands in the section: {commands}"
    seen_verbs = set()
    for command, argv in runnable:
        assert argv[0] == "graph", command
        try:
            parser().parse_args(argv)
        except SystemExit as exc:
            pytest.fail(f"does not parse ({exc.code}): {command}")
        seen_verbs.add(argv[1])
    assert {"validate", "start", "status"} <= seen_verbs, seen_verbs


def test_every_tool_name_the_section_mentions_is_a_registered_tool():
    from mcp.server import MCPServer

    from subagent.graph.mcp_tools import register

    registered = set(register(MCPServer("readme-check")))
    assert registered == TOOL_NAMES
    text = section_text()
    mentioned = set(re.findall(r"`(graph_[a-z]+)`", text)) - FIELD_NAMES_NOT_TOOLS
    assert mentioned, "the section must name the MCP tools"
    assert mentioned <= registered, mentioned - registered
    assert len(mentioned) >= 3


def test_section_mentions_the_cli_verbs_it_documents():
    text = section_text()
    for verb in ("validate", "start", "status", "answer"):
        assert f"subagent graph {verb}" in text, verb
