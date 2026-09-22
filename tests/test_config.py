"""The config file: layering, validation, providers, and what delegate does
with the provider it picks."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import anyio
import pytest

from subagent import config, health, runs
from subagent.config import ConfigError
from subagent.providers import claude as claude_provider
from subagent.runs import COMPLETED, Registry

from .conftest import default_providers, make_settings, write_config
from .test_runs import FakeProcess, _result

SAMPLING_WORDS = ("temperature", "top_p", "top_k", "topp", "topk")

GLM = {
    "driver": "claude",
    "base_url": "https://api.z.ai/api/anthropic",
    "model": "glm-5.3-flash[1m]",
    "api_key_env": "GLM_API_KEY",
}


def _load(tmp_path: Path, tables: dict, name: str = "c.toml"):
    return config.load(extra=write_config(tmp_path / name, tables))


# --- layering and merging ---------------------------------------------------------


def test_config_layers_xdg_then_project_then_named_file(tmp_path: Path, monkeypatch):
    xdg = tmp_path / "xdg"
    (xdg / "subagent").mkdir(parents=True)
    write_config(xdg / "subagent" / "config.toml", {
        "core": {"max_agents": 1, "summary_tokens": 11},
        "providers": {"glm": GLM},
    })
    project = tmp_path / "project"
    (project / ".subagent").mkdir(parents=True)
    write_config(project / ".subagent" / "config.toml", {"core": {"max_agents": 2}})
    named = write_config(tmp_path / "named.toml", {"core": {"max_agents": 3}})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv(config.CONFIG_ENV, str(named))

    assert config.load().max_agents == 3
    assert config.load(project_root=project).max_agents == 3
    monkeypatch.delenv(config.CONFIG_ENV)
    assert config.load(project_root=project).max_agents == 2
    # Layers merge: a key only the first file set survives the later ones.
    assert config.load(project_root=project).summary_tokens == 11
    assert list(config.load().providers) == ["glm"]


def test_config_merges_tables_key_by_key(tmp_path: Path, monkeypatch):
    xdg = tmp_path / "xdg"
    (xdg / "subagent").mkdir(parents=True)
    write_config(xdg / "subagent" / "config.toml", {
        "providers": {"glm": {**GLM, "max_steps": 9}},
    })
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    settings = _load(tmp_path, {"providers": {"glm": {"model": "glm-other"}}})
    glm = settings.provider("glm")
    # Only `model` was overridden; the rest of the provider's table survives.
    assert (glm.model, glm.driver, glm.max_steps) == ("glm-other", "claude", 9)
    assert glm.base_url == "https://api.z.ai/api/anthropic"


def test_config_a_missing_file_is_not_an_error(tmp_path: Path):
    assert config.load(extra=tmp_path / "nothing.toml").providers == {}


def test_config_broken_toml_says_which_file(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text("[core\n")
    with pytest.raises(ConfigError, match="bad.toml"):
        config.load(extra=path)


# --- providers --------------------------------------------------------------------


def test_config_no_providers_is_an_error_only_when_one_is_needed(tmp_path: Path):
    settings = _load(tmp_path, {"core": {"max_agents": 2}})
    assert settings.providers == {} and settings.default_provider == ""
    with pytest.raises(ConfigError, match="no providers configured"):
        settings.require_providers()


def test_config_unknown_driver_is_refused(tmp_path: Path):
    with pytest.raises(ConfigError, match="unknown driver 'telepathy'"):
        _load(tmp_path, {"providers": {"x": {"driver": "telepathy"}}})
    with pytest.raises(ConfigError, match="no driver"):
        _load(tmp_path, {"providers": {"x": {"model": "m"}}})


def test_config_a_ported_cli_driver_is_available(tmp_path: Path):
    settings = _load(tmp_path, {"providers": {"c": {"driver": "copilot", "model": "m"}}})
    cfg = settings.provider("c")
    assert cfg.vendor == "copilot"
    # The Copilot CLI owns its own connection; only its binary is checked, at boot.
    assert cfg.unavailable() is None


def test_config_undeclared_provider_in_the_chain_is_refused(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"\[fallback\].chain names 'nope'"):
        _load(tmp_path, {"providers": {"glm": GLM}, "fallback": {"chain": ["glm", "nope"]}})


@pytest.mark.parametrize("loop", [
    {"tiers": {"hard": {"provider": "nope"}}},
    {"review": {"provider": "nope"}},
    {"test_writer": {"provider": "nope"}},
])
def test_config_undeclared_provider_in_the_loop_is_refused(tmp_path: Path, loop):
    with pytest.raises(ConfigError, match="'nope', which is not declared"):
        _load(tmp_path, {"providers": {"glm": GLM}, "loop": loop})


def test_config_loop_tables_that_name_declared_providers_are_kept(tmp_path: Path):
    settings = _load(tmp_path, {
        "providers": {"glm": GLM},
        "loop": {"auto_max_rounds": 4, "tiers": {"normal": {"provider": "glm"}},
                 "review": {"provider": "glm"}},
    })
    assert settings.loop.auto_max_rounds == 4
    assert settings.loop.tiers["normal"].provider == "glm"
    assert settings.loop.review.provider == "glm"


def test_config_default_provider_must_be_declared(tmp_path: Path):
    with pytest.raises(ConfigError, match="default_provider 'nope'"):
        _load(tmp_path, {"core": {"default_provider": "nope"}, "providers": {"glm": GLM}})
    # Unset, it is the first declared provider.
    assert _load(tmp_path, {"providers": {"glm": GLM}}).default_provider == "glm"


def test_config_agent_supervisor_needs_a_command(tmp_path: Path):
    with pytest.raises(ConfigError, match="needs \\[guard\\].supervisor_cmd"):
        _load(tmp_path, {"guard": {"supervisor": "agent"}, "providers": {"glm": GLM}})
    settings = _load(tmp_path, {
        "guard": {"supervisor": "agent", "supervisor_cmd": "claude -p"},
        "providers": {"glm": GLM},
    })
    assert settings.supervisor_cmd == "claude -p"


def test_config_api_key_env_takes_a_string_or_a_list(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "z")
    settings = _load(tmp_path, {"providers": {
        "one": {**GLM, "api_key_env": "GLM_API_KEY"},
        "two": {**GLM, "api_key_env": ["GLM_API_KEY", "ZAI_API_KEY"]},
        "none": {**GLM, "api_key_env": []},
    }})
    assert settings.provider("one").api_key_envs == ("GLM_API_KEY",)
    assert settings.provider("two").api_key_envs == ("GLM_API_KEY", "ZAI_API_KEY")
    assert settings.provider("none").api_key_envs == ()
    # First set name wins, in the order the config lists them.
    monkeypatch.delenv("GLM_API_KEY")
    assert settings.provider("two").api_key() == "z"
    assert settings.provider("one").api_key() is None
    with pytest.raises(ConfigError, match="api_key_env must be a string or a list"):
        _load(tmp_path, {"providers": {"x": {**GLM, "api_key_env": 3}}}, name="d.toml")


def test_config_a_provider_with_no_key_is_a_warning_not_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    settings = _load(tmp_path, {"providers": {"glm": GLM}})
    assert settings.providers  # it loaded
    assert any("glm" in w and "GLM_API_KEY" in w for w in settings.warnings)
    monkeypatch.setenv("GLM_API_KEY", "k")
    assert _load(tmp_path, {"providers": {"glm": GLM}}, name="b.toml").warnings == ()


def test_config_api_key_literal_is_used_when_no_variable_is_set(tmp_path: Path):
    settings = _load(tmp_path, {"providers": {
        "local": {"driver": "claude", "base_url": "http://127.0.0.1:8080",
                  "api_key_env": "LOCAL_API_KEY", "api_key": "local"},
    }})
    assert settings.provider("local").api_key({}) == "local"
    assert settings.provider("local").api_key({"LOCAL_API_KEY": "x"}) == "x"
    assert settings.warnings == ()


@pytest.mark.parametrize(("table", "vendor"), [
    ({"driver": "codex"}, "codex"),
    ({"driver": "copilot"}, "copilot"),
    ({"driver": "grok"}, "grok"),
    ({"driver": "claude", "base_url": "https://api.z.ai/api/anthropic"}, "zai"),
    ({"driver": "claude", "base_url": "https://api.deepseek.com/anthropic"}, "deepseek"),
    ({"driver": "claude", "base_url": "http://127.0.0.1:8000"}, "none"),
    ({"driver": "claude", "base_url": "https://api.z.ai/x", "vendor": "stated"}, "stated"),
])
def test_config_vendor_is_derived_when_not_stated(tmp_path: Path, table, vendor):
    settings = _load(tmp_path, {"providers": {"p": table}})
    assert settings.provider("p").vendor == vendor


def test_config_provider_knobs_default_to_core(tmp_path: Path):
    settings = _load(tmp_path, {
        "core": {"max_steps": 12, "run_timeout": 60, "max_agents": 9},
        "providers": {"glm": GLM, "slow": {**GLM, "max_steps": 7, "max_agents": 1}},
    })
    glm, slow = settings.provider("glm"), settings.provider("slow")
    assert (glm.max_steps, glm.run_timeout, glm.max_agents) == (12, 60.0, 9)
    assert (slow.max_steps, slow.run_timeout, slow.max_agents) == (7, 60.0, 1)


def test_config_health_probe_and_pricing_blocks_are_read(tmp_path: Path):
    settings = _load(tmp_path, {"providers": {"local": {
        "driver": "claude",
        "model": "qwen",
        "local": True,
        "health": {"kind": "llamacpp", "candidates": ["http://a:8080", "http://b:8080/"],
                   "resolve_cmd": ["sh", "-c", "echo"], "warm": True,
                   "cold_load_seconds": 180},
        "probe": {"gpu_cmd": ["nvidia-smi"], "metrics_url": "http://{host}:8081/metrics",
                  "host_parser": "mac", "admit": {"max_swap_mb": 1000, "enforce": True}},
        "pricing": {"kind": "flat_plan", "monthly_usd": 80},
    }}})
    cfg = settings.provider("local")
    assert cfg.health.kind == "llamacpp" and cfg.health.warm is True
    assert cfg.health.candidates == ("http://a:8080", "http://b:8080/")
    assert cfg.health.resolve_cmd == ("sh", "-c", "echo")
    assert settings.cold_load_seconds("local") == 180.0
    assert cfg.probe.gpu_cmd == ("nvidia-smi",) and cfg.probe.host_parser == "mac"
    assert cfg.probe.admit == {"max_swap_mb": 1000, "enforce": True}
    assert cfg.pricing.kind == "flat_plan" and cfg.pricing.values == {"monthly_usd": 80}
    # Nothing declared is an empty spec, not None.
    plain = _load(tmp_path, {"providers": {"glm": GLM}}, name="p.toml").provider("glm")
    assert plain.health.kind == "none" and plain.probe.empty and plain.pricing.kind == "none"


def test_config_as_dict_names_key_variables_never_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "super-secret")
    out = _load(tmp_path, {"providers": {"glm": GLM}}).provider("glm").as_dict()
    assert out["api_key_envs"] == ["GLM_API_KEY"] and out["has_api_key"] is True
    assert "super-secret" not in json.dumps(out)


# --- the chain --------------------------------------------------------------------


def test_config_chain_full_local_and_none(tmp_path: Path):
    settings = make_settings(tmp_path)
    assert [c.name for c in settings.chain("glm", "full")] == [
        "glm", "codex", "deepseek", "bppc", "omlx"]
    assert [c.name for c in settings.chain("glm", "local")] == ["glm", "bppc", "omlx"]
    assert [c.name for c in settings.chain("glm", "none")] == ["glm"]
    assert [c.name for c in settings.chain("bppc", "local")] == ["bppc", "omlx"]


def test_config_chain_without_a_fallback_table_is_the_primary_alone(tmp_path: Path):
    settings = _load(tmp_path, {"providers": {"glm": GLM, "other": GLM}})
    assert [c.name for c in settings.chain("glm", "full")] == ["glm"]


# --- Settings behaviour -----------------------------------------------------------


def test_result_cap_chars_is_summary_tokens_times_ratio(tmp_path: Path):
    settings = make_settings(tmp_path, summary_tokens=2000, chars_per_token=3.5)
    assert settings.result_cap_chars == 7000


def test_hooks_config_lives_under_session_root_not_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    settings = make_settings(tmp_path, supervisor="agent")
    path = settings.hooks_config("a1")
    assert path.is_relative_to(settings.session_root)
    assert not (home / ".claude" / "settings.json").exists()
    payload = path.read_text()
    assert "PreToolUse" in payload
    assert "approval_hook.py" in payload


def test_child_env_strips_parent_oauth_and_sets_the_provider_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-oauth")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    settings = make_settings(tmp_path)
    env = settings.child_env("a1")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "glm-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # Another provider's key never reaches this child either.
    assert "DEEPSEEK_API_KEY" not in env
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(settings.session_root))
    assert env["SUBAGENT_APPROVAL_SOCKET"] == settings.approval_socket


def test_compact_window_from_the_config_file(tmp_path: Path):
    settings = make_settings(tmp_path, compact_window=40960)
    assert settings.compact_window == 40960
    env = settings.child_env("a1")
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "40960"
    payload = json.loads(settings.hooks_config("a1").read_text())
    assert payload["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "40960"
    assert payload["autoCompactWindow"] == 40960
    # A provider that states its own keeps it.
    assert settings.provider("omlx").compact_window == 98304


def test_loop_strikes_default_is_eight(tmp_path: Path):
    """At 3, a child re-running its tests between edits would be killed."""
    assert _load(tmp_path, {"providers": {"glm": GLM}}).loop_strikes == 8


# --- delegate ---------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    """The server module, pointed at a registry whose spawns are recorded."""
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
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

    monkeypatch.setattr(claude_provider, "_spawn_claude", spawn)
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
        ({"provider": "nope"}, "unknown provider"),
        ({"lane": "nope"}, "unknown provider"),
        ({"fallback": "sideways"}, "unknown fallback"),
    ],
)
def test_delegate_rejects_without_spawning(server, tmp_path: Path, kwargs, words):
    out = _delegate(server, tmp_path, **kwargs)
    assert out["state"] == "rejected"
    assert words in out["error"]
    assert server.created == []
    assert server.spawned == []
    assert server.registry.agents() == []


def test_delegate_records_provider_and_fallback(server, tmp_path: Path):
    out = _delegate(server, tmp_path, provider="glm", fallback="none", wait_seconds=5)
    assert out["state"] == COMPLETED
    assert (out["lane"], out["fallback"]) == ("glm", "none")
    assert out["model"] == "glm-5.3-flash[1m]"
    agent = server.created[0]
    assert (agent.cfg.name, agent.fallback) == ("glm", "none")
    run = agent.runs()[0]
    assert (run.lane, run.fallback) == ("glm", "none")
    assert agent.info()["lane"] == "glm"


def test_delegate_takes_lane_as_a_deprecated_alias(server, tmp_path: Path):
    out = _delegate(server, tmp_path, lane="deepseek", fallback="none", wait_seconds=5)
    assert out["lane"] == "deepseek"


def test_delegate_defaults_to_the_default_provider(server, tmp_path: Path):
    out = _delegate(server, tmp_path, wait_seconds=5)
    assert (out["lane"], out["fallback"]) == ("glm", "full")


def test_glm_argv_and_env(server, tmp_path: Path):
    _delegate(server, tmp_path, provider="glm", wait_seconds=5)
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


def test_caller_model_beats_the_provider_model(server, tmp_path: Path):
    _delegate(server, tmp_path, provider="glm", model="glm-5.3[1m]", wait_seconds=5)
    argv = server.spawned[0].argv
    assert argv[argv.index("--model") + 1] == "glm-5.3[1m]"


def test_omlx_child_gets_its_provider_values_and_no_sampling(
    server, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OMLX_API_KEY", "omlx-key")
    # The health gate is covered in test_health; here oMLX is up.
    monkeypatch.setattr(health, "check_omlx", lambda cfg: health.Health(True, cfg.base_url))
    out = _delegate(server, tmp_path, provider="omlx", wait_seconds=5)
    assert out["state"] == COMPLETED
    proc = server.spawned[0]
    argv, env = proc.argv, proc.env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "omlx-key"
    assert "OMLX_API_KEY" not in env
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


def test_per_provider_agent_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMLX_API_KEY", "k")
    providers = default_providers()
    providers["omlx"]["max_agents"] = 1
    settings = make_settings(tmp_path, providers=providers)
    reg = Registry(settings, start_reaper=False)
    try:
        reg.create_agent("a", tmp_path, provider="omlx")
        with pytest.raises(runs.RegistryError, match="provider 'omlx'"):
            reg.create_agent("b", tmp_path, provider="omlx")
        reg.create_agent("c", tmp_path, provider="glm")
    finally:
        reg.shutdown()
