"""Other project shapes: Python, no git (tarball checkpoints), a project inside a monorepo."""

import unittest

from helpers import Sandbox, need

CALC_TEST = """import unittest
from calc import add

class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    @unittest.skip("later")
    def test_later(self):
        pass
"""


@need("git")
class Projects(Sandbox):
    def test_python_project(self):
        root = self.tmp / "py"
        (root / "tests").mkdir(parents=True)
        (root / ".delegate").mkdir()
        (root / "tests" / "test_calc.py").write_text(CALC_TEST)
        (root / ".delegate" / "PLAN.md").write_text("implement calc.add")
        code, info = self.delegate("detect", cwd=root)
        self.assertIn(info["framework"], ("unittest", "pytest"))  # unittest when pytest isn't installed
        self.assertEqual(info["protected_files"], ["tests/test_calc.py"])
        code, r = self.delegate("run", "--plan", ".delegate/PLAN.md", cwd=root, scenario="py")
        self.assertEqual(r["status"], "done", r)
        self.assertEqual((r["tests"]["counts"]["total"], r["tests"]["counts"]["skipped"]), (2, 1))

    @need("node", "npm")
    def test_undo_without_git(self):
        root = self.node_project(git=False)
        code, r = self.delegate("run", "--plan", ".delegate/PLAN.md", cwd=root, scenario="good")
        self.assertEqual(r["status"], "done")
        code, r = self.delegate("undo", cwd=root)
        self.assertEqual(r["changes_undone"], ["A src/add.js"])
        self.assertFalse((root / "src").exists())

    @need("node", "npm")
    def test_monorepo_subfolder(self):
        mono = self.tmp / "mono"
        (mono / "other").mkdir(parents=True)
        (mono / "other" / "keep.txt").write_text("x")
        self.git_init(mono)
        app = mono / "apps" / "p"
        (app / "test").mkdir(parents=True)
        (app / ".delegate").mkdir()
        (app / "package.json").write_text('{"name":"p","type":"module","scripts":{"test":"node --test"}}')
        (app / "test" / "add.test.js").write_text(self.read(self.node_project(name="src"), "test/add.test.js"))
        (app / ".delegate" / "PLAN.md").write_text("plan")
        code, r = self.delegate("run", "--plan", ".delegate/PLAN.md", cwd=app, scenario="good")
        self.assertEqual(r["changed_files"], ["src/add.js"])
        (mono / "other" / "keep.txt").write_text("changed outside the project")
        code, r = self.delegate("undo", cwd=app)
        self.assertEqual(r["changes_undone"], ["A src/add.js"])
        self.assertEqual(self.read(mono, "other/keep.txt"), "changed outside the project")


if __name__ == "__main__":
    unittest.main()
