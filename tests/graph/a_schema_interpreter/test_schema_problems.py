"""Spec 2.5: one test per problem code, with its exact message and path.

Where the spec leaves the path open (a missing key can be reported at its
parent or at the key), the test accepts both spellings and pins the message.
"""

from __future__ import annotations

import copy
import json
import re
import sys

import pytest

from subagent.graph import GraphError, load_graph, validate
from subagent.graph.schema import parse

from ..conftest import minimal_graph

TASK = {"id": "t1", "kind": "task", "provider": "codex", "prompt": "p", "verification": "true"}


def doc(body=None, **top):
    out = {"schema": "subagent.graph/1", "id": "g1", "version": 1,
           "body": copy.deepcopy(body if body is not None else [TASK])}
    out.update(top)
    return out


def one(problems, code):
    found = [p for p in problems if p.code == code]
    assert found, f"no {code} in {[(p.code, p.message) for p in problems]}"
    return found[0]


def check(problem, message, *paths):
    assert problem.message == message
    assert problem.path in paths, (problem.path, paths)


def test_parse_error_message_has_detail_line_and_column(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(GraphError) as info:
        parse('{"a": }')
    problem = one(info.value.problems, "parse_error")
    check(problem, "not valid JSON: Expecting value (line 1, column 7)", "$")


def test_parse_error_second_line_column(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(GraphError) as info:
        load_graph('{\n  "schema": "x",\n  "id" 1\n}')
    problem = one(info.value.problems, "parse_error")
    assert re.fullmatch(r"not valid JSON: .+ \(line 3, column \d+\)", problem.message)


def test_yaml_unavailable_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(GraphError) as info:
        parse("schema: subagent.graph/1\nid: g1\n")
    problem = one(info.value.problems, "yaml_unavailable")
    check(problem, "YAML needs PyYAML; send JSON instead", "$")


def test_schema_missing():
    document = doc()
    del document["schema"]
    check(one(validate(document), "schema_missing"),
          'missing "schema"; expected "subagent.graph/1"', "$", "$.schema")


def test_schema_unsupported():
    document = doc()
    document["schema"] = "subagent.graph/2"
    check(one(validate(document), "schema_unsupported"),
          "unsupported schema 'subagent.graph/2'; this build reads \"subagent.graph/1\"",
          "$.schema", "$")


@pytest.mark.parametrize("key,where", [("id", "$"), ("version", "$"), ("body", "$")])
def test_required_top_level_key(key, where):
    document = doc()
    del document[key]
    check(one(validate(document), "required"), f"missing required key {key!r}",
          where, f"{where}.{key}")


def test_required_node_key_prompt():
    document = doc([{k: v for k, v in TASK.items() if k != "prompt"}])
    check(one(validate(document), "required"), "missing required key 'prompt'",
          "$.body[0]", "$.body[0].prompt")


def test_required_node_key_verification():
    document = doc([{k: v for k, v in TASK.items() if k != "verification"}])
    check(one(validate(document), "required"), "missing required key 'verification'",
          "$.body[0]", "$.body[0].verification")


def test_required_loop_keys():
    loop = {"id": "lp", "kind": "loop", "body": [TASK]}
    messages = {p.message for p in validate(doc([loop])) if p.code == "required"}
    assert "missing required key 'max_iterations'" in messages
    assert "missing required key 'until'" in messages


def test_unknown_key_top_level():
    document = doc()
    document["bogus"] = 1
    check(one(validate(document), "unknown_key"), "unknown key 'bogus'", "$", "$.bogus")


def test_unknown_key_in_node():
    document = doc([dict(TASK, bogus=1)])
    check(one(validate(document), "unknown_key"), "unknown key 'bogus'",
          "$.body[0]", "$.body[0].bogus")


def test_type_problem_version_string():
    document = doc()
    document["version"] = "1"
    problem = one(validate(document), "type")
    assert problem.path == "$.version"
    assert re.fullmatch(r"expected .+, got .+", problem.message)
    assert "str" in problem.message or "string" in problem.message


def test_type_problem_body_not_list():
    document = doc()
    document["body"] = {"id": "t1"}
    problem = one(validate(document), "type")
    assert problem.path == "$.body"
    assert problem.message.startswith("expected ")


@pytest.mark.parametrize("patch,key,lo,hi,value", [
    ({"timeout_s": 10}, "timeout_s", 60, 14400, 10),
    ({"retries": 5}, "retries", 0, 3, 5),
    ({"priority": 9}, "priority", 1, 5, 9),
    ({"max_steps": 501}, "max_steps", 1, 500, 501),
    ({"retry_backoff_s": 4000}, "retry_backoff_s", 0, 3600, 4000),
])
def test_range_on_task_keys(patch, key, lo, hi, value):
    problem = one(validate(doc([dict(TASK, **patch)])), "range")
    check(problem, f"{key} must be between {lo} and {hi}, got {value}", f"$.body[0].{key}")


def test_range_on_loop_max_iterations():
    loop = {"id": "lp", "kind": "loop", "max_iterations": 11, "body": [TASK],
            "until": {"path": "nodes.t1.status", "op": "eq", "value": "completed"}}
    problem = one(validate(doc([loop])), "range")
    check(problem, "max_iterations must be between 1 and 10, got 11",
          "$.body[0].max_iterations")


@pytest.mark.parametrize("value", ["Bad ID", "1abc", "a" * 49, "has/slash", "UP"])
def test_bad_id_graph_and_node(value):
    document = doc()
    document["id"] = value
    problem = one(validate(document), "bad_id")
    check(problem,
          f"invalid id {value!r}: use lowercase letters, digits, - and _, starting with a letter",
          "$.id")
    node_doc = doc([dict(TASK, id=value)])
    node_problem = one(validate(node_doc), "bad_id")
    assert node_problem.path == "$.body[0].id"
    assert node_problem.message == problem.message


def test_good_ids_accepted():
    for value in ("a", "feature-x", "a_b-9", "a" * 48):
        assert not [p for p in validate(doc([dict(TASK, id=value)])) if p.code == "bad_id"]


def test_duplicate_id():
    document = doc([TASK, dict(TASK)])
    problem = one(validate(document), "duplicate_id")
    check(problem, "duplicate node id 't1' (first at $.body[0])", "$.body[1]", "$.body[1].id")


def test_duplicate_id_across_parallel_branch_and_body():
    parallel = {"id": "pp", "kind": "parallel", "branches": [dict(TASK, id="x")]}
    problem = one(validate(doc([parallel, dict(TASK, id="x")])), "duplicate_id")
    assert problem.message.startswith("duplicate node id 'x' (first at $.body[0].branches[0]")


def test_unknown_kind():
    problem = one(validate(doc([{"id": "z", "kind": "bogus"}])), "unknown_kind")
    check(problem,
          "unknown node kind 'bogus'; expected one of task, gate, parallel, subgraph, "
          "owner_decision, loop", "$.body[0].kind", "$.body[0]")


@pytest.mark.parametrize("node,a,b", [
    ({"id": "g", "kind": "gate", "command": "true",
      "condition": {"path": "inputs.x", "op": "exists"}}, "command", "condition"),
    ({"id": "g", "kind": "gate"}, "command", "condition"),
    ({"id": "p", "kind": "parallel", "branches": [TASK],
      "for_each": {"items": "inputs.x", "as": "i", "node": dict(TASK, id="tt")}},
     "branches", "for_each"),
    ({"id": "s", "kind": "subgraph", "graph": "a", "body": [TASK]}, "graph", "body"),
    ({"id": "s", "kind": "subgraph"}, "graph", "body"),
])
def test_exactly_one(node, a, b):
    document = doc([node], graphs={"a": {"body": [dict(TASK, id="inner")]}},
                   inputs={"x": [1]})
    problem = one(validate(document), "exactly_one")
    check(problem, f"give exactly one of {a!r} or {b!r}", "$.body[0]")


def test_join_policy():
    parallel = {"id": "pp", "kind": "parallel", "join": "sometimes", "branches": [TASK]}
    problem = one(validate(doc([parallel])), "join_policy")
    check(problem, "unknown join 'sometimes'; expected allSettled, failFast or any",
          "$.body[0].join")


@pytest.mark.parametrize("cond", [
    {"path": "inputs.x", "op": "zz", "value": 1},
    {"path": "inputs.x", "op": "eq"},
    {"all": "no"},
    {"not": []},
    {"bogus": 1},
    "inputs.x",
])
def test_bad_condition(cond):
    gate = {"id": "g", "kind": "gate", "condition": cond}
    problem = one(validate(doc([gate], inputs={"x": 1})), "bad_condition")
    assert problem.message.startswith("invalid condition: ")
    assert problem.path.startswith("$.body[0].condition")


def test_good_conditions_have_no_problem():
    gate = {"id": "g", "kind": "gate", "condition": {"all": [
        {"path": "inputs.x", "op": "eq", "value": 1},
        {"any": [{"path": "inputs.x", "op": "exists"}, {"not": {"path": "inputs.x",
                                                                 "op": "truthy"}}]},
        {"path": "inputs.x", "op": "in", "value": [1, 2]},
    ]}}
    assert validate(doc([gate], inputs={"x": 1})) == []


def test_unknown_reference_later_node():
    body = [dict(TASK, id="a", prompt="{{ nodes.b.output.verdict }}"), dict(TASK, id="b")]
    problem = one(validate(doc(body)), "unknown_reference")
    check(problem, "'nodes.b.output.verdict' refers to node 'b', which does not run before "
          "this node", "$.body[0].prompt")


def test_unknown_reference_in_when_and_gate_condition():
    body = [
        dict(TASK, id="a", when={"path": "nodes.zzz.status", "op": "eq", "value": "completed"}),
        {"id": "g", "kind": "gate", "condition": {"path": "nodes.yyy.status", "op": "exists"}},
    ]
    messages = [p.message for p in validate(doc(body)) if p.code == "unknown_reference"]
    assert any("'zzz'" in m for m in messages)
    assert any("'yyy'" in m for m in messages)


def test_unknown_input():
    body = [dict(TASK, prompt="{{ inputs.nope }}")]
    problem = one(validate(doc(body, inputs={"yes": 1})), "unknown_input")
    check(problem, "'inputs.nope' refers to input 'nope', which the graph does not declare",
          "$.body[0].prompt")


def test_subgraph_unknown():
    problem = one(validate(doc([{"id": "s", "kind": "subgraph", "graph": "missing"}])),
                  "subgraph_unknown")
    check(problem, "subgraph 'missing' is not defined in \"graphs\"", "$.body[0].graph",
          "$.body[0]")


def _named(body):
    return {"body": body}


def test_subgraph_cycle_two_graphs():
    graphs = {
        "a": _named([{"id": "sb", "kind": "subgraph", "graph": "b"}]),
        "b": _named([{"id": "sa", "kind": "subgraph", "graph": "a"}]),
    }
    document = doc([{"id": "s", "kind": "subgraph", "graph": "a"}], graphs=graphs)
    problem = one(validate(document), "subgraph_cycle")
    match = re.fullmatch(r"subgraph cycle: (\w+) -> (\w+) -> (\w+)", problem.message)
    assert match, problem.message
    assert match.group(1) == match.group(3)
    assert {match.group(1), match.group(2)} == {"a", "b"}


def test_subgraph_cycle_self():
    graphs = {"a": _named([{"id": "sa", "kind": "subgraph", "graph": "a"}])}
    document = doc([{"id": "s", "kind": "subgraph", "graph": "a"}], graphs=graphs)
    problem = one(validate(document), "subgraph_cycle")
    assert problem.message == "subgraph cycle: a -> a"


def _chain(n):
    graphs = {}
    for i in range(1, n + 1):
        inner = ([{"id": f"s{i + 1}", "kind": "subgraph", "graph": f"g{i + 1}"}]
                 if i < n else [dict(TASK, id="leaf")])
        graphs[f"g{i}"] = _named(inner)
    return doc([{"id": "s1", "kind": "subgraph", "graph": "g1"}], graphs=graphs)


def test_depth_limit_default_three():
    assert not [p for p in validate(_chain(3)) if p.code == "depth_limit"]
    problem = one(validate(_chain(4)), "depth_limit")
    check(problem, "subgraph nesting 4 exceeds the limit of 3", "$.body[0]", "$.body[0].graph")


def test_depth_limit_parameter():
    problem = one(validate(_chain(3), max_depth=2), "depth_limit")
    assert problem.message == "subgraph nesting 3 exceeds the limit of 2"
    with pytest.raises(GraphError):
        load_graph(_chain(3), max_depth=2)
    assert load_graph(_chain(3), max_depth=3)


def test_inline_subgraph_counts_toward_depth():
    inline = {"id": "i3", "kind": "subgraph", "body": [dict(TASK, id="leaf")]}
    for level in (2, 1):
        inline = {"id": f"i{level}", "kind": "subgraph", "body": [inline]}
    assert not [p for p in validate(doc([inline])) if p.code == "depth_limit"]
    deeper = {"id": "i0", "kind": "subgraph", "body": [inline]}
    problem = one(validate(doc([deeper])), "depth_limit")
    assert problem.message == "subgraph nesting 4 exceeds the limit of 3"


def test_options_count_and_distinct():
    for options in (["only"], ["a", "a"], [f"o{i}" for i in range(11)], []):
        node = {"id": "d", "kind": "owner_decision", "question": "q?", "options": options}
        problem = one(validate(doc([node])), "options")
        check(problem, "options must be 2 to 10 distinct strings", "$.body[0].options")
    ten = {"id": "d", "kind": "owner_decision", "question": "q?",
           "options": [f"o{i}" for i in range(10)]}
    assert validate(doc([ten])) == []


def test_options_default_not_member():
    node = {"id": "d", "kind": "owner_decision", "question": "q?", "options": ["a", "b"],
            "default": "z"}
    problem = one(validate(doc([node])), "options")
    check(problem, "default 'z' is not one of the options", "$.body[0].default")


def test_too_many_nodes_parameter():
    body = [dict(TASK, id=f"t{i}") for i in range(4)]
    problem = one(validate(doc(body), max_nodes=3), "too_many_nodes")
    check(problem, "graph has 4 nodes; the limit is 3", "$")
    assert not [p for p in validate(doc(body), max_nodes=4) if p.code == "too_many_nodes"]


def test_too_many_nodes_default_500():
    parallel = {"id": "p", "kind": "parallel",
                "branches": [dict(TASK, id=f"b{i}") for i in range(64)]}
    body = [dict(parallel, id=f"p{j}", branches=[dict(TASK, id=f"b{j}_{i}") for i in range(64)])
            for j in range(8)]
    problem = one(validate(doc(body)), "too_many_nodes")
    assert problem.message == "graph has 520 nodes; the limit is 500"


def test_node_count_rules_named_graph_once_for_each_template_once():
    graphs = {"a": _named([dict(TASK, id="inner")])}
    body = [{"id": "s1", "kind": "subgraph", "graph": "a"},
            {"id": "s2", "kind": "subgraph", "graph": "a"}]
    document = doc(body, graphs=graphs)
    assert not [p for p in validate(document, max_nodes=3) if p.code == "too_many_nodes"]
    problem = one(validate(document, max_nodes=2), "too_many_nodes")
    assert problem.message == "graph has 3 nodes; the limit is 2"
    fan = {"id": "fan", "kind": "parallel",
           "for_each": {"items": "inputs.xs", "as": "x", "node": dict(TASK, id="one")}}
    assert not [p for p in validate(doc([fan], inputs={"xs": [1, 2, 3, 4, 5]}), max_nodes=2)
                if p.code == "too_many_nodes"]


def test_body_length_limits():
    problem = [p for p in validate(doc([])) if p.code == "range"]
    assert problem, "an empty body must be a range problem"
    many = [dict(TASK, id=f"t{i}") for i in range(201)]
    assert any(p.code == "range" for p in validate(doc(many), max_nodes=10_000))


def test_problem_messages_never_echo_a_secret_shaped_value_unquoted():
    document = doc()
    document["bogus"] = "sk-abcdefghijklmnop"
    # the unknown key is named, the value is not repeated
    problem = one(validate(document), "unknown_key")
    assert "sk-abcdefghijklmnop" not in problem.message


def test_json_text_with_problems_raises_graph_error():
    text = json.dumps(minimal_graph(retries=9))
    with pytest.raises(GraphError) as info:
        load_graph(text)
    assert any(p.code == "range" for p in info.value.problems)
