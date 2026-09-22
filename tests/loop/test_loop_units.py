"""Unit tests for pure functions: test-count parsing, the live log, settings, result shaping."""

import tempfile
import unittest
from pathlib import Path

from subagent import config
from subagent.config import LoopSettings, LoopTarget
from subagent.loop import detect
from subagent.loop import loop as delegate
from subagent.loop.events import LiveLog, LogSink


class ParseTestCounts(unittest.TestCase):
    SAMPLES = {
        "node:test": ("# tests 42\n# pass 40\n# fail 1\n# skipped 1\n# todo 0\n", (42, 40, 1, 1)),
        "jest": ("Tests:       1 failed, 2 skipped, 5 passed, 8 total\n", (8, 5, 1, 2)),
        "vitest": ("      Tests  1 failed | 5 passed | 1 skipped (7)\n", (7, 5, 1, 1)),
        "unittest ok": ("Ran 12 tests in 0.004s\n\nOK (skipped=2)\n", (12, 10, 0, 2)),
        "unittest fail": ("Ran 5 tests in 0.1s\n\nFAILED (failures=1, errors=1)\n", (5, 3, 2, 0)),
        "pytest -q": ("..F.s\n1 failed, 3 passed, 1 skipped in 0.12s\n", (5, 3, 1, 1)),
        "pytest long": ("======= 10 passed, 2 warnings in 1.01s =======\n", (10, 10, 0, 0)),
        "mocha": ("\n  5 passing (12ms)\n  2 failing\n  1 pending\n", (8, 5, 2, 1)),
        "cargo": ("test result: ok. 5 passed; 0 failed; 1 ignored;\ntest result: ok. 2 passed; 1 failed; 0 ignored;\n",
                  (9, 7, 1, 1)),
        "go -v": ("--- PASS: TestA (0.00s)\n--- SKIP: TestB (0.00s)\n    --- FAIL: TestC/sub (0.00s)\n", (3, 1, 1, 1)),
    }

    def test_frameworks(self):
        for name, (output, (total, passed, failed, skipped)) in self.SAMPLES.items():
            with self.subTest(name):
                self.assertEqual(detect.parse_test_counts(output),
                                 {"total": total, "passed": passed, "failed": failed, "skipped": skipped})

    def test_unknown_output(self):
        self.assertIsNone(detect.parse_test_counts("all good\n"))

    def test_unittest_passed_never_negative(self):
        counts = detect.parse_test_counts("Ran 2 tests in 0.1s\n\nFAILED (failures=5)\n")
        self.assertEqual(counts["passed"], 0)


class LiveLogFormatting(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sink = LogSink(self.dir / "x.log")
        self.live = LiveLog(self.sink, self.dir / "x.jsonl", self.dir)

    def text(self):
        self.live.close()
        self.sink.close()
        return (self.dir / "x.log").read_text()

    def test_assistant_text_and_tool_use_render(self):
        self.live.feed({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "1", "name": "Bash", "input": {"command": "echo hi"}},
        ]}})
        text = self.text()
        self.assertIn("💬 hello", text)
        self.assertIn("▸ Bash echo hi", text)

    def test_tool_result_marks(self):
        self.live.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "is_error": False},
            {"type": "tool_result", "tool_use_id": "b", "is_error": True},
        ]}})
        text = self.text()
        self.assertIn("✓ a", text)
        self.assertIn("✗ b", text)

    def test_credits_from_result_and_last_message(self):
        self.live.feed({"type": "assistant", "message": {"content": [
            {"type": "text", "text": '{"verdict": "ok"}'},
        ]}})
        self.live.feed({"type": "result", "is_error": False, "usage": {"credits": 2.5}})
        self.assertEqual(self.live.credits, 2.5)
        self.assertEqual(self.live.last_message, '{"verdict": "ok"}')

    def test_never_raises_on_odd_event(self):
        for event in ("not json", 42, {"type": "assistant", "message": {"content": None}},
                      {"type": "result", "usage": "?"}):
            self.live.feed(event)
        self.text()


class ResultShaping(unittest.TestCase):
    def test_verdict_from_message(self):
        self.assertEqual(delegate._verdict_from_text('Here: {"verdict": "ok", "issues": []} done')["verdict"], "ok")
        self.assertIsNone(delegate._verdict_from_text("no json"))
        self.assertIsNone(delegate._verdict_from_text('{"verdict": "maybe"}'))

    def test_test_review_brief_caps_missing_tests(self):
        issues = [{"severity": "medium", "kind": "missing_test", "issue": f"m{i}"} for i in range(6)]
        issues += [{"severity": "high", "kind": "missing_test", "issue": "core"},
                   {"severity": "high", "kind": "wrong_test", "issue": "bad"}]
        brief = delegate._brief_test_review({"verdict": "concerns", "model": "x", "issues": issues})
        self.assertEqual([i["issue"] for i in brief["issues"]], ["bad", "core", "m0", "m1"])
        self.assertEqual(brief["more_missing_tests"], 4)

    def test_outline_headings(self):
        text = "# Outline\n## test/a.test.js\n- x\n## `tests/test_b.py` (unittest)\n- y\n## Notes\n"
        self.assertEqual(delegate._outline_files(text), ["test/a.test.js", "tests/test_b.py"])

    def test_resolve_tier(self):
        loop = LoopSettings(tiers={
            "normal": LoopTarget(provider="fake", model="s"),
            "hard": LoopTarget(provider="fake", model="o"),
        })

        class Settings:
            pass

        settings = Settings()
        settings.loop = loop
        self.assertEqual(delegate.resolve_tier(settings, "hard", None, None), [("fake", "o"), ("fake", "s")])
        self.assertEqual(delegate.resolve_tier(settings, "normal", None, None), [("fake", "s")])
        self.assertEqual(delegate.resolve_tier(settings, "hard", "p", "x"), [("p", "x")])
        self.assertEqual(delegate.resolve_tier(settings, "hard", None, "x"), [("fake", "x")])
        self.assertEqual(delegate.resolve_tier(settings, "normal", "p", None), [("p", None)])

    def test_count_check(self):
        session = {}
        t = lambda passed, total, skipped: {"passed": passed, "counts": {"total": total, "skipped": skipped}}
        check = delegate._check_test_counts
        self.assertIsNone(check(session, "h1", t(False, 1, 0)))
        self.assertIsNone(check(session, "h1", t(True, 42, 0)))
        self.assertIn("fewer", check(session, "h1", t(True, 30, 0)))
        self.assertIn("skipped", check(session, "h1", t(True, 42, 3)))
        self.assertIsNone(check(session, "h1", t(False, 5, 0)))  # a crash can hide tests: not flagged
        self.assertIsNone(check(session, "h2", t(True, 30, 0)))  # tests changed: history resets


class SettingsFromToml(unittest.TestCase):
    def test_loop_settings_parse(self):
        root = Path(tempfile.mkdtemp())
        cfg = root / "config.toml"
        cfg.write_text("""
[providers.fake]
driver = "copilot"
binary = "copilot"

[loop]
auto_max_rounds = 7
review_tests = false

[loop.tiers.normal]
provider = "fake"
model = "claude-sonnet-5"

[loop.review]
provider = "fake"
model = "gpt-5.6-sol"

[loop.review.hard]
model = "gpt-6-astra"
""")
        settings = config.load(extra=cfg)
        self.assertEqual(settings.loop.auto_max_rounds, 7)
        self.assertFalse(settings.loop.review_tests)
        self.assertEqual(settings.loop.tiers["normal"], LoopTarget(provider="fake", model="claude-sonnet-5"))
        self.assertEqual(settings.loop.review, LoopTarget(provider="fake", model="gpt-5.6-sol"))
        self.assertEqual(settings.loop.review_hard, LoopTarget(model="gpt-6-astra"))


if __name__ == "__main__":
    unittest.main()
