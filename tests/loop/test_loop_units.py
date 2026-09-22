"""Unit tests for pure functions: test-count parsing, the live log, config, result shaping."""

import json
import tempfile
import unittest
from pathlib import Path

from subagent.loop import detect
from subagent.loop import loop as delegate
from subagent.loop.common import load_config
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

    def feed(self, kind, data):
        self.live.feed(json.dumps({"type": kind, "data": data, "timestamp": "2026-09-19T05:00:00Z"}))

    def text(self):
        self.live.close()
        self.sink.close()
        return (self.dir / "x.log").read_text()

    def test_cumulative_partial_output_printed_once(self):
        for out in ["hi\n", "hi\nby", "hi\nbye\n", "hi\nbye\nend\n"]:
            self.feed("tool.execution_partial_result", {"toolCallId": "c", "partialOutput": out})
        self.assertEqual([l.split("│ ")[1] for l in self.text().splitlines()], ["hi", "bye", "end"])

    def test_string_and_list_tool_arguments(self):
        # GPT models' apply_patch passes a plain string; this used to crash the reader thread.
        self.feed("tool.execution_start", {"toolCallId": "1", "toolName": "apply_patch", "arguments": "*** Begin Patch\nx"})
        self.feed("tool.execution_start", {"toolCallId": "2", "toolName": "odd", "arguments": ["a", "b"]})
        self.feed("tool.execution_complete", {"toolCallId": "1", "success": False, "error": "plain string"})
        self.feed("session.usage_checkpoint", {"totalNanoAiu": 4200000000})
        text = self.text()
        self.assertIn("apply_patch *** Begin Patch", text)
        self.assertIn("apply_patch failed: plain string", text)
        self.assertEqual(self.live.credits, 4.2)

    def test_error_object_and_last_message(self):
        self.feed("tool.execution_start", {"toolCallId": "1", "toolName": "create", "arguments": {"path": "a.js"}})
        self.feed("tool.execution_complete", {"toolCallId": "1", "success": False,
                                              "error": {"message": "Parent directory does not exist"}})
        self.feed("assistant.message", {"content": '{"verdict": "ok"}'})
        self.assertIn("create failed: Parent directory does not exist", self.text())
        self.assertEqual(self.live.last_message, '{"verdict": "ok"}')

    def test_never_raises_on_bad_event(self):
        self.live.feed('{"type": "assistant.turn_start", "data": {"turnId": "not a number"}}')
        self.assertIn("could not format an event", self.text())


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

    def test_resolve_models(self):
        cfg = {"models": {"normal": "s", "hard": "o"}, "model": None}
        self.assertEqual(delegate.resolve_models(cfg, "hard", None), ["o", "s"])
        self.assertEqual(delegate.resolve_models(cfg, "normal", None), ["s"])
        self.assertEqual(delegate.resolve_models(cfg, "hard", "x"), ["x"])
        self.assertEqual(delegate.resolve_models({**cfg, "model": "pin"}, "hard", None), ["pin"])

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


class Config(unittest.TestCase):
    def test_nested_maps_merge(self):
        root = Path(tempfile.mkdtemp())
        (root / ".delegate").mkdir()
        (root / ".delegate" / "config.json").write_text('{"review_models": {"hard": "gpt-6-astra"}}')
        cfg = load_config(root)
        self.assertEqual(cfg["review_models"], {"normal": "gpt-5.6-sol", "hard": "gpt-6-astra"})


if __name__ == "__main__":
    unittest.main()
