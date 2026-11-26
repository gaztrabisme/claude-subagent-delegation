"""Spec 3.4: ids, scopes and request ids are pure functions with exact formats."""

from __future__ import annotations

import hashlib
import re

from subagent.graph import ids


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_new_root_run_id_from_request_id_is_deterministic():
    assert ids.new_root_run_id("abc") == "gr-" + sha("graph-start:abc")[:12]
    assert ids.new_root_run_id("abc") == ids.new_root_run_id("abc")
    assert ids.new_root_run_id("abc") != ids.new_root_run_id("abd")


def test_new_root_run_id_random_when_no_request_id():
    first, second = ids.new_root_run_id(), ids.new_root_run_id(None)
    assert re.fullmatch(r"gr-[0-9a-f]{12}", first)
    assert re.fullmatch(r"gr-[0-9a-f]{12}", second)
    assert first != second


def test_node_scope_plain_and_enclosing():
    assert ids.node_scope("plan") == "plan"
    assert ids.node_scope("fix", [("review_loop", 1)]) == "review_loop[1]/fix"
    assert ids.node_scope("x", [("a", 0), ("b", 2)]) == "a[0]/b[2]/x"
    assert ids.node_scope("x", (("a", 0),)) == "a[0]/x"


def test_node_scope_retry_suffix():
    assert ids.node_scope("build", [("builds", 3)], retry=1) == "builds[3]/build~r1"
    assert ids.node_scope("plan", retry=2) == "plan~r2"
    assert ids.node_scope("plan", retry=0) == "plan"


def test_child_run_path():
    assert ids.child_run_path("gr-0123456789ab", "sub") == "gr-0123456789ab/sub"
    assert ids.child_run_path("gr-1/a", "loop[1]/sub") == "gr-1/a/loop[1]/sub"


def test_task_request_id_short():
    assert ids.task_request_id("gr-0123456789ab", "plan") == "gr-0123456789ab/plan"
    assert ids.task_request_id("gr-1/sub", "builds[3]/build~r1") == "gr-1/sub/builds[3]/build~r1"


def test_task_request_id_exactly_200_is_not_truncated():
    run_path = "gr-0123456789ab"
    scope = "s" * (200 - len(run_path) - 1)
    out = ids.task_request_id(run_path, scope)
    assert len(out) == 200
    assert out == f"{run_path}/{scope}"


def test_task_request_id_over_200_is_truncated_with_hash():
    run_path = "gr-0123456789ab"
    scope = "s" * 300
    full = f"{run_path}/{scope}"
    out = ids.task_request_id(run_path, scope)
    assert out == full[:180] + "~" + sha(full)[:16]
    assert len(out) == 197


def test_truncated_ids_stay_distinct_and_deterministic():
    run_path = "gr-0123456789ab"
    a = ids.task_request_id(run_path, "x" * 250 + "a")
    b = ids.task_request_id(run_path, "x" * 250 + "b")
    assert a != b
    assert a == ids.task_request_id(run_path, "x" * 250 + "a")


def test_attempt_request_id():
    assert ids.attempt_request_id("gr-1/plan", 1) == "gr-1/plan#a1"
    assert ids.attempt_request_id("gr-1/plan", 12) == "gr-1/plan#a12"


def test_exception_id_format():
    key = "gr-1/plan:node_failed"
    assert ids.exception_id(key) == "gx-" + sha(key)[:12]
    assert re.fullmatch(r"gx-[0-9a-f]{12}", ids.exception_id("anything"))
    assert ids.exception_id("a") != ids.exception_id("b")


def test_same_position_same_id_new_iteration_or_retry_new_id():
    run = "gr-0123456789ab"
    base = ids.task_request_id(run, ids.node_scope("fix", [("lp", 0)]))
    assert base == ids.task_request_id(run, ids.node_scope("fix", [("lp", 0)]))
    assert base != ids.task_request_id(run, ids.node_scope("fix", [("lp", 1)]))
    assert base != ids.task_request_id(run, ids.node_scope("fix", [("lp", 0)], retry=1))
