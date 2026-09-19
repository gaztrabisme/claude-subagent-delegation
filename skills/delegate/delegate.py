#!/usr/bin/env python3
"""Delegate implementation work to a cheaper coding agent (Copilot CLI by default).

Claude writes the plan + tests; the worker implements; the worker may run tests but may NOT change
them. Every round is checkpointed (undo-able), test files and test config are guarded, and the runner
re-runs the test suite itself after the worker finishes.

Subcommands (all print one JSON object to stdout):
  detect                           detected test command, protected files, models
  run --plan FILE                  one worker round on a plan
      [--continue] [--feedback T]    reuse the worker session for a retry
      [--tier normal|hard]           complexity tier -> model (config "models")
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

import argparse
import difflib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checkpoint  # noqa: E402
from common import (DEPENDENCY_DIRS, STATE_DIR, git_prefix, hash_tree, load_config, matches,  # noqa: E402
                    read_json, state_dir, tail, walk_files, write_json)
from detect import detect, run_tests  # noqa: E402
from guard import TestGuard, protected_hash  # noqa: E402
from livelog import END_MARKER, LiveLog, LogSink, follow, open_live_view, run_backend  # noqa: E402

SCRIPT = Path(__file__).resolve()
STATUS_EXIT = {"done": 0, "running": 3, "started": 0}


# ---------------------------------------------------------------- backends and prompts

def resolve_models(cfg, tier, explicit):
    """Models to try in order: the chosen one, then the normal-tier model as fallback for "hard"."""
    if explicit:
        return [explicit]
    if cfg.get("model"):
        return [cfg["model"]]
    models = cfg.get("models") or {}
    chosen = models.get(tier)
    fallback = models.get("normal")
    candidates = [m for m in (chosen, fallback if tier == "hard" else None) if m]
    return list(dict.fromkeys(candidates)) or [None]


def backend_argv(cfg, prompt, cwd, session_id, model):
    backend = cfg["backend"]
    if backend == "copilot":
        argv = ["copilot", "-p", prompt, "--allow-all-tools", "--output-format", "json", "-C", str(cwd),
                "--session-id", session_id]
        if not cfg.get("builtin_mcps"):
            argv.append("--disable-builtin-mcps")
        if model:
            argv += ["--model", model]
        return argv + list(cfg.get("extra_args") or [])
    if backend == "command":
        if not cfg.get("command"):
            raise SystemExit('backend "command" needs "command": [argv...] in config')
        return list(cfg["command"])
    raise SystemExit(f"unknown backend: {backend}")


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
   Any change to them is automatically reverted and your run is marked as a violation.
3. If you believe a test is wrong (contradicts the plan, or has a bug), do NOT work around it.
   Write `.delegate/test_change_request.md` with: the test file and test name, why it is wrong,
   and the exact change you propose. Then finish with status "needs_test_change".
4. Do not game the tests (no hardcoding expected outputs, no special-casing test inputs).
5. When you finish, write `.delegate/result.json` containing exactly:
   {{"status": "done" | "failed" | "needs_test_change", "summary": "<one or two sentences>"}}
   Use "done" only if the tests for your work pass.
"""


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
    return read_json(root / STATE_DIR / "session.json") or {}


def _save_session(root, session):
    write_json(root / STATE_DIR / "session.json", session)


def _check_test_counts(session, tests_hash, tests):
    """Flag a passing suite that runs fewer tests (or skips more) than seen before for the same tests."""
    if session.get("tests_hash") != tests_hash:
        session["tests_hash"], session["best_counts"] = tests_hash, None
    counts = (tests or {}).get("counts")
    if not tests or not counts or counts.get("total") is None:
        return None
    best = session.get("best_counts")
    problem = None
    if tests["passed"] and best:
        if counts["total"] < best["total"]:
            problem = f"the suite passed but ran {counts['total']} tests, fewer than the {best['total']} seen before"
        elif counts["skipped"] > best["skipped"]:
            problem = f"the suite passed but skipped {counts['skipped']} tests, more than the {best['skipped']} before"
    if not best or counts["total"] > best["total"] or (
            counts["total"] == best["total"] and counts["skipped"] < best["skipped"]):
        session["best_counts"] = {"total": counts["total"], "skipped": counts["skipped"]}
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


def _run_worker(cfg, prompt, cwd, session_id, candidates, live, env):
    """Run the worker with model fallback. Returns (exit_code, timed_out, model, fallback_note)."""
    fallback_note, exit_code, timed_out, model = None, None, False, None
    for attempt, model in enumerate(candidates):
        live.write(f"=== model: {model or 'default'}")
        exit_code, timed_out = run_backend(backend_argv(cfg, prompt, cwd, session_id, model), cwd, env,
                                           cfg["timeout"], live)
        if timed_out or exit_code == 0 or attempt == len(candidates) - 1:
            break
        fallback_note = f"{model} failed (exit {exit_code}); retried with {candidates[attempt + 1]}"
        live.write(f"=== {fallback_note}")
    return exit_code, timed_out, model, fallback_note


def _status(violations, timed_out, exit_code, report, tests):
    if violations:
        return "violated_tests"
    if timed_out:
        return "timeout"
    if exit_code != 0:
        return "backend_error"
    if report.get("status") == "needs_test_change":
        return "needs_test_change"
    if tests is not None:
        return "done" if tests["passed"] else "tests_failed"
    return report.get("status") if report.get("status") in ("done", "failed") else "no_report"


# ---------------------------------------------------------------- run: one round

def run_round(root, cfg, args, live_view=True):
    state = state_dir(root)
    info = detect(root, cfg)
    plan = Path(args.plan).read_text()
    feedback = _feedback(args)

    session = _load_session(root) if args.continue_session else {}
    session.setdefault("session_id", str(uuid.uuid4()))
    cp = checkpoint.create(root, f"before round ({'retry' if args.continue_session else 'new task'})")
    session.setdefault("base_checkpoint", cp["id"])

    for leftover in ("result.json", "test_change_request.md"):
        (state / leftover).unlink(missing_ok=True)

    log = _new_log(root, "run")
    sink = LogSink(log)
    sink.write(f"=== delegate run {time.strftime('%Y-%m-%d %H:%M:%S')} · tier {args.tier} · "
               f"{'continue' if args.continue_session else 'new'} session · checkpoint {cp['id']}")
    live_view_error = open_live_view(cfg, root, SCRIPT, ["--log", log]) if live_view else None
    live = LiveLog(sink, log.with_suffix(".jsonl"), root)

    guard = TestGuard(root, info["test_globs"])
    guard.lock()
    prompt = build_prompt(plan, feedback, info, guard.protected)
    env = {**os.environ, "DELEGATE_PROMPT": prompt, "DELEGATE_ROOT": str(root)}
    started = time.time()
    try:
        exit_code, timed_out, model, fallback_note = _run_worker(
            cfg, prompt, root, session["session_id"], resolve_models(cfg, args.tier, args.model), live, env)
    finally:
        violations, changed = guard.release()

    tests = None
    if info["test_cmd"] and not timed_out and exit_code == 0:
        sink.write("=== runner: running the test suite")
        tests = run_tests(root, info["test_cmd"], state / "logs", "test")
    count_problem = _check_test_counts(session, guard.protected_hash(), tests) if cfg.get("count_tests") else None
    if count_problem:
        violations.append({"file": "(test run)", "change": count_problem})

    report = read_json(state / "result.json") or {}
    status = _status(violations, timed_out, exit_code, report, tests)
    result = {
        "status": status,
        "summary": report.get("summary"),
        "worker_reported": report.get("status"),
        "changed_files": changed,
        "tests": _tests_summary(tests),
        "tier": args.tier,
        "model": model,
        "seconds": round(time.time() - started),
        "checkpoint": cp["id"],
        "log": str(log.relative_to(root)),
    }
    if live.credits is not None:
        result["worker_credits"] = round(live.credits, 2)
    if live_view_error:
        result["live_view_error"] = live_view_error
    if fallback_note:
        result["model_fallback"] = fallback_note
    if violations:
        result["violations"] = violations
    request = state / "test_change_request.md"
    if request.exists():
        result["test_change_request"] = request.read_text()[:4000]
    if status == "backend_error":
        result["log_tail"] = tail(log.read_text(errors="ignore"), 30)
    if not changed and status == "done":
        result["warning"] = "tests pass but the worker changed no files"

    credits = f" · AI credits {live.credits:.2f}" if live.credits is not None else ""
    sink.write(f"{END_MARKER}: {status} · {result['seconds']}s · {len(changed)} files changed{credits}")
    live.close()
    sink.close()
    _save_session(root, session)
    checkpoint.prune(root, cfg.get("keep_checkpoints") or 0)
    return {k: v for k, v in result.items() if v is not None}


# ---------------------------------------------------------------- run: parallel round

def run_parallel(root, cfg, args, live_view=True):
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
    info = detect(root, cfg)
    feedback = _feedback(args)
    session = _load_session(root) if args.continue_session else {}
    session.setdefault("parallel", {})
    cp = checkpoint.create(root, f"before parallel round ({', '.join(t['name'] for t in tasks)})")
    session.setdefault("base_checkpoint", cp["id"])

    log = _new_log(root, "parallel")
    sink = LogSink(log)
    sink.write(f"=== delegate parallel run {time.strftime('%Y-%m-%d %H:%M:%S')} · "
               f"{len(tasks)} workers · checkpoint {cp['id']}")
    live_view_error = open_live_view(cfg, root, SCRIPT, ["--log", log]) if live_view else None

    workers = []
    try:
        return _parallel_work(root, cfg, args, tasks, info, feedback, session, cp, log, sink, live_view_error,
                              workers)
    except BaseException:
        for w in workers:
            if not w.get("released"):
                w["guard"].release()
            checkpoint.worktree_remove(root, w["worktree"])
        sink.close()
        raise


def _parallel_work(root, cfg, args, tasks, info, feedback, session, cp, log, sink, live_view_error, workers):
    state = root / STATE_DIR
    for task in tasks:
        worktree, proj = checkpoint.worktree_add(root, cp)
        for dep in DEPENDENCY_DIRS:
            if (root / dep).exists() and not (proj / dep).exists():
                (proj / dep).symlink_to(root / dep)
        guard = TestGuard(proj, info["test_globs"])
        guard.lock()
        session_id = session["parallel"].get(task["name"]) if args.continue_session else None
        session["parallel"][task["name"]] = session_id or str(uuid.uuid4())
        others = [{"name": t["name"], "files": t["files"]} for t in tasks if t is not task]
        prompt = build_prompt(Path(task["plan"]).read_text(), task.get("feedback") or feedback, info,
                              guard.protected, {"name": task["name"], "files": task["files"],
                                                "others": others, "test_cmd": task.get("test_cmd")})
        workers.append({"task": task, "worktree": worktree, "proj": proj, "guard": guard, "prompt": prompt,
                        "session_id": session["parallel"][task["name"]],
                        "live": LiveLog(sink, log.with_name(f"{log.stem}-{task['name']}.jsonl"), proj, task["name"])})

    def work(w):
        env = {**os.environ, "DELEGATE_PROMPT": w["prompt"], "DELEGATE_ROOT": str(w["proj"])}
        tier = w["task"].get("tier", args.tier)
        w["outcome"] = _run_worker(cfg, w["prompt"], w["proj"], w["session_id"],
                                   resolve_models(cfg, tier, args.model), w["live"], env)

    started = time.time()
    threads = [threading.Thread(target=work, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    applied, results, all_violations = {}, [], []
    for w in workers:
        task, proj = w["task"], w["proj"]
        violations, changed = w["guard"].release()
        w["released"] = True
        exit_code, timed_out, model, fallback_note = w["outcome"]
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
        entry = {
            "name": task["name"],
            "status": _status(violations, timed_out, exit_code, report, None),
            "summary": report.get("summary"),
            "model": model,
            "applied_files": [f for f in owned if f not in conflicts],
            "out_of_scope_files": out_of_scope or None,
            "conflicts": [f"{f} (already changed by {applied[f]})" for f in conflicts] or None,
            "violations": violations or None,
            "model_fallback": fallback_note,
            "worker_credits": round(w["live"].credits, 2) if w["live"].credits is not None else None,
            "test_change_request": request.read_text()[:4000] if request.exists() else None,
        }
        results.append({k: v for k, v in entry.items() if v is not None})
        all_violations += violations
        w["live"].close()
        checkpoint.worktree_remove(root, w["worktree"])

    tests = None
    if info["test_cmd"]:
        sink.write("=== runner: running the full test suite on the merged result")
        tests = run_tests(root, info["test_cmd"], state / "logs", "test")
    count_problem = (_check_test_counts(session, protected_hash(root, info["test_globs"]), tests)
                     if cfg.get("count_tests") else None)

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
    _save_session(root, session)
    checkpoint.prune(root, cfg.get("keep_checkpoints") or 0)
    return {k: v for k, v in result.items() if v is not None}


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


def _brief_test_review(review):
    return {k: v for k, v in {"verdict": review.get("verdict"), "model": review.get("model"),
                              "issues": review.get("issues") or None, "note": review.get("note")}.items()
            if v is not None}


def _review_feedback(issues):
    lines = [f"- {i.get('file', '?')}:{i.get('line', '?')}: {i.get('issue', '')}" for i in issues]
    return ("An independent code review found these high-severity problems. Fix them and keep the whole "
            "test suite passing:\n" + "\n".join(lines))


def autopilot(root, cfg, args, run_id):
    """Run rounds until the tests pass and the review has no high-severity issues, or until stuck.

    Failing tests and high-severity review issues go back to the worker automatically, so Claude only
    sees the final result (or a problem only Claude can solve, like a disputed test).
    """
    live_view_error = open_live_view(cfg, root, SCRIPT, ["--run", run_id, "--since", time.time()])

    # Check Claude's tests against the plan before spending worker rounds on them.
    test_review = None
    if cfg.get("review_tests"):
        plans = [args.plan] if args.plan else [t.get("plan") for t in (read_json(args.parallel) or {}).get("tasks", [])
                                               if t.get("plan")]
        test_review = do_test_review(root, cfg, args.tier, plans)
        # Only wrong tests block (they would waste worker rounds); missing tests are reported only.
        high = [i for i in test_review.get("issues", [])
                if i.get("severity") == "high" and i.get("kind", "wrong_test") == "wrong_test"]
        if high:
            return {k: v for k, v in {
                "status": "tests_questioned",
                "summary": f"The test review found {len(high)} wrong test(s); no worker round was run.",
                "test_review": _brief_test_review(test_review),
                "worker_credits": test_review.get("worker_credits"),
                "live_view_error": live_view_error}.items() if v is not None}

    first = run_parallel(root, cfg, args, live_view=False) if args.parallel else run_round(root, cfg, args, False)
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
            if not cfg.get("auto_review") or len(reviews) >= cfg["auto_review_cycles"]:
                stop = "done"
                break
            review = do_review(root, cfg, tier, model=None)
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
                        "If a test is wrong, use .delegate/test_change_request.md.")
        else:  # tests_failed, failed, no_report, timeout, partial
            failed_in_row += 1
            output = (last.get("tests") or {}).get("output_tail")
            feedback = (f"The test suite fails:\n{output}" if output else
                        f"The previous round ended with status {status}: {last.get('summary') or 'no summary'}. "
                        "Finish the task and make the tests pass.")
        if len(rounds) >= cfg["auto_max_rounds"]:
            stop = "max_rounds"
            break
        if tier == "normal" and failed_in_row >= 2 and not args.model:
            tier, escalated = "hard", True
        next_args = SimpleNamespace(plan=fix_plan, feedback=feedback, feedback_file=None, continue_session=True,
                                    tier=tier, model=args.model)
        rounds.append(run_round(root, cfg, next_args, live_view=False))

    last = rounds[-1]
    final = last["status"]
    final_code_reviewed = bool(reviews) and reviewed_round == len(rounds)
    if final == "done" and final_code_reviewed and any(
            i.get("severity") == "high" for i in reviews[-1].get("issues", [])):
        final = "review_concerns"  # high issues remain and no rounds were left to fix them
    base = checkpoint.get(root, first.get("checkpoint")) if first.get("checkpoint") else None
    changed = [p for _, p in checkpoint.changes(root, base)] if base else last.get("changed_files")
    credits = [r.get("worker_credits") for r in rounds + reviews + [test_review or {}]
               if r.get("worker_credits") is not None]
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
    if args.parallel:
        result["parallel_tasks"] = [{k: v for k, v in t.items() if k in (
            "name", "status", "out_of_scope_files", "conflicts", "violations")} for t in first.get("tasks", [])]
    for key in ("test_change_request", "violations", "log_tail", "error", "model_fallback", "warning"):
        if last.get(key):
            result[key] = last[key]
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


def cmd_run(root, cfg, args):
    active = _active_run(root)
    if active and active["run_id"] != args.run_id:
        return {"status": "busy", "error": f"run {active['run_id']} is still in progress; use `wait`"}, 2
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    state = state_dir(root)

    if args.background or args.wait is not None:
        out = state / "logs" / f"runner-{run_id}.out"
        with open(out, "w") as fh:
            proc = subprocess.Popen([sys.executable, str(SCRIPT), *_child_argv(sys.argv[1:]), "--run-id", run_id],
                                    cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        write_json(state / "current.json", {"run_id": run_id, "pid": proc.pid, "started": time.time(),
                                             "runner_output": str(out.relative_to(root))})
        if args.wait is not None:
            return cmd_wait(root, cfg, SimpleNamespace(timeout=args.wait))
        return {"status": "started", "run_id": run_id, "next": "wait --timeout 540"}, 0

    if not args.run_id:  # a background child's record was written by its parent
        write_json(state / "current.json", {"run_id": run_id, "pid": os.getpid(), "started": time.time()})
    try:
        if args.auto:
            result = autopilot(root, cfg, args, run_id)
        else:
            result = run_parallel(root, cfg, args) if args.parallel else run_round(root, cfg, args)
    except Exception as exc:  # the result file must always be written, or `wait` reports a crash
        result = {"status": "runner_error", "error": f"{type(exc).__name__}: {exc}"}
    result["run_id"] = run_id
    write_json(state / "runs" / f"{run_id}.json", result)
    return result, STATUS_EXIT.get(result["status"], 2)


def cmd_wait(root, cfg, args):
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


def cmd_detect(root, cfg, _args):
    info = detect(root, cfg)
    info["protected_files"] = sorted(f for f in walk_files(root) if matches(f, info["test_globs"]))
    info["backend"] = cfg["backend"]
    info["models"] = {"pinned": cfg["model"]} if cfg.get("model") else cfg.get("models")
    info["git"] = git_prefix(root) is not None
    return info, 0


def cmd_test(root, cfg, _args):
    info = detect(root, cfg)
    if not info["test_cmd"]:
        return {"passed": False, "error": "no test command detected; set test_cmd in .delegate/config.json"}, 2
    result = run_tests(root, info["test_cmd"], state_dir(root) / "logs", "test")
    return result, 0 if result["passed"] else 1


def cmd_undo(root, cfg, args):
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


def cmd_checkpoints(root, cfg, _args):
    return {"checkpoints": checkpoint.list_all(root)}, 0


REVIEW_PROMPT = """You are a code reviewer. Do NOT modify any file except `.delegate/review/result.json`.

The patch in `.delegate/review/diff.patch` contains all implementation changes made by another agent for
this task (tests excluded; the tests pass). You may read other files in the repository for context.

Report only issues that matter:
- security problems (injection, path traversal, unsafe deserialization, secrets, missing auth checks)
- clearly wrong behavior the tests might not catch (crashes on valid input, data loss, wrong results)
- code that games tests (hardcoded expected values, special-casing test inputs)
Do not report style, naming, or minor improvements.

Write `.delegate/review/result.json` exactly as:
{"verdict": "ok" | "concerns",
 "issues": [{"severity": "high" | "medium", "file": "path", "line": 123, "issue": "one sentence"}]}
Use "ok" with an empty list when there is nothing that matters.
"""

TEST_REVIEW_PROMPT = """You are checking a TEST SUITE before the code under test is written. Do NOT modify any
file except `.delegate/review/tests_result.json`.

The tests were written from the plan in `.delegate/review/plan.md`. Read it, and any spec file it
references (for example TASK.md). The test files are listed in `.delegate/review/test_files.txt`. The
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

Write `.delegate/review/tests_result.json` exactly as:
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


def _review_model(cfg, tier, explicit=None):
    return explicit or cfg.get("review_model") or (cfg.get("review_models") or {}).get(tier) or "gpt-5.6-sol"


def _run_reviewer(root, cfg, reviewer, prompt, result_name, header):
    """Run a read-only reviewer; retry once without a verdict; undo any edits. Returns a result dict."""
    result_path = root / STATE_DIR / "review" / result_name
    result_path.unlink(missing_ok=True)
    cp = checkpoint.create(root, "before review")
    before = hash_tree(root)
    log = _new_log(root, "review")
    sink = LogSink(log)
    sink.write(f"=== delegate {header} {time.strftime('%Y-%m-%d %H:%M:%S')} · model {reviewer}")
    live = LiveLog(sink, log.with_suffix(".jsonl"), root)
    review, credits, attempts, exit_code, timed_out, used = {}, 0.0, 0, None, False, reviewer
    for attempt in range(2):  # retry once if the reviewer produced no verdict
        attempts += 1
        text = prompt if attempt == 0 else prompt + _retry_prompt(f".delegate/review/{result_name}")
        env = {**os.environ, "DELEGATE_PROMPT": text, "DELEGATE_ROOT": str(root)}
        live.credits, live.last_message = None, ""
        exit_code, timed_out, used, _ = _run_worker(cfg, text, root, str(uuid.uuid4()), [reviewer], live, env)
        credits += live.credits or 0.0
        review = read_json(result_path) or _verdict_from_text(live.last_message) or {}
        if review.get("verdict") in ("ok", "concerns") or exit_code != 0 or timed_out:
            break
        sink.write("=== reviewer returned no verdict; asking once more")
    note = None
    if hash_tree(root) != before:
        checkpoint.restore(root, cp)
        note = "the reviewer modified files; they were restored"
    result = {
        "verdict": review.get("verdict") or ("error" if exit_code != 0 or timed_out else "no_report"),
        "issues": review.get("issues") or [],
        "model": used,
        "worker_credits": round(credits, 2) if credits else None,
        "note": note,
        "attempts": attempts if attempts > 1 else None,
    }
    tail_credits = f" · AI credits {credits:.2f}" if credits else ""
    sink.write(f"{END_MARKER}: {header} {result['verdict']} · {len(result['issues'])} issues{tail_credits}")
    live.close()
    sink.close()
    return result


def do_review(root, cfg, tier="normal", model=None, base_id=None):
    """Review everything changed since the task started (tests excluded) with a model of another family."""
    state = state_dir(root)
    session = _load_session(root)
    base = checkpoint.get(root, base_id or session.get("base_checkpoint"))
    if not base:
        return {"verdict": "error", "issues": [], "error": "no base checkpoint; pass --base ID"}
    info = detect(root, cfg)
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
    result = _run_reviewer(root, cfg, _review_model(cfg, tier, model), REVIEW_PROMPT, "result.json",
                           f"review ({len(changes)} files)")
    result["reviewed_files"] = [p for _, p in changes]
    result["truncated_diff"] = truncated or None
    return {k: v for k, v in result.items() if v is not None}


def do_test_review(root, cfg, tier, plan_paths, model=None, force=False):
    """Check Claude's tests against the plan/spec before any worker round.

    Skipped (returns {"skipped": ...}) when the tests are unchanged since the last test review, so a
    rerun after Claude disagreed with the reviewer doesn't ask again.
    """
    state = state_dir(root)
    info = detect(root, cfg)
    test_files = sorted(f for f in walk_files(root) if matches(f, info["test_globs"]))
    if not test_files:
        return {"skipped": "no test files"}
    tests_hash = protected_hash(root, info["test_globs"])
    previous = read_json(state / "test_review.json") or {}
    if not force and previous.get("tests_hash") == tests_hash:
        return {"skipped": "tests unchanged since the last test review"}
    plans = []
    for path in plan_paths:
        try:
            plans.append(f"<!-- {path} -->\n{Path(path).read_text()}")
        except OSError:
            continue
    review_dir = state / "review"
    review_dir.mkdir(exist_ok=True)
    (review_dir / "plan.md").write_text("\n\n".join(plans) or "(no plan file; use the spec files in the repository)")
    (review_dir / "test_files.txt").write_text("\n".join(test_files) + "\n")
    result = _run_reviewer(root, cfg, _review_model(cfg, tier, model), TEST_REVIEW_PROMPT, "tests_result.json",
                           f"test review ({len(test_files)} files)")
    result["test_files"] = test_files
    if result["verdict"] in ("ok", "concerns"):
        write_json(state / "test_review.json", {"tests_hash": tests_hash, "verdict": result["verdict"]})
    return {k: v for k, v in result.items() if v is not None}


def cmd_review(root, cfg, args):
    if _active_run(root):
        return {"status": "busy", "error": "a run is in progress; wait for it first"}, 2
    if args.tests:
        result = do_test_review(root, cfg, args.tier, [args.plan], args.model, force=True)
    else:
        result = do_review(root, cfg, args.tier, args.model, args.base)
    return result, 0 if result.get("verdict") == "ok" else 1


def _hold(args):
    if args.hold:
        try:
            input("\nRun finished. Press Enter to close.")
        except EOFError:
            pass


def cmd_watch(root, cfg, args):
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
        print(f"Waiting for a delegate run in {root} ... (Ctrl-C to stop)", flush=True)
        while not latest.exists():
            time.sleep(0.5)
    try:
        while True:
            follow(latest.resolve(), stop_at_end=False, latest=latest)
    except KeyboardInterrupt:
        return None, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="project root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect")
    sub.add_parser("test")
    sub.add_parser("checkpoints")
    run = sub.add_parser("run")
    what = run.add_mutually_exclusive_group(required=True)
    what.add_argument("--plan", help="path to the plan markdown file")
    what.add_argument("--parallel", help="path to a manifest of parallel tasks (see SKILL.md)")
    run.add_argument("--feedback", help="feedback text for a retry (failing tests, decisions)")
    run.add_argument("--feedback-file", help="read feedback from a file (\"-\" = stdin); safest for test output")
    run.add_argument("--continue", dest="continue_session", action="store_true",
                     help="continue the previous worker session(s) instead of starting fresh")
    run.add_argument("--tier", choices=["normal", "hard"], default="normal",
                     help="task complexity; picks the model from config \"models\"")
    run.add_argument("--model", help="explicit model, overrides --tier and config")
    run.add_argument("--auto", action="store_true",
                     help="autopilot: retry failing tests and fix high-severity review issues automatically")
    run.add_argument("--background", action="store_true", help="start the run and return immediately")
    run.add_argument("--wait", type=int, metavar="S",
                     help="start in the background and wait up to S seconds for the result in this call")
    run.add_argument("--run-id", help=argparse.SUPPRESS)
    wait = sub.add_parser("wait")
    wait.add_argument("--timeout", type=int, default=540, help="seconds to wait before returning 'running'")
    review = sub.add_parser("review")
    review.add_argument("--base", help="checkpoint to diff against (default: start of the current task)")
    review.add_argument("--tier", choices=["normal", "hard"], default="normal",
                        help="picks the reviewer from config \"review_models\"")
    review.add_argument("--model", help="explicit reviewer model")
    review.add_argument("--tests", action="store_true",
                        help="review the test files against the plan instead of the code")
    review.add_argument("--plan", default=".delegate/PLAN.md", help="plan for --tests (default .delegate/PLAN.md)")
    undo = sub.add_parser("undo")
    undo.add_argument("--to", help="checkpoint id or prefix (default: the one before the last worker round)")
    watch = sub.add_parser("watch", help="follow the worker's live log")
    watch.add_argument("--log", help="follow this log file until its run ends (default: latest, forever)")
    watch.add_argument("--run", help="follow all logs of this run (autopilot) until it finishes")
    watch.add_argument("--since", type=float, help=argparse.SUPPRESS)
    watch.add_argument("--hold", action="store_true", help="wait for Enter after the run ends")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    cfg = load_config(root)
    handler = {"detect": cmd_detect, "test": cmd_test, "run": cmd_run, "wait": cmd_wait, "review": cmd_review,
               "undo": cmd_undo, "checkpoints": cmd_checkpoints, "watch": cmd_watch}[args.command]
    result, code = handler(root, cfg, args)
    if result is not None:
        print(json.dumps(result, indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
