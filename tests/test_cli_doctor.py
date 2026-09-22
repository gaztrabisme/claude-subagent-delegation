"""`subagent doctor` reports binary, key, health, adapter and prompt per provider."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from subagent import config
from subagent.cli import main


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
