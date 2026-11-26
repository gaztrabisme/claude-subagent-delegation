"""B4 acceptance tests: parallel worktrees never symlink root's dependency folders."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from subagent.loop import checkpoint
from subagent.loop import loop as loop_module
from subagent.loop.common import state_dir
from subagent.loop.events import LogSink
from tests.conftest import make_settings

DEP_DIRS = ("node_modules", ".venv", "venv")


def _init_project(tmp_path: Path) -> tuple[Path, list[dict]]:
    root = tmp_path / "repo"
    (root / ".subagent" / "plans").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_text("init\n", encoding="utf-8")
    (root / "src" / "b.txt").write_text("init\n", encoding="utf-8")
    (root / ".gitignore").write_text(".subagent/\nnode_modules/\n.venv/\nvenv/\n", encoding="utf-8")

    # Root dependency directories (untracked, gitignored).
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("root-v1", encoding="utf-8")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text("root-venv", encoding="utf-8")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "pyvenv.cfg").write_text("root-venv2", encoding="utf-8")

    git_env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    import os

    env = {**os.environ, **git_env, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "init"],
        cwd=root,
        env=env,
        check=True,
    )

    (root / ".subagent" / "plans" / "a.md").write_text("build a", encoding="utf-8")
    (root / ".subagent" / "plans" / "b.md").write_text("build b", encoding="utf-8")
    tasks = [
        {"name": "a", "plan": str(root / ".subagent" / "plans" / "a.md"), "files": ["src/a.txt"]},
        {"name": "b", "plan": str(root / ".subagent" / "plans" / "b.md"), "files": ["src/b.txt"]},
    ]
    return root, tasks


def _run_parallel_with_worker(root: Path, tasks: list[dict], worker_cls, monkeypatch) -> dict:
    settings = make_settings(root)
    state_dir(root)
    cp = checkpoint.create(root, "before parallel")
    log = root / ".subagent" / "logs" / "parallel.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    sink = LogSink(log)
    server = SimpleNamespace(
        settings=settings,
        registry=SimpleNamespace(agents=lambda: []),
    )
    args = SimpleNamespace(
        tier="normal",
        provider=None,
        model=None,
        continue_session=False,
        feedback=None,
        feedback_file=None,
        test_output="",
    )
    info = {"test_globs": [], "test_cmd": "", "language": "python", "files": []}
    record = {"parallel": {}}

    monkeypatch.setattr(loop_module, "Worker", worker_cls)
    return loop_module._parallel_work(
        root, server, args, tasks, info, "", record, cp, log, sink, None
    )


def test_b4_parallel_worktrees_have_no_dependency_symlinks(tmp_path: Path, monkeypatch) -> None:
    """B4.1: no parallel worktree gets a symlink to root's node_modules, .venv, or venv."""
    root, tasks = _init_project(tmp_path)
    observations: list[dict] = []
    lock = threading.Lock()

    class InspectingWorker:
        def __init__(self, server, proj, test_cmd, guard=None, live=None, override_envelope=None):
            self.proj = proj
            self.agent = None
            self.run = None
            self.model = "fake"
            self.fallback_note = None

        def run_round(self, candidates, fallback, prompt, source_chars=None):
            dep_state = {}
            for dep in DEP_DIRS:
                p = self.proj / dep
                dep_state[dep] = {
                    "is_symlink": p.is_symlink(),
                    "exists": p.exists(),
                    "is_dir": p.is_dir(),
                }
            with lock:
                observations.append({"proj": self.proj, "deps": dep_state})
            (self.proj / ".subagent").mkdir(parents=True, exist_ok=True)
            (self.proj / ".subagent" / "result.json").write_text(
                json.dumps({"status": "done", "summary": "ok"}), encoding="utf-8"
            )
            return self

    result = _run_parallel_with_worker(root, tasks, InspectingWorker, monkeypatch)
    assert result["status"] == "done", result
    assert len(observations) == 2

    for obs in observations:
        for dep in DEP_DIRS:
            state = obs["deps"][dep]
            assert state["is_symlink"] is False, (
                f"worktree {obs['proj']} has symlink for {dep}"
            )
            if state["exists"]:
                assert state["is_dir"] is True, f"worktree {obs['proj']} {dep} is not a real dir"


def test_b4_install_in_one_worktree_does_not_mutate_root_or_sibling(
    tmp_path: Path, monkeypatch
) -> None:
    """B4.2: an install in one parallel worktree cannot change root's or a sibling's dependency folder."""
    root, tasks = _init_project(tmp_path)
    install_done = threading.Barrier(2, timeout=5.0)
    sibling_saw_mutation: list[str] = []

    class MutatingWorker:
        def __init__(self, server, proj, test_cmd, guard=None, live=None, override_envelope=None):
            self.proj = proj
            self.agent = None
            self.run = None
            self.model = "fake"
            self.fallback_note = None

        def run_round(self, candidates, fallback, prompt, source_chars=None):
            is_a = "build a" in prompt
            if is_a:
                for dep in DEP_DIRS:
                    target = self.proj / dep
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "installed.txt").write_text("from-a", encoding="utf-8")
                pkg_index = self.proj / "node_modules" / "pkg" / "index.js"
                if pkg_index.exists():
                    pkg_index.write_text("mutated-by-a", encoding="utf-8")
                install_done.wait()
            else:
                install_done.wait()
                for dep in DEP_DIRS:
                    if (self.proj / dep / "installed.txt").exists():
                        sibling_saw_mutation.append(f"sibling saw {dep}/installed.txt")
                pkg_index = self.proj / "node_modules" / "pkg" / "index.js"
                if pkg_index.exists() and pkg_index.read_text(encoding="utf-8") != "root-v1":
                    sibling_saw_mutation.append("sibling saw mutated node_modules/pkg/index.js")

            (self.proj / ".subagent").mkdir(parents=True, exist_ok=True)
            (self.proj / ".subagent" / "result.json").write_text(
                json.dumps({"status": "done", "summary": "ok"}), encoding="utf-8"
            )
            return self

    result = _run_parallel_with_worker(root, tasks, MutatingWorker, monkeypatch)
    assert result["status"] == "done", result
    assert not sibling_saw_mutation, sibling_saw_mutation

    for dep in DEP_DIRS:
        assert not (root / dep / "installed.txt").exists(), f"root {dep} mutated by worktree"
    assert (root / "node_modules" / "pkg" / "index.js").read_text(encoding="utf-8") == "root-v1"
    assert (root / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8") == "root-venv"
    assert (root / "venv" / "pyvenv.cfg").read_text(encoding="utf-8") == "root-venv2"
