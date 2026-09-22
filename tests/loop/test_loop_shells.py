"""The documented commands work the same when typed in bash, zsh and fish (missing shells are skipped)."""

import json
import shlex
import shutil
import subprocess
import sys
import unittest

from .helpers import REPO, SCENARIOS, Sandbox, need

# The documented invocation, quoted so every shell sees one word per argument.
DELEGATE = f"{shlex.quote(sys.executable)} -m subagent.cli"

NASTY = ("2 tests fail: expected \"a-b\" got 'a b'\n  $HOME `whoami` $(echo pwned) ${x} %s \\n "
         "* ? [x] {a,b} ~ ; | & > < # !! ^ (fish) Việt Nam 💥\nend")


@need("git", "node", "npm")
class Shells(Sandbox):
    def in_shell(self, shell, command, cwd):
        proc = subprocess.run([shell, "-c", command], cwd=cwd, env=self.env, capture_output=True, text=True,
                              timeout=120)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            return proc.returncode, proc.stdout + proc.stderr

    def test_commands_in_each_shell(self):
        shells = [s for s in ("bash", "zsh", "fish") if shutil.which(s)]
        home = self.env["HOME"]
        self.env["PATH"] = f"{home}/.local/bin:{self.env['PATH']}"
        self.env["FAKE_SCRIPT"] = str(SCENARIOS / "capture.sh")
        for shell in shells:
            with self.subTest(shell):
                root = self.node_project(name=f"p-{shell}")
                code, out = self.in_shell(shell, "./install.sh", REPO)
                self.assertEqual(code, 0, out)
                steps = [
                    (f"{DELEGATE} detect", lambda r: r["test_cmd"] == "npm test"),
                    (f"{DELEGATE} detect", lambda r: "protected_files" in r),
                    (f"{DELEGATE} run --plan .delegate/PLAN.md --background", lambda r: r["status"] == "started"),
                    (f"{DELEGATE} wait --timeout 60", lambda r: r["status"] == "done"),
                    (f"env DELEGATE_LIVE_VIEW=off {DELEGATE} test", lambda r: r["passed"] is True),
                    (f"{DELEGATE} checkpoints", lambda r: len(r["checkpoints"]) >= 1),
                    (f"{DELEGATE} undo", lambda r: r["status"] == "undone"),
                ]
                for command, ok in steps:
                    code, r = self.in_shell(shell, command, root)
                    self.assertTrue(isinstance(r, dict) and ok(r), f"{shell}: {command} -> {r}")
                (root / ".delegate" / "feedback.md").write_text(NASTY)
                for command in (f"{DELEGATE} run --plan .delegate/PLAN.md --feedback-file .delegate/feedback.md",
                                f"cat .delegate/feedback.md | {DELEGATE} run --plan .delegate/PLAN.md "
                                "--feedback-file -"):
                    self.in_shell(shell, command, root)
                    self.assertIn(NASTY, self.read(root, ".delegate/prompt_seen.txt"), f"{shell}: {command}")


if __name__ == "__main__":
    unittest.main()
