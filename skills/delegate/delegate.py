#!/usr/bin/env python3
"""Delegate implementation work to a cheaper coding agent (Copilot CLI by default).

Claude writes the plan + tests; the worker implements; the worker may run tests but
may NOT change them. Test files are snapshotted before each run and any change is
reverted and reported as a violation.

Subcommands (all print one JSON object to stdout):
  detect                          show detected test command / protected test globs
  run --plan FILE [--feedback T]  delegate the plan to the worker, return its status
      [--continue]                  reuse the previous worker session (for retries)
      [--tier normal|hard]          complexity tier -> model (config "models")
  test                            run the test command, return pass/fail + output tail
"""

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

STATE_DIR = ".delegate"
SKIP_DIRS = {
    ".git", ".hg", ".svn", STATE_DIR, "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", ".next", ".nuxt", "coverage", ".turbo",
}
DEFAULT_CONFIG = {
    "backend": "copilot",
    "models": {             # model per complexity tier; Claude picks the tier with --tier
        "normal": "claude-sonnet-5",
        "hard": "claude-opus-5",
    },
    "model": None,          # pin one model for every tier (disables tier selection)
    "timeout": 1800,        # seconds per worker run
    "extra_args": [],       # extra CLI args for the backend
    "command": None,        # argv for the "command" backend; prompt in $DELEGATE_PROMPT
    "test_cmd": None,       # override detection, e.g. "npm run test:unit"
    "test_globs": None,     # override detection, e.g. ["tests/**"]
    "extra_protected": [],  # added on top of detected globs
}
TAIL_LINES = 60


# ---------------------------------------------------------------- detection

def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _detect_js(root):
    pkg = _read_json(root / "package.json")
    if not pkg:
        return None
    script = (pkg.get("scripts") or {}).get("test", "")
    if not script or "no test specified" in script:
        return None
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    framework = next((f for f in ("vitest", "jest", "mocha", "ava") if f in deps), None)
    if framework is None:
        framework = "node:test" if "node --test" in script else "npm-script"
    if (root / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"
    elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        pm = "bun"
    else:
        pm = "npm"
    return {
        "framework": framework,
        "test_cmd": f"{pm} test",
        "test_globs": [
            "**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/test/**", "**/tests/**",
            "**/jest.config.*", "**/jest.setup.*", "**/vitest.config.*", "**/vitest.setup.*",
            "**/.mocharc*",
        ],
    }


def _detect_python(root):
    pyproject = root / "pyproject.toml"
    setup_cfg = root / "setup.cfg"
    signals = [
        pyproject.exists() and "pytest" in pyproject.read_text(errors="ignore"),
        (root / "pytest.ini").exists(),
        (root / "conftest.py").exists(),
        setup_cfg.exists() and "tool:pytest" in setup_cfg.read_text(errors="ignore"),
        any(root.glob("test_*.py")),
        any(root.glob("tests/**/test_*.py")) or any(root.glob("tests/**/*_test.py")),
    ]
    if not any(signals):
        return None
    if (root / "uv.lock").exists():
        runner = "uv run pytest"
    elif (root / "poetry.lock").exists():
        runner = "poetry run pytest"
    elif (root / ".venv/bin/pytest").exists():
        runner = ".venv/bin/pytest"
    else:
        runner = "python3 -m pytest"
    return {
        "framework": "pytest",
        "test_cmd": f"{runner} -q",
        "test_globs": ["**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "pytest.ini"],
    }


def _detect_go(root):
    if not (root / "go.mod").exists():
        return None
    return {"framework": "go test", "test_cmd": "go test ./...",
            "test_globs": ["**/*_test.go", "**/testdata/**"]}


def _detect_rust(root):
    if not (root / "Cargo.toml").exists():
        return None
    # Inline #[cfg(test)] modules live in src/ and cannot be protected by path.
    return {"framework": "cargo test", "test_cmd": "cargo test", "test_globs": ["**/tests/**"]}


def _detect_make(root):
    makefile = root / "Makefile"
    if makefile.exists() and any(l.startswith("test:") for l in makefile.read_text(errors="ignore").splitlines()):
        return {"framework": "make", "test_cmd": "make test", "test_globs": ["**/tests/**", "**/test/**"]}
    return None


DETECTORS = [_detect_js, _detect_python, _detect_go, _detect_rust, _detect_make]


def load_config(root):
    cfg = dict(DEFAULT_CONFIG)
    user = _read_json(root / STATE_DIR / "config.json")
    if user:
        cfg.update(user)
    return cfg


def detect(root, cfg):
    info = {"framework": None, "test_cmd": None, "test_globs": []}
    for detector in DETECTORS:
        found = detector(root)
        if found:
            info = found
            break
    if cfg.get("test_cmd"):
        info["test_cmd"] = cfg["test_cmd"]
        info["framework"] = info["framework"] or "custom"
    if cfg.get("test_globs"):
        info["test_globs"] = list(cfg["test_globs"])
    info["test_globs"] = info["test_globs"] + list(cfg.get("extra_protected") or [])
    return info


# ---------------------------------------------------------------- file tracking

def walk_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                yield path.relative_to(root).as_posix()


def matches(rel, globs):
    for pattern in globs:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]):
            return True
    return False


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_tree(root):
    return {rel: file_hash(root / rel) for rel in walk_files(root)}


# ---------------------------------------------------------------- backends

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


def backend_argv(cfg, prompt, root, session_id, model):
    backend = cfg["backend"]
    if backend == "copilot":
        argv = ["copilot", "-p", prompt, "--allow-all-tools", "-s", "-C", str(root),
                "--session-id", session_id]
        if model:
            argv += ["--model", model]
        return argv + list(cfg.get("extra_args") or [])
    if backend == "command":
        if not cfg.get("command"):
            raise SystemExit('backend "command" needs "command": [argv...] in config')
        return list(cfg["command"])
    raise SystemExit(f"unknown backend: {backend}")


def build_prompt(plan, feedback, info, protected_files):
    shown = protected_files[:50]
    more = f"\n  ... and {len(protected_files) - 50} more" if len(protected_files) > 50 else ""
    feedback_block = f"\n## Feedback from the reviewer on your previous attempt\n{feedback}\n" if feedback else ""
    test_cmd = info["test_cmd"] or "(none detected - verify by reasoning and any checks you can run)"
    return f"""You are implementing a task in this repository. Work autonomously; nobody will answer questions during this run.

## Task plan
{plan}
{feedback_block}
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
   Use "done" only if the test command passes.
"""


# ---------------------------------------------------------------- commands

def tail(text, n=TAIL_LINES):
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


def cmd_detect(root, cfg, _args):
    info = detect(root, cfg)
    info["protected_files"] = sorted(f for f in walk_files(root) if matches(f, info["test_globs"]))
    info["backend"] = cfg["backend"]
    info["models"] = {"pinned": cfg["model"]} if cfg.get("model") else cfg.get("models")
    return info, 0


def cmd_test(root, cfg, _args):
    info = detect(root, cfg)
    if not info["test_cmd"]:
        return {"passed": False, "error": "no test command detected; set test_cmd in .delegate/config.json"}, 2
    logs = root / STATE_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CI": "1"}  # keeps vitest/jest out of watch mode
    proc = subprocess.run(info["test_cmd"], shell=True, cwd=root, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log = logs / f"test-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log.write_text(proc.stdout)
    result = {"passed": proc.returncode == 0, "exit_code": proc.returncode,
              "cmd": info["test_cmd"], "log": str(log.relative_to(root))}
    if proc.returncode != 0:
        result["output_tail"] = tail(proc.stdout)
    return result, 0 if proc.returncode == 0 else 1


def cmd_run(root, cfg, args):
    state = root / STATE_DIR
    logs = state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (state / ".gitignore").write_text("*\n")

    plan = Path(args.plan).read_text()
    feedback = args.feedback or ""
    if args.feedback_file:
        feedback += Path(args.feedback_file).read_text()

    info = detect(root, cfg)
    before = hash_tree(root)
    protected = sorted(f for f in before if matches(f, info["test_globs"]))

    # Snapshot protected files (outside the project, so test runners don't collect the
    # copies) so they can be restored, then make them read-only.
    snapshot = Path(tempfile.mkdtemp(prefix="delegate-snapshot-"))
    modes = {}
    for rel in protected:
        dst = snapshot / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dst)
        modes[rel] = (root / rel).stat().st_mode
        os.chmod(root / rel, modes[rel] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    for leftover in ("result.json", "test_change_request.md"):
        (state / leftover).unlink(missing_ok=True)

    session_file = state / "session.json"
    session = _read_json(session_file) if args.continue_session else None
    session_id = (session or {}).get("session_id") or str(uuid.uuid4())
    session_file.write_text(json.dumps({"session_id": session_id}))

    prompt = build_prompt(plan, feedback, info, protected)
    candidates = resolve_models(cfg, args.tier, args.model)
    log = logs / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    env = {**os.environ, "DELEGATE_PROMPT": prompt, "DELEGATE_ROOT": str(root)}
    started = time.time()
    timed_out = False
    fallback_note = None
    try:
        for attempt, model in enumerate(candidates):
            argv = backend_argv(cfg, prompt, root, session_id, model)
            try:
                with open(log, "a") as fh:
                    fh.write(f"=== model: {model or 'default'}\n")
                    fh.flush()
                    proc = subprocess.run(argv, cwd=root, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, timeout=cfg["timeout"])
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out, exit_code = True, None
            except FileNotFoundError as exc:
                exit_code = 127
                with open(log, "a") as fh:
                    fh.write(f"backend not found: {exc}\n")
            if timed_out or exit_code == 0 or attempt == len(candidates) - 1:
                break
            fallback_note = f"{model} failed (exit {exit_code}); retried with {candidates[attempt + 1]}"
    finally:
        # Guard: revert any change to protected files, delete newly added ones.
        violations = []
        for rel in protected:
            path = root / rel
            if not path.exists() or file_hash(path) != before[rel]:
                violations.append({"file": rel, "change": "deleted" if not path.exists() else "modified"})
                if path.exists():
                    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snapshot / rel, path)
            os.chmod(path, modes[rel])
        after = hash_tree(root)
        for rel in sorted(set(after) - set(before)):
            if matches(rel, info["test_globs"]):
                violations.append({"file": rel, "change": "added"})
                (root / rel).unlink()
                after.pop(rel)
        shutil.rmtree(snapshot, ignore_errors=True)

    changed = sorted(
        [f for f in after if before.get(f) != after[f]] + [f for f in before if f not in after]
    )
    report = _read_json(state / "result.json") or {}
    request_path = state / "test_change_request.md"

    if violations:
        status = "violated_tests"
    elif timed_out:
        status = "timeout"
    elif exit_code != 0:
        status = "backend_error"
    elif report.get("status") in ("done", "failed", "needs_test_change"):
        status = report["status"]
    else:
        status = "no_report"

    result = {
        "status": status,
        "summary": report.get("summary"),
        "worker_reported": report.get("status"),
        "changed_files": changed,
        "test_cmd": info["test_cmd"],
        "backend": cfg["backend"],
        "tier": args.tier,
        "model": model,
        "seconds": round(time.time() - started),
        "log": str(log.relative_to(root)),
    }
    if fallback_note:
        result["model_fallback"] = fallback_note
    if violations:
        result["violations"] = violations
    if request_path.exists():
        result["test_change_request"] = request_path.read_text()[:4000]
    if status == "backend_error":
        result["log_tail"] = tail(log.read_text(errors="ignore"), 30)
    if status == "done" and not changed:
        result["warning"] = "worker reported done but changed no files"
    return result, 0 if status == "done" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="project root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect")
    sub.add_parser("test")
    run = sub.add_parser("run")
    run.add_argument("--plan", required=True, help="path to the plan markdown file")
    run.add_argument("--feedback", help="feedback text for a retry (failing tests, decisions)")
    run.add_argument("--feedback-file", help="read feedback from a file")
    run.add_argument("--continue", dest="continue_session", action="store_true",
                     help="continue the previous worker session instead of starting fresh")
    run.add_argument("--tier", choices=["normal", "hard"], default="normal",
                     help="task complexity; picks the model from config \"models\"")
    run.add_argument("--model", help="explicit model, overrides --tier and config")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    cfg = load_config(root)
    handler = {"detect": cmd_detect, "test": cmd_test, "run": cmd_run}[args.command]
    result, code = handler(root, cfg, args)
    print(json.dumps(result, indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
