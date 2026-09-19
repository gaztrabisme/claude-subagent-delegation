"""Code review and test review: what the reviewer sees, verdict recovery, blocking rules."""

import unittest

from helpers import Sandbox, need


@need("git", "node", "npm")
class CodeReview(Sandbox):
    def test_review_sees_code_not_tests_and_cannot_edit(self):
        root = self.node_project()
        self.delegate("run", "--plan", ".delegate/PLAN.md", cwd=root, scenario="good")
        code, r = self.delegate("review", cwd=root, scenario="reviewer")
        self.assertEqual((code, r["verdict"]), (1, "concerns"))
        self.assertEqual(r["reviewed_files"], ["src/add.js"])
        self.assertEqual(r["model"], "gpt-5.6-sol")
        self.assertIn("restored", r["note"])
        self.assertNotIn("reviewer was here", self.read(root, "src/add.js"))
        log = "".join(p.read_text() for p in (root / ".delegate/logs").glob("review-*.log"))
        self.assertIn("diff has src/add.js", log)
        self.assertNotIn("LEAK", log)

    def test_verdict_given_in_chat_is_recovered(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".delegate/PLAN.md", "--auto", cwd=root, scenario="revchat",
                                env={"REVIEWER": "chat"})
        self.assertEqual(r["review"]["verdict"], "concerns")
        self.assertEqual(self.read(root, ".delegate/review_attempts").strip(), "1")

    def test_no_verdict_is_retried_then_unverified(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".delegate/PLAN.md", "--auto", cwd=root, scenario="revchat",
                                env={"REVIEWER": "silent"})
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["review"]["verdict"], "no_report")
        self.assertIn("no verdict", r["review"]["unverified"])
        self.assertEqual(self.read(root, ".delegate/review_attempts").strip(), "2")


@need("git", "node", "npm")
class TestReview(Sandbox):
    def calls(self, root):
        path = root / ".delegate" / "calls.txt"
        text = path.read_text().split() if path.exists() else []
        path.write_text("")
        return [c for c in text if c in ("test_review", "worker", "code_review")]

    def run_auto(self, root, **env):
        return self.delegate("run", "--plan", ".delegate/PLAN.md", "--auto", cwd=root, scenario="testrev",
                             env={k.upper(): v for k, v in env.items()})[1]

    def test_wrong_test_blocks_then_rereviewed_after_fix(self):
        root = self.node_project()
        r = self.run_auto(root)
        self.assertEqual(r["status"], "tests_questioned")
        self.assertEqual(self.calls(root), ["test_review"])  # no worker round
        with open(root / "test" / "add.test.js", "a") as fh:
            fh.write("// FIXED\n")
        r = self.run_auto(root)
        self.assertEqual(r["status"], "done")
        self.assertEqual(self.calls(root), ["test_review", "worker", "code_review"])

    def test_rerun_after_disagreeing_skips_test_review(self):
        root = self.node_project()
        self.run_auto(root)
        self.calls(root)
        r = self.run_auto(root)
        self.assertEqual(r["status"], "done")
        self.assertEqual(self.calls(root), ["worker", "code_review"])

    def test_missing_tests_never_block(self):
        for scenario in ("medium", "highmissing"):
            with self.subTest(scenario):
                root = self.node_project(name=scenario)
                r = self.run_auto(root, tr=scenario)
                self.assertEqual(r["status"], "done")
                self.assertEqual(r["test_review"]["issues"][0]["kind"], "missing_test")

    def test_can_be_disabled(self):
        root = self.node_project()
        (root / ".delegate" / "config.json").write_text('{"review_tests": false}')
        r = self.run_auto(root)
        self.assertEqual(self.calls(root), ["worker", "code_review"])

    def test_by_hand(self):
        root = self.node_project()
        code, r = self.delegate("review", "--tests", cwd=root, scenario="testrev")
        self.assertEqual((r["verdict"], r["test_files"]), ("concerns", ["test/add.test.js"]))


if __name__ == "__main__":
    unittest.main()
