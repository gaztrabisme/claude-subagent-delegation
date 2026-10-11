"""Fail when a test that passed on Windows at 485e14c now fails (run win-guard-2026-10-11).

Run as `uv run pytest -q scripts/windows_regression_check.py`. It runs the whole
suite in a subprocess, compares its failures with the baseline list in
wiki/data/windows-pytest-485e14c.txt, and fails on any newly failing test or any
failure in tests/test_guard_windows.py.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.M)


def _failures(text: str) -> set[str]:
    return {match.strip() for match in LINE.findall(text)}


def test_no_windows_regression():
    baseline = _failures(
        (ROOT / "wiki" / "data" / "windows-pytest-485e14c.txt").read_text(encoding="utf-8")
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rfE", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    failing = _failures(result.stdout)
    new = sorted(
        test for test in failing
        if test not in baseline or test.startswith("tests/test_guard_windows.py")
    )
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(no output)"
    report = (
        f"suite: {summary}\nbaseline failures: {len(baseline)}; now failing: {len(failing)}; "
        f"fixed: {len(baseline - failing)}\n"
    )
    assert not new, report + f"NEWLY FAILING ({len(new)}):\n" + "\n".join(f"  {t}" for t in new)
    print(report)
