"""The loop's Registry seam: `--continue` adopts the recorded agent, the guard
release runs before verification, and the guard context reaches the classifier."""

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subagent.config import Settings
from subagent.guard import supervisor as sup
from subagent.guard.classify import ALLOW, Verdict

from .helpers import Sandbox, for_drivers, need


@need("git", "node", "npm")
class ContinueSession(Sandbox):
    def test_continue_reuses_the_agent_session(self):
        root = self.node_project()
        self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good")
        first = json.loads(self.read(root, ".subagent/session.json"))
        self.delegate("run", "--plan", ".subagent/PLAN.md", "--continue", cwd=root, scenario="good")
        second = json.loads(self.read(root, ".subagent/session.json"))
        # Registry.adopt reopens the recorded agent: same id, session and home.
        self.assertEqual(second["agent_id"], first["agent_id"])
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["agent_home"], first["agent_home"])
        # A fresh run (no --continue) mints a brand-new agent.
        self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good")
        third = json.loads(self.read(root, ".subagent/session.json"))
        self.assertNotEqual(third["agent_id"], first["agent_id"])

    def test_pre_verify_restores_tests_before_the_suite_runs(self):
        root = self.node_project()
        code, r = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="cheat")
        self.assertEqual(r["status"], "violated_tests")
        # The guard release (pre_verify) restored the tampered tests first, so
        # the runner's own suite passed against the original files.
        self.assertTrue(r["tests"]["passed"])
        self.assertEqual(r["tests"]["counts"],
                         {"total": 2, "passed": 2, "failed": 0, "skipped": 0})


for_drivers(ContinueSession)


class GuardContext(unittest.TestCase):
    def test_workspace_for_returns_the_agents_guard_context(self):
        settings = Settings(workspace=Path("/w"), session_root=Path("/s"))

        class FakeRegistry:
            def find_agent(self, agent_id):
                return SimpleNamespace(
                    workspace=Path("/w/proj"),
                    guard_context={"protected": ["test/x.js"],
                                   "state_allow": [".subagent/result.json"]},
                )

        supervisor = sup.Supervisor(settings, registry=FakeRegistry())
        workspace, context = supervisor.workspace_for("a1", "/fallback")
        self.assertEqual(workspace, Path("/w/proj"))
        self.assertEqual(context["protected"], ["test/x.js"])
        self.assertEqual(context["state_allow"], [".subagent/result.json"])

    def test_decide_passes_guard_context_to_the_classifier(self):
        settings = Settings(workspace=Path("/w"), session_root=Path("/s"))
        supervisor = sup.Supervisor(settings, registry=None)
        seen = {}

        def fake_classify(tool_name, tool_input, workspace, cwd=None, guard_context=None):
            seen["guard_context"] = guard_context
            return Verdict(ALLOW, "ok")

        with patch.object(sup, "_classify", fake_classify):
            verdict = asyncio.run(supervisor.decide(
                "Bash", {"command": "ls"}, Path("/w"), "a1", None,
                {"protected": ["test/x.js"]},
            ))
        self.assertEqual(verdict.action, ALLOW)
        self.assertEqual(seen["guard_context"], {"protected": ["test/x.js"]})


if __name__ == "__main__":
    unittest.main()
