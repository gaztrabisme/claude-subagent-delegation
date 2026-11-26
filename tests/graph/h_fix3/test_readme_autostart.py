"""Full-hydrogen review C10: README "Durable graphs" must not say the worker is not started."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[3] / "README.md"


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    match = re.search(r"^#{1,3} Durable graphs\b.*?(?=^## (?!#)|\Z)", text, re.S | re.M)
    assert match, "README has no 'Durable graphs' section"
    return match.group(0)


def test_readme_does_not_claim_the_worker_is_not_started_for_you():
    section = _section().lower()
    assert not re.search(r"worker is not started( for you)?", section), \
        "README still says the worker is not started although [graph].autostart defaults to true"
    assert "not started for you" not in section


def test_readme_says_graph_start_autostarts_a_detached_worker():
    section = _section().lower()
    assert "autostart" in section and "worker" in section
    assert re.search(r"graph_start.{0,300}autostart|autostart.{0,300}graph_start", section, re.S), \
        "README does not tie graph_start to autostart of the worker"
