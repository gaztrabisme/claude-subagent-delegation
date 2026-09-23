"""Single worker rounds: test guard, runner test run, background runs, undo, model fallback."""

import json
import time
import unittest

from .helpers import Sandbox, for_drivers, need


@need("git", "node", "npm")
class Rounds(Sandbox):
    def test_good_round(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["changed_files"], ["src/add.js"])
        self.assertEqual(r["tests"]["counts"], {"total": 2, "passed": 2, "failed": 0, "skipped": 0})
        self.assertEqual(r["worker_credits"], 2.5)
        self.assertIn("checkpoint", r)

    def test_runner_catches_failing_tests(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="buggy")
        self.assertEqual(r["status"], "tests_failed")
        self.assertEqual(r["worker_reported"], "done")
        self.assertIn("output_tail", r["tests"])

    def test_edited_and_added_tests_are_reverted(self):
        root = self.node_project()
        original = self.read(root, "test/add.test.js")
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="cheat")
        self.assertEqual(r["status"], "violated_tests")
        self.assertEqual({(v["file"], v["change"]) for v in r["violations"]},
                         {("test/add.test.js", "modified"), ("test/extra.test.js", "added")})
        self.assertEqual(self.read(root, "test/add.test.js"), original)
        self.assertFalse((root / "test" / "extra.test.js").exists())

    def test_test_config_restored_but_other_edits_kept(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="cheatcfg")
        self.assertEqual(r["status"], "violated_tests")
        self.assertEqual(r["violations"][0]["file"], "package.json#scripts.test")
        pkg = json.loads(self.read(root, "package.json"))
        self.assertEqual(pkg["scripts"]["test"], "node --test")
        self.assertEqual(pkg["dependencies"], {"left-pad": "1.0.0"})

    def test_hard_tier_falls_back_when_model_unavailable(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", "--tier", "hard", cwd=root,
                                scenario="good", env={"FAKE_FAIL_MODEL": "claude-opus-5"})
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["model"], "claude-sonnet-5")
        self.assertIn("claude-opus-5 failed", r["model_fallback"])

    def test_disputed_test(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="dispute")
        self.assertEqual(r["status"], "needs_test_change")
        self.assertIn("spec says 6", r["test_change_request"])

    def test_feedback_file_reaches_worker_verbatim(self):
        root = self.node_project()
        nasty = "expected \"a\" got 'b'\n$HOME `whoami` $(echo pwned) {a,b} ~ ; | & 💥"
        (root / ".subagent" / "feedback.md").write_text(nasty)
        self.delegate("run", "--plan", ".subagent/PLAN.md", "--feedback-file", ".subagent/feedback.md",
                      cwd=root, scenario="capture")
        self.assertIn(nasty, self.read(root, ".subagent/prompt_seen.txt"))
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", "--feedback-file", "-", "--continue",
                                cwd=root, scenario="capture", stdin=nasty)
        self.assertIn(nasty, self.read(root, ".subagent/prompt_seen.txt"))


@need("git", "node", "npm")
class BackgroundAndUndo(Sandbox):
    def test_background_busy_wait(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", "--background", cwd=root, scenario="slow")
        self.assertEqual(r["status"], "started")
        code, busy = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good")
        self.assertEqual(busy["status"], "busy")
        code, running = self.delegate("wait", "--timeout", "1", cwd=root)
        self.assertEqual((code, running["status"]), (3, "running"))
        code, done = self.delegate("wait", "--timeout", "60", cwd=root)
        self.assertEqual((code, done["status"]), (0, "done"))

    def test_background_child_uses_its_own_socket_with_configured_parent_path(self):
        root = self.node_project()
        configured = self.tmp / "shared-approval.sock"
        config_text = self.config.read_text()
        self.config.write_text(config_text.replace(
            f'approval_socket = "{self.tmp / "approval.sock"}"',
            "approval_socket = " + json.dumps(str(configured)),
        ))

        code, started = self.delegate(
            "run", "--plan", ".subagent/PLAN.md", "--background", cwd=root, scenario="slow"
        )
        self.assertIsInstance(started, dict, started)
        self.assertEqual((code, started["status"]), (0, "started"))
        session_root = self.tmp / "sessions"
        deadline = time.monotonic() + 4
        child_sockets = []
        while time.monotonic() < deadline:
            child_sockets = list(session_root.glob("a[0-9a-f]*"))
            if child_sockets:
                break
            time.sleep(0.05)

        self.assertFalse(configured.exists())
        runner_logs = list((root / ".subagent" / "logs").glob("runner-*.out"))
        log_tail = runner_logs[-1].read_text()[-2000:] if runner_logs else "no runner log"
        self.assertEqual(len(child_sockets), 1, log_tail)
        code, done = self.delegate("wait", "--timeout", "60", cwd=root)
        self.assertEqual((code, done["status"]), (0, "done"))

    def test_run_with_wait_in_one_call(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", "--wait", "60", cwd=root, scenario="good")
        self.assertEqual((code, r["status"]), (0, "done"))

    def test_undo_and_redo(self):
        root = self.node_project()
        self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good")
        self.delegate("run", "--plan", ".subagent/PLAN.md", "--continue", cwd=root, scenario="slow")
        v2 = self.read(root, "src/add.js")
        code, r = self.delegate("undo", cwd=root)
        self.assertEqual(r["changes_undone"], ["M src/add.js"])
        self.assertNotIn("v2", self.read(root, "src/add.js"))
        code, r = self.delegate("undo", "--to", r["redo_checkpoint"], cwd=root)
        self.assertEqual(self.read(root, "src/add.js"), v2)

    def test_checkpoints_leave_branch_index_and_stash_alone(self):
        root = self.node_project()
        self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good")
        self.delegate("undo", cwd=root)
        log = __import__("subprocess").run(["git", "rev-list", "--count", "HEAD"], cwd=root, capture_output=True,
                                           text=True).stdout.strip()
        stash = __import__("subprocess").run(["git", "stash", "list"], cwd=root, capture_output=True,
                                             text=True).stdout.strip()
        self.assertEqual((log, stash), ("1", ""))


for_drivers(Rounds)
for_drivers(BackgroundAndUndo)


if __name__ == "__main__":
    unittest.main()
