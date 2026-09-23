"""Shared helpers: an isolated sandbox with two fake providers, and small project builders.

The sandbox writes one config.toml declaring `fake` (a copilot driver whose
binary is `tests/fakes/fake_copilot.py`) and `fakeclaude` (a claude driver whose
binary is `tests/fakes/fake_claude.py`), points every `[loop]` slot at the
chosen driver's provider, and exports it as `SUBAGENT_CONFIG` so every CLI
process a test spawns reads the same settings.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
REPO = TESTS.parent
FAKES = TESTS / "fakes"
SCENARIOS = FAKES / "scenarios"
# The runner is a module now, not a script. `uv run` installs the package
# editable, so `-m subagent.cli` resolves; PYTHONPATH covers a bare checkout.
RUNNER = [sys.executable, "-m", "subagent.cli"]

ADD_TEST = """import { test } from 'node:test'; import assert from 'node:assert/strict'; import { add } from '../src/add.js';
test('adds', () => assert.equal(add(2, 3), 5));
test('adds negatives', () => assert.equal(add(-2, -3), -5));
"""


def need(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    return unittest.skipIf(missing, f"needs {', '.join(missing)}")


class Sandbox(unittest.TestCase):
    """Each test gets a temp dir, two fake providers, and its own HOME/config (no live view)."""

    # Which fake provider the loop slots point at; the round/autopilot/outline/
    # review classes parametrise this over ("copilot", "claude").
    DRIVER = "copilot"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="delegate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "copilot").symlink_to(FAKES / "fake_copilot.py")
        (bin_dir / "claude").symlink_to(FAKES / "fake_claude.py")
        home = self.tmp / "home"
        home.mkdir()
        src = str(REPO / "src")
        pythonpath = os.pathsep.join([src, os.environ["PYTHONPATH"]]) if os.environ.get("PYTHONPATH") else src
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config"), "SUBAGENT_LIVE_VIEW": "off",
                    "PYTHONPATH": pythonpath,
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}
        self.config = self.tmp / "config.toml"
        self.write_config()

    def write_config(self, **loop_overrides):
        """The sandbox config: two fake providers, `[loop]` slots on self.DRIVER.

        Scalar `[loop]` keys are overridable per test (e.g. review_tests=False);
        the provider/model slots are fixed for the chosen driver.
        """
        provider = "fake" if self.DRIVER == "copilot" else "fakeclaude"
        loop = {
            "review_tests": True,
            "auto_max_rounds": 4,
            "auto_review": True,
            "auto_review_cycles": 2,
            "count_tests": True,
            "keep_checkpoints": 20,
            "tiers": {"normal": {"model": "claude-sonnet-5"}, "hard": {"model": "claude-opus-5"}},
            "review": {"model": "gpt-5.6-sol"},
            "review_hard": {"model": "gpt-5.6-sol"},
            "test_writer": {"model": "gpt-5.6-sol"},
        }
        loop.update(loop_overrides)
        scalars = {k: v for k, v in loop.items() if not isinstance(v, dict)}
        targets = (("tiers.normal", loop["tiers"]["normal"]), ("tiers.hard", loop["tiers"]["hard"]),
                   ("review", loop["review"]), ("review.hard", loop["review_hard"]),
                   ("test_writer", loop["test_writer"]))

        def lit(value):
            return "true" if value is True else "false" if value is False else repr(value)

        lines = [
            "[core]",
            "default_provider = \"fake\"",
            "run_timeout = 60",
            f'session_root = "{self.tmp / "sessions"}"',
            ('child_env_passthrough = ["FAKE_SCRIPT", "FAKE_MODEL", "FAKE_PROMPT", '
             '"FAKE_FAIL_MODEL", "AUTO_SCENARIO", "OUT", "REVIEWER", "TR"]'),
            "",
            "[guard]",
            "supervisor = \"deterministic\"",
            f'approval_socket = "{self.tmp / "approval.sock"}"',
            "",
            "[providers.fake]",
            "driver = \"copilot\"",
            f'binary = "{FAKES / "fake_copilot.py"}"',
            "loop = true",
            "",
            "[providers.fakeclaude]",
            "driver = \"claude\"",
            f'binary = "{FAKES / "fake_claude.py"}"',
            "base_url = \"http://127.0.0.1:1\"",
            "api_key = \"fake\"",
            "",
            "[loop]",
        ]
        for key, value in scalars.items():
            lines.append(f"{key} = {lit(value)}")
        for table, target in targets:
            lines += ["", f"[loop.{table}]", f'provider = "{provider}"', f'model = "{target["model"]}"']
        lines.append("")
        self.config.write_text("\n".join(lines))
        self.env["SUBAGENT_CONFIG"] = str(self.config)
        return self.config

    # ------------------------------------------------------------ running the CLI

    def delegate(self, *args, cwd, scenario=None, env=None, stdin=None, timeout=120):
        """Run the delegate loop; returns (exit_code, parsed JSON or raw stdout)."""
        run_env = {**self.env, **(env or {})}
        if scenario:
            run_env["FAKE_SCRIPT"] = str(SCENARIOS / f"{scenario}.sh")
        proc = subprocess.run([*RUNNER, *args], cwd=cwd, env=run_env, input=stdin,
                              capture_output=True, text=True, timeout=timeout)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            return proc.returncode, proc.stdout + proc.stderr

    def sh(self, command, cwd):
        subprocess.run(command, shell=True, cwd=cwd, env=self.env, check=True, capture_output=True)

    # ------------------------------------------------------------ projects

    def git_init(self, path):
        self.sh("git init -q -b main && git add -A && git commit -qm init", path)

    def node_project(self, name="proj", git=True, tests=True):
        """A node:test project with tests for src/add.js and a plan in .subagent/PLAN.md."""
        root = self.tmp / name
        (root / ".subagent").mkdir(parents=True)
        (root / "package.json").write_text('{"name":"p","type":"module","scripts":{"test":"node --test"}}')
        (root / ".gitignore").write_text("node_modules/\n")
        if tests:
            (root / "test").mkdir()
            (root / "test" / "add.test.js").write_text(ADD_TEST)
        if git:
            self.git_init(root)
        (root / ".subagent" / "PLAN.md").write_text("implement add in src/add.js")
        return root

    def read(self, root, rel):
        return (root / rel).read_text()


# --- driver parametrisation -----------------------------------------------------

def for_drivers(base):
    """Instantiate `base` once per fake driver, named `<base>_<driver>`.

    The subclasses are registered in `base`'s own module (so pytest collects
    them) and the base class is hidden, so the copilot driver does not run
    twice. Returns the two subclasses.
    """
    module = sys.modules[base.__module__]
    subclasses = []
    for driver in ("copilot", "claude"):
        name = f"{base.__name__}_{driver}"
        cls = type(name, (base,), {"DRIVER": driver, "__module__": base.__module__,
                                   "__test__": True})
        setattr(module, name, cls)
        subclasses.append(cls)
    base.__test__ = False
    return subclasses
