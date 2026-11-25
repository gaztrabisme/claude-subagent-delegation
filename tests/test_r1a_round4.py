"""R1a fix round 4: regression tests for the security route's findings.

1. claude login: the owner's config directory is writable only for the CLI's own
   state, never for what the owner's sessions load.
2. claude-home settings.json and omp's config files are write-denied.
3. A workspace that contains the session root is refused.
4. Every A9 profile denies Apple Events, LaunchServices, ServiceManagement,
   nsurlsessiond and the tools that drive them.
5. codex: no /dev subpath and no user cache; a per-agent cache instead.
6. A12: a DSH tamper flag stays set for the agent's whole life.
7. approval_hook.py exits 2, never 1, even when its timeout or stdout is broken.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from subagent import sandbox
from subagent.guard.supervisor import Supervisor
from subagent.providers import claude as claude_driver
from subagent.providers import omp as omp_driver
from subagent.runs import Registry, RegistryError
from tests.conftest import make_settings
from tests.r1a._helpers import APPROVAL_HOOK, sbpl_allows
from tests.test_runs import FakeProcess, _result

HOOK_DRIVERS = ("claude", "codex", "omp", "dsh")


def _can_nest() -> bool:
    try:
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except OSError:
        return False


def _setup(tmp_path: Path, monkeypatch, *, claude_config: Path | None = None):
    """Providers for every hook driver, a fake home and a fake binary."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    if claude_config is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        (home / ".claude").mkdir()
    else:
        claude_config.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))
    ws = tmp_path / "ws"
    ws.mkdir()
    fake_bin = tmp_path / "bin" / "fake"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    codex_home = tmp_path / "user-codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("DSH_OWNER_HOME", str(tmp_path / "owner-dsh"))
    (tmp_path / "owner-dsh").mkdir()
    monkeypatch.setenv("GLM_API_KEY", "test-glm")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh")
    providers = {
        "claude": {"driver": "claude", "base_url": "https://api.z.ai/api/anthropic",
                   "model": "glm-5.3-flash[1m]", "api_key_env": "GLM_API_KEY",
                   "binary": str(fake_bin)},
        "claude_login": {"driver": "claude", "auth": "login", "model": "sonnet",
                         "binary": str(fake_bin), "max_agents": 1},
        "codex": {"driver": "codex", "binary": str(fake_bin)},
        "omp": {"driver": "omp", "base_url": "http://127.0.0.1:8000/v1", "model": "qwen-test",
                "binary": str(fake_bin), "experimental": True},
        "dsh": {"driver": "dsh", "vendor": "deepseek", "api_key_env": "DEEPSEEK_API_KEY",
                "binary": str(fake_bin), "experimental": True},
    }
    settings = make_settings(tmp_path, workspace=str(ws), providers=providers, max_agents=8,
                             supervisor="off", trace="off")
    return ws, settings


def _capture(monkeypatch, settings, ws: Path, provider: str, agent_id: str):
    """The argv and env the first spawn of `provider` would start."""
    captured: list[tuple[list[str], dict[str, str]]] = []

    def fake_start(self, *, stdin=False, closed_stdin=False):
        captured.append((list(self.argv), dict(self.env)))
        raise RuntimeError("captured-spawn")

    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent(provider, ws, provider=provider, agent_id=agent_id)
        run = agent.submit("do work", verification="true")
        run.done.wait(timeout=5)
    finally:
        reg.shutdown()
    assert captured, f"{provider} did not start"
    argv, env = captured[0]
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    return argv, env


def _run_under(profile: str, code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code, *args],
        capture_output=True, text=True, timeout=20,
    )


_WRITE_PROBE = (
    "import sys\n"
    "try:\n"
    "    open(sys.argv[1], 'a').write('x')\n"
    "except PermissionError:\n"
    "    sys.exit(3)\n"
)


# --- 1. claude login config directory ---------------------------------------------


@pytest.mark.parametrize("named", [True, False], ids=["CLAUDE_CONFIG_DIR", "default"])
def test_login_profile_writes_only_cli_state_in_owner_config(tmp_path, monkeypatch, named):
    config = tmp_path / "owner-claude" if named else None
    ws, settings = _setup(tmp_path, monkeypatch, claude_config=config)
    config = config or (tmp_path / "home" / ".claude")
    argv, _ = _capture(monkeypatch, settings, ws, "claude_login", "login1")
    profile = argv[2]

    project = claude_driver._project_dir_names(ws)[0]
    for allowed in (config / "projects" / project / "s.jsonl", config / "todos" / "t.json"):
        assert sbpl_allows(profile, "file-write*", allowed), allowed
    denied = [config / name for name in (
        "settings.json", "settings.local.json", "CLAUDE.md", "hooks/h.sh", "agents/a.md",
        "commands/c.md", "skills/s/SKILL.md", "plugins/p.json", "output-styles/o.md",
        "shell-snapshots/snapshot.sh", "session-env/e", "ide/1.lock", "local/claude",
        ".claude.json", "projects/-other-project/s.jsonl",
    )]
    denied.append(config)  # renaming the directory itself
    if not named:
        denied.append(tmp_path / "home" / ".claude.json")
    for path in denied:
        assert not sbpl_allows(profile, "file-write*", path), path
    assert f'(subpath "{config.resolve()}")' not in profile.split("\n")[3]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt runtime check")
def test_login_profile_live_blocks_owner_settings_even_through_a_symlink(tmp_path, monkeypatch):
    if not _can_nest():
        pytest.skip("Seatbelt cannot nest inside this runner")
    config = tmp_path / "owner-claude"
    ws, settings = _setup(tmp_path, monkeypatch, claude_config=config)
    argv, _ = _capture(monkeypatch, settings, ws, "claude_login", "login2")
    profile = argv[2]
    (config / "settings.json").write_text("{}\n", encoding="utf-8")
    link = ws / "innocent.json"
    link.symlink_to(config / "settings.json")
    project = config / "projects" / claude_driver._project_dir_names(ws)[0]
    project.mkdir(parents=True)

    assert _run_under(profile, _WRITE_PROBE, str(config / "settings.json")).returncode == 3
    assert _run_under(profile, _WRITE_PROBE, str(link)).returncode == 3
    assert _run_under(profile, _WRITE_PROBE, str(config / "CLAUDE.md")).returncode == 3
    assert _run_under(profile, _WRITE_PROBE, str(project / "s.jsonl")).returncode == 0
    assert (config / "settings.json").read_text(encoding="utf-8") == "{}\n"


# --- 2. claude-home settings.json and omp config ------------------------------------


def test_claude_home_settings_and_autoloads_are_write_denied(tmp_path, monkeypatch):
    ws, settings = _setup(tmp_path, monkeypatch)
    argv, env = _capture(monkeypatch, settings, ws, "claude", "c1")
    home = Path(env["CLAUDE_CONFIG_DIR"])
    profile = argv[2]
    for name in ("settings.json", "CLAUDE.md", "hooks/x", "agents/x.md", "plugins/x"):
        assert not sbpl_allows(profile, "file-write*", home / name), name
    assert not sbpl_allows(profile, "file-write*", home)  # no rename of the home
    assert sbpl_allows(profile, "file-write*", home / "projects" / "x.jsonl")
    assert sbpl_allows(profile, "file-write*", home / ".claude.json")
    assert str(home / "settings.json") in claude_driver.sandbox_write_deny(
        settings, "c1", settings.provider("claude"), ws)


def test_omp_config_models_and_extensions_are_write_denied(tmp_path, monkeypatch):
    ws, settings = _setup(tmp_path, monkeypatch)
    argv, env = _capture(monkeypatch, settings, ws, "omp", "o1")
    home = Path(env["PI_CODING_AGENT_DIR"])
    profile = argv[2]
    for name in ("config.yml", "models.yml", "extensions/x.ts", "hooks/x.ts", "AGENTS.md"):
        assert not sbpl_allows(profile, "file-write*", home / name), name
    assert not sbpl_allows(profile, "file-write*", home)
    assert sbpl_allows(profile, "file-write*", home / "cache.bin")
    assert str(home / "config.yml") in omp_driver.sandbox_write_deny(
        settings, "o1", settings.provider("omp"), ws)


# --- 3. workspace containing the session root ---------------------------------------


@pytest.mark.parametrize("layout", ["inside", "equal", "symlink"])
def test_workspace_containing_session_root_is_refused(tmp_path, layout):
    ws = tmp_path / "proj"
    ws.mkdir()
    if layout == "inside":
        session_root = ws / ".subagent" / "sessions"
    elif layout == "equal":
        session_root = ws
    else:
        real = ws / "state"
        real.mkdir()
        session_root = tmp_path / "state-link"
        session_root.symlink_to(real, target_is_directory=True)
    (tmp_path / "cfg").mkdir()
    settings = make_settings(tmp_path / "cfg", workspace=str(ws), session_root=str(session_root))
    assert "session root" in (settings.workspace_refusal(ws) or "")
    reg = Registry(settings, start_reaper=False)
    try:
        with pytest.raises(RegistryError, match="session root"):
            reg.create_agent("t", ws, provider="glm")
    finally:
        reg.shutdown()


def test_workspace_beside_session_root_is_accepted(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    (tmp_path / "cfg").mkdir()
    settings = make_settings(tmp_path / "cfg", workspace=str(ws),
                             session_root=str(tmp_path / "state"))
    assert settings.workspace_refusal(ws) is None


# --- 4. IPC escapes ------------------------------------------------------------------


IPC_RULES = (
    "(deny appleevent-send)",
    "(deny mach-lookup " + " ".join(
        f'(global-name "{name}")' for name in sandbox.IPC_ESCAPE_MACH_SERVICES) + ")",
    "(deny process-exec " + " ".join(
        f'(literal "{path}")' for path in sandbox.IPC_ESCAPE_EXECUTABLES) + ")",
)


@pytest.mark.parametrize("driver", HOOK_DRIVERS)
def test_every_a9_profile_denies_ipc_escapes(tmp_path, monkeypatch, driver):
    ws, settings = _setup(tmp_path, monkeypatch)
    argv, _ = _capture(monkeypatch, settings, ws, driver, f"ipc-{driver}")
    lines = argv[2].splitlines()
    for rule in IPC_RULES:
        assert rule in lines, (driver, rule)
    assert {"com.apple.coreservices.launchservicesd", "com.apple.xpc.smd",
            "com.apple.nsurlsessiond"} <= set(sandbox.IPC_ESCAPE_MACH_SERVICES)
    assert set(sandbox.IPC_ESCAPE_EXECUTABLES) == {
        "/bin/launchctl", "/usr/bin/open", "/usr/bin/osascript"}


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt runtime check")
@pytest.mark.parametrize("driver", HOOK_DRIVERS)
def test_every_a9_profile_loads_and_blocks_the_escape_tools(tmp_path, monkeypatch, driver):
    if not _can_nest():
        pytest.skip("Seatbelt cannot nest inside this runner")
    ws, settings = _setup(tmp_path, monkeypatch)
    argv, _ = _capture(monkeypatch, settings, ws, driver, f"live-{driver}")
    profile = argv[2]
    ok = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/true"],
                        capture_output=True, text=True, timeout=10)
    assert ok.returncode == 0, ok.stderr
    devnull = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", "echo hi > /dev/null"],
        capture_output=True, text=True, timeout=10)
    assert devnull.returncode == 0, devnull.stderr
    for tool in sandbox.IPC_ESCAPE_EXECUTABLES:
        blocked = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, tool],
                                 capture_output=True, text=True, timeout=10)
        assert blocked.returncode != 0 and "Operation not permitted" in blocked.stderr, tool


# --- 5. codex devices and cache --------------------------------------------------------


def test_codex_profile_has_standard_devices_and_a_per_agent_cache(tmp_path, monkeypatch):
    ws, settings = _setup(tmp_path, monkeypatch)
    user_cache = tmp_path / "user-cache"
    monkeypatch.setattr("subagent.providers.codex._darwin_user_cache_dir",
                        lambda: str(user_cache))
    argv, env = _capture(monkeypatch, settings, ws, "codex", "x1")
    profile = argv[2]
    agent_dir = settings.session_root / "agents" / "x1"
    assert '(subpath "/dev")' not in profile
    assert not sbpl_allows(profile, "file-write*", Path("/dev/ttys000"))
    assert sbpl_allows(profile, "file-write*", Path("/dev/null"))
    assert not sbpl_allows(profile, "file-write*", user_cache / "x")
    assert str(user_cache) not in profile
    cache = Path(env["XDG_CACHE_HOME"])
    assert cache.resolve() == (agent_dir / "cache").resolve() and cache.is_dir()
    assert sbpl_allows(profile, "file-write*", cache / "x")


# --- 6. A12 tamper flag for the agent's life ---------------------------------------------


def test_dsh_tamper_flag_survives_a_later_launch(tmp_path, monkeypatch):
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("bootstrap\n", encoding="utf-8")

    def spawn(argv, env, cwd):
        process = FakeProcess(_result("ok"), argv, env)
        process.cordis_path = cordis
        return process

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    settings = make_settings(tmp_path, workspace=str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("t", tmp_path / "ws", provider="glm")
        first = agent.submit("one", verification="true")
        assert first.done.wait(10)
        agent.guard_context["dsh_guard_tampered"] = True
        agent.guard_context["dsh_guard_tamper_traced"] = True
        second = agent.submit("two", verification="true")
        assert second.done.wait(10)
        assert agent.guard_context["dsh_guard_tampered"] is True
        assert agent.guard_context["dsh_guard_tamper_traced"] is True
        supervisor = Supervisor(settings, registry=reg)
        assert supervisor._dsh_tampered(agent.agent_id, agent, agent.guard_context)
    finally:
        reg.shutdown()


# --- 7. hook exit status ---------------------------------------------------------------


def _hook(env_extra: dict[str, str], *, stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "SUBAGENT_APPROVAL_SOCKET": "/nonexistent.sock",
           **env_extra}
    return subprocess.run(
        [sys.executable, str(APPROVAL_HOOK), "--agent", "a1", "--key-file", "/nonexistent"],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}),
        stdout=stdout, stderr=subprocess.PIPE, text=True, env=env, timeout=20,
    )


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf"])
def test_hook_invalid_timeout_denies_with_exit_2(value):
    result = _hook({"SUBAGENT_HOOK_TIMEOUT": value})
    assert result.returncode == 2, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_hook_module_imports_with_an_invalid_timeout():
    result = subprocess.run(
        [sys.executable, "-c", "import runpy, sys; runpy.run_path(sys.argv[1], run_name='x')",
         str(APPROVAL_HOOK)],
        env={"PATH": os.environ.get("PATH", ""), "SUBAGENT_HOOK_TIMEOUT": "abc"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_hook_exits_2_when_stdout_cannot_be_written(tmp_path):
    target = tmp_path / "ro"
    target.write_text("", encoding="utf-8")
    with open(target, "rb") as read_only:
        result = _hook({}, stdout=read_only)
    assert result.returncode == 2, result.stderr


# --- round 5: the claude child's adapter proxy is reachable --------------------------


def test_claude_profile_allows_the_adapter_proxy_the_child_dials(tmp_path, monkeypatch):
    """A lane with an adapter (omlx: strip_sampling) points ANTHROPIC_BASE_URL at a
    loopback adapter proxy on a random port; the profile must allow that port, and the
    upstream port only the server-side adapter dials need not be open to the child."""
    from urllib.parse import urlsplit

    from subagent import adapter

    ws = tmp_path / "ws"
    ws.mkdir()
    providers = {"lane": {"driver": "claude", "base_url": "http://127.0.0.1:8000",
                          "model": "m", "api_key": "local", "local": True,
                          "send_sampling": False, "adapter": "strip_sampling"}}
    settings = make_settings(tmp_path, workspace=str(ws), providers=providers,
                             default_provider="lane", supervisor="off", trace="off")
    try:
        argv, env = _capture(monkeypatch, settings, ws, "lane", "ad1")
        child_url = urlsplit(env["ANTHROPIC_BASE_URL"])
        assert child_url.hostname == "127.0.0.1" and child_url.port not in (None, 8000)
        lines = argv[2].splitlines()
        assert f'(allow network-outbound (remote ip "localhost:{child_url.port}"))' in lines
        assert '(allow network-outbound (remote ip "localhost:8000"))' not in lines
    finally:
        adapter.shutdown()
