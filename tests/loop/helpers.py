"""Shared helpers: an isolated sandbox with a fake `copilot`, and small project builders."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
REPO = TESTS.parent
FAKES = TESTS / "fakes"
SCENARIOS = FAKES / "scenarios"
# The runner is a module now, not a script. `uv run` installs the package
# editable, so `-m subagent.cli` resolves; PYTHONPATH covers a bare checkout.
RUNNER = [sys.executable, "-m", "subagent.cli"]

ADD_TEST = """import { test } from 'node:test'; import assert from 'node:assert/strict'; import { add } from '../src/add.js';
test('adds', () => assert.equal(add(2, 3), 5));
test('adds negatives', () => assert.equal(add(-2, -3), -5));
"""


def need(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    return unittest.skipIf(missing, f"needs {', '.join(missing)}")


class Sandbox(unittest.TestCase):
    """Each test gets a temp dir, a fake `copilot` on PATH, and its own HOME/config (no live view)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="delegate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "copilot").symlink_to(FAKES / "fake_copilot.py")
        home = self.tmp / "home"
        home.mkdir()
        src = str(REPO / "src")
        pythonpath = os.pathsep.join([src, os.environ["PYTHONPATH"]]) if os.environ.get("PYTHONPATH") else src
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config"), "DELEGATE_LIVE_VIEW": "off",
                    "PYTHONPATH": pythonpath,
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}

    # ------------------------------------------------------------ running the CLI

    def delegate(self, *args, cwd, scenario=None, env=None, stdin=None, timeout=120):
        """Run the delegate loop; returns (exit_code, parsed JSON or raw stdout)."""
        run_env = {**self.env, **(env or {})}
        if scenario:
            run_env["FAKE_SCRIPT"] = str(SCENARIOS / f"{scenario}.sh")
        proc = subprocess.run([*RUNNER, *args], cwd=cwd, env=run_env, input=stdin,
                              capture_output=True, text=True, timeout=timeout)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            return proc.returncode, proc.stdout + proc.stderr

    def sh(self, command, cwd):
        subprocess.run(command, shell=True, cwd=cwd, env=self.env, check=True, capture_output=True)

    # ------------------------------------------------------------ projects

    def git_init(self, path):
        self.sh("git init -q -b main && git add -A && git commit -qm init", path)

    def node_project(self, name="proj", git=True, tests=True):
        """A node:test project with tests for src/add.js and a plan in .delegate/PLAN.md."""
        root = self.tmp / name
        (root / ".delegate").mkdir(parents=True)
        (root / "package.json").write_text('{"name":"p","type":"module","scripts":{"test":"node --test"}}')
        (root / ".gitignore").write_text("node_modules/\n")
        if tests:
            (root / "test").mkdir()
            (root / "test" / "add.test.js").write_text(ADD_TEST)
        if git:
            self.git_init(root)
        (root / ".delegate" / "PLAN.md").write_text("implement add in src/add.js")
        return root

    def read(self, root, rel):
        return (root / rel).read_text()
