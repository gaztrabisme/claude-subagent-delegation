"""Guard-context rules: protected test paths, the state dir, and the guard's refs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from subagent.guard.classify import (
    ALLOW,
    DENY,
    ESCALATE,
    TEST_FILES_READ_ONLY,
    classify,
)

SCENARIOS = Path(__file__).resolve().parents[1] / "tests" / "fakes" / "scenarios"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "test").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".subagent").mkdir()
    (tmp_path / "test" / "add.test.js").write_text("// test\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}\n')
    (tmp_path / "src" / "add.js").write_text("")
    return tmp_path


@pytest.fixture
def ctx(ws: Path) -> dict:
    """A guard context like the loop sets: the tests and the state files."""
    return {
        "protected": [
            str(ws / "test" / "add.test.js"),
            str(ws / "test" / "extra.test.js"),
            str(ws / "package.json"),
        ],
        "state_allow": [
            str(ws / ".subagent" / "result.json"),
            str(ws / ".subagent" / "test_change_request.md"),
        ],
    }


# --- protected test paths: every write family is denied ---------------------


@pytest.mark.parametrize("command", [
    "echo x > test/add.test.js",
    "echo x >> test/add.test.js",
    "tee test/add.test.js",
    "cp src/add.js test/add.test.js",
    "mv src/add.js test/add.test.js",
    "rm test/add.test.js",
    "chmod u+w test/add.test.js",
    "chown u test/add.test.js",
    "sed -i 's/a/b/' test/add.test.js",
    "perl -i -pe 's/a/b/' test/add.test.js",
    "truncate -s 0 test/add.test.js",
    "install src/add.js test/add.test.js",
    "ln -sf src/add.js test/add.test.js",
])
def test_bash_writes_to_a_protected_test_are_denied(command, ws, ctx):
    verdict = classify("Bash", {"command": command}, ws, context=ctx)
    assert verdict.action == DENY, (command, verdict.reason)
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason


def test_inline_python_naming_a_protected_path_is_denied(ws, ctx):
    command = "python3 -c \"open('test/add.test.js', 'w').write('x')\""
    verdict = classify("Bash", {"command": command}, ws, context=ctx)
    assert verdict.action == DENY, verdict.reason
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason


def test_heredoc_body_naming_a_protected_path_is_denied(ws, ctx):
    command = "python3 - <<'PY'\nimport json\njson.dump({}, open('package.json', 'w'))\nPY"
    verdict = classify("Bash", {"command": command}, ws, context=ctx)
    assert verdict.action == DENY, verdict.reason
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason


def test_protected_path_comparison_is_case_folded(ws, ctx):
    verdict = classify("Bash", {"command": "echo x > TEST/ADD.TEST.JS"}, ws, context=ctx)
    assert verdict.action == DENY, verdict.reason
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason


@pytest.mark.parametrize("tool,payload", [
    ("Write", {"file_path": "test/add.test.js"}),
    ("Edit", {"file_path": "test/add.test.js", "old_string": "a", "new_string": "b"}),
    ("write", {"path": "test/add.test.js"}),
    ("edit", {"file_path": "test/add.test.js"}),
    ("NotebookEdit", {"path": "test/add.test.js"}),
    ("MultiEdit", {"edits": [
        {"file_path": "test/add.test.js", "old_string": "a", "new_string": "b"},
    ]}),
])
def test_write_tools_on_a_protected_test_are_denied(tool, payload, ws, ctx):
    verdict = classify(tool, payload, ws, context=ctx)
    assert verdict.action == DENY, (tool, verdict.reason)
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason


def test_write_tool_on_an_absolute_protected_path_is_denied(ws, ctx):
    verdict = classify("Write", {"file_path": str(ws / "test" / "add.test.js")}, ws, context=ctx)
    assert verdict.action == DENY, verdict.reason


def test_relative_paths_resolve_against_the_calls_workdir(ws, ctx):
    (ws / "sub").mkdir()
    denied = classify(
        "Bash", {"command": "echo x > ../test/add.test.js", "workdir": "sub"}, ws, context=ctx
    )
    assert denied.action == DENY, denied.reason
    allowed = classify(
        "Bash", {"command": "echo x > test/add.test.js", "workdir": "sub"}, ws, context=ctx
    )
    assert allowed.action == ALLOW, allowed.reason


def test_write_tool_resolves_against_the_reported_cwd(ws, ctx):
    verdict = classify("Write", {"file_path": "add.test.js"}, ws, cwd="test", context=ctx)
    assert verdict.action == DENY, verdict.reason
    assert classify("Write", {"file_path": "add.test.js"}, ws, cwd="src", context=ctx).action == ALLOW


# --- the state dir: writes are limited to the allowlist ---------------------


def test_state_dir_writes_are_denied_unless_allowlisted(ws, ctx):
    assert classify("Bash", {"command": "echo x > .subagent/other.txt"}, ws,
                    context=ctx).action == DENY
    assert classify("Bash", {"command": "tee .subagent/other.txt"}, ws,
                    context=ctx).action == DENY
    assert classify("Bash", {"command": "mkdir -p .subagent/review"}, ws,
                    context=ctx).action == DENY
    assert classify("Write", {"file_path": ".subagent/other.txt"}, ws,
                    context=ctx).action == DENY


def test_allowlisted_state_files_are_writable(ws, ctx):
    assert classify("Bash", {"command": "echo x > .subagent/result.json"}, ws,
                    context=ctx).action == ALLOW
    assert classify("Write", {"file_path": ".subagent/test_change_request.md"}, ws,
                    context=ctx).action == ALLOW


# --- git state commands and the guard's refs are denied ---------------------


@pytest.mark.parametrize("command", [
    "git update-ref HEAD",
    "git worktree add /tmp/x",
    "git reflog expire --all",
    "git gc --prune=now",
    "git prune",
])
def test_git_state_commands_are_denied_with_a_context(command, ws, ctx):
    verdict = classify("Bash", {"command": command}, ws, context=ctx)
    assert verdict.action == DENY, (command, verdict.reason)


def test_any_token_naming_refs_subagent_is_denied(ws, ctx):
    verdict = classify("Bash", {"command": "git show refs/subagent/checkpoints/x"}, ws, context=ctx)
    assert verdict.action == DENY, verdict.reason


def test_file_tools_deny_checkpoint_refs_and_worktree_metadata(ws, ctx):
    git_dir = ws / ".git"
    for rel in (
        "refs/subagent/checkpoints/existing-id",
        "worktrees/linked-worktree/gitdir",
    ):
        target = git_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        for tool, payload in (
            ("Write", {"file_path": str(target)}),
            ("Edit", {"file_path": str(target)}),
            ("MultiEdit", {"edits": [{"file_path": str(target)}]}),
            ("NotebookEdit", {"path": str(target)}),
        ):
            verdict = classify(tool, payload, ws, context=ctx)
            assert verdict.action == DENY, (tool, rel, verdict.reason)
            assert "git" in verdict.reason.lower()


def test_file_tools_resolve_worktree_gitdir_before_protecting_refs(ws, ctx, tmp_path):
    common = tmp_path / "common.git"
    worktree_git = common / "worktrees" / "project"
    worktree_git.mkdir(parents=True)
    (worktree_git / "commondir").write_text("../..\n")
    (ws / ".git").write_text(f"gitdir: {worktree_git}\n")
    target = common / "refs" / "subagent" / "checkpoints" / "checkpoint-id"
    target.parent.mkdir(parents=True)

    verdict = classify("Write", {"file_path": str(target)}, ws, context=ctx)
    assert verdict.action == DENY, verdict.reason
    assert "git" in verdict.reason.lower()


# --- reads and ordinary writes are untouched ----------------------------------


@pytest.mark.parametrize("command", [
    "cat test/add.test.js",
    "grep -rn add test/",
    "pytest tests/",
    "cat .subagent/result.json",
])
def test_reads_and_test_runs_are_allowed(command, ws, ctx):
    assert classify("Bash", {"command": command}, ws, context=ctx).action == ALLOW, command


def test_ordinary_writes_stay_allowed_with_a_context(ws, ctx):
    assert classify("Write", {"file_path": "src/add.js"}, ws, context=ctx).action == ALLOW
    assert classify("Bash", {"command": "echo x > notes.txt"}, ws, context=ctx).action == ALLOW


def test_multiedit_of_an_unprotected_file_is_allowed(ws, ctx):
    payload = {"edits": [{"file_path": "src/add.js", "old_string": "a", "new_string": "b"}]}
    assert classify("MultiEdit", payload, ws, context=ctx).action == ALLOW


# --- without a context, nothing changes ---------------------------------------


def test_no_context_keeps_previous_behaviour(ws):
    assert classify("Bash", {"command": "git update-ref HEAD"}, ws).action == ESCALATE
    assert classify("Bash", {"command": "chmod u+w test/add.test.js"}, ws).action == ESCALATE
    assert classify("MultiEdit", {"edits": [{"file_path": "src/add.js"}]}, ws).action == ESCALATE


# --- the tampering scenarios --------------------------------------------------


def _commands(script: str) -> list[tuple[int, str]]:
    """Whole commands with their first line number, heredoc bodies joined."""
    commands: list[tuple[int, str]] = []
    buf: list[str] = []
    heredoc: str | None = None
    start: int | None = None
    for number, raw in enumerate(script.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if heredoc is None:
            match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)", raw)
            if match:
                heredoc, start, buf = match.group(1), number, [raw]
                continue
            commands.append((number, line))
        else:
            buf.append(raw)
            if line == heredoc:
                commands.append((start, "\n".join(buf)))
                buf, heredoc, start = [], None, None
    if buf:
        commands.append((start, "\n".join(buf)))
    return commands


def _first_denied(path: Path, ws: Path, ctx: dict):
    for number, command in _commands(path.read_text()):
        verdict = classify("Bash", {"command": command}, ws, context=ctx)
        if verdict.action == DENY:
            return number, verdict
    return None, None


def test_scenario_cheat_is_denied_on_the_chmod_line(ws, ctx):
    # Line 1 is a comment (not denied); the chmod on line 2 is the first deny.
    number, verdict = _first_denied(SCENARIOS / "cheat.sh", ws, ctx)
    assert number == 2, verdict
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason


def test_scenario_cheatcfg_is_denied_on_the_heredoc_line(ws, ctx):
    # Line 1 only writes src/add.js (not denied); the heredoc on line 2 is.
    number, verdict = _first_denied(SCENARIOS / "cheatcfg.sh", ws, ctx)
    assert number == 2, verdict
    assert verdict.reason == TEST_FILES_READ_ONLY, verdict.reason
