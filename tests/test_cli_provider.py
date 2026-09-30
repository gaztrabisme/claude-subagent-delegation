"""Provider configuration and local endpoint commands."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from subagent import config
from subagent.cli import main


def _user_config(monkeypatch, tmp_path: Path, text: str = "") -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    path = tmp_path / "xdg" / "subagent" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _provider_config(*, default: str = "one") -> str:
    return (
        "[core]\n"
        f'default_provider = "{default}"\n'
        "run_timeout = 900\n\n"
        "[providers.one]\n"
        'driver = "codex"\n'
        'model = "model-one"\n\n'
        "[providers.two]\n"
        'driver = "codex"\n'
        'model = "model-two"\n'
    )


def test_provider_add_copies_preset_without_secret_values(tmp_path, monkeypatch):
    path = _user_config(monkeypatch, tmp_path, "[core]\nrun_timeout = 777\n")
    assert main(["provider", "add", "renamed", "--from", "glm"]) == 0

    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["core"]["run_timeout"] == 777
    assert written["providers"]["renamed"]["driver"] == "claude"
    assert written["providers"]["renamed"]["api_key_env"] == ["GLM_API_KEY", "ZAI_API_KEY"]
    assert "glm" not in written["providers"]


def test_provider_add_never_writes_secret_values(tmp_path, monkeypatch):
    path = _user_config(monkeypatch, tmp_path)
    monkeypatch.setenv("GLM_API_KEY", "actual-secret-value")
    assert main(["provider", "add", "glm", "--from", "glm"]) == 0
    assert "actual-secret-value" not in path.read_text(encoding="utf-8")


def test_provider_add_unknown_preset_exits_two(tmp_path, monkeypatch, capsys):
    path = _user_config(monkeypatch, tmp_path)
    assert main(["provider", "add", "missing", "--from", "no-such-preset"]) == 2
    assert "error: unknown example 'no-such-preset'; examples:" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == ""


def test_provider_add_project_writes_project_config(tmp_path, monkeypatch):
    user = _user_config(monkeypatch, tmp_path, "[core]\nrun_timeout = 700\n")
    root = tmp_path / "project"
    root.mkdir()
    assert main(["--root", str(root), "provider", "add", "local-copy", "--from", "omlx",
                 "--project"]) == 0

    project = tomllib.loads((root / ".subagent" / "config.toml").read_text(encoding="utf-8"))
    assert project["providers"]["local-copy"]["local"] is True
    assert tomllib.loads(user.read_text(encoding="utf-8")) == {"core": {"run_timeout": 700}}


def test_provider_add_existing_requires_force(tmp_path, monkeypatch, capsys):
    path = _user_config(monkeypatch, tmp_path, _provider_config())
    before = path.read_text(encoding="utf-8")
    assert main(["provider", "add", "one", "--from", "glm"]) == 1
    assert "error: provider 'one' already exists; use --force" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before

    assert main(["provider", "add", "one", "--from", "glm", "--force"]) == 0
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["providers"]["one"]["driver"] == "claude"
    assert written["providers"]["two"]["model"] == "model-two"
    assert written["core"]["run_timeout"] == 900


def test_provider_list_json_redacts_credentials(tmp_path, monkeypatch, capsys):
    path = _user_config(monkeypatch, tmp_path, (
        "[core]\n"
        'default_provider = "one"\n\n'
        "[providers.one]\n"
        'driver = "claude"\n'
        'base_url = "https://example.test/api"\n'
        'model = "visible-model"\n'
        'api_key = "literal-secret"\n'
        'api_key_env = "PROVIDER_TOKEN"\n'
    ))
    del path
    monkeypatch.setenv("PROVIDER_TOKEN", "environment-secret")

    assert main(["provider", "list", "--json"]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["default_provider"] == "one"
    assert data["providers"]["one"]["is_default"] is True
    assert data["providers"]["one"]["api_key"] == "<redacted>"
    assert data["providers"]["one"]["api_key_env"] == "PROVIDER_TOKEN"
    assert "visible-model" in output and "literal-secret" not in output
    assert "environment-secret" not in output


def test_provider_remove_keeps_other_tables(tmp_path, monkeypatch, capsys):
    path = _user_config(monkeypatch, tmp_path, _provider_config())
    assert main(["provider", "remove", "one"]) == 1
    assert "cannot remove default provider 'one'" in capsys.readouterr().err
    assert main(["provider", "remove", "two"]) == 0

    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["core"]["run_timeout"] == 900
    assert set(written["providers"]) == {"one"}


def test_use_sets_default_provider(tmp_path, monkeypatch):
    path = _user_config(monkeypatch, tmp_path, _provider_config())
    assert main(["use", "two"]) == 0
    assert config.load().default_provider == "two"
    assert tomllib.loads(path.read_text(encoding="utf-8"))["core"]["run_timeout"] == 900


def test_provider_test_uses_doctor_checks(tmp_path, monkeypatch, fake_codex, capsys):
    binary = os.environ["SUBAGENT_TEST_CODEX_BIN"]
    _user_config(monkeypatch, tmp_path, (
        "[core]\n"
        'default_provider = "codex"\n\n'
        "[providers.codex]\n"
        'driver = "codex"\n'
        f'binary = "{binary}"\n'
        'api_key_env = "CODEX_TEST_KEY"\n'
    ))
    monkeypatch.setenv("CODEX_TEST_KEY", "present-but-never-rendered")

    assert main(["provider", "test", "codex", "--json"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    row = result["providers"][0]
    assert result["ok"] is True
    assert row["binary_ok"] is True
    assert row["health_ok"] is True
    assert row["api_key_envs"] == [{"name": "CODEX_TEST_KEY", "set": True}]
    assert "present-but-never-rendered" not in output
    assert fake_codex.calls() == []


def _local_add(tmp_path, monkeypatch, mock_endpoint, name: str | None = None) -> Path:
    _user_config(monkeypatch, tmp_path)
    argv = ["provider", "add"]
    if name:
        argv.append(name)
    argv.extend(("--url", mock_endpoint.url("endpoint")))
    assert main(argv) == 0
    return tmp_path / "xdg" / "subagent" / "config.toml"


def test_add_local_reads_openai_models_response(tmp_path, monkeypatch, mock_endpoint):
    mock_endpoint.routes["/endpoint/v1/models"] = (
        200, {"data": [{"id": "first-model"}, {"id": "second-model"}]}
    )
    path = _local_add(tmp_path, monkeypatch, mock_endpoint)
    provider = tomllib.loads(path.read_text(encoding="utf-8"))["providers"]["local"]
    assert provider["driver"] == "omp"
    assert provider["base_url"] == mock_endpoint.url("endpoint")
    assert provider["model"] == "first-model"
    assert provider["local"] is True
    assert provider["health"] == {
        "kind": "http", "url": f'{mock_endpoint.url("endpoint")}/v1/models'
    }


def test_add_local_detects_omlx_health(tmp_path, monkeypatch, mock_endpoint):
    mock_endpoint.routes["/endpoint/v1/models"] = (200, {"data": [{"id": "api-model"}]})
    mock_endpoint.routes["/endpoint/api/status"] = (
        200, {"status": "ok", "loaded_models": ["configured-model"]}
    )
    path = _local_add(tmp_path, monkeypatch, mock_endpoint)
    provider = tomllib.loads(path.read_text(encoding="utf-8"))["providers"]["local"]
    assert provider["model"] == "configured-model"
    assert provider["health"] == {
        "kind": "omlx", "url": f'{mock_endpoint.url("endpoint")}/api/status'
    }


def test_add_local_fills_health_and_probe_urls(tmp_path, monkeypatch, mock_endpoint):
    mock_endpoint.routes["/endpoint/v1/models"] = (200, {"data": [{"id": "model-a"}]})
    mock_endpoint.routes["/endpoint/api/status"] = (
        200, {"status": "ok", "loaded_models": ["model-a"]}
    )
    mock_endpoint.routes["/endpoint/metrics"] = (200, "metrics")
    mock_endpoint.routes["/endpoint/slots"] = (204, b"")
    path = _local_add(tmp_path, monkeypatch, mock_endpoint)
    provider = tomllib.loads(path.read_text(encoding="utf-8"))["providers"]["local"]
    assert provider["probe"] == {
        "metrics_url": f'{mock_endpoint.url("endpoint")}/metrics',
        "slots_url": f'{mock_endpoint.url("endpoint")}/slots',
    }


def test_add_local_rejects_empty_model_list(tmp_path, monkeypatch, mock_endpoint, capsys):
    mock_endpoint.routes["/endpoint/v1/models"] = (200, {"data": []})
    _user_config(monkeypatch, tmp_path)
    assert main(["provider", "add", "--url", mock_endpoint.url("endpoint")]) == 1
    captured = capsys.readouterr()
    assert "returned no model id" in captured.err
    assert "data" not in captured.err and "not found" not in captured.err
    assert not (tmp_path / "xdg" / "subagent" / "config.toml").read_text(encoding="utf-8")


def test_add_local_no_supported_endpoint_summarizes_failures(
    tmp_path, monkeypatch, mock_endpoint, capsys
):
    _user_config(monkeypatch, tmp_path)
    assert main(["provider", "add", "--url", mock_endpoint.url("endpoint")]) == 1
    error = capsys.readouterr().err
    assert f"no supported local model endpoint at '{mock_endpoint.url('endpoint')}'" in error
    assert "not found" not in error


def test_add_local_invalid_url_is_usage_error(capsys):
    try:
        main(["provider", "add", "--url", "http://user:secret@localhost:9000"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("invalid URL should be rejected by argparse")
    error = capsys.readouterr().err
    assert "usage: subagent provider add" in error
    assert "secret" not in error


def test_add_local_does_not_start_or_load_server(tmp_path, monkeypatch, mock_endpoint):
    mock_endpoint.routes["/endpoint/v1/models"] = (200, {"data": [{"id": "model-a"}]})
    order: list[str] = []
    for path in ("/endpoint/v1/models", "/endpoint/api/status", "/endpoint/metrics",
                 "/endpoint/slots"):
        mock_endpoint.on_hit[path] = lambda path=path: order.append(path)

    _local_add(tmp_path, monkeypatch, mock_endpoint)

    assert order == [
        "/endpoint/v1/models", "/endpoint/api/status", "/endpoint/metrics",
        "/endpoint/slots",
    ]
    assert sum(mock_endpoint.hits.values()) == 4
