"""route(): the outcome -> exception table (SPEC 6.1), row by row."""

from __future__ import annotations

import pytest


def _node(on_failure: str = "fail"):
    from subagent.graph.schema import load_graph

    graph = load_graph({
        "schema": "subagent.graph/1", "id": "g", "version": 1,
        "body": [{"id": "t", "kind": "task", "provider": "glm", "prompt": "p",
                  "verification": "true", "on_failure": on_failure}],
    })
    return graph.node("t")


def _scope():
    from subagent.graph.interpreter import Scope

    return Scope(run_path="gr-1", node_id="t", scope="t", request_id="gr-1/t", priority=3)


def _route(outcome, node=None):
    from subagent.graph.exceptions import route

    return route(outcome, node or _node(), _scope())


def _outcome(status: str, **kw):
    from subagent.graph.dispatch import TaskOutcome

    return TaskOutcome(status=status, **kw)


def _by_kind(records):
    return {r["kind"]: r for r in records}


@pytest.mark.parametrize("status", ["failed", "unverified", "interrupted"])
def test_failed_node_with_on_failure_exception_raises_node_failed(status):
    records = _route(_outcome(status), _node("exception"))
    rec = _by_kind(records)["node_failed"]
    assert rec["needs_answer"] is True
    assert list(rec["options"]) == ["retry", "skip", "abort"]


@pytest.mark.parametrize("on_failure", ["fail", "continue"])
def test_failed_node_without_on_failure_exception_raises_nothing(on_failure):
    assert _route(_outcome("failed"), _node(on_failure)) == []


def test_completed_task_raises_nothing():
    assert _route(_outcome("completed"), _node("exception")) == []


def test_completed_task_with_supervisor_verdicts_creates_no_record():
    """Allowed, or denied by policy with the task still completing: settled."""
    denials = [{"tool": "Bash", "action": "deny", "reason": "policy", "tier": "agent"}]
    assert _route(_outcome("completed", guard_denials=denials)) == []
    assert _route(_outcome("completed", guard_denials=denials), _node("exception")) == []


def test_unfinished_task_with_guard_denials_raises_guard_denied_information():
    denials = [{"tool": "Bash", "action": "deny", "reason": "curl to unknown host"}]
    records = _route(_outcome("failed", guard_denials=denials))
    rec = _by_kind(records)["guard_denied"]
    assert rec["needs_answer"] is False
    assert not rec["options"]
    # on_failure "fail" adds no node_failed record
    assert set(_by_kind(records)) == {"guard_denied"}


def test_failed_with_guard_denials_and_exception_policy_raises_both():
    denials = [{"tool": "Write", "action": "deny", "reason": "protected path"}]
    records = _route(_outcome("failed", guard_denials=denials), _node("exception"))
    assert {r["kind"] for r in records} == {"node_failed", "guard_denied"}


def test_test_change_request_needs_an_answer_with_reject_or_accept():
    outcome = _outcome("unverified", test_change_request="loosen the timeout in test_x")
    rec = _by_kind(_route(outcome))["test_change_request"]
    assert rec["needs_answer"] is True
    assert list(rec["options"]) == ["reject", "accept"]
    assert set(_by_kind(_route(outcome))) == {"test_change_request"}


def test_test_change_request_is_routed_even_for_a_failed_node_on_exception_policy():
    outcome = _outcome("failed", test_change_request="the test is wrong")
    kinds = {r["kind"] for r in _route(outcome, _node("exception"))}
    assert kinds == {"node_failed", "test_change_request"}


@pytest.mark.parametrize("refusal,kind", [("budget_exhausted", "budget_exhausted"),
                                          ("lane_closed_timeout", "lane_closed")])
def test_admission_refusals_raise_informational_records(refusal, kind):
    """The refusal reaches route() as an outcome; SPEC 6.1 does not say whether
    it is the status or the error, so either carrier must produce the record."""
    candidates = [_outcome(refusal), _outcome("failed", error=refusal)]
    results = [_by_kind(_route(c)) for c in candidates]
    hits = [r[kind] for r in results if kind in r]
    assert hits, f"neither carrier of {refusal!r} produced a {kind!r} record"
    assert all(h["needs_answer"] is False for h in hits)
    assert all(not h["options"] for h in hits)
