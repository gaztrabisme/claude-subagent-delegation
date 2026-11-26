"""Spec 3.1: the interpreter's semantics on LocalRuntime + FakeDispatcher (no Temporal)."""

from __future__ import annotations

import json
import time

import pytest
from subagent.graph.dispatch import FakeDispatcher, FakeStep
from subagent.graph.interpreter import STATUSES, GraphInput, GraphResult, NodeResult

from ..conftest import worked_example
from ._helpers import (
    ROOT_ID,
    answer_with,
    doc,
    graph_input,
    make_queue,
    make_runtime,
    node_status,
    request_ids,
    run_graph,
    task,
)


def fd(script=None, default=None, audit_log=None):
    steps = {k: [s if isinstance(s, FakeStep) else FakeStep(**s) for s in v]
             for k, v in (script or {}).items()}
    return FakeDispatcher(steps, default=default or FakeStep(cost_usd=0.1), audit_log=audit_log)


@pytest.fixture
def settings(graph_settings):
    return graph_settings


# --- data classes ----------------------------------------------------------


def test_statuses_constant():
    assert STATUSES == ("completed", "failed", "skipped", "not_run", "cancelled",
                        "budget_exhausted", "interrupted")


def test_graph_input_round_trips_through_dict():
    inp = GraphInput(doc={"a": 1}, inputs={"x": 2}, root_run_id="gr-1", run_path="gr-1/sub",
                     parent_path="gr-1", depth=1, priority=2, pool="p", body_ref="sub",
                     inline_body=[{"id": "t"}])
    data = inp.as_dict()
    assert json.loads(json.dumps(data)) == data
    assert GraphInput.from_dict(data) == inp
    defaults = GraphInput(doc={}, inputs={}, root_run_id="gr-1", run_path="gr-1",
                          parent_path=None, depth=0)
    assert (defaults.priority, defaults.pool, defaults.body_ref, defaults.inline_body) == (
        3, "default", None, None)


def test_node_result_as_dict_has_documented_fields():
    result = NodeResult(id="a", kind="task", status="completed")
    data = result.as_dict()
    for key in ("id", "kind", "status", "attempts", "request_id", "output", "cost_usd",
                "error", "started_at", "finished_at"):
        assert key in data
    assert data["attempts"] == 0 and data["output"] == {}
    json.dumps(data)


# --- sequence, when, templates --------------------------------------------


def test_sequence_runs_in_order_and_assigns_request_ids(settings):
    dispatcher = fd()
    result, _, _ = run_graph(settings, doc([task("a"), task("b"), task("c")]), dispatcher)
    assert isinstance(result, GraphResult)
    assert result.status == "completed"
    assert result.run_path == ROOT_ID
    assert [c.node_id for c in dispatcher.calls] == ["a", "b", "c"]
    assert request_ids(dispatcher) == [f"{ROOT_ID}/a", f"{ROOT_ID}/b", f"{ROOT_ID}/c"]
    assert [c.attempt for c in dispatcher.calls] == [1, 1, 1]
    assert [c.attempt_id for c in dispatcher.calls] == [f"{ROOT_ID}/{n}#a1" for n in "abc"]
    assert all(c.root_run_id == ROOT_ID and c.run_path == ROOT_ID for c in dispatcher.calls)
    for node_id in "abc":
        entry = result.nodes[node_id]
        assert entry["status"] == "completed"
        assert entry["attempts"] == 1
        assert entry["request_id"] == f"{ROOT_ID}/{node_id}"
        assert entry["kind"] == "task"
    assert result.error is None


def test_task_request_fields_come_from_node_and_defaults(settings, tmp_path):
    document = doc(
        [task("a", prompt="hello {{ inputs.who }}", instructions="rules {{ inputs.who }}",
              role="builder", max_steps=12, verification="uv run pytest -q", model="m1")],
        inputs={"who": "world"}, workspace=str(tmp_path),
        defaults={"provider": "glm", "timeout_s": 900})
    document["body"][0].pop("provider")
    dispatcher = fd()
    result, _, _ = run_graph(settings, document, dispatcher, inputs={"who": "ada"})
    assert result.status == "completed"
    call = dispatcher.calls[0]
    assert call.prompt == "hello ada"
    assert call.instructions == "rules ada"
    assert call.provider == "glm"
    assert call.model == "m1"
    assert call.role == "builder"
    assert call.max_steps == 12
    assert call.verification == "uv run pytest -q"
    assert call.timeout_s == 900
    assert str(call.workspace) == str(tmp_path)


def test_node_outputs_feed_later_templates(settings):
    script = {"plan": [FakeStep(result_json={"packages": ["pa", "pb"]}, verdict="PASS")]}
    body = [task("plan"),
            task("use", prompt="{{ nodes.plan.output.result_json.packages.1 }}/"
                               "{{ nodes.plan.output.verdict }}")]
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    assert result.status == "completed"
    assert dispatcher.calls[1].prompt == "pb/PASS"
    output = result.nodes["plan"]["output"]
    assert output["result_json"] == {"packages": ["pa", "pb"]}
    assert output["verdict"] == "PASS"


def test_unresolved_template_fails_the_node_without_dispatch(settings):
    import asyncio

    from subagent.graph.interpreter import Interpreter

    # the static check would reject an unknown input; declare it, then drop it at run time
    graph, inp = graph_input(doc([task("a", prompt="{{ inputs.maybe }}"), task("b")],
                                 inputs={"maybe": 1}))
    inp.inputs.pop("maybe")
    dispatcher = fd()
    result = asyncio.run(Interpreter(graph, make_runtime(settings, dispatcher), inp).run())
    assert result.nodes["a"]["status"] == "failed"
    assert "template_unresolved" in (result.nodes["a"]["error"] or "")
    assert dispatcher.calls == []
    assert result.nodes["b"]["status"] == "not_run"


def test_when_false_skips_and_does_not_dispatch(settings):
    body = [task("a"), task("b", when={"path": "nodes.a.status", "op": "eq", "value": "failed"}),
            task("c")]
    dispatcher = fd()
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    assert result.status == "completed"
    assert node_status(result, "b") == "skipped"
    assert [c.node_id for c in dispatcher.calls] == ["a", "c"]


def test_when_true_runs_node(settings):
    body = [task("a"),
            task("b", when={"path": "nodes.a.status", "op": "eq", "value": "completed"})]
    dispatcher = fd()
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    assert node_status(result, "b") == "completed"


def test_graph_output_paths_evaluated_at_end(settings):
    body = [task("review")]
    document = doc(body, output={"verdict": "nodes.review.output.verdict",
                                 "node_status": "nodes.review.status"})
    result, _, _ = run_graph(settings, document, fd({"review": [FakeStep(verdict="PASS")]}))
    assert result.output == {"verdict": "PASS", "node_status": "completed"}


def test_determinism_same_graph_same_requests(settings):
    document = doc([task("a"), task("b")])
    first, second = fd(), fd()
    run_graph(settings, document, first)
    run_graph(settings, document, second)
    assert request_ids(first) == request_ids(second)


def test_snapshot_shape_after_run(settings):
    result, _, interp = run_graph(settings, doc([task("a")]), fd())
    snap = interp.snapshot()
    for key in ("run_path", "state", "paused", "pause_reason", "nodes", "running", "queued",
                "waiting_exceptions", "children"):
        assert key in snap, key
    assert snap["run_path"] == ROOT_ID
    assert snap["state"] == "completed"
    assert snap["paused"] is False and snap["pause_reason"] is None
    assert snap["running"] == [] and snap["waiting_exceptions"] == []
    assert snap["nodes"]["a"]["status"] == "completed"
    for key in ("status", "attempts", "request_id", "cost_usd", "error"):
        assert key in snap["nodes"]["a"]
    json.dumps(snap)
    assert result.status == "completed"


# --- on_failure ------------------------------------------------------------


def test_on_failure_fail_stops_body_and_marks_rest_not_run(settings):
    body = [task("a"), task("b"), task("c")]
    dispatcher = fd({"b": [FakeStep(status="failed")]})
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    assert result.status == "failed"
    assert node_status(result, "a") == "completed"
    assert node_status(result, "b") == "failed"
    assert node_status(result, "c") == "not_run"
    assert [c.node_id for c in dispatcher.calls] == ["a", "b"]


def test_on_failure_continue_records_failure_and_goes_on(settings):
    body = [task("a", on_failure="continue"), task("b")]
    dispatcher = fd({"a": [FakeStep(status="failed")]})
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    assert node_status(result, "a") == "failed"
    assert node_status(result, "b") == "completed"
    assert result.status == "completed"


def flaky_doc():
    return doc([task("a"), task("flaky", on_failure="exception"), task("z")])


def test_on_failure_exception_retry_runs_again_under_new_scope(settings, tmp_path):
    dispatcher = fd({"flaky": [FakeStep(status="failed"), FakeStep(status="completed")]})
    queue = make_queue(tmp_path)
    result, _, _ = run_graph(settings, flaky_doc(), dispatcher, answers=answer_with("retry"),
                             exceptions=queue)
    assert request_ids(dispatcher) == [f"{ROOT_ID}/a", f"{ROOT_ID}/flaky",
                                       f"{ROOT_ID}/flaky~r1", f"{ROOT_ID}/z"]
    assert result.status == "completed"
    assert node_status(result, "flaky") == "completed"
    records = queue.list(state="all")
    assert len(records) == 1
    record = records[0]
    assert record.kind == "node_failed"
    assert record.options == ["retry", "skip", "abort"]
    assert record.needs_answer is True
    assert record.node_id == "flaky"
    assert record.root_run_id == ROOT_ID
    assert record.key.startswith(f"{ROOT_ID}/flaky") and ":node_failed" in record.key


def test_on_failure_exception_skip_sets_skipped_and_continues(settings, tmp_path):
    dispatcher = fd({"flaky": [FakeStep(status="failed")]})
    result, _, _ = run_graph(settings, flaky_doc(), dispatcher, answers=answer_with("skip"),
                             exceptions=make_queue(tmp_path))
    assert node_status(result, "flaky") == "skipped"
    assert node_status(result, "z") == "completed"
    assert result.status == "completed"
    assert len([c for c in dispatcher.calls if c.node_id == "flaky"]) == 1


def test_on_failure_exception_abort_fails_the_body(settings, tmp_path):
    dispatcher = fd({"flaky": [FakeStep(status="failed")]})
    result, _, _ = run_graph(settings, flaky_doc(), dispatcher, answers=answer_with("abort"),
                             exceptions=make_queue(tmp_path))
    assert result.status == "failed"
    assert node_status(result, "z") == "not_run"


# --- retries ---------------------------------------------------------------


def test_interrupted_is_retried_by_default_with_attempt_2(settings):
    dispatcher = fd({"a": [FakeStep(status="interrupted"), FakeStep(status="completed")]})
    result, runtime, _ = run_graph(settings, doc([task("a", retry_backoff_s=7)]), dispatcher)
    assert result.status == "completed"
    assert [c.attempt for c in dispatcher.calls] == [1, 2]
    assert [c.attempt_id for c in dispatcher.calls] == [f"{ROOT_ID}/a#a1", f"{ROOT_ID}/a#a2"]
    assert {c.request_id for c in dispatcher.calls} == {f"{ROOT_ID}/a"}
    assert result.nodes["a"]["attempts"] == 2
    assert runtime.sleeps == [7]


def test_lane_refused_is_retried_by_default(settings):
    dispatcher = fd({"a": [FakeStep(status="lane_refused"), FakeStep()]})
    result, _, _ = run_graph(settings, doc([task("a")]), dispatcher)
    assert result.status == "completed"
    assert len(dispatcher.calls) == 2


def test_failed_is_not_retried_by_default(settings):
    dispatcher = fd({"a": [FakeStep(status="failed"), FakeStep()]})
    result, runtime, _ = run_graph(settings, doc([task("a")]), dispatcher)
    assert result.status == "failed"
    assert len(dispatcher.calls) == 1
    assert result.nodes["a"]["attempts"] == 1
    assert runtime.sleeps == []


def test_retry_on_failed_with_two_retries(settings):
    dispatcher = fd({"a": [FakeStep(status="failed"), FakeStep(status="failed"), FakeStep()]})
    document = doc([task("a", retries=2, retry_on=["failed"])])
    result, _, _ = run_graph(settings, document, dispatcher)
    assert result.status == "completed"
    assert [c.attempt for c in dispatcher.calls] == [1, 2, 3]


def test_retries_exhausted_keeps_last_status(settings):
    dispatcher = fd({"a": [FakeStep(status="interrupted")]})  # the last step repeats
    result, _, _ = run_graph(settings, doc([task("a", retries=1)]), dispatcher)
    assert len(dispatcher.calls) == 2
    assert node_status(result, "a") == "interrupted"
    assert result.nodes["a"]["attempts"] == 2
    assert result.status == "failed"


def test_retries_zero_never_retries(settings):
    dispatcher = fd({"a": [FakeStep(status="interrupted"), FakeStep()]})
    result, _, _ = run_graph(settings, doc([task("a", retries=0)]), dispatcher)
    assert len(dispatcher.calls) == 1
    assert node_status(result, "a") == "interrupted"


def test_unverified_maps_to_failed_with_output_flag(settings):
    dispatcher = fd({"a": [FakeStep(status="unverified")]})
    result, _, _ = run_graph(settings, doc([task("a")]), dispatcher)
    assert node_status(result, "a") == "failed"
    assert result.nodes["a"]["output"]["unverified"] is True
    assert len(dispatcher.calls) == 1


def test_unverified_retried_when_listed(settings):
    dispatcher = fd({"a": [FakeStep(status="unverified"), FakeStep()]})
    document = doc([task("a", retry_on=["unverified"])])
    result, _, _ = run_graph(settings, document, dispatcher)
    assert result.status == "completed"
    assert len(dispatcher.calls) == 2


def test_cost_is_recorded_on_the_node(settings):
    dispatcher = fd({"a": [FakeStep(cost_usd=0.25)]})
    result, _, _ = run_graph(settings, doc([task("a")]), dispatcher)
    assert result.nodes["a"]["cost_usd"] == pytest.approx(0.25)


# --- joins -----------------------------------------------------------------


def parallel(node_id, branches, join=None, **extra):
    node = {"id": node_id, "kind": "parallel", "branches": branches}
    if join:
        node["join"] = join
    node.update(extra)
    return node


def test_all_settled_waits_for_all_and_reports_counts(settings):
    body = [parallel("par", [task("a"), task("b"), task("c")])]
    dispatcher = fd({"b": [FakeStep(status="failed")]})
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    out = result.nodes["par"]
    assert out["status"] == "completed"
    assert out["output"]["completed"] == 2
    assert out["output"]["failed"] == 1
    assert out["output"]["branches"] == {"a": "completed", "b": "failed", "c": "completed"}
    assert result.status == "completed"
    assert {c.node_id for c in dispatcher.calls} == {"a", "b", "c"}


def test_all_settled_is_the_default_join(settings):
    body = [parallel("par", [task("a"), task("b")], join=None)]
    result, _, _ = run_graph(settings, doc(body), fd({"a": [FakeStep(status="failed")]}))
    assert node_status(result, "par") == "completed"


def test_parallel_branches_run_concurrently(settings):
    body = [parallel("par", [task("a"), task("b"), task("c")])]
    default = FakeStep(sleep_s=0.6, cost_usd=0.1)
    started = time.monotonic()
    result, _, _ = run_graph(settings, doc(body), fd(default=default))
    elapsed = time.monotonic() - started
    assert result.status == "completed"
    assert elapsed < 1.5, elapsed  # serial would take at least 1.8 s


def test_fail_fast_cancels_the_rest(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    body = [parallel("par", [task("a"), task("b")], join="failFast"), task("after")]
    dispatcher = fd({"a": [FakeStep(status="failed", sleep_s=0.2)],
                     "b": [FakeStep(sleep_s=20.0)]}, audit_log=audit)
    started = time.monotonic()
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    elapsed = time.monotonic() - started
    assert elapsed < 8, f"loser was not cancelled ({elapsed:.1f}s)"
    assert node_status(result, "par") == "failed"
    assert node_status(result, "after") == "not_run"
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    b_events = [e["event"] for e in events if e["attempt_id"].startswith(f"{ROOT_ID}/b#")]
    assert b_events == ["start", "end"], "the cancelled branch's dispatch must return"


def test_fail_fast_all_completed_is_completed(settings):
    body = [parallel("par", [task("a"), task("b", when={"path": "inputs.no", "op": "exists"})],
                     join="failFast")]
    result, _, _ = run_graph(settings, doc(body, inputs={"x": 1}), fd())
    assert node_status(result, "par") == "completed"


def test_any_first_completed_wins_and_cancels_losers(settings, tmp_path):
    audit = tmp_path / "audit.jsonl"
    body = [parallel("par", [task("slow"), task("fast")], join="any")]
    dispatcher = fd({"slow": [FakeStep(sleep_s=20.0)], "fast": [FakeStep(sleep_s=0.2)]},
                    audit_log=audit)
    started = time.monotonic()
    result, _, _ = run_graph(settings, doc(body), dispatcher)
    assert time.monotonic() - started < 8
    assert node_status(result, "par") == "completed"
    assert result.nodes["par"]["output"]["winner"] == "fast"
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    slow = [e["event"] for e in events if e["attempt_id"].startswith(f"{ROOT_ID}/slow#")]
    assert slow == ["start", "end"]


def test_any_all_failed_is_failed(settings):
    body = [parallel("par", [task("a"), task("b")], join="any")]
    default = FakeStep(status="failed")
    result, _, _ = run_graph(settings, doc(body), fd(default=default))
    assert node_status(result, "par") == "failed"


# --- for_each --------------------------------------------------------------


def fan(items="nodes.plan.output.result_json.packages", **extra):
    node = {"id": "builds", "kind": "parallel", "for_each": {
        "items": items, "as": "pkg",
        "node": task("build", prompt="build {{ item.pkg }}"), **extra}}
    return node


def test_for_each_expands_over_result_json_list(settings):
    script = {"plan": [FakeStep(result_json={"packages": ["x", "y", "z"]})]}
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([task("plan"), fan()]), dispatcher)
    builds = [c for c in dispatcher.calls if c.node_id == "build"]
    assert {c.request_id for c in builds} == {f"{ROOT_ID}/builds[{i}]/build" for i in range(3)}
    assert sorted(c.prompt for c in builds) == ["build x", "build y", "build z"]
    assert node_status(result, "builds") == "completed"
    assert result.nodes["builds"]["output"]["completed"] == 3
    assert result.nodes["builds"]["output"]["failed"] == 0


def test_for_each_each_item_sees_its_own_item(settings):
    script = {"plan": [FakeStep(result_json={"packages": [{"n": 1}, {"n": 2}]})]}
    node = {"id": "builds", "kind": "parallel", "for_each": {
        "items": "nodes.plan.output.result_json.packages", "as": "pkg",
        "node": task("build", prompt="n={{ item.pkg.n }}")}}
    dispatcher = fd(script)
    run_graph(settings, doc([task("plan"), node]), dispatcher)
    by_request = {c.request_id: c.prompt for c in dispatcher.calls if c.node_id == "build"}
    assert by_request == {f"{ROOT_ID}/builds[0]/build": "n=1", f"{ROOT_ID}/builds[1]/build": "n=2"}


def test_for_each_manual_retry_scope(settings, tmp_path):
    script = {"plan": [FakeStep(result_json={"packages": ["x"]})],
              "build": [FakeStep(status="failed"), FakeStep()]}
    node = fan()
    node["for_each"]["node"]["on_failure"] = "exception"
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([task("plan"), node]), dispatcher,
                             answers=answer_with("retry"), exceptions=make_queue(tmp_path))
    builds = [c.request_id for c in dispatcher.calls if c.node_id == "build"]
    assert builds == [f"{ROOT_ID}/builds[0]/build", f"{ROOT_ID}/builds[0]/build~r1"]
    assert result.status == "completed"


def test_for_each_not_a_list_fails_the_node(settings):
    script = {"plan": [FakeStep(result_json={"packages": "not-a-list"})]}
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([task("plan"), fan()]), dispatcher)
    assert node_status(result, "builds") == "failed"
    assert "for_each_not_list" in (result.nodes["builds"]["error"] or "")
    assert not [c for c in dispatcher.calls if c.node_id == "build"]


def test_for_each_missing_path_is_not_a_list(settings):
    result, _, _ = run_graph(settings, doc([task("plan"), fan()]), fd())
    assert node_status(result, "builds") == "failed"
    assert "for_each_not_list" in (result.nodes["builds"]["error"] or "")


def test_for_each_too_many_items_fails_the_node(settings):
    script = {"plan": [FakeStep(result_json={"packages": ["a", "b", "c"]})]}
    node = fan(max_items=2)
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([task("plan"), node]), dispatcher)
    assert node_status(result, "builds") == "failed"
    assert "for_each_too_many" in (result.nodes["builds"]["error"] or "")
    assert not [c for c in dispatcher.calls if c.node_id == "build"]


def test_for_each_default_max_items_is_64(settings):
    items = [f"i{n}" for n in range(65)]
    script = {"plan": [FakeStep(result_json={"packages": items})]}
    dispatcher = fd(script, default=FakeStep(cost_usd=0.0))
    result, _, _ = run_graph(settings, doc([task("plan"), fan()]), dispatcher, timeout=60)
    assert "for_each_too_many" in (result.nodes["builds"]["error"] or "")


def test_for_each_empty_list_completes_with_zero(settings):
    script = {"plan": [FakeStep(result_json={"packages": []})]}
    result, _, _ = run_graph(settings, doc([task("plan"), fan()]), fd(script))
    assert node_status(result, "builds") == "completed"
    assert result.nodes["builds"]["output"]["completed"] == 0


# --- loops -----------------------------------------------------------------


def review_loop(max_iterations=3):
    return {"id": "lp", "kind": "loop", "max_iterations": max_iterations,
            "until": {"path": "nodes.review.output.verdict", "op": "eq", "value": "PASS"},
            "body": [
                task("review", prompt="iteration {{ loop.lp.iteration }}"),
                task("fix", when={"path": "nodes.review.output.verdict", "op": "eq",
                                  "value": "FAIL"}),
            ]}


def test_loop_until_met_after_second_iteration(settings):
    script = {"review": [FakeStep(verdict="FAIL"), FakeStep(verdict="PASS")]}
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([review_loop()]), dispatcher)
    assert node_status(result, "lp") == "completed"
    assert result.nodes["lp"]["output"] == {"iterations": 2}
    assert request_ids(dispatcher) == [f"{ROOT_ID}/lp[0]/review", f"{ROOT_ID}/lp[0]/fix",
                                       f"{ROOT_ID}/lp[1]/review"]
    assert [c.prompt for c in dispatcher.calls if c.node_id == "review"] == [
        "iteration 0", "iteration 1"]
    assert result.status == "completed"


def test_loop_until_met_on_first_iteration(settings):
    script = {"review": [FakeStep(verdict="PASS")]}
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([review_loop()]), dispatcher)
    assert result.nodes["lp"]["output"] == {"iterations": 1}
    assert [c.node_id for c in dispatcher.calls] == ["review"]


def test_loop_exhausted_fails_with_error(settings):
    script = {"review": [FakeStep(verdict="FAIL")]}
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([review_loop(max_iterations=2), task("after")]),
                             dispatcher)
    assert node_status(result, "lp") == "failed"
    assert "loop_exhausted" in (result.nodes["lp"]["error"] or "")
    assert len([c for c in dispatcher.calls if c.node_id == "review"]) == 2
    assert node_status(result, "after") == "not_run"
    assert result.status == "failed"


def test_loop_nodes_name_the_latest_iteration(settings):
    script = {"review": [FakeStep(verdict="FAIL"), FakeStep(verdict="PASS")]}
    loop = review_loop()
    loop["body"].append(task("note", prompt="seen {{ nodes.review.output.verdict }}"))
    dispatcher = fd(script)
    result, _, _ = run_graph(settings, doc([loop]), dispatcher)
    assert result.status == "completed"
    assert [c.prompt for c in dispatcher.calls if c.node_id == "note"] == ["seen FAIL",
                                                                           "seen PASS"]


# --- gates -----------------------------------------------------------------


def gate_cond(op_value):
    return {"id": "g", "kind": "gate",
            "condition": {"path": "inputs.n", "op": "gte", "value": op_value}}


def test_condition_gate_pass_and_fail(settings):
    ok, _, _ = run_graph(settings, doc([gate_cond(3), task("after")], inputs={"n": 5}), fd())
    assert node_status(ok, "g") == "completed"
    assert node_status(ok, "after") == "completed"
    bad, _, _ = run_graph(settings, doc([gate_cond(9), task("after")], inputs={"n": 5}), fd())
    assert node_status(bad, "g") == "failed"
    assert node_status(bad, "after") == "not_run"
    assert bad.status == "failed"


def test_condition_gate_on_failure_continue(settings):
    gate = dict(gate_cond(9), on_failure="continue")
    result, _, _ = run_graph(settings, doc([gate, task("after")], inputs={"n": 5}), fd())
    assert node_status(result, "g") == "failed"
    assert node_status(result, "after") == "completed"


def _stub_verification(monkeypatch, passed, calls):
    from subagent.graph import local as local_module

    from subagent import verify

    def stub(command, workspace, settings, budget=None):
        calls.append((command, workspace))
        return verify.VerificationResult(command, passed, "stub", exit_code=0 if passed else 1,
                                         output="out")

    monkeypatch.setattr(verify, "run_verification", stub)
    if hasattr(local_module, "run_verification"):
        monkeypatch.setattr(local_module, "run_verification", stub)


@pytest.mark.parametrize("passed", [True, False])
def test_command_gate_runs_through_run_verification(settings, tmp_path, monkeypatch, passed):
    calls: list = []
    _stub_verification(monkeypatch, passed, calls)
    gate = {"id": "g", "kind": "gate", "command": "uv run pytest -q tests/x",
            "workspace": str(tmp_path)}
    result, _, _ = run_graph(settings, doc([gate, task("after")]), fd())
    assert [c[0] for c in calls] == ["uv run pytest -q tests/x"]
    assert str(calls[0][1]) == str(tmp_path)
    assert node_status(result, "g") == ("completed" if passed else "failed")
    assert node_status(result, "after") == ("completed" if passed else "not_run")


# --- owner decisions -------------------------------------------------------


def decision(**extra):
    node = {"id": "ask", "kind": "owner_decision", "question": "Merge {{ inputs.t }}?",
            "options": ["merge", "hold"]}
    node.update(extra)
    return node


def test_owner_decision_answer_completes_with_output(settings, tmp_path):
    queue = make_queue(tmp_path)
    answers = answer_with("merge", note="looks fine", by="the owner")
    document = doc([decision(), task("after")], inputs={"t": "x1"})
    result, _, _ = run_graph(settings, document, fd(), answers=answers, exceptions=queue)
    assert node_status(result, "ask") == "completed"
    output = result.nodes["ask"]["output"]
    assert output["answer"] == "merge"
    assert output["note"] == "looks fine"
    assert output["answered_by"] == "the owner"
    assert not output.get("timed_out")
    assert node_status(result, "after") == "completed"
    records = queue.list(state="all")
    assert [r.kind for r in records] == ["owner_decision"]
    assert records[0].options == ["merge", "hold"]
    assert records[0].needs_answer is True
    assert "Merge x1?" in records[0].summary


def test_owner_decision_waits_until_answered(settings, tmp_path):
    polls = {"n": 0}

    def answers(_id):
        polls["n"] += 1
        if polls["n"] < 4:
            return None
        return {"answer": "hold", "note": None, "answered_by": "test"}

    result, _, _ = run_graph(settings, doc([decision()], inputs={"t": "x"}), fd(),
                             answers=answers, exceptions=make_queue(tmp_path))
    assert polls["n"] >= 4
    assert result.nodes["ask"]["output"]["answer"] == "hold"


def _timed_out_runtime(answer):
    def factory(settings, dispatcher, answers, exceptions):
        runtime = make_runtime(settings, dispatcher, answers=answers, exceptions=exceptions)

        async def ask(scope, kind, summary, options, default, timeout_s, detail):
            runtime.asked = (kind, options, default, timeout_s)
            return {"answer": answer(default), "note": None, "answered_by": "timeout",
                    "timed_out": True}

        runtime.ask = ask
        return runtime

    return factory


def test_owner_decision_timeout_with_default_completes_with_default(settings):
    document = doc([decision(default="hold", timeout_s=60), task("after")], inputs={"t": "x"})
    result, runtime, _ = run_graph(settings, document, fd(),
                                   runtime_factory=_timed_out_runtime(lambda d: d))
    assert node_status(result, "ask") == "completed"
    assert result.nodes["ask"]["output"]["answer"] == "hold"
    assert result.nodes["ask"]["output"]["timed_out"] is True
    assert runtime.asked == ("owner_decision", ["merge", "hold"], "hold", 60)
    assert node_status(result, "after") == "completed"


def test_owner_decision_timeout_without_default_fails(settings):
    document = doc([decision(timeout_s=60), task("after")], inputs={"t": "x"})
    result, _, _ = run_graph(settings, document, fd(),
                             runtime_factory=_timed_out_runtime(lambda d: None))
    assert node_status(result, "ask") == "failed"
    assert "decision_timeout" in (result.nodes["ask"]["error"] or "")
    assert node_status(result, "after") == "not_run"


def test_local_runtime_times_out_on_its_own_clock(settings, tmp_path):
    """No answer ever arrives: the runtime's clock (now/sleep) decides the timeout."""
    document = doc([decision(default="hold", timeout_s=60)], inputs={"t": "x"})
    result, runtime, _ = run_graph(settings, document, fd(), answers=lambda _id: None,
                                   exceptions=make_queue(tmp_path), timeout=20)
    assert result.nodes["ask"]["output"]["answer"] == "hold"
    assert result.nodes["ask"]["output"]["timed_out"] is True
    assert runtime.now() >= 1_000_000.0 + 60 - 1


# --- subgraph --------------------------------------------------------------


def test_subgraph_runs_child_and_returns_its_output(settings):
    graphs = {"sub": {"inputs": {"x": "d"}, "body": [task("inner", prompt="work {{ inputs.x }}")],
                      "output": {"v": "nodes.inner.output.verdict"}}}
    node = {"id": "child", "kind": "subgraph", "graph": "sub",
            "inputs": {"x": "{{ inputs.who }}"}}
    document = doc([node, task("after", prompt="got {{ nodes.child.output.v }}")],
                   graphs=graphs, inputs={"who": "ada"})
    dispatcher = fd({"inner": [FakeStep(verdict="PASS")]})
    result, _, _ = run_graph(settings, document, dispatcher)
    assert result.status == "completed"
    assert node_status(result, "child") == "completed"
    assert result.nodes["child"]["output"] == {"v": "PASS"}
    inner = dispatcher.calls[0]
    assert inner.prompt == "work ada"
    assert inner.request_id == f"{ROOT_ID}/child/inner"
    assert inner.run_path == f"{ROOT_ID}/child"
    assert dispatcher.calls[1].prompt == "got PASS"


def test_subgraph_failure_status_propagates(settings):
    node = {"id": "child", "kind": "subgraph", "body": [task("inner")]}
    result, _, _ = run_graph(settings, doc([node, task("after")]), fd({"inner": [
        FakeStep(status="failed")]}))
    assert node_status(result, "child") == "failed"
    assert node_status(result, "after") == "not_run"


# --- the worked example ----------------------------------------------------


def worked_script():
    return {
        "plan": [FakeStep(result_json={"packages": ["pkg_a", "pkg_b"]}, cost_usd=0.4)],
        "review": [FakeStep(verdict="PASS", result_json={"verdict": "PASS", "findings": []},
                            cost_usd=0.2)],
    }


def test_worked_example_completes_on_local_runtime(settings, tmp_path):
    dispatcher = fd(worked_script())
    result, _, _ = run_graph(settings, worked_example(), dispatcher,
                             answers=answer_with("merge"), exceptions=make_queue(tmp_path))
    assert result.status == "completed"
    assert result.output == {"verdict": "PASS", "decision": "merge"}
    assert node_status(result, "builds") == "completed"
    assert result.nodes["builds"]["output"]["failed"] == 0
    assert node_status(result, "both_built") == "completed"
    prompts = {c.node_id: c.prompt for c in dispatcher.calls}
    assert prompts["build_a"] == "Build pkg_a per PLAN.md."
    assert prompts["build_b"] == "Build pkg_b per PLAN.md."
    assert {c.node_id: c.provider for c in dispatcher.calls}["build_b"] == "deepseek"
    assert "fix" not in prompts  # the review passed on the first iteration
