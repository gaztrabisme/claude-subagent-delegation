"""Shared fixtures for the R1a round-6 tests: a fake owner home and real driver profiles."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from subagent.runs import Registry
from tests.conftest import make_settings

# (lane id, provider name, provider table entry minus binary)
LANES = {
    "claude-api": {"driver": "claude", "base_url": "https://api.z.ai/api/anthropic",
                   "model": "glm-test", "api_key_env": "GLM_API_KEY"},
    "claude-login": {"driver": "claude", "auth": "login", "model": "sonnet"},
    "codex": {"driver": "codex"},
    "omp": {"driver": "omp", "base_url": "http://127.0.0.1:8000/v1", "model": "qwen-test",
            "experimental": True},
    "dsh-api": {"driver": "dsh", "vendor": "deepseek", "api_key_env": "DEEPSEEK_API_KEY",
                "experimental": True},
    "dsh-login": {"driver": "dsh", "vendor": "deepseek", "auth": "login",
                  "experimental": True},
}
LANE_IDS = tuple(LANES)
HOME_ENV = {  # the child env variable that names each driver's CLI home
    "claude-api": "CLAUDE_CONFIG_DIR", "claude-login": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME", "omp": "PI_CODING_AGENT_DIR", "dsh-api": "DSH_HOME",
    "dsh-login": "DSH_HOME",
}


class Lane:
    """One captured spawn: argv, env, profile text and the surrounding paths."""

    def __init__(self, lane_id, ws, owner, settings, argv, env, agent_id):
        self.lane_id, self.ws, self.owner, self.settings = lane_id, ws, owner, settings
        self.argv, self.env, self.agent_id = argv, env, agent_id
        self.agent_dir = settings.session_root / "agents" / agent_id

    @property
    def profile(self) -> str:
        assert self.argv[:2] == ["/usr/bin/sandbox-exec", "-p"], self.argv[:3]
        return self.argv[2]

    @property
    def child_home(self) -> Path | None:
        value = self.env.get(HOME_ENV[self.lane_id])
        return Path(value) if value else None


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_owner_home(tmp_path: Path, monkeypatch) -> Path:
    """A fake owner home with CLI state, and HOME / Path.home() pointed at it."""
    owner = (tmp_path / "ownerhome").resolve()
    for rel in (
        ".claude/settings.json", ".claude/CLAUDE.md", ".claude/.credentials.json",
        ".claude/statsig/gate", ".claude/todos/t.json", ".claude/debug/d.log",
        ".claude/shell-snapshots/snap.sh", ".claude/projects/p/memory/MEMORY.md",
        ".claude.json", ".claude.json.backup",
        ".codex/auth.json", ".codex/config.toml", ".codex/hooks.json",
        ".codex/AGENTS.md", ".codex/skills/s/SKILL.md",
        ".omp/agent/config.yml", ".omp/agent/models.yml", ".omp/agent/AGENTS.md",
        ".dsh/.credentials.yaml", ".dsh/.env", ".dsh/storages/s.json",
        ".dsh/profiles/p/cordis.yml", ".dsh/sessions/old.json",
        ".zshrc", "owner-notes.txt",
    ):
        _write(owner / rel, "{}\n" if rel.endswith(".json") else "owner\n")
    monkeypatch.setenv("HOME", str(owner))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: owner))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "DSH_OWNER_HOME", "DSH_HOME",
                "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GLM_API_KEY", "test-glm")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh")
    return owner


def capture_lane(tmp_path: Path, monkeypatch, lane_id: str) -> Lane:
    """Build the real spawn argv and env for one lane without running anything."""
    owner = make_owner_home(tmp_path, monkeypatch)
    ws = (tmp_path / "ws").resolve()
    _write(ws / "README.md")
    (ws / ".codex").mkdir()
    _write(ws / ".codex" / "config.toml", "# project codex config\n")
    fake_bin = tmp_path / "bin" / "fake"
    _write(fake_bin, "#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    cfg = {**LANES[lane_id], "binary": str(fake_bin)}
    sock_dir = (tmp_path / "sockdir").resolve()
    sock_dir.mkdir(mode=0o700)
    settings = make_settings(
        tmp_path, workspace=str(ws), providers={lane_id: cfg},
        approval_socket=str(sock_dir / "approval.sock"),
        max_agents=4, supervisor="off", trace="off",
    )
    captured: list[tuple[list[str], dict]] = []

    def fake_start(self, *, stdin=False, closed_stdin=False):
        captured.append((list(self.argv), dict(getattr(self, "env", {}) or {})))
        raise RuntimeError("captured-spawn")

    monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
    agent_id = f"r6-{lane_id}"
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent(agent_id, ws, provider=lane_id, agent_id=agent_id)
        run = agent.submit("do work", verification="true")
        run.done.wait(timeout=10)
    finally:
        reg.shutdown()
    assert captured, f"{lane_id} did not reach Process._start"
    argv, env = captured[0]
    return Lane(lane_id, ws, owner, settings, argv, env, agent_id)


def can_nest_seatbelt() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def skip_unless_seatbelt() -> None:
    if not can_nest_seatbelt():
        pytest.skip("Seatbelt cannot nest in this runner (or not macOS); the live probe did not run")


def write_allow_roots(profile: str) -> list[str]:
    """Every path named in an `(allow file-write* ...)` rule of the profile, in order."""
    roots: list[str] = []
    for line in profile.splitlines():
        line = line.strip()
        if line.startswith("(allow file-write*"):
            roots.extend(m.replace('\\"', '"').replace("\\\\", "\\")
                         for m in re.findall(r'\((?:subpath|literal) "((?:[^"\\]|\\.)*)"\)', line))
    return roots


def run_probe(profile: str, targets: list[Path]) -> dict[str, str]:
    """Try to open each target for append under the profile; map path -> 'ok' or error name."""
    script = (
        "import json, sys\n"
        "out = {}\n"
        "for p in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        open(p, 'ab').close()\n"
        "        out[p] = 'ok'\n"
        "    except OSError as e:\n"
        "        out[p] = type(e).__name__\n"
        "print(json.dumps(out))\n"
    )
    import json
    proc = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", script,
         json.dumps([str(t) for t in targets])],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, f"probe failed rc={proc.returncode} stderr={proc.stderr!r}"
    return json.loads(proc.stdout.strip().splitlines()[-1])
