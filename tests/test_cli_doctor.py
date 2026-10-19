"""`subagent doctor` reports binary, key, health, adapter and prompt per provider."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from subagent import cli, config
from subagent.cli import main

# Fake keys, named in the tests so a value can be looked for in the output.
FILE_ONLY_KEY = "key-from-the-env-file"
SHELL_KEY = "key-from-the-calling-shell"

# A provider whose status endpoint answers only to a request that carries a key.
OMLX_STATUS = "/omlx/api/status"


def _gate_on_key(endpoint, path: str = OMLX_STATUS) -> None:
    """Answer 200 to a request with a bearer token and 401 to one without it."""
    def gate() -> None:
        headers = {k.lower(): v for k, v in endpoint.headers[path].items()}
        endpoint.routes[path] = (
            (200, {"status": "ok", "models_loaded": 2})
            if headers.get("authorization", "").startswith("Bearer ")
            else (401, {"error": "no key"})
        )

    endpoint.routes[path] = (401, {"error": "no key"})
    endpoint.on_hit[path] = gate


def _keyed_omlx_cfg(base: str) -> str:
    """One provider with `api_key_env = "OMLX_API_KEY"` and an oMLX health gate."""
    return (
        "[core]\n"
        'default_provider = "omlx"\n\n'
        "[providers.omlx]\n"
        'driver = "claude"\n'
        f'binary = "{sys.executable}"\n'
        'model = "qwen3.8-27b"\n'
        'api_key_env = "OMLX_API_KEY"\n'
        "[providers.omlx.health]\n"
        'kind = "omlx"\n'
        f'url = "{base}/api/status"\n'
    )


def _env_file(monkeypatch, tmp_path: Path, body: str) -> Path:
    """Write `body` to tmp_path/subagent.env, name it in SUBAGENT_ENV_FILE, return it."""
    path = tmp_path / "subagent.env"
    path.write_text(body)
    monkeypatch.setenv(cli.PROVIDER_ENV_FILE, str(path))
    return path


def _sent_key(endpoint, path: str = OMLX_STATUS) -> str:
    headers = {k.lower(): v for k, v in endpoint.headers[path].items()}
    return headers.get("authorization", "")


def _set_config(monkeypatch, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    return path


def _codex_cfg(binary: str) -> str:
    return (
        "[core]\n"
        'default_provider = "codex"\n\n'
        "[providers.codex]\n"
        'driver = "codex"\n'
        f'binary = "{binary}"\n'
    )


def test_doctor_table_reports_binary_and_key(tmp_path, monkeypatch, fake_codex, capsys):
    binary = os.environ["SUBAGENT_TEST_CODEX_BIN"]
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(_codex_cfg(binary))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "codex" in out
    assert "MISSING" not in out


def test_doctor_exits_1_on_missing_binary(tmp_path, monkeypatch, capsys):
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _codex_cfg("/nonexistent/codex")
    )
    assert main(["doctor"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_doctor_no_probe_does_not_fail_on_missing_binary(tmp_path, monkeypatch, capsys):
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _codex_cfg("/nonexistent/codex")
    )
    assert main(["doctor", "--no-probe"]) == 0


def test_doctor_requires_a_provider(tmp_path, monkeypatch, capsys):
    _set_config(monkeypatch, tmp_path / "empty.toml").write_text("")
    assert main(["doctor", "--no-probe"]) == 1
    captured = capsys.readouterr()
    assert "error: no providers configured; run `subagent init`" in captured.err


def test_doctor_json_requires_a_provider(tmp_path, monkeypatch, capsys):
    _set_config(monkeypatch, tmp_path / "empty.toml").write_text("")
    assert main(["doctor", "--no-probe", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "error": "no providers configured; run `subagent init`"
    }
    assert captured.err == ""


def test_doctor_json(tmp_path, monkeypatch, fake_codex, capsys):
    binary = os.environ["SUBAGENT_TEST_CODEX_BIN"]
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(_codex_cfg(binary))
    assert main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["providers"][0]["name"] == "codex"
    assert data["providers"][0]["binary_ok"] is True


def test_doctor_healthy_local(tmp_path, monkeypatch, mock_endpoint, capsys):
    mock_endpoint.routes["/local/health"] = (200, {"status": "ok", "backend": "running"})
    base = mock_endpoint.url("local")
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        "[core]\n"
        'default_provider = "local"\n\n'
        "[providers.local]\n"
        'driver = "claude"\n'
        f'binary = "{sys.executable}"\n'
        'model = "qwen3.8-27b"\n'
        'api_key = "local"\n'
        "[providers.local.health]\n"
        'kind = "llamacpp"\n'
        f'candidates = ["{base}"]\n'
    )
    assert main(["doctor"]) == 0


def test_doctor_health_failure_is_hard(tmp_path, monkeypatch, mock_endpoint, capsys):
    base = mock_endpoint.url("down")  # no /down/health route -> 404
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        "[core]\n"
        'default_provider = "local"\n\n'
        "[providers.local]\n"
        'driver = "claude"\n'
        f'binary = "{sys.executable}"\n'
        'model = "qwen3.8-27b"\n'
        'api_key = "local"\n'
        "[providers.local.health]\n"
        'kind = "llamacpp"\n'
        f'candidates = ["{base}"]\n'
    )
    assert main(["doctor"]) == 1


def test_every_example_passes_doctor_no_probe(monkeypatch, capsys):
    examples = Path(__file__).resolve().parents[1] / "examples"
    for path in sorted(examples.glob("config.*.toml")):
        monkeypatch.setenv(config.CONFIG_ENV, str(path))
        assert main(["doctor", "--no-probe"]) == 0, path.name
        capsys.readouterr()


# --- the provider env file (SUBAGENT_ENV_FILE) ----------------------------------

# A file like the one the MCP wrapper sources: a comment, a `set -a`, blank
# lines, quoted values, a trailing comment, and lines that are not assignments.
# A file like the one the MCP wrapper sources: a comment, a `set -a`, blank
# lines, quoted values, a trailing comment, and lines that are not assignments.
# Read through a dict, so importing it touches nothing outside the test.
SAMPLE_ENV = (
    "# provider keys; the MCP wrapper sources this file with set -a\n"
    "set -a\n"
    "\n"
    f"export ZAI_API_KEY={FILE_ONLY_KEY}\n"
    "export DEEPSEEK_API_KEY=plain_value # a trailing comment\n"
    "export   QUOTED_ONE='single quoted value'\n"
    'QUOTED_TWO="double quoted value"\n'
    "  SPACED = spaced_value\n"
    "not an assignment\n"
    "9BAD=nope\n"
    "export =nope\n"
    "# declared last on purpose: the shell value of GLM_API_KEY must win\n"
    f"export GLM_API_KEY={FILE_ONLY_KEY}\n"
)


OMLX_ENV_FILE = (
    "# the local server's key, sourced by the MCP wrapper with set -a\n"
    f"export OMLX_API_KEY={FILE_ONLY_KEY}\n"
)


def test_doctor_resolves_a_key_from_the_env_file(tmp_path, monkeypatch, mock_endpoint, capsys):
    """The key lives only in the file the wrapper sources: doctor finds it, so the
    health probe carries it and the provider answers 200 instead of 401."""
    _env_file(monkeypatch, tmp_path, OMLX_ENV_FILE)
    _gate_on_key(mock_endpoint)
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _keyed_omlx_cfg(mock_endpoint.url("omlx"))
    )
    assert main(["doctor"]) == 0
    captured = capsys.readouterr()
    assert "OMLX_API_KEY=set" in captured.out
    assert _sent_key(mock_endpoint) == f"Bearer {FILE_ONLY_KEY}"
    assert FILE_ONLY_KEY not in captured.out + captured.err


def test_doctor_reports_the_401_an_unloaded_key_causes(tmp_path, monkeypatch,
                                                       mock_endpoint, capsys):
    """No file to load: the probe sends no key. This is the report being fixed."""
    monkeypatch.setenv(cli.PROVIDER_ENV_FILE, str(tmp_path / "absent.env"))
    _gate_on_key(mock_endpoint)
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _keyed_omlx_cfg(mock_endpoint.url("omlx"))
    )
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "OMLX_API_KEY=missing" in out
    assert "HTTP 401" in out


def test_doctor_keeps_a_variable_the_shell_already_set(tmp_path, monkeypatch, mock_endpoint):
    """An exported value in the calling shell beats the one in the file."""
    monkeypatch.setenv("OMLX_API_KEY", SHELL_KEY)
    _env_file(monkeypatch, tmp_path, OMLX_ENV_FILE)
    _gate_on_key(mock_endpoint)
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _keyed_omlx_cfg(mock_endpoint.url("omlx"))
    )
    assert main(["doctor"]) == 0
    assert _sent_key(mock_endpoint) == f"Bearer {SHELL_KEY}"
    assert os.environ["OMLX_API_KEY"] == SHELL_KEY


def test_doctor_reads_the_default_env_file_under_home(tmp_path, monkeypatch, mock_endpoint):
    """With SUBAGENT_ENV_FILE unset, doctor reads the wrapper's default path."""
    monkeypatch.delenv(cli.PROVIDER_ENV_FILE, raising=False)
    home = tmp_path / "home"
    env_file = home / ".config" / "agent-capabilities" / "subagent.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"export OMLX_API_KEY={FILE_ONLY_KEY}\n")
    monkeypatch.setenv("HOME", str(home))
    _gate_on_key(mock_endpoint)
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _keyed_omlx_cfg(mock_endpoint.url("omlx"))
    )
    assert main(["doctor", "--json"]) == 0
    assert _sent_key(mock_endpoint) == f"Bearer {FILE_ONLY_KEY}"


def test_doctor_json_prints_no_value_from_the_env_file(tmp_path, monkeypatch, mock_endpoint,
                                                       capsys):
    _env_file(monkeypatch, tmp_path, OMLX_ENV_FILE)
    _gate_on_key(mock_endpoint)
    _set_config(monkeypatch, tmp_path / "config.toml").write_text(
        _keyed_omlx_cfg(mock_endpoint.url("omlx"))
    )
    assert main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    row = json.loads(captured.out)["providers"][0]
    assert row["api_key_envs"][0] == {"name": "OMLX_API_KEY", "set": True}
    assert FILE_ONLY_KEY not in captured.out + captured.err


def test_load_provider_env_imports_only_key_value_lines(tmp_path, capsys):
    """Every KEY=VALUE line lands in the mapping, and nothing else does.

    A variable set to an empty string is filled; one with a value is not.
    """
    path = tmp_path / "subagent.env"
    path.write_text(SAMPLE_ENV)
    env = {
        cli.PROVIDER_ENV_FILE: str(path),
        "PATH": "/bin",
        "ZAI_API_KEY": "",            # set to nothing: the file fills it
        "GLM_API_KEY": SHELL_KEY,     # set to a value: the shell wins
    }
    assert cli.load_provider_env(env) == path
    assert env["ZAI_API_KEY"] == FILE_ONLY_KEY
    assert env["GLM_API_KEY"] == SHELL_KEY
    assert env["DEEPSEEK_API_KEY"] == "plain_value"
    assert env["QUOTED_ONE"] == "single quoted value"
    assert env["QUOTED_TWO"] == "double quoted value"
    assert env["SPACED"] == "spaced_value"
    assert env["PATH"] == "/bin"
    for junk in ("9BAD", "", "not", "export"):
        assert junk not in env
    assert capsys.readouterr().out == ""  # nothing is announced, name or value


def test_load_provider_env_skips_a_missing_file_silently(tmp_path, capsys):
    env = {cli.PROVIDER_ENV_FILE: str(tmp_path / "no-such.env")}
    assert cli.load_provider_env(env) is None
    assert env == {cli.PROVIDER_ENV_FILE: str(tmp_path / "no-such.env")}
    assert capsys.readouterr().out == ""


def test_provider_env_path_names_the_wrapper_file(tmp_path):
    named = tmp_path / "elsewhere.env"
    assert cli._provider_env_path({cli.PROVIDER_ENV_FILE: f" {named} "}) == named
    home = tmp_path / "owner"
    assert cli._provider_env_path({"HOME": str(home)}) == (
        home / ".config" / "agent-capabilities" / "subagent.env"
    )
