"""The lane registry, per-lane knobs, and what delegate does with a lane."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import anyio
import pytest

from subagent_mcp import runs
from subagent_mcp.lanes import load_lanes, omlx_settings_key
from subagent_mcp.runs import COMPLETED, Registry

from .conftest import make_settings
from .test_runs import FakeProcess, _result

SAMPLING_WORDS = ("temperature", "top_p", "top_k", "topp", "topk")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A HOME with no ~/.omlx, so no test reads the machine's real key."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def test_registry_lists_five_lanes_in_order(home):
    lanes = load_lanes({})
    assert list(lanes) == ["codex", "deepseek", "glm", "bppc", "omlx"]
    expected = {
        "codex": ("codex", "openai", None, None),
        "deepseek": ("claude", "deepseek", "https://api.deepseek.com/anthropic", "deepseek-v4-pro"),
        "glm": ("claude", "zai", "https://api.z.ai/api/anthropic", "glm-5.3-flash[1m]"),
        "bppc": ("claude", "local-llamacpp", None, "qwen3.8-27b"),
        "omlx": (
            "claude",
            "local-omlx",
            "http://127.0.0.1:8000",
            "Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B",
        ),
    }
    for name, (driver, provider, base_url, model) in expected.items():
        lane = lanes[name]
        assert lane.name == name
        assert (lane.driver, lane.provider, lane.base_url, lane.model) == (
            driver, provider, base_url, model,
        )
    assert lanes["glm"].api_key_envs == ("GLM_API_KEY", "ZAI_API_KEY")
    assert lanes["deepseek"].api_key_envs == ("DEEPSEEK_API_KEY",)
    assert lanes["codex"].api_key_envs == ()
    assert [n for n, lane in lanes.items() if lane.local] == ["bppc", "omlx"]
    assert [n for n, lane in lanes.items() if not lane.send_sampling] == ["omlx"]
    bppc, omlx = lanes["bppc"], lanes["omlx"]
    assert (bppc.max_agents, bppc.compact_window, bppc.default_api_key) == (1, 40960, "local")
    assert bppc.health_url is None
    assert (omlx.max_agents, omlx.compact_window) == (4, 98304)
    assert omlx.health_url == "http://127.0.0.1:8000/api/status"
    # Cloud lanes take the Settings defaults.
    glm = lanes["glm"]
    assert (glm.max_agents, glm.compact_window, glm.max_steps) == (4, 1_000_000, 40)
    assert (glm.run_timeout, glm.idle_timeout) == (1800.0, 900.0)


def test_per_lane_override_beats_global_beats_default(home):
    env = {"SAM_MAX_STEPS": "12", "SAM_OMLX_MAX_STEPS": "7", "SAM_RUN_TIMEOUT": "60"}
    lanes = load_lanes(env)
    assert lanes["omlx"].max_steps == 7
    assert lanes["glm"].max_steps == 12
    assert lanes["deepseek"].run_timeout == 60.0
    assert load_lanes({})["glm"].max_steps == 40

    env = {"SAM_OMLX_MAX_AGENTS": "2", "SAM_MAX_AGENTS": "9"}
    lanes = load_lanes(env)
    assert lanes["omlx"].max_agents == 2
    assert lanes["bppc"].max_agents == 9
    assert load_lanes({})["bppc"].max_agents == 1

    env = {
        "SAM_GLM_MODEL": "glm-5.3[1m]",
        "SAM_BPPC_BASE_URL": "http://192.0.2.1:8080/",
        "SAM_OMLX_COMPACT_WINDOW": "65536",
    }
    lanes = load_lanes(env)
    assert lanes["glm"].model == "glm-5.3[1m]"
    assert lanes["bppc"].base_url == "http://192.0.2.1:8080"
    assert lanes["omlx"].compact_window == 65536
    assert lanes["deepseek"].model == "deepseek-v4-pro"


def test_global_model_and_base_url_do_not_touch_every_lane(home):
    lanes = load_lanes({"SAM_MODEL": "x", "SAM_BASE_URL": "http://x"})
    assert lanes["omlx"].model == "Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B"
    assert lanes["glm"].base_url == "https://api.z.ai/api/anthropic"


def test_omlx_key_falls_back_to_omlx_settings(home, monkeypatch):
    assert load_lanes({})["omlx"].api_key({}) is None
    (home / ".omlx").mkdir()
    (home / ".omlx" / "settings.json").write_text(json.dumps({"auth": {"api_key": "from-file"}}))
    lane = load_lanes({})["omlx"]
    assert lane.default_api_key == "from-file"
    assert lane.api_key({}) == "from-file"
    assert lane.api_key({"SAM_OMLX_API_KEY": "from-env"}) == "from-env"


def test_omlx_key_missing_or_malformed_settings_is_none(home):
    (home / ".omlx").mkdir()
    settings = home / ".omlx" / "settings.json"
    settings.write_text(json.dumps({"auth": {}}))
    assert omlx_settings_key() is None
    settings.write_text("not json")
    assert omlx_settings_key() is None


def test_lane_key_order_and_default(home):
    lanes = load_lanes({})
    assert lanes["glm"].api_key({"ZAI_API_KEY": "z", "GLM_API_KEY": "g"}) == "g"
    assert lanes["glm"].api_key({"ZAI_API_KEY": "z"}) == "z"
    assert lanes["bppc"].api_key({}) == "local"
    assert lanes["deepseek"].api_key({}) is None


def test_default_lane_from_env(monkeypatch, home):
    from subagent_mcp.config import Settings

    monkeypatch.delenv("SAM_DEFAULT_LANE", raising=False)
    assert Settings.from_env().default_lane == "glm"
    monkeypatch.setenv("SAM_DEFAULT_LANE", "omlx")
    assert Settings.from_env().lane().name == "omlx"


# --- delegate ------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path, monkeypatch, home):
    """The server module, pointed at a registry whose spawns are recorded."""
    monkeypatch.setenv("SAM_SESSION_ROOT", str(tmp_path / "server-sessions"))
    monkeypatch.setenv("SAM_WORKSPACE", str(tmp_path))
    module = importlib.import_module("subagent_mcp.server")
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    spawned: list[FakeProcess] = []
    created: list[object] = []

    def spawn(argv, env, cwd):
        spawned.append(FakeProcess(_result("done"), argv, env))
        return spawned[-1]

    original = registry.create_agent

    def create_agent(*args, **kwargs):
        agent = original(*args, **kwargs)
        created.append(agent)
        return agent

    monkeypatch.setattr(runs, "_spawn_claude", spawn)
    monkeypatch.setattr(registry, "create_agent", create_agent)
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    module.spawned = spawned  # type: ignore[attr-defined]
    module.created = created  # type: ignore[attr-defined]
    yield module
    registry.shutdown()


def _delegate(server, tmp_path: Path, **kwargs):
    async def call():
        return await server.delegate(
            task="do it", verification="true", workspace=str(tmp_path), **kwargs
        )

    return anyio.run(call)


@pytest.mark.parametrize(
    ("kwargs", "words"),
    [
        ({"lane": "nope"}, "unknown lane"),
        ({"fallback": "sideways"}, "unknown fallback"),
        ({"lane": "codex"}, "not available in this build"),
        ({"lane": "bppc"}, "not available in this build"),
    ],
)
def test_delegate_rejects_without_spawning(server, tmp_path: Path, kwargs, words):
    out = _delegate(server, tmp_path, **kwargs)
    assert out["state"] == "rejected"
    assert words in out["error"]
    assert server.created == []
    assert server.spawned == []
    assert server.registry.agents() == []


def test_delegate_records_lane_and_fallback(server, tmp_path: Path):
    out = _delegate(server, tmp_path, lane="glm", fallback="none", wait_seconds=5)
    assert out["state"] == COMPLETED
    assert (out["lane"], out["fallback"]) == ("glm", "none")
    assert out["model"] == "glm-5.3-flash[1m]"
    agent = server.created[0]
    assert (agent.lane.name, agent.fallback) == ("glm", "none")
    run = agent.runs()[0]
    assert (run.lane, run.fallback) == ("glm", "none")
    assert agent.info()["lane"] == "glm"


def test_delegate_defaults_to_the_default_lane(server, tmp_path: Path):
    out = _delegate(server, tmp_path, wait_seconds=5)
    assert (out["lane"], out["fallback"]) == ("glm", "full")


def test_glm_lane_argv_and_env(server, tmp_path: Path):
    _delegate(server, tmp_path, lane="glm", wait_seconds=5)
    proc = server.spawned[0]
    argv = proc.argv
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Edit,Write,Grep,Glob"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--bare" not in argv
    assert argv[argv.index("--model") + 1] == "glm-5.3-flash[1m]"
    assert argv[argv.index("--max-turns") + 1] == "40"
    assert proc.env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert proc.env["ANTHROPIC_AUTH_TOKEN"] == "test-key"


def test_caller_model_beats_lane_model(server, tmp_path: Path):
    _delegate(server, tmp_path, lane="glm", model="glm-5.3[1m]", wait_seconds=5)
    argv = server.spawned[0].argv
    assert argv[argv.index("--model") + 1] == "glm-5.3[1m]"


def test_omlx_child_gets_lane_values_and_no_sampling(server, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAM_OMLX_API_KEY", "omlx-key")
    out = _delegate(server, tmp_path, lane="omlx", wait_seconds=5)
    assert out["state"] == COMPLETED
    proc = server.spawned[0]
    argv, env = proc.argv, proc.env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "omlx-key"
    assert "SAM_OMLX_API_KEY" not in env
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "98304"
    assert argv[argv.index("--model") + 1] == "Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B"
    settings_file = Path(argv[argv.index("--settings") + 1])
    payload = json.loads(settings_file.read_text())
    assert payload["autoCompactWindow"] == 98304

    haystacks = {
        "argv": " ".join(argv).lower(),
        "env": json.dumps(dict(env)).lower(),
        "settings": json.dumps(payload).lower(),
    }
    # A list of hits, not `word in text`, so a failure never prints the env.
    hits = [(where, w) for where, text in haystacks.items() for w in SAMPLING_WORDS if w in text]
    assert hits == []


def test_per_lane_agent_cap(tmp_path: Path, monkeypatch, home):
    monkeypatch.setenv("SAM_OMLX_API_KEY", "k")
    settings = make_settings(tmp_path)
    lanes = dict(settings.lanes)
    lanes.update(load_lanes({"SAM_OMLX_MAX_AGENTS": "1"}))
    settings = make_settings(tmp_path, lanes=lanes)
    reg = Registry(settings, start_reaper=False)
    try:
        reg.create_agent("a", tmp_path, lane="omlx")
        with pytest.raises(runs.RegistryError, match="lane 'omlx'"):
            reg.create_agent("b", tmp_path, lane="omlx")
        reg.create_agent("c", tmp_path, lane="glm")
    finally:
        reg.shutdown()
