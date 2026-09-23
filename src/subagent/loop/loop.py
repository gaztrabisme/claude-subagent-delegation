#!/usr/bin/env python3
"""The plan-build-test-review loop and its CLI commands.

Claude writes the plan + tests; the worker implements; the worker may run tests but may NOT change
them. Every round is checkpointed (undo-able), test files and test config are guarded, and the runner
re-runs the test suite itself (server-side, through `verify.run_verification`) after the worker
finishes. Workers run on whatever providers the config declares, through the provider `Registry`.

Subcommands (all print one JSON object to stdout):
  detect                           detected test command, protected files
  run --plan FILE                  one worker round on a plan
      [--continue] [--feedback T]    reuse the worker session for a retry
      [--tier normal|hard]           complexity tier -> provider/model ([loop.tiers])
      [--background]                 return immediately; collect the result with `wait`
  run --parallel MANIFEST          several workers at once, each in its own git worktree
      [--auto]                       autopilot: retry failing tests and fix high-severity review
                                     issues automatically; return only when done or stuck
      [--wait S]                     start in the background and wait up to S seconds in this call
  wait [--timeout S]               wait for the current run; {"status": "running"} if still going
  test                             run the test suite
  review [--tier T] [--base ID]    review of everything changed since the task started
  review --tests [--plan FILE]     review Claude's tests against the plan/spec (autopilot does this first)
  undo [--to ID]                   restore the working tree to a checkpoint (default: before last round)
  checkpoints                      list checkpoints
  watch [--log FILE | --run ID]    follow the live log (run it in another terminal)
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from ..verify import run_verification
from . import checkpoint, session
from .common import (DEPENDENCY_DIRS, STATE_DIR, changed_between, git_prefix, hash_tree,
                    matches, read_json, state_dir, tail, walk_files, write_json)
from .detect import detect
from .events import END_MARKER, LiveLog, LogSink, follow, open_live_view
from .testguard import TestGuard, protected_hash

# How to re-run this runner: as a module, so its relative imports resolve.
RUNNER = [sys.executable, "-m", "subagent.cli"]
STATUS_EXIT = {"done": 0, "running": 3, "started": 0}

# A failed run with no work done walks to the next candidate on these endings.
FALLBACK_REASONS = {"no_lane", "refused", "auth", "cli_error"}


# ---------------------------------------------------------------- tiers and prompts

def resolve_tier(settings, tier, explicit_provider=None, explicit_model=None):
    """(provider, model) candidates in order; the hard tier falls back to normal."""
    if explicit_provider is not None or explicit_model is not None:
        provider = explicit_provider
        if provider is None:
            target = settings.loop.tiers.get(tier)
            provider = target.provider if target is not None else None
        return [(provider, explicit_model)]
    chosen = settings.loop.tiers.get(tier)
    candidates = []
    for target in ([chosen, settings.loop.tiers.get("normal")] if tier == "hard" else [chosen]):
        if target is None or (target.provider is None and target.model is None):
            continue
        candidates.append((target.provider, target.model))
    candidates = list(dict.fromkeys(candidates))
    return candidates or [(None, None)]


def build_prompt(plan, feedback, info, protected_files, parallel=None):
    shown = protected_files[:50]
    more = f"\n  ... and {len(protected_files) - 50} more" if len(protected_files) > 50 else ""
    feedback_block = f"\n## Feedback from the reviewer on your previous attempt\n{feedback}\n" if feedback else ""
    test_cmd = info["test_cmd"] or "(none detected - verify by reasoning and any checks you can run)"
    parallel_block = ""
    if parallel:
        others = "\n".join(f"   - {t['name']}: {', '.join(t['files'])}" for t in parallel["others"]) or "   (none)"
        own_tests = parallel.get("test_cmd") or f"`{test_cmd}` (other parts may still fail in your copy)"
        parallel_block = f"""
## Parallel work
You are worker "{parallel['name']}", one of several workers running at the same time, each in its own
copy of the repository. Only create or modify files matching: {', '.join(parallel['files'])}
Changes to any other file are discarded. Other workers are building:
{others}
Their code is not in your copy. Code against the interfaces described in the plan. Test your part with:
{own_tests}
"""
    return f"""You are implementing a task in this repository. Work autonomously; nobody will answer questions during this run.

## Task plan
{plan}
{feedback_block}{parallel_block}
## Rules
1. Implement the plan. The test command is: `{test_cmd}`
   Run it yourself and keep iterating until it passes.
2. Test files are READ-ONLY and owned by the reviewer. Do NOT modify, delete, rename, skip,
   or add test files or test configuration. Protected globs: {", ".join(info["test_globs"]) or "(none)"}
   Protected files:
  {chr(10).join("  " + f for f in shown) or "  (none yet)"}{more}
   Any change to them is automatically reverted and your run is marked as a violation. Protected files
   are also denied by the guard hook: writes to them fail before they run.
3. If you believe a test is wrong (contradicts the plan, or has a bug), do NOT work around it.
   Write `.subagent/test_change_request.md` with: the test file and test name, why it is wrong,
   and the exact change you propose. Then finish with status "needs_test_change".
4. Do not game the tests (no hardcoding expected outputs, no special-casing test inputs).
5. When you finish, write `.subagent/result.json` containing exactly:
   {{"status": "done" | "failed" | "needs_test_change", "summary": "<one or two sentences>"}}
   Use "done" only if the tests for your work pass.
"""


# ---------------------------------------------------------------- worker

class Worker:
    """One worker turn through the provider Registry: candidates, guard, verification."""

    def __init__(self, server, root, test_cmd, guard=None, live=None):
        self.server = server
        self.registry = server.registry
        self.settings = server.settings
        self.root = root
        self.test_cmd = test_cmd or ""
        self.guard = guard
        self.live = live
        self.agent = None
        self.run = None
        self.model = None
        self.fallback_note = None

    def _release(self):
        if self.guard is None:
            return {"violations": [], "changed_files": []}
        violations, changed = self.guard.release()
        return {"violations": violations, "changed_files": changed}

    def _on_event(self):
        return self.live.feed if self.live is not None else None

    def _wait(self, run):
        timeout = self.agent.cfg.run_timeout
        if not run.done.wait(timeout):
            self.agent.close("run deadline exceeded", kind="timeout")
        run.done.wait()

    def _guard_denials(self):
        if self.agent is None:
            return []
        return self.server.supervisor.pop_denials(self.agent.agent_id)

    def _make_agent(self, provider, model, fallback):
        agent = self.registry.create_agent(
            None, self.root, provider=provider, model=model, fallback=fallback,
            on_event=self._on_event(), agent_id=f"loop-{uuid.uuid4()}",
        )
        agent.guard_context = self._guard_context()
        return agent

    def _guard_context(self):
        protected = list(self.guard.protected) if self.guard is not None else []
        return {
            "protected": protected,
            "state_allow": [f"{STATE_DIR}/result.json", f"{STATE_DIR}/test_change_request.md"],
        }

    def run_round(self, candidates, fallback, prompt):
        """A fresh worker round; a failed, no-work candidate walks to the next."""
        for attempt, (provider, model) in enumerate(candidates):
            if self.live is not None:
                self.live.write(f"=== model: {model or 'default'}")
            self.agent = self._make_agent(provider, model, fallback)
            self.run = self.agent.delegate(
                prompt, self.test_cmd, pre_verify=self._release,
                on_event=self._on_event(), distill=False,
            )
            self._wait(self.run)
            self.model = model
            if attempt < len(candidates) - 1 and self._fallback_ok(self.run):
                nxt = candidates[attempt + 1][1]
                self.fallback_note = (
                    f"{model or 'default'} failed ({self.run.finish_reason}); "
                    f"retried with {nxt or 'default'}"
                )
                if self.live is not None:
                    self.live.write(f"=== {self.fallback_note}")
                self.agent.close("candidate failed")
                continue
            break
        return self

    def follow_up(self, record, prompt):
        """Continue the recorded agent session (--continue, autopilot rounds 2+)."""
        self.model = record.get("model")
        if self.live is not None:
            self.live.write(f"=== model: {self.model or 'default'}")
        self.agent = self.registry.adopt(record, workspace=self.root, on_event=self._on_event())
        self.agent.guard_context = self._guard_context()
        self.run = self.agent.follow_up(
            prompt, verification=self.test_cmd, pre_verify=self._release,
            on_event=self._on_event(), distill=False,
        )
        self._wait(self.run)
        return self

    @staticmethod
    def _fallback_ok(run):
        return run.state == "failed" and run.finish_reason in FALLBACK_REASONS and not run.worked


# ---------------------------------------------------------------- run bookkeeping

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _active_run(root):
    current = read_json(root / STATE_DIR / "current.json")
    if not current or (root / STATE_DIR / "runs" / f"{current['run_id']}.json").exists():
        return None
    return current if _pid_alive(current["pid"]) else None


def _stamp():
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"


def _new_log(root, name):
    logs = state_dir(root) / "logs"
    log = logs / f"{name}-{_stamp()}.log"
    latest = logs / "latest.log"
    latest.unlink(missing_ok=True)
    latest.symlink_to(log.name)
    return log


def _feedback(args):
    """--feedback text plus --feedback-file contents ("-" reads stdin). Files avoid shell quoting issues."""
    text = args.feedback or ""
    if args.feedback_file:
        source = sys.stdin.read() if args.feedback_file == "-" else Path(args.feedback_file).read_text(encoding="utf-8")
        text = f"{text}\n{source}" if text else source
    return text


def _load_session(root):
    return session.load(root)


def _save_session(root, data):
    session.save(root, data)


def _check_test_counts(session_data, tests_hash, tests):
    """Flag a passing suite that runs fewer tests (or skips more) than seen before for the same tests."""
    if session_data.get("tests_hash") != tests_hash:
        session_data["tests_hash"], session_data["best_counts"] = tests_hash, None
    counts = (tests or {}).get("counts")
    if not tests or not counts or counts.get("total") is None:
        return None
    best = session_data.get("best_counts")
    problem = None
    if tests["passed"] and best:
        if counts["total"] < best["total"]:
            problem = f"the suite passed but ran {counts['total']} tests, fewer than the {best['total']} seen before"
        elif counts["skipped"] > best["skipped"]:
            problem = f"the suite passed but skipped {counts['skipped']} tests, more than the {best['skipped']} before"
    if not best or counts["total"] > best["total"] or (
            counts["total"] == best["total"] and counts["skipped"] < best["skipped"]):
        session_data["best_counts"] = {"total": counts["total"], "skipped": counts["skipped"]}
    return problem


def _tests_summary(tests):
    if not tests:
        return None
    out = {"passed": tests["passed"], "cmd": tests["cmd"]}
    if tests.get("counts"):
        out["counts"] = tests["counts"]
    if not tests["passed"]:
        out["output_tail"] = tests.get("output_tail")
    return out


def _tests_from_verification(verification, state):
    """The {passed, cmd, counts, output_tail} dict for a run_verification result; writes its full log."""
    if verification is None or verification.exit_code is None:
        return None
    tests = {"passed": verification.passed, "cmd": verification.command}
    if verification.counts is not None:
        tests["counts"] = verification.counts
    if not verification.passed:
        tests["output_tail"] = tail(verification.output)
    log_path = state / "logs" / f"test-{_stamp()}.log"
    log_path.write_text(verification.output)
    return tests


def _status(violations, timed_out, failed, report, tests):
    if violations:
        return "violated_tests"
    if timed_out:
        return "timeout"
    if failed:
        return "backend_error"
    if report.get("status") == "needs_test_change":
        return "needs_test_change"
    if tests is not None:
        return "done" if tests["passed"] else "tests_failed"
    return report.get("status") if report.get("status") in ("done", "failed") else "no_report"


# ---------------------------------------------------------------- delegation trace

_USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")


def _set_delegation_id(server, run_id):
    """Tag every record this delegation writes with its run id."""
    trace = getattr(getattr(server, "registry", None), "trace", None)
    if trace is not None:
        trace.set_context(delegation_id=run_id)


def _run_block(run, model=None):
    """The JSON-safe per-run facts a delegation record sums."""
    return {
        "run_id": run.run_id,
        "provider": run.lane or run.provider,
        "model": model,
        "usage": run.usage.as_dict(),
        "credits": run.usage.credits,
        "cost": run.cost,
    }


def _usage_sum(usages):
    total = {key: 0 for key in _USAGE_FIELDS}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key in _USAGE_FIELDS:
            total[key] += int(usage.get(key) or 0)
    return total


def _cost_sum(costs):
    provider = [c.get("provider_usd") for c in costs if isinstance(c, dict)]
    counterfactual = sum(
        float(c.get("counterfactual_usd") or 0) for c in costs if isinstance(c, dict)
    )
    provider_usd = None if not provider or any(p is None for p in provider) else float(sum(provider))
    return {"provider_usd": provider_usd, "counterfactual_usd": counterfactual}


def _delegation_rounds(result, blocks):
    """One `rounds[]` entry per worker run, carrying that run's usage/credits/cost."""
    tier = result.get("tier")
    status = result.get("status")
    guard = result.get("guard_denials") or None
    violations = result.get("violations") or None
    tests = result.get("tests")
    seconds = result.get("seconds")
    return [
        {
            "run_id": block["run_id"],
            "provider": block["provider"],
            "model": block["model"] or result.get("model"),
            "tier": tier,
            "status": status,
            "guard": guard,
            "violations": violations,
            "tests": tests,
            "seconds": seconds,
            "usage": block["usage"],
            "credits": block["credits"],
            "cost": block["cost"],
        }
        for block in blocks
    ]


def _delegation_reviews(review, blocks):
    """One `reviews[]` entry per reviewer run."""
    return [
        {
            "run_id": block["run_id"],
            "provider": block["provider"],
            "model": block["model"] or review.get("model"),
            "verdict": review.get("verdict"),
            "usage": block["usage"],
            "credits": block["credits"],
            "cost": block["cost"],
        }
        for block in blocks
    ]


def _delegation_record(*, mode, status, stopped_because, changed_files, wall_seconds,
                       rounds, reviews, test_writer, blocks):
    """The schema-3 `delegation` record for one whole loop run."""
    credits = [b.get("credits") for b in blocks if isinstance(b, dict) and b.get("credits") is not None]
    return {
        "orchestrator": {
            "harness": os.environ.get("SUBAGENT_ORCHESTRATOR", "unknown"),
            "model": os.environ.get("SUBAGENT_ORCHESTRATOR_MODEL"),
        },
        "mode": mode,
        "status": status,
        "stopped_because": stopped_because,
        "rounds": rounds,
        "reviews": reviews,
        "test_writer": test_writer,
        "usage_total": _usage_sum([b.get("usage") for b in blocks if isinstance(b, dict)]),
        "credits_total": round(sum(float(v) for v in credits), 2) if credits else None,
        "cost_total": _cost_sum([b.get("cost") for b in blocks if isinstance(b, dict)]),
        "wall_seconds": wall_seconds,
        "changed_files": changed_files,
        "verified_pass": status == "done",
    }


def _write_delegation(server, record):
    trace = getattr(getattr(server, "registry", None), "trace", None)
    if trace is not None:
        trace.delegation(**record)


# ---------------------------------------------------------------- run: one round

def run_round(root, server, args, live_view=True):
    settings = server.settings
    state = state_dir(root)
    info = detect(root, settings.loop)
    plan = Path(args.plan).read_text()
    feedback = _feedback(args)

    record = _load_session(root) if args.continue_session else {}
    cp = checkpoint.create(root, f"before round ({'retry' if args.continue_session else 'new task'})")
    record.setdefault("base_checkpoint", cp["id"])

    for leftover in ("result.json", "test_change_request.md"):
        (state / leftover).unlink(missing_ok=True)

    log = _new_log(root, "run")
    sink = LogSink(log)
    sink.write(f"=== subagent run {time.strftime('%Y-%m-%d %H:%M:%S')} · tier {args.tier} · "
               f"{'continue' if args.continue_session else 'new'} session · checkpoint {cp['id']}")
    live_view_error = open_live_view(settings.loop, root, RUNNER, ["--log", log]) if live_view else None
    live = LiveLog(sink, log.with_suffix(".jsonl"), root)

    guard = TestGuard(root, info["test_globs"])
    guard.lock()
    prompt = build_prompt(plan, feedback, info, guard.protected)
    started = time.time()
    worker = Worker(server, root, info["test_cmd"], guard, live)
    try:
        if args.continue_session and record.get("agent_id"):
            worker.follow_up(record, prompt)
        else:
            worker.run_round(resolve_tier(settings, args.tier, args.provider, args.model),
                             settings.loop.fallback, prompt)
    except Exception:
        if not guard.released:
            guard.release()
        raise

    run = worker.run
    if run is not None and run.pre_verify_result is not None:
        violations = run.pre_verify_result.get("violations", [])
        changed = run.pre_verify_result.get("changed_files", [])
    else:
        violations, changed = guard.release()

    tests = _tests_from_verification(run.verification_result if run is not None else None, state)
    count_problem = (_check_test_counts(record, guard.protected_hash(), tests)
                     if settings.loop.count_tests else None)
    if count_problem:
        violations.append({"file": "(test run)", "change": count_problem})

    report = read_json(state / "result.json") or {}
    timed_out = run is not None and run.state == "failed" and run.finish_reason == "timeout"
    failed = run is not None and run.state == "failed" and not timed_out
    status = _status(violations, timed_out, failed, report, tests)
    result = {
        "status": status,
        "summary": report.get("summary"),
        "worker_reported": report.get("status"),
        "changed_files": changed,
        "tests": _tests_summary(tests),
        "tier": args.tier,
        "model": worker.model,
        "seconds": round(time.time() - started),
        "checkpoint": cp["id"],
        "log": str(log.relative_to(root)),
    }
    if run is not None and run.usage.credits is not None:
        result["worker_credits"] = round(run.usage.credits, 2)
    if live_view_error:
        result["live_view_error"] = live_view_error
    if worker.fallback_note:
        result["model_fallback"] = worker.fallback_note
    if violations:
        result["violations"] = violations
    if run is not None:
        run.guard_verdicts = worker._guard_denials()
        if run.guard_verdicts:
            result["guard_denials"] = run.guard_verdicts
    request = state / "test_change_request.md"
    if request.exists():
        result["test_change_request"] = request.read_text()[:4000]
    if status == "backend_error":
        result["log_tail"] = tail(log.read_text(errors="ignore"), 30)
    if not changed and status == "done":
        result["warning"] = "tests pass but the worker changed no files"

    credits_val = run.usage.credits if run is not None else None
    credits = f" · AI credits {credits_val:.2f}" if credits_val is not None else ""
    sink.write(f"{END_MARKER}: {status} · {result['seconds']}s · {len(changed)} files changed{credits}")
    live.close()
    sink.close()
    if worker.agent is not None:
        record.update(session.record_for(worker.agent, record.get("base_checkpoint")))
    _save_session(root, record)
    checkpoint.prune(root, settings.loop.keep_checkpoints or 0)
    result["_runs"] = [_run_block(run, worker.model)] if run is not None else []
    return {k: v for k, v in result.items() if v is not None}


# ---------------------------------------------------------------- run: parallel round

def run_parallel(root, server, args, live_view=True):
    manifest = read_json(args.parallel)
    tasks = (manifest or {}).get("tasks") or []
    if len(tasks) < 2 or any(not t.get("name") or not t.get("plan") or not t.get("files") for t in tasks):
        return {"status": "bad_manifest",
                "error": 'manifest needs 2+ tasks, each with "name", "plan" (file) and "files" (globs)'}
    if len({t["name"] for t in tasks}) != len(tasks):
        return {"status": "bad_manifest", "error": "task names must be unique"}
    if git_prefix(root) is None:
        return {"status": "unsupported", "error": "parallel rounds need a git repository; run the tasks one by one"}

    state_dir(root)
    info = detect(root, server.settings.loop)
    feedback = _feedback(args)
    record = _load_session(root) if args.continue_session else {}
    record.setdefault("parallel", {})
    cp = checkpoint.create(root, f"before parallel round ({', '.join(t['name'] for t in tasks)})")
    record.setdefault("base_checkpoint", cp["id"])

    log = _new_log(root, "parallel")
    sink = LogSink(log)
    sink.write(f"=== subagent parallel run {time.strftime('%Y-%m-%d %H:%M:%S')} · "
               f"{len(tasks)} workers · checkpoint {cp['id']}")
    live_view_error = open_live_view(server.settings.loop, root, RUNNER, ["--log", log]) if live_view else None

    try:
        return _parallel_work(root, server, args, tasks, info, feedback, record, cp, log, sink,
                              live_view_error)
    except BaseException:
        sink.close()
        raise


def _parallel_work(root, server, args, tasks, info, feedback, record, cp, log, sink, live_view_error):
    settings = server.settings
    state = root / STATE_DIR
    workers = []
    for task in tasks:
        worktree, proj = checkpoint.worktree_add(root, cp)
        for dep in DEPENDENCY_DIRS:
            if (root / dep).exists() and not (proj / dep).exists():
                (proj / dep).symlink_to(root / dep)
        guard = TestGuard(proj, info["test_globs"])
        guard.lock()
        others = [{"name": t["name"], "files": t["files"]} for t in tasks if t is not task]
        prompt = build_prompt(Path(task["plan"]).read_text(), task.get("feedback") or feedback, info,
                              guard.protected, {"name": task["name"], "files": task["files"],
                                                "others": others, "test_cmd": task.get("test_cmd")})
        live = LiveLog(sink, log.with_name(f"{log.stem}-{task['name']}.jsonl"), proj, task["name"])
        worker = Worker(server, proj, "true", guard, live)
        workers.append({"task": task, "worktree": worktree, "proj": proj, "guard": guard,
                        "worker": worker, "live": live, "prompt": prompt,
                        "tier": task.get("tier", args.tier)})

    def work(w):
        wrec = record["parallel"].get(w["task"]["name"]) if args.continue_session else None
        if isinstance(wrec, dict) and wrec.get("agent_id"):
            w["worker"].follow_up(wrec, w["prompt"])
        else:
            candidates = resolve_tier(settings, w["tier"], args.provider, args.model)
            w["worker"].run_round(candidates, settings.loop.fallback, w["prompt"])
        w["run"] = w["worker"].run
        w["model"] = w["worker"].model
        w["fallback_note"] = w["worker"].fallback_note

    started = time.time()
    threads = [threading.Thread(target=work, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    applied, results, all_violations = {}, [], []
    for w in workers:
        task, proj = w["task"], w["proj"]
        run = w["run"]
        if run is not None and run.pre_verify_result is not None:
            violations = run.pre_verify_result.get("violations", [])
            changed = run.pre_verify_result.get("changed_files", [])
        else:
            violations, changed = w["guard"].release()
        owned = [f for f in changed if matches(f, task["files"])]
        out_of_scope = [f for f in changed if f not in owned]
        conflicts = [f for f in owned if f in applied]
        for rel in owned:
            if rel in conflicts:
                continue
            src, dst = proj / rel, root / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                dst.unlink(missing_ok=True)
            applied[rel] = task["name"]
        report = read_json(proj / STATE_DIR / "result.json") or {}
        request = proj / STATE_DIR / "test_change_request.md"
        timed_out = run is not None and run.state == "failed" and run.finish_reason == "timeout"
        failed = run is not None and run.state == "failed" and not timed_out
        entry = {
            "name": task["name"],
            "status": _status(violations, timed_out, failed, report, None),
            "summary": report.get("summary"),
            "model": w["model"],
            "applied_files": [f for f in owned if f not in conflicts],
            "out_of_scope_files": out_of_scope or None,
            "conflicts": [f"{f} (already changed by {applied[f]})" for f in conflicts] or None,
            "violations": violations or None,
            "model_fallback": w["fallback_note"],
            "worker_credits": round(run.usage.credits, 2) if run is not None and run.usage.credits is not None else None,
            "test_change_request": request.read_text()[:4000] if request.exists() else None,
        }
        results.append({k: v for k, v in entry.items() if v is not None})
        all_violations += violations
        if w["worker"].agent is not None:
            record["parallel"][task["name"]] = session.record_for(w["worker"].agent)
        w["live"].close()
        checkpoint.worktree_remove(root, w["worktree"])

    tests = None
    if info["test_cmd"]:
        sink.write("=== runner: running the full test suite on the merged result")
        tests = _tests_from_verification(run_verification(info["test_cmd"], root, settings), state)
    count_problem = (_check_test_counts(record, protected_hash(root, info["test_globs"]), tests)
                     if settings.loop.count_tests else None)

    statuses = {r["status"] for r in results}
    if all_violations or count_problem:
        status = "violated_tests"
    elif "needs_test_change" in statuses:
        status = "needs_test_change"
    elif tests is not None:
        status = "done" if tests["passed"] else "tests_failed"
    else:
        status = "done" if statuses == {"done"} else "partial"
    credits = [r["worker_credits"] for r in results if "worker_credits" in r]
    result = {
        "status": status,
        "tasks": results,
        "tests": _tests_summary(tests),
        "seconds": round(time.time() - started),
        "checkpoint": cp["id"],
        "log": str(log.relative_to(root)),
        "worker_credits": round(sum(credits), 2) if credits else None,
        "violations": ([{"file": "(test run)", "change": count_problem}] if count_problem else None),
        "live_view_error": live_view_error,
    }
    total = f" · AI credits {sum(credits):.2f}" if credits else ""
    sink.write(f"{END_MARKER}: {status} · {result['seconds']}s · {len(applied)} files applied{total}")
    sink.close()
    _save_session(root, record)
    checkpoint.prune(root, settings.loop.keep_checkpoints or 0)
    result["changed_files"] = sorted(applied) or None
    result["_runs"] = [_run_block(w["run"], w["model"]) for w in workers if w["run"] is not None]
    return {k: v for k, v in result.items() if v is not None}


# ---------------------------------------------------------------- test writer (outline mode)

TEST_WRITER_PROMPT = """You are writing a TEST SUITE from an outline. The code under test does not exist yet; another
agent will implement it later against your tests. Do NOT write or change implementation code: only the
test files named in the outline.

Read the outline in `{outline}` and the plan in `.subagent/review/plan.md` (and any spec it references).
For every case in the outline, write one test that checks exactly the stated input and expected result.
Use the project's test framework and conventions (test command: `{test_cmd}`). Put each test in the file
named by the outline heading it is listed under. Keep tests plain and direct: no helpers that compute
expected values, no skipped tests.

The tests will fail until the code exists; that is expected. Only check that each test file is
syntactically valid (for example `node --check file.js` or `python3 -m py_compile file.py`).
{feedback}
When you finish, write `.subagent/test_writer_result.json`:
{{"status": "done" | "failed", "files": ["path", ...], "cases": <number of tests written>,
  "notes": "anything the planner should know, e.g. where you deviated from the outline and why"}}
"""


def _outline_files(text):
    """Test files declared by `## path/to/test_file` headings in an outline."""
    files = []
    for line in text.splitlines():
        if line.startswith("## "):
            token = line[3:].strip().split()[0].strip("`") if line[3:].strip() else ""
            if "/" in token or "." in token:
                files.append(token)
    return files


def _write_review_plan(root, plan_paths):
    parts = []
    for path in plan_paths:
        if not path:
            continue
        try:
            parts.append(f"<!-- {path} -->\n{Path(path).read_text()}")
        except OSError:
            continue
    review_dir = state_dir(root) / "review"
    review_dir.mkdir(exist_ok=True)
    (review_dir / "plan.md").write_text("\n\n".join(parts) or "(no plan file; use the spec files in the repository)")


def write_tests_from_outline(root, server, outline_path, plan_paths, issues=None):
    """Turn Claude's test outline into test files with a separate worker session (test files only).

    Skipped when the outline is unchanged and its files exist, unless `issues` (wrong tests found by the
    test review) ask for a fix pass.
    """
    state = state_dir(root)
    try:
        outline = Path(outline_path).read_text()
    except OSError as exc:
        return {"status": "bad_outline", "error": f"cannot read the outline: {exc}"}
    declared = _outline_files(outline)
    if not declared:
        return {"status": "bad_outline",
                "error": "the outline needs one '## path/to/test_file' heading per test file, cases below it"}
    outline_hash = hashlib.sha256(outline.encode()).hexdigest()
    record = read_json(state / "test_writer.json") or {}
    if not issues and record.get("outline_hash") == outline_hash and all((root / f).exists() for f in declared):
        return {"status": "skipped", "note": "outline unchanged; tests already written"}

    info = detect(root, server.settings.loop)
    _write_review_plan(root, plan_paths)
    feedback = ""
    if issues:
        listed = "\n".join(f"- {i.get('file', '?')}:{i.get('line', '?')}: {i.get('issue', '')}" for i in issues)
        feedback = ("\n## Fix pass\nAn independent review found these wrong tests:\n" + listed +
                    "\nFix them. Where the outline itself contradicts the plan or spec, follow the spec and "
                    "explain each such deviation in `notes`.\n")
    prompt = TEST_WRITER_PROMPT.format(outline=Path(outline_path).as_posix(),
                                       test_cmd=info["test_cmd"] or "(use the project's test setup)",
                                       feedback=feedback)
    target = server.settings.loop.test_writer
    cp = checkpoint.create(root, "before test writing")
    before = hash_tree(root)
    (state / "test_writer_result.json").unlink(missing_ok=True)
    log = _new_log(root, "tests")
    sink = LogSink(log)
    sink.write(f"=== subagent test writer {time.strftime('%Y-%m-%d %H:%M:%S')} · {len(declared)} files"
               f" · model {target.model or 'default'}" + (" · fix pass" if issues else ""))
    live = LiveLog(sink, log.with_suffix(".jsonl"), root)
    previous_record = record.get("session") if issues and isinstance(record.get("session"), dict) else None
    agent = None
    if previous_record and previous_record.get("agent_id"):
        agent = server.registry.adopt(previous_record, workspace=root, on_event=live.feed)
        run = agent.follow_up(prompt, verification="true", on_event=live.feed, distill=False)
    else:
        agent = server.registry.create_agent(
            None, root, provider=target.provider, model=target.model, fallback="none",
            on_event=live.feed, agent_id=f"loop-{uuid.uuid4()}",
        )
        run = agent.delegate(prompt, "true", on_event=live.feed, distill=False)
    agent.guard_context = {"protected": [], "state_allow": [f"{STATE_DIR}/test_writer_result.json"]}
    if not run.done.wait(agent.cfg.run_timeout):
        agent.close("run deadline exceeded", kind="timeout")
    run.done.wait()
    used = target.model
    agent.close("test writer done")

    # Only test files may change: revert anything else the writer touched.
    changed = changed_between(before, hash_tree(root))
    allowed = info["test_globs"] + declared
    reverted = [f for f in changed if not matches(f, allowed)]
    for rel in reverted:
        data = checkpoint.file_at(root, cp, rel)
        if data is None:
            (root / rel).unlink(missing_ok=True)
        else:
            (root / rel).write_bytes(data)
    written = sorted(f for f in changed if f not in reverted and (root / f).exists())
    report = read_json(state / "test_writer_result.json") or {}
    if run.state == "failed" or run.finish_reason == "timeout":
        status = "test_writer_error"
    elif not written and not issues and not all((root / f).exists() for f in declared):
        status = "no_tests_written"
    else:
        status = "done"
    if status == "done":
        files = sorted(set(record.get("files", [])) | set(written) | {f for f in declared if (root / f).exists()})
        write_json(state / "test_writer.json",
                   {"outline_hash": outline_hash, "session": session.record_for(agent), "files": files})
    result = {
        "status": status,
        "model": used,
        "files": written or None,
        "cases": report.get("cases"),
        "outline_cases": sum(1 for line in outline.splitlines() if line.lstrip().startswith(("- ", "* "))),
        "notes": report.get("notes") or None,
        "reverted_files": reverted or None,
        "worker_credits": round(run.usage.credits, 2) if run.usage.credits is not None else None,
        "log_tail": tail(log.read_text(errors="ignore"), 30) if status == "test_writer_error" else None,
    }
    credits = f" · AI credits {run.usage.credits:.2f}" if run.usage.credits is not None else ""
    sink.write(f"{END_MARKER}: test writer {status} · {len(written)} files{credits}")
    live.close()
    sink.close()
    return {k: v for k, v in result.items() if v is not None}


def _wrong_tests(review):
    return [i for i in (review or {}).get("issues", [])
            if i.get("severity") == "high" and i.get("kind", "wrong_test") == "wrong_test"]


def _prepare_tests(root, server, args):
    """Outline mode: write the tests; then review them. Returns (hand_back_result | None, extras)."""
    plans = [args.plan] if args.plan else [t.get("plan") for t in (read_json(args.parallel) or {}).get("tasks", [])
                                           if t.get("plan")]
    extras = {"test_writer": None, "test_review": None, "credits": []}
    writer = None
    if args.test_outline:
        writer = write_tests_from_outline(root, server, args.test_outline, plans)
        extras["test_writer"] = writer
        extras["credits"].append(writer.get("worker_credits"))
        if writer["status"] not in ("done", "skipped"):
            return {"status": writer["status"], "summary": writer.get("error") or "the test writer failed",
                    "test_writer": writer}, extras
    if not (server.settings.loop.review_tests or args.test_outline):
        return None, extras
    review_plans = plans + ([args.test_outline] if args.test_outline else [])
    review = do_test_review(root, server, args.tier, review_plans)
    extras["credits"].append(review.get("worker_credits"))
    wrong = _wrong_tests(review)
    if wrong and writer and writer["status"] == "done":
        # The generated tests may have transcription errors: one fix pass by the test writer.
        fix = write_tests_from_outline(root, server, args.test_outline, plans, issues=wrong)
        extras["credits"].append(fix.get("worker_credits"))
        writer["fix_pass"] = fix["status"]
        if fix.get("notes"):
            writer["notes"] = fix["notes"]
        if fix["status"] == "done":
            # force: even if the fix pass changed nothing, the wrong tests must be checked again.
            review = do_test_review(root, server, args.tier, review_plans, force=True)
            extras["credits"].append(review.get("worker_credits"))
            wrong = _wrong_tests(review)
    extras["test_review"] = review
    if wrong:
        return {"status": "tests_questioned",
                "summary": f"The test review found {len(wrong)} wrong test(s); no worker round was run.",
                "test_review": _brief_test_review(review),
                "test_writer": _brief_writer(writer)}, extras
    return None, extras


def _brief_writer(writer):
    if not writer or writer.get("status") == "skipped":
        return None
    return {k: v for k, v in writer.items() if k in ("model", "files", "cases", "outline_cases", "notes",
                                                     "reverted_files", "fix_pass")}


# ---------------------------------------------------------------- autopilot

# Statuses the autopilot cannot handle itself: they need Claude (test ownership) or a setup fix.
AUTO_HAND_BACK = {"needs_test_change", "backend_error", "runner_error", "unsupported", "bad_manifest"}


def _combined_plan(root, manifest_path):
    """One plan for follow-up rounds after a parallel round: all parts' plans together."""
    manifest = read_json(manifest_path) or {}
    parts = [f"## Part: {t['name']} (files: {', '.join(t['files'])})\n\n{Path(t['plan']).read_text()}"
             for t in manifest.get("tasks", [])]
    path = state_dir(root) / "auto_plan.md"
    path.write_text("# Integration plan\n\nSeveral workers built these parts in parallel. The merged "
                    "result now needs fixing; you may change any of these files.\n\n" + "\n\n".join(parts))
    return str(path)


def _round_brief(number, result):
    counts = (result.get("tests") or {}).get("counts")
    tests = f"{counts['passed']}/{counts['total']}" if counts else (
        None if result.get("tests") is None else ("passed" if result["tests"]["passed"] else "failed"))
    models = result.get("model") or ", ".join(sorted({t.get("model") for t in result.get("tasks", [])
                                                      if t.get("model")})) or None
    brief = {"round": number, "status": result["status"], "tier": result.get("tier"), "model": models,
             "tests": tests, "credits": result.get("worker_credits"), "checkpoint": result.get("checkpoint")}
    return {k: v for k, v in brief.items() if v is not None}


MAX_MISSING_TESTS_SHOWN = 3


def _brief_test_review(review):
    """Every wrong test, but only the top few missing tests: Claude reads this, so keep it short."""
    issues = review.get("issues") or []
    wrong = [i for i in issues if i.get("kind", "wrong_test") == "wrong_test"]
    missing = sorted((i for i in issues if i.get("kind") == "missing_test"),
                     key=lambda i: i.get("severity") != "high")
    shown = wrong + missing[:MAX_MISSING_TESTS_SHOWN]
    hidden = len(missing) - MAX_MISSING_TESTS_SHOWN
    return {k: v for k, v in {"verdict": review.get("verdict"), "model": review.get("model"),
                              "issues": shown or None,
                              "more_missing_tests": hidden if hidden > 0 else None,
                              "note": review.get("note")}.items() if v is not None}


def _review_feedback(issues):
    lines = [f"- {i.get('file', '?')}:{i.get('line', '?')}: {i.get('issue', '')}" for i in issues]
    return ("An independent code review found these high-severity problems. Fix them and keep the whole "
            "test suite passing:\n" + "\n".join(lines))


def autopilot(root, server, args, run_id):
    """Run rounds until the tests pass and the review has no high-severity issues, or until stuck.

    Failing tests and high-severity review issues go back to the worker automatically, so Claude only
    sees the final result (or a problem only Claude can solve, like a disputed test).
    """
    settings = server.settings
    started = time.time()
    _set_delegation_id(server, run_id)
    live_view_error = open_live_view(settings.loop, root, RUNNER, ["--run", run_id, "--since", time.time()])

    # Tests first: write them from the outline (outline mode), then check them against the plan/spec.
    hand_back, extras = _prepare_tests(root, server, args)
    test_review = extras["test_review"]
    if hand_back:
        credits = [c for c in extras["credits"] if c is not None]
        hand_back.update({"worker_credits": round(sum(credits), 2) if credits else None,
                          "live_view_error": live_view_error})
        _write_delegation(server, _delegation_record(
            mode="auto", status=hand_back.get("status"), stopped_because=None,
            changed_files=None, wall_seconds=round(time.time() - started),
            rounds=[], reviews=[], test_writer=_brief_writer(extras["test_writer"]),
            blocks=[],
        ))
        return {k: v for k, v in hand_back.items() if v is not None}

    first = run_parallel(root, server, args, live_view=False) if args.parallel else run_round(root, server, args, False)
    rounds, reviews = [first], []
    fix_plan = args.plan or (_combined_plan(root, args.parallel) if first["status"] not in AUTO_HAND_BACK else None)
    tier, failed_in_row, escalated, stop, reviewed_round = args.tier, 0, False, None, 0
    while True:
        last = rounds[-1]
        status = last["status"]
        if status in AUTO_HAND_BACK:
            stop = status
            break
        if status == "done":
            failed_in_row = 0
            if not settings.loop.auto_review or len(reviews) >= settings.loop.auto_review_cycles:
                stop = "done"
                break
            review = do_review(root, server, tier, model=None)
            reviews.append(review)
            reviewed_round = len(rounds)
            if review.get("verdict") not in ("ok", "concerns"):
                stop = "done"  # tests pass; the missing verdict is reported as unverified below
                break
            high = [i for i in review.get("issues", []) if i.get("severity") == "high"]
            if not high:
                stop = "done"
                break
            feedback = _review_feedback(high)
        elif status == "violated_tests":
            failed_in_row += 1
            if sum(1 for r in rounds if r["status"] == "violated_tests") >= 2:
                stop = "repeated_violations"
                break
            feedback = ("Your changes to tests or test configuration were reverted: "
                        + json.dumps(last.get("violations")) + "\nDo not modify tests or test settings. "
                        "If a test is wrong, use .subagent/test_change_request.md.")
        else:  # tests_failed, failed, no_report, timeout, partial
            failed_in_row += 1
            output = (last.get("tests") or {}).get("output_tail")
            feedback = (f"The test suite fails:\n{output}" if output else
                        f"The previous round ended with status {status}: {last.get('summary') or 'no summary'}. "
                        "Finish the task and make the tests pass.")
        if len(rounds) >= settings.loop.auto_max_rounds:
            stop = "max_rounds"
            break
        if tier == "normal" and failed_in_row >= 2 and not args.model:
            tier, escalated = "hard", True
        next_args = SimpleNamespace(plan=fix_plan, feedback=feedback, feedback_file=None, continue_session=True,
                                    tier=tier, model=args.model, provider=args.provider)
        rounds.append(run_round(root, server, next_args, live_view=False))

    last = rounds[-1]
    final = last["status"]
    final_code_reviewed = bool(reviews) and reviewed_round == len(rounds)
    if final == "done" and final_code_reviewed and any(
            i.get("severity") == "high" for i in reviews[-1].get("issues", [])):
        final = "review_concerns"  # high issues remain and no rounds were left to fix them
    base = checkpoint.get(root, first.get("checkpoint")) if first.get("checkpoint") else None
    changed = [p for _, p in checkpoint.changes(root, base)] if base else last.get("changed_files")
    credits = [r.get("worker_credits") for r in rounds + reviews if r.get("worker_credits") is not None]
    credits += [c for c in extras["credits"] if c is not None]
    summary = last.get("summary") or "; ".join(f"{t['name']}: {t.get('summary', '')}" for t in last.get("tasks", []))
    result = {
        "status": final,
        "stopped_because": None if stop == "done" else stop,
        "summary": summary or None,
        "rounds": [_round_brief(n, r) for n, r in enumerate(rounds, 1)],
        "tests": last.get("tests"),
        "changed_files": changed,
        "worker_credits": round(sum(credits), 2) if credits else None,
        "first_checkpoint": first.get("checkpoint"),
        "escalated_to_hard": escalated or None,
        "live_view_error": live_view_error,
    }
    if reviews:
        latest_review = reviews[-1]
        result["review"] = {k: v for k, v in {
            "verdict": latest_review.get("verdict"), "model": latest_review.get("model"),
            "issues": latest_review.get("issues") or None, "cycles": len(reviews),
            "note": latest_review.get("note")}.items() if v is not None}
        if latest_review.get("verdict") not in ("ok", "concerns"):
            result["review"]["unverified"] = "the reviewer returned no verdict (twice); the final code was not reviewed"
        elif not final_code_reviewed:
            result["review"]["unverified"] = "the issues above were sent back to the worker, but the final " \
                                             "code was not re-reviewed (auto_review_cycles or auto_max_rounds reached)"
    if test_review and not test_review.get("skipped"):
        result["test_review"] = _brief_test_review(test_review)
    if _brief_writer(extras["test_writer"]):
        result["test_writer"] = _brief_writer(extras["test_writer"])
    if args.parallel:
        result["parallel_tasks"] = [{k: v for k, v in t.items() if k in (
            "name", "status", "out_of_scope_files", "conflicts", "violations")} for t in first.get("tasks", [])]
    for key in ("test_change_request", "violations", "log_tail", "error", "model_fallback", "warning"):
        if last.get(key):
            result[key] = last[key]
    all_blocks = [b for r in rounds for b in (r.get("_runs") or [])]
    all_blocks += [b for rev in reviews for b in (rev.get("_runs") or [])]
    _write_delegation(server, _delegation_record(
        mode="auto",
        status=result["status"],
        stopped_because=result.get("stopped_because"),
        changed_files=changed,
        wall_seconds=round(time.time() - started),
        rounds=[e for r in rounds for e in _delegation_rounds(r, r.get("_runs") or [])],
        reviews=[e for rev in reviews for e in _delegation_reviews(rev, rev.get("_runs") or [])],
        test_writer=result.get("test_writer"),
        blocks=all_blocks,
    ))
    return {k: v for k, v in result.items() if v is not None}


# ---------------------------------------------------------------- commands

def _child_argv(argv):
    """The current command line minus the options that only matter to the launching process."""
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--background":
            continue
        elif arg == "--wait":
            skip = True
        elif not arg.startswith("--wait="):
            out.append(arg)
    return out


def cmd_run(root, server, args, raw):
    active = _active_run(root)
    if active and active["run_id"] != args.run_id:
        return {"status": "busy", "error": f"run {active['run_id']} is still in progress; use `wait`"}, 2
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    state = state_dir(root)

    if args.test_outline and not args.auto:
        return {"status": "bad_arguments", "error": "--test-outline needs --auto"}, 2
    if args.background or args.wait is not None:
        out = state / "logs" / f"runner-{run_id}.out"
        with open(out, "w") as fh:
            proc = subprocess.Popen([*RUNNER, *_child_argv(raw), "--run-id", run_id],
                                    cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        write_json(state / "current.json", {"run_id": run_id, "pid": proc.pid, "started": time.time(),
                                             "runner_output": str(out.relative_to(root))})
        if args.wait is not None:
            return cmd_wait(root, server, SimpleNamespace(timeout=args.wait))
        return {"status": "started", "run_id": run_id, "next": "wait --timeout 540"}, 0

    if not args.run_id:  # a background child's record was written by its parent
        write_json(state / "current.json", {"run_id": run_id, "pid": os.getpid(), "started": time.time()})
    try:
        if args.auto:
            result = autopilot(root, server, args, run_id)
        else:
            _set_delegation_id(server, run_id)
            if args.parallel:
                result = run_parallel(root, server, args)
                mode = "parallel"
            else:
                result = run_round(root, server, args)
                mode = "single"
            blocks = result.pop("_runs", []) or []
            _write_delegation(server, _delegation_record(
                mode=mode,
                status=result.get("status"),
                stopped_because=result.get("stopped_because"),
                changed_files=result.get("changed_files"),
                wall_seconds=result.get("seconds"),
                rounds=_delegation_rounds(result, blocks),
                reviews=[],
                test_writer=None,
                blocks=blocks,
            ))
    except Exception as exc:  # the result file must always be written, or `wait` reports a crash
        result = {"status": "runner_error", "error": f"{type(exc).__name__}: {exc}"}
    result["run_id"] = run_id
    write_json(state / "runs" / f"{run_id}.json", result)
    return result, STATUS_EXIT.get(result["status"], 2)


def cmd_wait(root, server, args):
    current = read_json(root / STATE_DIR / "current.json")
    if not current:
        return {"status": "no_run", "error": "no run has been started in this project"}, 2
    result_path = root / STATE_DIR / "runs" / f"{current['run_id']}.json"
    deadline = time.time() + args.timeout
    while True:
        result = read_json(result_path)
        if result:
            return result, STATUS_EXIT.get(result["status"], 2)
        if not _pid_alive(current["pid"]):
            time.sleep(1)
            result = read_json(result_path)
            if result:
                return result, STATUS_EXIT.get(result["status"], 2)
            out = root / current.get("runner_output", "")
            return {"status": "crashed", "run_id": current["run_id"],
                    "log_tail": tail(out.read_text(errors="ignore"), 30) if out.is_file() else None}, 2
        if time.time() >= deadline:
            return {"status": "running", "run_id": current["run_id"],
                    "elapsed_seconds": round(time.time() - current["started"]),
                    "next": "wait --timeout 540"}, 3
        time.sleep(2)


def cmd_detect(root, server, args):
    info = detect(root, server.settings.loop)
    info["protected_files"] = sorted(f for f in walk_files(root) if matches(f, info["test_globs"]))
    info["git"] = git_prefix(root) is not None
    return info, 0


def cmd_test(root, server, args):
    info = detect(root, server.settings.loop)
    if not info["test_cmd"]:
        return {"passed": False, "error": "no test command detected; set test_cmd in [loop]"}, 2
    verification = run_verification(info["test_cmd"], root, server.settings)
    result = {"passed": verification.passed, "exit_code": verification.exit_code,
              "cmd": verification.command, "counts": verification.counts}
    log_path = state_dir(root) / "logs" / f"test-{_stamp()}.log"
    log_path.write_text(verification.output)
    result["log"] = str(log_path.relative_to(root))
    if not verification.passed:
        result["output_tail"] = tail(verification.output)
    return result, 0 if result["passed"] else 1


def cmd_undo(root, server, args):
    if _active_run(root):
        return {"status": "busy", "error": "a run is in progress; wait for it first"}, 2
    if args.to:
        record = checkpoint.get(root, args.to)
    else:  # the last worker round, not a review's or an undo's own checkpoint
        record = next((r for r in reversed(checkpoint.list_all(root))
                       if r["label"].startswith(("before round", "before parallel"))), None)
    if not record:
        return {"status": "no_checkpoint", "error": "no matching checkpoint"}, 2
    safety = checkpoint.create(root, f"before undo to {record['id']}")
    undone = checkpoint.restore(root, record)
    return {"status": "undone", "restored_to": record, "changes_undone": [f"{s} {p}" for s, p in undone],
            "redo_checkpoint": safety["id"]}, 0


def cmd_checkpoints(root, server, args):
    return {"checkpoints": checkpoint.list_all(root)}, 0


REVIEW_PROMPT = """You are a code reviewer. Do NOT modify any file except `.subagent/review/result.json`.

The patch in `.subagent/review/diff.patch` contains all implementation changes made by another agent for
this task (tests excluded; the tests pass). You may read other files in the repository for context.

Report only issues that matter:
- security problems (injection, path traversal, unsafe deserialization, secrets, missing auth checks)
- clearly wrong behavior the tests might not catch (crashes on valid input, data loss, wrong results)
- code that games tests (hardcoded expected values, special-casing test inputs)
Do not report style, naming, or minor improvements.

Write `.subagent/review/result.json` exactly as:
{"verdict": "ok" | "concerns",
 "issues": [{"severity": "high" | "medium", "file": "path", "line": 123, "issue": "one sentence"}]}
Use "ok" with an empty list when there is nothing that matters.
"""

TEST_REVIEW_PROMPT = """You are checking a TEST SUITE before the code under test is written. Do NOT modify any
file except `.subagent/review/tests_result.json`.

The tests were written from the plan in `.subagent/review/plan.md`. Read it, and any spec file it
references (for example TASK.md). The test files are listed in `.subagent/review/test_files.txt`. The
code under test may not exist yet: do not run the tests.

Your main job is to find WRONG tests: assertions whose expected value, error type or input contradicts
the plan or spec. Go through the assertions one by one and work out each expected value yourself from
the spec (compute dates, weekdays, numbers, strings step by step). Also check the tests against each
other: two assertions that imply different rules for the same situation mean one of them is wrong.
Where the spec is silent, the standard behavior of the domain applies (e.g. how cron, HTTP, SQL work).
Report each wrong test with severity "high" and the correct expectation.

Secondary: requirements of the plan or spec with no test at all ("missing_test"), always severity
"medium", and only for behavior users would rely on. Do not report style or test organization.
Check each claim before reporting it: a false alarm costs a round.

Write `.subagent/review/tests_result.json` exactly as:
{"verdict": "ok" | "concerns",
 "issues": [{"severity": "high" | "medium", "kind": "wrong_test" | "missing_test", "file": "path",
             "line": 12, "issue": "one sentence, including the correct expectation"}]}
Use "ok" with an empty list when every assertion is right.
"""


def _retry_prompt(result_file):
    return (f"\nYour previous attempt did not produce a verdict. Write the JSON to `{result_file}` with your "
            "file tool; if you cannot, make your final message exactly that JSON object and nothing else.\n")


def _verdict_from_text(text):
    """A review JSON object found in the reviewer's last message, or None."""
    for match in re.finditer(r"\{.*\}", text or "", re.S):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("verdict") in ("ok", "concerns"):
            return data
    return None


def _review_target(settings, tier, explicit=None):
    target = settings.loop.review_hard if tier == "hard" else settings.loop.review
    return target.provider, explicit or target.model


def _run_reviewer(root, server, target, prompt, result_name, kind, detail):
    """Run a read-only reviewer; retry once without a verdict; undo any edits. Returns a result dict."""
    result_path = root / STATE_DIR / "review" / result_name
    result_path.unlink(missing_ok=True)
    cp = checkpoint.create(root, "before review")
    before = hash_tree(root)
    log = _new_log(root, "review")
    sink = LogSink(log)
    provider, model = target
    sink.write(f"=== subagent {kind} {time.strftime('%Y-%m-%d %H:%M:%S')} · {detail} · model {model or 'default'}")
    live = LiveLog(sink, log.with_suffix(".jsonl"), root)
    review, credits, attempts, used, errored, timed_out = {}, 0.0, 0, model, False, False
    review_runs = []
    for attempt in range(2):  # retry once if the reviewer produced no verdict
        attempts += 1
        text = prompt if attempt == 0 else prompt + _retry_prompt(f".subagent/review/{result_name}")
        live.credits, live.last_message = None, ""
        agent = server.registry.create_agent(
            None, root, provider=provider, model=model, fallback="none",
            on_event=live.feed, agent_id=f"loop-{uuid.uuid4()}",
        )
        agent.guard_context = {
            "protected": [],
            "state_allow": [f"{STATE_DIR}/review/result.json", f"{STATE_DIR}/review/tests_result.json"],
        }
        run = agent.delegate(text, "true", on_event=live.feed, distill=False)
        if not run.done.wait(agent.cfg.run_timeout):
            agent.close("run deadline exceeded", kind="timeout")
        run.done.wait()
        review_runs.append(run)
        credits += run.usage.credits or 0.0
        used = model
        errored = run.state == "failed"
        timed_out = run.finish_reason == "timeout"
        agent.close("review done")
        review = read_json(result_path) or _verdict_from_text(live.last_message) or {}
        if review.get("verdict") in ("ok", "concerns") or errored or timed_out:
            break
        sink.write("=== reviewer returned no verdict; asking once more")
    note = None
    if hash_tree(root) != before:
        checkpoint.restore(root, cp)
        note = "the reviewer modified files; they were restored"
    result = {
        "verdict": review.get("verdict") or ("error" if errored or timed_out else "no_report"),
        "issues": review.get("issues") or [],
        "model": used,
        "worker_credits": round(credits, 2) if credits else None,
        "note": note,
        "attempts": attempts if attempts > 1 else None,
        "_runs": [_run_block(r, model) for r in review_runs],
    }
    tail_credits = f" · AI credits {credits:.2f}" if credits else ""
    sink.write(f"{END_MARKER}: {kind} {result['verdict']} · {len(result['issues'])} issues{tail_credits}")
    live.close()
    sink.close()
    return result


def do_review(root, server, tier="normal", model=None, base_id=None):
    """Review everything changed since the task started (tests excluded) with a model of another family."""
    state = state_dir(root)
    session_data = _load_session(root)
    base = checkpoint.get(root, base_id or session_data.get("base_checkpoint"))
    if not base:
        return {"verdict": "error", "issues": [], "error": "no base checkpoint; pass --base ID"}
    info = detect(root, server.settings.loop)
    changes = [(s, p) for s, p in checkpoint.changes(root, base) if not matches(p, info["test_globs"])]
    if not changes:
        return {"verdict": "ok", "issues": [], "note": "no implementation changes since the base checkpoint"}

    parts = []
    for status, rel in changes:
        old = checkpoint.file_at(root, base, rel) if status != "A" else b""
        new = (root / rel).read_bytes() if status != "D" else b""
        if b"\0" in old[:8000] or b"\0" in new[:8000]:
            parts.append(f"Binary file {rel} ({status})\n")
            continue
        parts.extend(difflib.unified_diff(old.decode(errors="replace").splitlines(keepends=True),
                                          new.decode(errors="replace").splitlines(keepends=True),
                                          f"a/{rel}", f"b/{rel}"))
    patch = "".join(parts)
    limit = 150_000
    truncated = len(patch) > limit
    review_dir = state / "review"
    review_dir.mkdir(exist_ok=True)
    (review_dir / "diff.patch").write_text(patch[:limit] + ("\n... (truncated)\n" if truncated else ""))
    result = _run_reviewer(root, server, _review_target(server.settings, tier, model), REVIEW_PROMPT,
                           "result.json", "review", f"{len(changes)} files")
    result["reviewed_files"] = [p for _, p in changes]
    result["truncated_diff"] = truncated or None
    return {k: v for k, v in result.items() if v is not None}


def do_test_review(root, server, tier, plan_paths, model=None, force=False):
    """Check Claude's tests against the plan/spec before any worker round.

    Skipped (returns {"skipped": ...}) when the tests are unchanged since the last test review, so a
    rerun after Claude disagreed with the reviewer doesn't ask again.
    """
    state = state_dir(root)
    info = detect(root, server.settings.loop)
    test_files = sorted(f for f in walk_files(root) if matches(f, info["test_globs"]))
    if not test_files:
        return {"skipped": "no test files"}
    tests_hash = protected_hash(root, info["test_globs"])
    previous = read_json(state / "test_review.json") or {}
    if not force and previous.get("tests_hash") == tests_hash:
        return {"skipped": "tests unchanged since the last test review"}
    _write_review_plan(root, plan_paths)
    (state / "review" / "test_files.txt").write_text("\n".join(test_files) + "\n")
    result = _run_reviewer(root, server, _review_target(server.settings, tier, model), TEST_REVIEW_PROMPT,
                           "tests_result.json", "test review", f"{len(test_files)} test files")
    result["test_files"] = test_files
    if result["verdict"] in ("ok", "concerns"):
        write_json(state / "test_review.json", {"tests_hash": tests_hash, "verdict": result["verdict"]})
    return {k: v for k, v in result.items() if v is not None}


def cmd_review(root, server, args):
    if _active_run(root):
        return {"status": "busy", "error": "a run is in progress; wait for it first"}, 2
    if args.tests:
        result = do_test_review(root, server, args.tier, [args.plan], args.model, force=True)
    else:
        result = do_review(root, server, args.tier, args.model, args.base)
    result.pop("_runs", None)
    return result, 0 if result.get("verdict") == "ok" else 1


def _hold(args):
    if args.hold:
        try:
            input("\nRun finished. Press Enter to close.")
        except EOFError:
            pass


def cmd_watch(root, server, args):
    latest = root / STATE_DIR / "logs" / "latest.log"
    if args.log:
        follow(Path(args.log), stop_at_end=True)
        _hold(args)
        return None, 0
    if args.run:
        # Follow every log of one (autopilot) run in order, round after round, until its result is written.
        done = root / STATE_DIR / "runs" / f"{args.run}.json"
        current = read_json(root / STATE_DIR / "current.json") or {}
        since = args.since or (current.get("started", 0) if current.get("run_id") == args.run else 0)
        seen = set()
        while True:
            fresh = sorted((p for p in latest.parent.glob("*.log")
                            if p.name != "latest.log" and not p.name.startswith("test-")
                            and p not in seen and p.stat().st_mtime >= since),
                           key=lambda p: p.stem.split("-", 1)[1])
            if fresh:
                seen.add(fresh[0])
                follow(fresh[0], stop_at_end=True, stop_when=done.exists)
            elif done.exists():
                break
            else:
                time.sleep(0.3)
        _hold(args)
        return None, 0
    if not latest.exists():
        print(f"Waiting for a subagent run in {root} ... (Ctrl-C to stop)", flush=True)
        while not latest.exists():
            time.sleep(0.5)
    try:
        while True:
            follow(latest.resolve(), stop_at_end=False, latest=latest)
    except KeyboardInterrupt:
        return None, 0


LOOP_COMMANDS = {
    "detect": cmd_detect,
    "run": cmd_run,
    "wait": cmd_wait,
    "test": cmd_test,
    "review": cmd_review,
    "undo": cmd_undo,
    "checkpoints": cmd_checkpoints,
    "watch": cmd_watch,
}
