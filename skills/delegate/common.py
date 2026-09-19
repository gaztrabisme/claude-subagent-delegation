"""Shared constants, configuration and file helpers."""

import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path

STATE_DIR = ".delegate"
GLOBAL_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "delegate" / "config.json"
SKIP_DIRS = {
    ".git", ".hg", ".svn", STATE_DIR, "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", ".next", ".nuxt", "coverage", ".turbo",
}
# Dependency directories shared (symlinked) into parallel worktrees.
DEPENDENCY_DIRS = ["node_modules", ".venv", "venv"]
DEFAULT_CONFIG = {
    "backend": "copilot",
    "models": {             # model per complexity tier; Claude picks the tier with --tier
        "normal": "claude-sonnet-5",
        "hard": "claude-opus-5",
    },
    "model": None,          # pin one model for every tier (disables tier selection)
    "timeout": 1800,        # seconds per worker run
    "extra_args": [],       # extra CLI args for the backend
    "builtin_mcps": False,  # Copilot's built-in GitHub MCP server (unused by workers; costs tokens)
    "command": None,        # argv for the "command" backend; prompt in $DELEGATE_PROMPT
    "test_cmd": None,       # override detection, e.g. "npm run test:unit"
    "test_globs": None,     # override detection, e.g. ["tests/**"]
    "extra_protected": [],  # added on top of detected globs
    "count_tests": True,    # run the suite before/after each round and check the test count
    "live_view": None,      # "auto" opens a terminal following the live log; or argv with "{cmd}"
    "review_model": "gpt-5-mini",  # cheap model for `review`
    "keep_checkpoints": 20,
}
TAIL_LINES = 60


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_config(root):
    """Defaults, then ~/.config/delegate/config.json, then the project's .delegate/config.json."""
    cfg = dict(DEFAULT_CONFIG)
    for path in (GLOBAL_CONFIG, root / STATE_DIR / "config.json"):
        user = read_json(path)
        if user:
            cfg.update(user)
    return cfg


def state_dir(root):
    state = root / STATE_DIR
    (state / "logs").mkdir(parents=True, exist_ok=True)
    gitignore = state / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    return state


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
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hash_tree(root):
    return {rel: file_hash(root / rel) for rel in walk_files(root)}


def changed_between(before, after):
    return sorted([f for f in after if before.get(f) != after[f]] + [f for f in before if f not in after])


def tail(text, n=TAIL_LINES):
    return "\n".join(text.rstrip().splitlines()[-n:])


# ---------------------------------------------------------------- git

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "delegate", "GIT_AUTHOR_EMAIL": "delegate@localhost",
    "GIT_COMMITTER_NAME": "delegate", "GIT_COMMITTER_EMAIL": "delegate@localhost",
}


def git(root, *args, env=None, check=True, text=True):
    proc = subprocess.run(["git", *args], cwd=root, env={**os.environ, **GIT_IDENTITY, **(env or {})},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip() if text else proc.stdout


def git_prefix(root):
    """Path of root inside its git work tree ('' at the top level, 'pkg/app/' in a monorepo), or None."""
    try:
        if git(root, "rev-parse", "--is-inside-work-tree") != "true":
            return None
        proc = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return proc.stdout.strip()
    except (RuntimeError, FileNotFoundError):
        return None
