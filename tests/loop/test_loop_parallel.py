"""Parallel rounds: one git worktree per part, merge of owned files only, then the full suite."""

import json
import subprocess
import unittest

from .helpers import Sandbox, for_drivers, need

AB_TEST = """import { test } from 'node:test'; import assert from 'node:assert/strict';
import { a } from '../src/a.js'; import { b } from '../src/b.js';
test('a', () => assert.equal(a(), 'A')); test('b', () => assert.equal(b(), 'B'));
"""
A_TEST = """import { test } from 'node:test'; import assert from 'node:assert/strict'; import { a } from '../src/a.js';
test('a', () => assert.equal(a(), 'A'));
"""
B_TEST = A_TEST.replace("a }", "b }").replace("a.js", "b.js").replace("'a'", "'b'").replace("a()", "b()").replace("'A'", "'B'")


@need("git", "node", "npm")
class Parallel(Sandbox):
    def project(self, tests):
        root = self.tmp / "par"
        (root / "test").mkdir(parents=True)
        (root / ".subagent" / "plans").mkdir(parents=True)
        (root / "node_modules" / "dummy-dep").mkdir(parents=True)
        (root / "node_modules" / "dummy-dep" / "index.js").write_text("module.exports = 1;")
        (root / "package.json").write_text('{"name":"p","type":"module","scripts":{"test":"node --test"}}')
        (root / ".gitignore").write_text("node_modules/\n")
        (root / "README.md").write_text("# readme\n")
        for name, text in tests.items():
            (root / "test" / name).write_text(text)
        self.git_init(root)
        (root / ".subagent/plans/a.md").write_text("Build src/a.js: a() returns 'A'")
        (root / ".subagent/plans/b.md").write_text("Build src/b.js: b() returns 'B'")
        (root / ".subagent/parallel.json").write_text(json.dumps({"tasks": [
            {"name": "a", "plan": ".subagent/plans/a.md", "files": ["src/a.js"]},
            {"name": "b", "plan": ".subagent/plans/b.md", "files": ["src/b.js"], "tier": "hard"}]}))
        return root

    def worktrees(self, root):
        out = subprocess.run(["git", "worktree", "list"], cwd=root, capture_output=True, text=True).stdout
        return len(out.strip().splitlines())

    def test_merge_owned_files_only(self):
        root = self.project({"ab.test.js": AB_TEST})
        code, r = self.delegate("run", "--parallel", ".subagent/parallel.json", cwd=root, scenario="par")
        self.assertEqual(r["status"], "done", r)
        tasks = {t["name"]: t for t in r["tasks"]}
        self.assertEqual(tasks["a"]["applied_files"], ["src/a.js"])
        self.assertEqual(tasks["a"]["out_of_scope_files"], ["README.md"])  # discarded
        self.assertEqual(tasks["b"]["model"], "claude-opus-5")  # per-task tier
        self.assertEqual(self.read(root, "README.md"), "# readme\n")
        self.assertEqual(self.worktrees(root), 1)
        log = self.read(root, r["log"])
        self.assertIn("[a]", log)
        self.assertIn("dep resolved in a", log)  # node_modules linked into the worktree

    def test_autopilot_fixes_failed_merge(self):
        root = self.project({"a.test.js": A_TEST, "b.test.js": B_TEST})
        code, r = self.delegate("run", "--parallel", ".subagent/parallel.json", "--auto", cwd=root,
                                scenario="parauto")
        self.assertEqual(r["status"], "done", r)
        self.assertEqual([x["status"] for x in r["rounds"]], ["tests_failed", "done"])
        round_logs = "".join(p.read_text() for p in (root / ".subagent/logs").glob("run-*.log"))
        self.assertIn("integration round sees both parts", round_logs)
        self.assertEqual(self.worktrees(root), 1)

    def test_needs_git(self):
        root = self.project({"ab.test.js": AB_TEST})
        subprocess.run(["rm", "-rf", ".git"], cwd=root, check=True)
        code, r = self.delegate("run", "--parallel", ".subagent/parallel.json", cwd=root, scenario="par")
        self.assertEqual(r["status"], "unsupported")


for_drivers(Parallel)


if __name__ == "__main__":
    unittest.main()
