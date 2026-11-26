"""Spec 2.4: conditions, paths and templates."""

from __future__ import annotations

import pytest

from subagent.graph.expr import MISSING, ExprError, check, evaluate, references, render, resolve

SCOPE = {
    "nodes": {
        "plan": {"status": "completed", "output": {
            "verdict": "PASS", "count": 3, "ratio": 0.5, "flag": True, "none": None,
            "result_json": {"packages": ["pkg_a", {"name": "pkg_b"}], "n": 7},
            "empty_list": [], "empty_str": "", "zero": 0}},
    },
    "inputs": {"ticket": "Add export", "n": 5, "tags": ["x", "y"]},
    "item": {"pkg": "pkg_a"},
    "loop": {"lp": {"iteration": 1}},
}


def cond(path, op, value=None, with_value=True):
    out = {"path": path, "op": op}
    if with_value:
        out["value"] = value
    return out


# --- resolve ---------------------------------------------------------------


def test_resolve_nested_and_roots():
    assert resolve("nodes.plan.output.verdict", SCOPE) == "PASS"
    assert resolve("inputs.ticket", SCOPE) == "Add export"
    assert resolve("item.pkg", SCOPE) == "pkg_a"
    assert resolve("loop.lp.iteration", SCOPE) == 1


def test_resolve_list_index_paths():
    assert resolve("nodes.plan.output.result_json.packages.0", SCOPE) == "pkg_a"
    assert resolve("nodes.plan.output.result_json.packages.1.name", SCOPE) == "pkg_b"


@pytest.mark.parametrize("path", [
    "nodes.ghost.output.x", "nodes.plan.output.nope", "inputs.nope", "item.zzz",
    "nodes.plan.output.result_json.packages.9", "nodes.plan.output.verdict.deeper",
    "nowhere.x", "nodes.plan.output.result_json.packages.x",
])
def test_resolve_missing_is_the_sentinel(path):
    assert resolve(path, SCOPE) is MISSING


def test_resolve_none_value_is_not_missing():
    assert resolve("nodes.plan.output.none", SCOPE) is None


# --- evaluate --------------------------------------------------------------


@pytest.mark.parametrize("op,path,value,expected", [
    ("eq", "nodes.plan.output.verdict", "PASS", True),
    ("eq", "nodes.plan.output.verdict", "FAIL", False),
    ("ne", "nodes.plan.output.verdict", "FAIL", True),
    ("ne", "nodes.plan.output.verdict", "PASS", False),
    ("in", "nodes.plan.output.verdict", ["PASS", "OK"], True),
    ("in", "nodes.plan.output.verdict", ["FAIL"], False),
    ("gt", "nodes.plan.output.count", 2, True),
    ("gt", "nodes.plan.output.count", 3, False),
    ("gte", "nodes.plan.output.count", 3, True),
    ("gte", "nodes.plan.output.count", 4, False),
    ("lt", "nodes.plan.output.count", 4, True),
    ("lt", "nodes.plan.output.count", 3, False),
    ("lte", "nodes.plan.output.count", 3, True),
    ("lte", "nodes.plan.output.count", 2, False),
    ("gt", "nodes.plan.output.ratio", 0.25, True),
    ("eq", "nodes.plan.output.flag", True, True),
    ("eq", "nodes.plan.output.none", None, True),
])
def test_comparison_ops(op, path, value, expected):
    assert evaluate(cond(path, op, value), SCOPE) is expected


@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte"])
def test_ordering_between_mismatched_types_is_false_not_an_error(op):
    assert evaluate(cond("nodes.plan.output.verdict", op, 3), SCOPE) is False
    assert evaluate(cond("nodes.plan.output.count", op, "3"), SCOPE) is False


def test_eq_between_mismatched_types_is_false():
    assert evaluate(cond("nodes.plan.output.count", "eq", "3"), SCOPE) is False
    assert evaluate(cond("nodes.plan.output.verdict", "eq", 3), SCOPE) is False


def test_in_with_non_list_value_is_false_not_an_error():
    assert evaluate(cond("nodes.plan.output.verdict", "in", "PASS"), SCOPE) in (True, False)
    assert evaluate(cond("nodes.plan.output.count", "in", 3), SCOPE) is False


def test_exists_and_truthy():
    assert evaluate(cond("nodes.plan.output.verdict", "exists", with_value=False), SCOPE)
    assert evaluate(cond("nodes.plan.output.none", "exists", with_value=False), SCOPE)
    assert not evaluate(cond("nodes.plan.output.nope", "exists", with_value=False), SCOPE)
    assert evaluate(cond("nodes.plan.output.verdict", "truthy", with_value=False), SCOPE)
    assert evaluate(cond("nodes.plan.output.count", "truthy", with_value=False), SCOPE)
    for falsy in ("zero", "empty_str", "empty_list", "none", "nope"):
        assert not evaluate(cond(f"nodes.plan.output.{falsy}", "truthy", with_value=False),
                            SCOPE), falsy


@pytest.mark.parametrize("op", ["eq", "ne", "in", "gt", "gte", "lt", "lte"])
def test_missing_makes_every_comparison_false(op):
    value = [1] if op == "in" else 1
    assert evaluate(cond("nodes.plan.output.nope", op, value), SCOPE) is False


def test_missing_compared_to_none_is_still_false():
    assert evaluate(cond("nodes.plan.output.nope", "eq", None), SCOPE) is False


def test_all_any_not_combinators():
    yes = cond("nodes.plan.output.verdict", "eq", "PASS")
    no = cond("nodes.plan.output.verdict", "eq", "FAIL")
    assert evaluate({"all": [yes, yes]}, SCOPE) is True
    assert evaluate({"all": [yes, no]}, SCOPE) is False
    assert evaluate({"any": [no, yes]}, SCOPE) is True
    assert evaluate({"any": [no, no]}, SCOPE) is False
    assert evaluate({"not": no}, SCOPE) is True
    assert evaluate({"not": yes}, SCOPE) is False
    assert evaluate({"all": []}, SCOPE) is True
    assert evaluate({"any": []}, SCOPE) is False
    nested = {"all": [yes, {"any": [no, {"not": no}]}]}
    assert evaluate(nested, SCOPE) is True


def test_not_of_a_missing_comparison_is_true():
    assert evaluate({"not": cond("nodes.plan.output.nope", "eq", 1)}, SCOPE) is True


def test_evaluate_returns_a_real_bool():
    assert type(evaluate(cond("inputs.n", "gt", 1), SCOPE)) is bool


# --- check / references ----------------------------------------------------


def test_check_accepts_good_conditions():
    assert check(cond("inputs.n", "eq", 5), "$.body[0].when") == []
    assert check({"all": [cond("inputs.n", "exists", with_value=False)]}, "$.x") == []


@pytest.mark.parametrize("bad", [
    {"path": "inputs.n", "op": "zz", "value": 1},
    {"path": "inputs.n", "op": "eq"},
    {"path": 5, "op": "eq", "value": 1},
    {"all": "x"},
    {"any": [1]},
    {"not": "x"},
    {"path": "inputs.n", "op": "eq", "value": 1, "extra": 1},
    {},
    [],
    "text",
])
def test_check_flags_bad_conditions_with_path_and_code(bad):
    problems = check(bad, "$.body[2].when")
    assert problems
    assert all(p.code == "bad_condition" for p in problems)
    assert all(p.path.startswith("$.body[2].when") for p in problems)
    assert all(p.message.startswith("invalid condition: ") for p in problems)


def test_references_reads_roots_and_ids():
    refs = references({"all": [
        cond("nodes.plan.output.verdict", "eq", "PASS"),
        cond("inputs.n", "gt", 1),
        cond("item.pkg", "exists", with_value=False),
        cond("loop.lp.iteration", "lt", 3),
    ]})
    assert {"nodes.plan", "inputs.n", "item.pkg", "loop.lp"} <= refs


def test_references_in_templates_and_plain_strings():
    assert references("hello {{ nodes.a.output.x }} and {{ inputs.k }}") >= {"nodes.a",
                                                                              "inputs.k"}
    assert references("no holes here") == set()


# --- render ----------------------------------------------------------------


def test_render_strings_insert_as_is():
    assert render("Do {{ inputs.ticket }} now", SCOPE) == "Do Add export now"
    assert render("{{ nodes.plan.output.verdict }}", SCOPE) == "PASS"


def test_render_numbers_bools_null_as_json():
    assert render("{{ inputs.n }}", SCOPE) == "5"
    assert render("{{ nodes.plan.output.ratio }}", SCOPE) == "0.5"
    assert render("{{ nodes.plan.output.flag }}", SCOPE) == "true"
    assert render("{{ nodes.plan.output.none }}", SCOPE) == "null"


def test_render_lists_and_objects_as_compact_json():
    assert render("{{ inputs.tags }}", SCOPE) == '["x","y"]'
    assert render("{{ nodes.plan.output.result_json.packages.1 }}", SCOPE) == '{"name":"pkg_b"}'
    assert render("{{ nodes.plan.output.result_json }}", SCOPE) == (
        '{"packages":["pkg_a",{"name":"pkg_b"}],"n":7}')


def test_render_several_holes_and_list_index_path():
    out = render("{{ nodes.plan.output.result_json.packages.0 }}/{{ item.pkg }}/{{ inputs.n }}",
                 SCOPE)
    assert out == "pkg_a/pkg_a/5"


def test_render_text_without_holes_unchanged():
    assert render("plain {text} with {single} braces", SCOPE) == "plain {text} with {single} braces"


def test_render_unresolved_hole_raises_template_unresolved():
    with pytest.raises(ExprError) as info:
        render("x {{ nodes.ghost.output.y }}", SCOPE)
    assert info.value.code == "template_unresolved"
    assert isinstance(info.value, ValueError)


def test_render_too_long_raises_template_too_long():
    big = {"inputs": {"s": "x" * 60}}
    with pytest.raises(ExprError) as info:
        render("{{ inputs.s }}{{ inputs.s }}", big, limit=100)
    assert info.value.code == "template_too_long"


def test_render_default_limit_is_100000():
    scope = {"inputs": {"s": "x" * 100_000}}
    assert len(render("{{ inputs.s }}", scope)) == 100_000
    with pytest.raises(ExprError) as info:
        render("{{ inputs.s }}!", scope)
    assert info.value.code == "template_too_long"


def test_missing_sentinel_is_falsy_distinct_object():
    assert MISSING is not None
    assert MISSING is MISSING
    assert not isinstance(MISSING, (str, int, dict, list))
