"""Spec 2.5 reference rule: a node may read only what is guaranteed to have run first."""

from __future__ import annotations

import copy

from subagent.graph import validate

TASK = {"id": "t1", "kind": "task", "provider": "codex", "prompt": "p", "verification": "true"}


def t(node_id, prompt="p", **extra):
    return dict(TASK, id=node_id, prompt=prompt, **extra)


def doc(body, **top):
    out = {"schema": "subagent.graph/1", "id": "g1", "version": 1, "body": copy.deepcopy(body)}
    out.update(top)
    return out


def codes(document):
    return [p.code for p in validate(document)]


def test_earlier_node_reference_accepted():
    body = [t("a"), t("b", "{{ nodes.a.output.result_text }}")]
    assert validate(doc(body)) == []


def test_later_node_reference_rejected():
    body = [t("a", "{{ nodes.b.output.result_text }}"), t("b")]
    assert codes(doc(body)) == ["unknown_reference"]


def test_self_reference_rejected():
    assert "unknown_reference" in codes(doc([t("a", "{{ nodes.a.output.x }}")]))


def test_reference_to_nonexistent_node_rejected():
    assert "unknown_reference" in codes(doc([t("a", "{{ nodes.ghost.output.x }}")]))


def test_sibling_branch_reference_rejected():
    parallel = {"id": "par", "kind": "parallel", "branches": [
        t("left"), t("right", "{{ nodes.left.output.verdict }}")]}
    assert "unknown_reference" in codes(doc([parallel]))


def test_branch_may_read_nodes_before_the_parallel():
    parallel = {"id": "par", "kind": "parallel", "branches": [
        t("left", "{{ nodes.plan.output.result_json.x }}"),
        t("right", "{{ nodes.plan.output.result_json.y }}")]}
    assert validate(doc([t("plan"), parallel])) == []


def test_node_after_parallel_may_read_the_parallel_node():
    parallel = {"id": "par", "kind": "parallel", "branches": [t("left")]}
    gate = {"id": "g", "kind": "gate",
            "condition": {"path": "nodes.par.output.failed", "op": "eq", "value": 0}}
    assert validate(doc([parallel, gate])) == []


def test_undeclared_input_rejected_declared_accepted():
    assert "unknown_input" in codes(doc([t("a", "{{ inputs.nope }}")]))
    assert validate(doc([t("a", "{{ inputs.yes }}")], inputs={"yes": 1})) == []


def test_input_reference_in_condition_checked_too():
    gate = {"id": "g", "kind": "gate",
            "condition": {"path": "inputs.nope", "op": "exists"}}
    assert "unknown_input" in codes(doc([gate]))


def test_loop_body_may_read_earlier_loop_node_and_outer_earlier_node():
    loop = {"id": "lp", "kind": "loop", "max_iterations": 2,
            "until": {"path": "nodes.second.output.verdict", "op": "eq", "value": "PASS"},
            "body": [t("first", "{{ nodes.outer.output.result_text }}"),
                     t("second", "{{ nodes.first.output.result_text }}")]}
    assert validate(doc([t("outer"), loop])) == []


def test_loop_body_may_not_read_later_loop_node():
    loop = {"id": "lp", "kind": "loop", "max_iterations": 2,
            "until": {"path": "nodes.second.output.verdict", "op": "eq", "value": "PASS"},
            "body": [t("first", "{{ nodes.second.output.result_text }}"), t("second")]}
    assert "unknown_reference" in codes(doc([loop]))


def test_loop_until_may_read_body_nodes():
    loop = {"id": "lp", "kind": "loop", "max_iterations": 2,
            "until": {"path": "nodes.only.output.verdict", "op": "eq", "value": "PASS"},
            "body": [t("only")]}
    assert validate(doc([loop])) == []


def test_loop_iteration_path_is_readable_inside_loop():
    loop = {"id": "lp", "kind": "loop", "max_iterations": 2,
            "until": {"path": "nodes.only.output.verdict", "op": "eq", "value": "PASS"},
            "body": [t("only", "iteration {{ loop.lp.iteration }}")]}
    assert validate(doc([loop])) == []


def test_item_reference_accepted_inside_for_each_template():
    fan = {"id": "fan", "kind": "parallel", "for_each": {
        "items": "nodes.plan.output.result_json.packages", "as": "pkg",
        "node": t("build", "build {{ item.pkg }}",
                  when={"path": "item.pkg", "op": "exists"})}}
    assert validate(doc([t("plan"), fan])) == []


def test_for_each_items_must_read_earlier_node():
    fan = {"id": "fan", "kind": "parallel", "for_each": {
        "items": "nodes.later.output.result_json.packages", "as": "pkg",
        "node": t("build", "build {{ item.pkg }}")}}
    assert "unknown_reference" in codes(doc([fan, t("later")]))


def test_when_may_not_read_later_node():
    body = [t("a", when={"path": "nodes.b.status", "op": "eq", "value": "completed"}), t("b")]
    assert "unknown_reference" in codes(doc(body))


def test_subgraph_input_templates_checked_in_parent_scope():
    graphs = {"sub": {"inputs": {"x": 1}, "body": [t("inner", "{{ inputs.x }}")]}}
    ok = {"id": "s", "kind": "subgraph", "graph": "sub",
          "inputs": {"x": "{{ nodes.a.output.result_text }}"}}
    assert validate(doc([t("a"), ok], graphs=graphs)) == []
    bad = dict(ok, inputs={"x": "{{ nodes.zz.output.result_text }}"})
    assert "unknown_reference" in codes(doc([t("a"), bad], graphs=graphs))


def test_subgraph_body_cannot_read_parent_nodes_but_reads_its_own_inputs():
    graphs = {"sub": {"inputs": {"x": 1}, "body": [t("inner", "{{ nodes.a.output.x }}")]}}
    sub = {"id": "s", "kind": "subgraph", "graph": "sub"}
    assert "unknown_reference" in codes(doc([t("a"), sub], graphs=graphs))
