"""N6 fix: run_demo.py must not dispatch to a real provider unless asked."""

from __future__ import annotations

import sys
import tomllib
from argparse import Namespace

from ._util import DEMO, README, load


def _parsed(monkeypatch, argv):
    mod = load(DEMO, "run_demo_under_test")
    seen = {}

    class Capture:
        def __init__(self, args):
            seen["args"] = args

        def run(self):
            return 0

    monkeypatch.setattr(mod, "Demo", Capture)
    monkeypatch.setattr(sys, "argv", ["run_demo.py", *argv])
    mod.main()
    return seen["args"]


def test_bare_run_uses_the_fake_dispatcher(monkeypatch):
    assert _parsed(monkeypatch, []).dispatcher == "fake"


def test_provider_flag_alone_does_not_turn_on_real_dispatch(monkeypatch):
    assert _parsed(monkeypatch, ["--provider", "deepseek"]).dispatcher == "fake"


def test_readme_real_provider_command_asks_for_the_registry_dispatcher():
    lines = [ln for ln in README.read_text(encoding="utf-8").splitlines()
             if "run_demo.py" in ln and "--provider" in ln]
    assert lines, "README has no real-provider command"
    for ln in lines:
        assert "--dispatcher registry" in ln, ln


def test_fake_mode_config_does_not_autostart(tmp_path):
    mod = load(DEMO, "run_demo_under_test2")
    args = Namespace(dispatcher="fake", provider="omlx", budget=0.5, max_minutes=1.0)
    demo = mod.Demo(args)
    run_dir = tmp_path / "run"
    demo.run_dir = run_dir
    demo.project = run_dir / "project"
    demo.state = run_dir / "graph-state"
    demo.trace = run_dir / "trace.jsonl"
    demo.setup()
    text = (demo.project / ".subagent" / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["graph"]["autostart"] is False
