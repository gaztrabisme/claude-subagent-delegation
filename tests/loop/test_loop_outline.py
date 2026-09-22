"""Test outline mode: a Copilot test writer turns Claude's outline into test files."""

import unittest

from .helpers import Sandbox, need


@need("git", "node", "npm")
class Outline(Sandbox):
    def project(self, name="o", test_file="test/add.test.js"):
        root = self.node_project(name=name, tests=False)
        (root / ".delegate" / "TESTS.md").write_text(f"# Test outline\n\n## {test_file}\n- adds: add(2, 3) -> 5\n")
        return root

    def run_outline(self, root, mode="", *extra):
        return self.delegate("run", "--plan", ".delegate/PLAN.md", "--test-outline", ".delegate/TESTS.md", "--auto",
                             *extra, cwd=root, scenario="outline", env={"OUT": mode})[1]

    def calls(self, root):
        path = root / ".delegate" / "calls.txt"
        text = path.read_text().split() if path.exists() else []
        path.write_text("")
        return text

    def test_writes_tests_and_reverts_implementation(self):
        root = self.project()
        r = self.run_outline(root, "sneaky")
        self.assertEqual(r["status"], "done")
        self.assertEqual(self.calls(root), ["writer", "test_review", "worker", "code_review"])
        self.assertEqual(r["test_writer"]["files"], ["test/add.test.js"])
        self.assertEqual(r["test_writer"]["reverted_files"], ["src/add.js"])
        self.assertEqual((r["test_writer"]["cases"], r["test_writer"]["outline_cases"]), (1, 1))
        self.assertIn("add(2, 3), 5", self.read(root, "test/add.test.js"))

    def test_fix_pass_for_transcription_errors(self):
        root = self.project()
        r = self.run_outline(root, "wrong_once")
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["test_writer"]["fix_pass"], "done")
        self.assertEqual(self.calls(root), ["writer", "test_review", "writer", "fixpass", "test_review", "worker",
                                            "code_review"])

    def test_still_wrong_after_fix_pass_goes_back_to_claude(self):
        root = self.project()
        r = self.run_outline(root, "wrong_always")
        self.assertEqual(r["status"], "tests_questioned")
        self.assertEqual(self.calls(root), ["writer", "test_review", "writer", "test_review"])

    def test_unchanged_outline_is_not_rewritten(self):
        root = self.project()
        self.run_outline(root)
        self.calls(root)
        r = self.run_outline(root, "", "--continue")
        self.assertEqual(r["status"], "done")
        self.assertEqual(self.calls(root), ["worker", "code_review"])

    def test_files_outside_usual_patterns_are_protected(self):
        root = self.project(test_file="checks/add_check.js")
        r = self.run_outline(root, "odd_path")
        self.assertEqual(r["status"], "done")
        code, info = self.delegate("detect", cwd=root)
        self.assertEqual(info["protected_files"], ["checks/add_check.js"])
        self.assertNotIn("worker tampering", self.read(root, "checks/add_check.js"))

    def test_bad_input(self):
        root = self.project()
        (root / ".delegate" / "TESTS.md").write_text("no headings here")
        self.assertEqual(self.run_outline(root)["status"], "bad_outline")
        code, r = self.delegate("run", "--plan", ".delegate/PLAN.md", "--test-outline", ".delegate/TESTS.md",
                                cwd=root, scenario="outline")
        self.assertEqual(r["status"], "bad_arguments")


if __name__ == "__main__":
    unittest.main()
