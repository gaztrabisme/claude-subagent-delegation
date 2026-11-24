"""Claude subscription login stays scoped, guarded and cash-unknown."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from subagent import config
from subagent.cli import main
from subagent.commands import init as init_command
from subagent.guard.classify import ALLOW, DENY, ESCALATE, classify, protected
from subagent.providers.base import Session
from subagent.providers.claude import CLAUDE_PROVIDER
from subagent.providers.codex import codex_env
from subagent.providers.grok import GROK_PROVIDER
from subagent.runs import FAILED, Registry
from subagent.telemetry import cost
from subagent.telemetry.trace import Trace
from tests.conftest import make_settings

FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude.py"
OWNER_WARNING = (
    "uses the owner's Claude login and plan quota; worker sessions are written into the "
    "owner's Claude history, and the owner's global instructions, commands and skills may load"
)


def _login_provider(*, model: str = "401", binary: Path = FAKE_CLAUDE) -> dict:
    return {
        "driver": "claude",
        "auth": "login",
        "model": model,
        "binary": str(binary),
        "pricing": {"kind": "flat_plan"},
    }


def _login_settings(tmp_path: Path, *, model: str = "401"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return make_settings(
        tmp_path,
        providers={"login": _login_provider(model=model)},
        run_timeout=15,
    )


def _run_login(tmp_path: Path, *, trace: Trace | None = None):
    settings = _login_settings(tmp_path)
    registry = Registry(settings, start_reaper=False, trace=trace)
    agent = registry.create_agent("login-test", tmp_path, provider="login")
    run = agent.delegate("check login", "true")
    assert run.done.wait(10), f"login run did not finish: {run.state} {run.error}"
    return registry, run


def test_auth_login_is_opt_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OWNER_API_KEY", "api-key-value")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "api").mkdir()
    api = make_settings(
        tmp_path / "api",
        providers={"api": {
            "driver": "claude",
            "base_url": "https://api.example.test",
            "api_key_env": "OWNER_API_KEY",
        }},
    ).provider()
    assert api.extra.get("auth") is None
    assert api.api_key() == "api-key-value"

    login = _login_settings(tmp_path / "login").provider()
    assert login.extra["auth"] == "login"
    assert login.api_key_envs == ()
    assert login.api_key_default is None
    assert login.api_key() is None
    assert login.as_dict()["auth"] == "login"
    assert login.as_dict()["has_api_key"] is False
    assert login.base_url is None
    assert login.unavailable() is None
    env = _login_settings(tmp_path / "child").child_env("agent", login)
    # No config dir is forced when the parent set none: the child must read the
    # CLI's own credential store (the login Keychain on macOS). An explicit dir
    # makes Claude Code skip that store and report "Not logged in".
    assert "CLAUDE_CONFIG_DIR" not in env
    assert not {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    } & env.keys()

    invalid = tmp_path / "invalid.toml"
    invalid.write_text(
        '[providers.test]\ndriver = "codex"\nauth = "login"\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="requires driver = 'claude'"):
        config.load(extra=invalid)

    key_login = tmp_path / "key-login.toml"
    key_login.write_text(
        '[providers.test]\ndriver = "claude"\nauth = "login"\n'
        'api_key_env = "OWNER_API_KEY"\n',
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="cannot set api_key_env"):
        config.load(extra=key_login)


def test_auth_login_configuration_errors_are_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cases = (
        (
            'driver = "codex"\nauth = "login"\n',
            "error: provider 'test' auth = 'login' requires driver = 'claude' or 'dsh'",
        ),
        (
            'driver = "claude"\nauth = "oauth"\n',
            "error: provider 'test' auth must be 'login'",
        ),
        (
            'driver = "claude"\nauth = "login"\napi_key_env = "OWNER_API_KEY"\n',
            "error: provider 'test' auth = 'login' cannot set api_key_env",
        ),
    )
    for index, (provider, expected) in enumerate(cases):
        xdg = tmp_path / f"case-{index}"
        path = xdg / "subagent" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(
            '[core]\ndefault_provider = "test"\n\n'
            f"[providers.test]\n{provider}",
            encoding="utf-8",
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        assert main(["doctor", "--no-probe"]) == 1
        assert capsys.readouterr().err.strip() == expected


def test_auth_login_is_scoped_to_selected_provider(tmp_path: Path, monkeypatch) -> None:
    owner_home = tmp_path / "owner-claude"
    owner_home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(owner_home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-auth-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "parent-oauth-token")
    monkeypatch.setenv("OTHER_API_KEY", "other-provider-key")
    monkeypatch.setenv("LOGIN_API_KEY", "login-provider-key")

    settings = make_settings(tmp_path, providers={
        "login": _login_provider(),
        "other": {
            "driver": "claude",
            "base_url": "https://api.example.test",
            "api_key_env": "OTHER_API_KEY",
        },
    })
    login_env = settings.child_env("login-agent", settings.provider("login"))
    other_env = settings.child_env("other-agent", settings.provider("other"))

    assert login_env["CLAUDE_CONFIG_DIR"] == str(owner_home.resolve())
    assert not {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    } & login_env.keys()
    assert "other-provider-key" not in login_env.values()
    assert "login-provider-key" not in login_env.values()
    assert other_env["ANTHROPIC_AUTH_TOKEN"] == "other-provider-key"
    assert other_env["CLAUDE_CONFIG_DIR"] != str(owner_home.resolve())


def test_auth_login_custom_config_dir_is_guard_protected_and_restored(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owner_config = tmp_path / "owner-config-root"
    owner_config.mkdir()
    target = owner_config / "settings.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(owner_config))
    settings = _login_settings(tmp_path / "settings")
    cfg = settings.provider("login")
    process = CLAUDE_PROVIDER.spawn(
        cfg, settings, "guard-test", "prompt", workspace, Session(provider="login"), None
    )

    def fail_after_protection():
        assert classify("Write", {"file_path": str(target)}, workspace).action == DENY
        raise RuntimeError("fake process start")

    monkeypatch.setattr(process, "_start", fail_after_protection)
    with pytest.raises(RuntimeError, match="fake process start"):
        next(process.events())
    assert classify("Write", {"file_path": str(target)}, workspace).action == ESCALATE


def test_auth_login_doctor_warns_about_owner_history_and_instructions(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    xdg = tmp_path / "xdg"
    config_path = xdg / "subagent" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[core]\ndefault_provider = "login"\n\n'
        '[providers.login]\ndriver = "claude"\nauth = "login"\nmax_agents = 1\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert main(["doctor", "--provider", "login", "--no-probe", "--json"]) == 0

    row = json.loads(capsys.readouterr().out)["providers"][0]
    assert row["warning"] == f"warning: provider 'login' {OWNER_WARNING}"


def test_auth_login_context_reaches_claude_child(tmp_path: Path, monkeypatch) -> None:
    owner_config = tmp_path / "custom-claude"
    owner_config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(owner_config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-auth-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "parent-oauth-token")
    settings = _login_settings(tmp_path / "settings")
    cfg = settings.provider("login")

    process = CLAUDE_PROVIDER.spawn(
        cfg, settings, "child-context", "prompt", tmp_path, Session(provider="login"), None
    )

    assert process.env["CLAUDE_CONFIG_DIR"] == str(owner_config.resolve())
    assert not {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    } & process.env.keys()
    assert "--strict-mcp-config" in process.argv
    sources = process.argv.index("--setting-sources")
    assert process.argv[sources + 1] == ""
    settings_index = process.argv.index("--settings")
    generated_settings = Path(process.argv[settings_index + 1])
    assert generated_settings.is_file()
    assert process.protected_root == owner_config.resolve()


def test_auth_login_failure_keeps_owner_config_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    owner_config = tmp_path / "owner-config"
    owner_config.mkdir()
    owner_file = owner_config / "settings.json"
    original = '{"owner": "keep"}\n'
    owner_file.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(owner_config))
    monkeypatch.setenv("FAKE_FAIL_MODEL", "401")

    registry, run = _run_login(tmp_path / "run")
    try:
        assert run.state == FAILED
        assert run.finish_reason == "auth"
        assert owner_file.read_text(encoding="utf-8") == original
    finally:
        registry.shutdown()


def test_auth_login_plan_use_is_recorded_on_failure(tmp_path: Path, monkeypatch) -> None:
    owner_config = tmp_path / "owner-config"
    owner_config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(owner_config))
    monkeypatch.setenv("FAKE_FAIL_MODEL", "401")
    trace_path = tmp_path / "trace.jsonl"
    trace = Trace(trace_path)
    registry, run = _run_login(tmp_path / "run", trace=trace)
    try:
        records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        record = next(item for item in records if item["kind"] == "run")
        assert run.finish_reason == "auth"
        assert record["cost"] == {
            "provider_usd": None,
            "counterfactual_usd": 0.0,
            "kind": "flat_plan",
            "note": "subscription plan; cash price unknown",
        }
        assert str(owner_config) not in json.dumps(record)
    finally:
        registry.shutdown()


def test_auth_login_flat_plan_keeps_unknown_cash_unknown(tmp_path: Path) -> None:
    settings = _login_settings(tmp_path)
    result = cost.price_run(
        {"input": 100, "output": 40, "cache_read": 0, "cache_write": 0},
        None,
        settings.provider("login"),
        cost.Pricing.from_settings(settings),
        0,
    )
    assert result["kind"] == "flat_plan"
    assert result["provider_usd"] is None
    assert result["note"] == "subscription plan; cash price unknown"


def test_auth_login_passes_user_and_logname_only_to_login_providers(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("USER", "owner-user")
    monkeypatch.setenv("LOGNAME", "owner-logname")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    monkeypatch.setenv("OMLX_API_KEY", "omlx-secret")
    monkeypatch.setenv("GROK_API_KEY", "grok-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-auth-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "parent-oauth-token")
    monkeypatch.setenv("USER_SECRET", "user-secret-val")
    monkeypatch.setenv("USER_TOKEN", "user-token-val")
    monkeypatch.setenv("LOGNAME_KEY", "logname-key-val")
    monkeypatch.setenv("MY_PASSWORD", "password-val")
    monkeypatch.setenv("UNRELATED_PARENT_VAR", "not-passed")

    providers = {
        "login": _login_provider(),
        "deepseek": {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "api_key_env": "GLM_API_KEY",
        },
        "omlx": {
            "driver": "claude",
            "base_url": "http://127.0.0.1:8000",
            "api_key_env": "OMLX_API_KEY",
        },
        "codex": {"driver": "codex"},
        "grok": {"driver": "grok", "api_key_env": "GROK_API_KEY"},
    }
    secret_like = {
        "USER_SECRET",
        "USER_TOKEN",
        "LOGNAME_KEY",
        "MY_PASSWORD",
        "UNRELATED_PARENT_VAR",
    }

    plain_root = tmp_path / "plain"
    plain_root.mkdir()
    settings = make_settings(plain_root, providers=providers, child_env_passthrough=[])

    login_env = settings.child_env("a-login", settings.provider("login"))
    assert login_env["USER"] == "owner-user"
    assert login_env["LOGNAME"] == "owner-logname"
    assert not (
        secret_like
        | {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "DEEPSEEK_API_KEY",
            "GLM_API_KEY",
            "OMLX_API_KEY",
            "GROK_API_KEY",
        }
    ) & login_env.keys()

    base_env = settings.base_child_env()
    codex_child_env = codex_env(settings, "a-codex", plain_root / "codex-home")
    grok_cfg = settings.provider("grok")
    grok_child_env = GROK_PROVIDER.env(
        settings, "a-grok", grok_cfg, Session(provider=grok_cfg.name)
    )
    non_login_envs = [
        base_env,
        settings.child_env("a-deepseek", settings.provider("deepseek")),
        settings.child_env("a-glm", settings.provider("glm")),
        settings.child_env("a-omlx", settings.provider("omlx")),
        codex_child_env,
        grok_child_env,
    ]
    for env in non_login_envs:
        assert "USER" not in env
        assert "LOGNAME" not in env
        assert not secret_like & env.keys()

    pass_root = tmp_path / "passthrough"
    pass_root.mkdir()
    pass_settings = make_settings(
        pass_root,
        providers=providers,
        child_env_passthrough=["USER", "LOGNAME", "USER_SECRET", "LOGNAME_KEY"],
    )
    passed_envs = [
        pass_settings.base_child_env(),
        pass_settings.child_env("a-login", pass_settings.provider("login")),
        pass_settings.child_env("a-glm", pass_settings.provider("glm")),
        codex_env(pass_settings, "a-codex", pass_root / "codex-home"),
        GROK_PROVIDER.env(
            pass_settings, "a-grok", pass_settings.provider("grok"), Session(provider="grok")
        ),
    ]
    for env in passed_envs:
        assert env["USER"] == "owner-user"
        assert env["LOGNAME"] == "owner-logname"
        assert not secret_like & env.keys()


def test_auth_login_omits_unset_user_and_logname(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    settings = _login_settings(tmp_path / "settings")
    cfg = settings.provider("login")

    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    neither = settings.child_env("a-neither", cfg)
    assert "USER" not in neither
    assert "LOGNAME" not in neither

    monkeypatch.setenv("USER", "only-user")
    only_user = settings.child_env("a-user", cfg)
    assert only_user["USER"] == "only-user"
    assert "LOGNAME" not in only_user

    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("LOGNAME", "only-logname")
    only_logname = settings.child_env("a-logname", cfg)
    assert "USER" not in only_logname
    assert only_logname["LOGNAME"] == "only-logname"


def test_auth_login_unset_config_dir_protects_home_claude_during_events(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    settings = _login_settings(tmp_path / "settings")
    cfg = settings.provider("login")
    process = CLAUDE_PROVIDER.spawn(
        cfg, settings, "unset-cfg", "prompt", workspace, Session(provider="login"), None
    )

    expected_root = (fake_home / ".claude").resolve()
    target = fake_home / ".claude" / "settings.json"
    assert "CLAUDE_CONFIG_DIR" not in process.env
    assert process.protected_root.resolve() == expected_root
    assert protected(target) is None

    def check_during_events():
        assert protected(target) is not None
        assert classify("Read", {"file_path": str(target)}, workspace).action == DENY
        assert classify("Write", {"file_path": str(target)}, workspace).action == DENY
        raise RuntimeError("fake process start")

    monkeypatch.setattr(process, "_start", check_during_events)
    with pytest.raises(RuntimeError, match="fake process start"):
        next(process.events())

    assert protected(target) is None
    monkeypatch.setenv("HOME", str(other_home))
    assert classify("Read", {"file_path": str(target)}, workspace).action == ALLOW
    assert classify("Write", {"file_path": str(target)}, workspace).action == ESCALATE


@pytest.mark.parametrize("blank", ["   ", "\t\n "])
def test_auth_login_blank_config_dir_treated_as_unset(
    tmp_path: Path, monkeypatch, blank: str
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", blank)

    settings = _login_settings(tmp_path / "settings")
    cfg = settings.provider("login")
    process = CLAUDE_PROVIDER.spawn(
        cfg, settings, "blank-cfg", "prompt", workspace, Session(provider="login"), None
    )

    expected_root = (fake_home / ".claude").resolve()
    target = fake_home / ".claude" / "settings.json"
    assert "CLAUDE_CONFIG_DIR" not in process.env
    assert process.protected_root.resolve() == expected_root
    assert protected(target) is None

    def check_during_events():
        assert protected(target) is not None
        assert classify("Read", {"file_path": str(target)}, workspace).action == DENY
        assert classify("Write", {"file_path": str(target)}, workspace).action == DENY
        raise RuntimeError("fake process start")

    monkeypatch.setattr(process, "_start", check_during_events)
    with pytest.raises(RuntimeError, match="fake process start"):
        next(process.events())

    assert protected(target) is None
    monkeypatch.setenv("HOME", str(other_home))
    assert classify("Read", {"file_path": str(target)}, workspace).action == ALLOW
    assert classify("Write", {"file_path": str(target)}, workspace).action == ESCALATE


def test_auth_login_set_config_dir_preserved_and_protected(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))

    settings = _login_settings(tmp_path / "settings")
    cfg = settings.provider("login")
    process = CLAUDE_PROVIDER.spawn(
        cfg, settings, "set-cfg", "prompt", workspace, Session(provider="login"), None
    )

    target = cfg_dir / "settings.json"
    assert process.env["CLAUDE_CONFIG_DIR"] == str(cfg_dir.resolve())
    assert process.protected_root.resolve() == cfg_dir.resolve()

    def check_during_events():
        assert classify("Read", {"file_path": str(target)}, workspace).action == DENY
        assert classify("Write", {"file_path": str(target)}, workspace).action == DENY
        raise RuntimeError("fake process start")

    monkeypatch.setattr(process, "_start", check_during_events)
    with pytest.raises(RuntimeError, match="fake process start"):
        next(process.events())

    assert classify("Read", {"file_path": str(target)}, workspace).action == ALLOW
    assert classify("Write", {"file_path": str(target)}, workspace).action == ESCALATE


def test_auth_login_example_and_init_default_to_serial_max_agents(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "config.claude.toml"
    raw = tomllib.loads(example.read_text(encoding="utf-8"))
    assert raw["providers"]["claude"]["max_agents"] == 1
    assert config.load(extra=example).provider("claude").max_agents == 1

    for detected in (False, True):
        monkeypatch.setattr(
            init_command.shutil,
            "which",
            (lambda name: "/fake/bin/claude" if name == "claude" else None)
            if detected
            else (lambda _name: None),
        )
        dest = tmp_path / f"init-claude-{detected}.toml"
        assert main(["init", "--yes", "--from", "claude", "--path", str(dest)]) == 0
        capsys.readouterr()
        data = tomllib.loads(dest.read_text(encoding="utf-8"))
        assert data["providers"]["claude"]["max_agents"] == 1
        assert config.load(extra=dest).provider("claude").max_agents == 1


def test_auth_login_doctor_warns_when_max_agents_exceeds_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    xdg = tmp_path / "xdg"
    config_path = xdg / "subagent" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[core]\ndefault_provider = "login_parallel"\n\n'
        '[providers.login_parallel]\n'
        'driver = "claude"\nauth = "login"\nmax_agents = 2\n\n'
        '[providers.login_serial]\n'
        'driver = "claude"\nauth = "login"\nmax_agents = 1\n\n'
        '[providers.api_parallel]\n'
        'driver = "claude"\nbase_url = "https://api.example.test"\n'
        'api_key_env = "GLM_API_KEY"\nmax_agents = 4\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert main(["doctor", "--provider", "login_parallel", "--no-probe", "--json"]) == 0
    parallel_data = json.loads(capsys.readouterr().out)
    parallel_warnings = "\n".join(
        filter(
            None,
            [parallel_data["providers"][0].get("warning", ""), *parallel_data.get("warnings", ())],
        )
    )
    assert "max_agents" in parallel_warnings

    assert main(["doctor", "--provider", "login_parallel", "--no-probe"]) == 0
    assert "max_agents" in capsys.readouterr().err

    for name in ("login_serial", "api_parallel"):
        assert main(["doctor", "--provider", name, "--no-probe", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        warnings_text = "\n".join(
            filter(None, [data["providers"][0].get("warning", ""), *data.get("warnings", ())])
        )
        assert "max_agents" not in warnings_text

        assert main(["doctor", "--provider", name, "--no-probe"]) == 0
        assert "max_agents" not in capsys.readouterr().err
