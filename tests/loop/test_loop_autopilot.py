"""Autopilot: automatic retries, escalation, review-driven fix rounds, hand-back rules, watch --run."""

import subprocess
import unittest

from .helpers import RUNNER, Sandbox, need


@need("git", "node", "npm")
class Autopilot(Sandbox):
    def auto(self, root, scenario_env, *extra):
        config = root / ".delegate" / "config.json"
        if not config.exists():  # these tests are about the round loop, not the test review
            config.write_text('{"review_tests": false}')
        return self.delegate("run", "--plan", ".delegate/PLAN.md", "--auto", *extra, cwd=root, scenario="auto",
                             env={"AUTO_SCENARIO": scenario_env})

    def test_retry_then_review_fix(self):
        root = self.node_project()
        code, r = self.auto(root, "fixloop")
        self.assertEqual(r["status"], "done")
        self.assertEqual([x["status"] for x in r["rounds"]], ["tests_failed", "done", "done"])
        self.assertEqual((r["review"]["verdict"], r["review"]["cycles"]), ("ok", 2))
        models = self.read(root, ".delegate/models_seen.txt").split()
        self.assertEqual(models, ["claude-sonnet-5", "claude-sonnet-5", "gpt-5.6-sol", "claude-sonnet-5",
                                  "gpt-5.6-sol"])

    def test_escalates_to_hard_then_stops(self):
        root = self.node_project()
        code, r = self.auto(root, "escalate")
        self.assertEqual((r["status"], r["stopped_because"]), ("tests_failed", "max_rounds"))
        self.assertEqual([x["tier"] for x in r["rounds"]], ["normal", "normal", "hard", "hard"])
        self.assertTrue(r["escalated_to_hard"])

    def test_disputed_test_hands_back_at_once(self):
        root = self.node_project()
        code, r = self.auto(root, "dispute")
        self.assertEqual((r["status"], len(r["rounds"])), ("needs_test_change", 1))
        self.assertIn("spec says 6", r["test_change_request"])

    def test_unfixed_concerns(self):
        root = self.node_project()
        code, r = self.auto(root, "concerns")
        self.assertEqual(r["status"], "done")
        self.assertIn("not re-reviewed", r["review"]["unverified"])
        root = self.node_project(name="limited")
        (root / ".delegate" / "config.json").write_text('{"review_tests": false, "auto_max_rounds": 2}')
        code, r = self.auto(root, "concerns")
        self.assertEqual((r["status"], r["stopped_because"]), ("review_concerns", "max_rounds"))

    def test_hard_tier_uses_hard_reviewer_and_wait(self):
        root = self.node_project()
        code, r = self.auto(root, "good", "--tier", "hard", "--wait", "60")
        self.assertEqual((code, r["status"]), (0, "done"))
        self.assertEqual(self.read(root, ".delegate/models_seen.txt").split()[-1], "gpt-5.6-sol")

    def test_watch_run_follows_every_round_and_review(self):
        root = self.node_project()
        (root / ".delegate" / "config.json").write_text('{"review_tests": false}')
        code, started = self.delegate("run", "--plan", ".delegate/PLAN.md", "--auto", "--background", cwd=root,
                                      scenario="auto", env={"AUTO_SCENARIO": "fixloop"})
        out = subprocess.run([*RUNNER, "watch", "--run", started["run_id"]], cwd=root,
                             env=self.env, capture_output=True, text=True, timeout=60).stdout
        ends = [line.split(":", 1)[1].split("·")[0].strip() for line in out.splitlines() if line.startswith("=== end")]
        self.assertEqual(ends, ["tests_failed", "done", "review concerns", "done", "review ok"])

    def test_undo_skips_review_checkpoints(self):
        root = self.node_project()
        self.auto(root, "fixloop")
        code, r = self.delegate("undo", cwd=root)
        self.assertTrue(r["restored_to"]["label"].startswith("before round"))


if __name__ == "__main__":
    unittest.main()
