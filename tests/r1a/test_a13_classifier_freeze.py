"""A13 acceptance tests: classifier allow-lists frozen as subsets of base commit 0ba5249."""

from __future__ import annotations

from subagent.guard import classify

# Allow-list literals snapshotted directly from base commit 0ba5249 (src/subagent/guard/classify.py).
BASE_READ_ONLY = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "pwd", "echo", "true",
    "grep", "egrep", "fgrep", "rg", "ag", "find", "which", "type", "basename",
    "dirname", "realpath", "readlink", "date", "env", "printenv", "uname",
    "sort", "uniq", "cut", "tr", "diff", "cmp", "md5", "shasum", "sha256sum",
    "tree", "du", "df", "ps", "id", "whoami", "man", "help", "jq", "column",
    "test", "[",
})

BASE_TEST_RUNNERS = frozenset({"pytest", "tox", "nose2", "unittest", "make", "cargo", "go"})
BASE_JS_RUNNERS = frozenset({"vitest", "tsc"})
BASE_GRADLE_TASKS = frozenset({"test", "check", "build"})

BASE_SAFE_ENV = frozenset({
    "CI", "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "TERM",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "COLUMNS", "LINES",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
    "PYTHONIOENCODING", "PYTHONUTF8", "RUST_BACKTRACE", "RUST_LOG", "NODE_ENV",
    "DEBUG", "VERBOSE",
})

BASE_GIT_READ_ONLY = frozenset({
    "status", "diff", "log", "show", "branch", "remote", "rev-parse", "describe",
    "blame", "shortlog", "ls-files", "config", "stash",
})
BASE_GIT_GLOBAL_OK = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs"})
BASE_GIT_CONFIG_READ = frozenset({
    "--list", "-l", "--get", "--get-all", "--get-regexp", "--get-urlmatch",
    "--show-origin", "--show-scope", "--local", "--name-only", "-z", "--null",
    "--includes", "--no-includes",
})
BASE_GIT_BRANCH_READ = frozenset({
    "-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "--verbose",
    "--show-current", "--contains", "--no-contains", "--merged", "--no-merged",
    "--points-at", "--no-color", "--color",
})

BASE_INTERPRETERS = frozenset({
    "python", "python3", "node", "ruby", "perl", "deno", "bun", "tsx", "bash",
})
BASE_SAFE_LETTERS = {
    "python": "BbdEIOqsSuRPv", "python3": "BbdEIOqsSuRPv", "node": "",
    "ruby": "wv", "perl": "wWT", "bash": "euxvn", "deno": "", "bun": "", "tsx": "",
}
BASE_SAFE_LONG_OPTIONS = frozenset({
    "--no-warnings", "--trace-warnings", "--enable-source-maps", "--no-deprecation",
    "--trace-uncaught",
})
BASE_TEST_MODULES = frozenset({"pytest", "unittest", "nose2", "tox"})
BASE_RUNNER_SUBCOMMANDS = {
    "cargo": frozenset({"test", "check", "build", "clippy", "bench"}),
    "go": frozenset({"test", "build", "vet"}),
}
BASE_READ_TOOLS = frozenset({
    "read", "read_image", "Read", "list", "glob", "grep", "Grep", "Glob",
    "LS", "TodoRead", "TodoWrite",
})


def test_a13_classifier_allowlists_are_subsets_of_base_commit() -> None:
    """A13: every allow-list in guard/classify.py is a subset of its value at base commit 0ba5249."""
    assert set(classify.READ_ONLY) <= BASE_READ_ONLY
    assert set(classify.TEST_RUNNERS) <= BASE_TEST_RUNNERS
    assert set(classify.JS_RUNNERS) <= BASE_JS_RUNNERS
    assert set(classify.GRADLE_TASKS) <= BASE_GRADLE_TASKS
    assert set(classify.SAFE_ENV) <= BASE_SAFE_ENV
    assert set(classify.GIT_READ_ONLY) <= BASE_GIT_READ_ONLY
    assert set(classify.GIT_GLOBAL_OK) <= BASE_GIT_GLOBAL_OK
    assert set(classify.GIT_CONFIG_READ) <= BASE_GIT_CONFIG_READ
    assert set(classify.GIT_BRANCH_READ) <= BASE_GIT_BRANCH_READ
    assert set(classify.INTERPRETERS) <= BASE_INTERPRETERS
    assert set(classify.SAFE_LONG_OPTIONS) <= BASE_SAFE_LONG_OPTIONS
    assert set(classify.TEST_MODULES) <= BASE_TEST_MODULES
    assert set(classify.READ_TOOLS) <= BASE_READ_TOOLS

    assert set(classify.SAFE_LETTERS) <= set(BASE_SAFE_LETTERS)
    for key, letters in classify.SAFE_LETTERS.items():
        assert set(letters) <= set(BASE_SAFE_LETTERS[key]), key

    assert set(classify.RUNNER_SUBCOMMANDS) <= set(BASE_RUNNER_SUBCOMMANDS)
    for key, subcmds in classify.RUNNER_SUBCOMMANDS.items():
        assert set(subcmds) <= BASE_RUNNER_SUBCOMMANDS[key], key
