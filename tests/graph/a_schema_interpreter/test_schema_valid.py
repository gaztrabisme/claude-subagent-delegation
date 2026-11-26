"""Spec 2.1-2.6: the worked example, hashing, loading and the node model."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys

import pytest

from subagent.graph import SCHEMA_ID, Graph, GraphError, Problem, graph_hash, load_graph, validate
from subagent.graph.schema import parse

from ..conftest import minimal_graph, worked_example


def test_worked_example_has_zero_problems():
    assert validate(worked_example()) == []


def test_worked_example_loads_as_graph_model():
    graph = load_graph(worked_example())
    assert isinstance(graph, Graph)
    assert graph.id == "feature-x"
    assert graph.version == 1
    assert graph.budget_usd == 6.0
    assert graph.workspace == "worktrees/feature-x"
    assert dict(graph.inputs) == {"ticket": "Add the export command"}
    assert dict(graph.defaults)["provider"] == "codex"
    assert dict(graph.output) == {"verdict": "nodes.review.output.verdict",
                                  "decision": "nodes.merge_ok.output.answer"}
    assert [n.id for n in graph.body] == ["plan", "builds", "both_built", "review_loop",
                                          "merge_ok"]
    assert isinstance(graph.body, tuple)
    assert graph.doc["id"] == "feature-x"


def test_node_classes_and_defaults_applied():
    graph = load_graph(worked_example())
    plan = graph.node("plan")
    assert type(plan).__name__ == "TaskNode"
    assert plan.kind == "task"
    assert plan.provider == "codex"  # from defaults.provider
    assert plan.timeout_s == 2400  # defaults.timeout_s
    assert plan.reserve_usd == 0.8  # defaults.reserve_usd
    assert plan.retries == 1
    assert list(plan.retry_on) == ["interrupted", "lane_refused"]
    assert plan.retry_backoff_s == 30
    assert plan.priority == 3
    assert plan.on_failure == "fail"
    assert plan.role == "planner"
    review = graph.node("review")
    assert review.provider == "gemini"
    assert review.reserve_usd == 0.3
    assert type(graph.node("both_built")).__name__ == "GateNode"
    assert type(graph.node("builds")).__name__ == "ParallelNode"
    assert graph.node("builds").join == "allSettled"
    assert type(graph.node("review_loop")).__name__ == "LoopNode"
    assert type(graph.node("merge_ok")).__name__ == "OwnerDecisionNode"


def test_reserve_default_is_graph_default_when_no_defaults_block():
    graph = load_graph(minimal_graph())
    assert graph.node("t1").reserve_usd == 0.50
    assert graph.node("t1").timeout_s == 1800


def test_nodes_are_frozen():
    node = load_graph(minimal_graph()).node("t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.prompt = "changed"  # type: ignore[misc]


def test_problem_is_frozen_dataclass_with_three_fields():
    problem = Problem(path="$", code="x", message="m")
    assert (problem.path, problem.code, problem.message) == ("$", "x", "m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        problem.code = "y"  # type: ignore[misc]


def test_schema_id_constant():
    assert SCHEMA_ID == "subagent.graph/1"


def test_graph_hash_is_sha256_of_canonical_json():
    document = worked_example()
    expected = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert graph_hash(document) == expected
    assert len(graph_hash(document)) == 64


def test_graph_hash_stable_under_key_reorder_and_changes_with_content():
    document = worked_example()
    reordered = json.loads(json.dumps(document))
    reordered = dict(reversed(list(reordered.items())))
    reordered["body"][0] = dict(reversed(list(reordered["body"][0].items())))
    assert graph_hash(reordered) == graph_hash(document)
    changed = worked_example()
    changed["version"] = 2
    assert graph_hash(changed) != graph_hash(document)


def test_load_graph_from_json_text_bytes_and_path(tmp_path):
    document = minimal_graph()
    text = json.dumps(document)
    assert load_graph(text).id == "g1"
    assert load_graph(text.encode()).id == "g1"
    path = tmp_path / "g.json"
    path.write_text(text)
    assert load_graph(path).id == "g1"
    assert load_graph(str(path)).id == "g1"  # a str naming an existing file reads it


def test_parse_returns_plain_dict():
    document = minimal_graph()
    assert parse(json.dumps(document)) == document
    assert parse(document) == document


def test_yaml_accepted_when_pyyaml_importable():
    pytest.importorskip("yaml")
    text = ("schema: subagent.graph/1\nid: g1\nversion: 1\nbody:\n"
            "  - {id: t1, kind: task, provider: codex, prompt: hi, verification: 'true'}\n")
    assert load_graph(text).id == "g1"


def test_validate_returns_all_problems_not_just_first():
    document = minimal_graph()
    document["bogus"] = 1
    document["version"] = "one"
    document["id"] = "Bad Id"
    problems = validate(document)
    codes = {p.code for p in problems}
    assert {"unknown_key", "type", "bad_id"} <= codes


def test_load_graph_raises_graph_error_with_one_line_per_problem():
    document = minimal_graph()
    document["bogus"] = 1
    document["id"] = "Bad Id"
    problems = validate(document)
    assert len(problems) >= 2
    with pytest.raises(GraphError) as info:
        load_graph(document)
    err = info.value
    assert [(p.path, p.code, p.message) for p in err.problems] == [
        (p.path, p.code, p.message) for p in problems]
    lines = str(err).split("\n")
    assert lines == [f"{p.path}: {p.message} [{p.code}]" for p in err.problems]


def test_unknown_keys_are_errors_in_nodes_too():
    document = minimal_graph(surprise=True)
    problems = validate(document)
    assert any(p.code == "unknown_key" and "surprise" in p.message for p in problems)


def test_loop_and_for_each_ids_unique_across_branches():
    document = worked_example()
    document["body"].append({"id": "build_a", "kind": "task", "provider": "codex",
                             "prompt": "x", "verification": "true"})
    assert any(p.code == "duplicate_id" for p in validate(document))


def test_nodes_in_separate_named_graph_namespaces_may_reuse_ids():
    document = minimal_graph()
    document["graphs"] = {"a": {"body": [{"id": "t1", "kind": "task", "provider": "codex",
                                          "prompt": "x", "verification": "true"}]}}
    document["body"].append({"id": "s", "kind": "subgraph", "graph": "a"})
    assert validate(document) == []


def test_no_temporalio_needed_to_validate(monkeypatch):
    monkeypatch.setitem(sys.modules, "temporalio", None)
    assert validate(worked_example()) == []
