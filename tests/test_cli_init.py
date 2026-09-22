"""`subagent init` writes loadable configs and never a key value."""

from __future__ import annotations

import os
from pathlib import Path

from subagent import config
from subagent.cli import main

NAMES = ("copilot", "codex", "glm", "deepseek", "llama.cpp", "omlx", "vllm", "full")


def test_yes_from_each_example_loads(tmp_path: Path, capsys) -> None:
    for name in NAMES:
        dest = tmp_path / f"{name}.toml"
        assert main(["init", "--yes", "--from", name, "--path", str(dest)]) == 0, name
        capsys.readouterr()
        settings = config.load(extra=dest)  # raises on any config error
        assert settings.providers, name


def test_never_writes_a_key_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "secret-glm-key")
    dest = tmp_path / "glm.toml"
    assert main(["init", "--yes", "--from", "glm", "--path", str(dest)]) == 0
    text = dest.read_text(encoding="utf-8")
    assert "secret-glm-key" not in text
    assert "GLM_API_KEY" in text  # the variable is named, never its value


def test_from_env_conversion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
    monkeypatch.setenv("SAM_CODEX_BIN", "/opt/codex/bin/codex")
    dest = tmp_path / "config.toml"
    assert main(["init", "--from-env", "--path", str(dest)]) == 0
    settings = config.load(extra=dest)
    assert set(settings.providers) == {"glm", "deepseek", "codex"}
    assert settings.providers["glm"].driver == "claude"
    assert settings.providers["glm"].api_key_envs == ("GLM_API_KEY",)
    assert settings.providers["deepseek"].api_key_envs == ("DEEPSEEK_API_KEY",)
    assert settings.providers["codex"].binary == "/opt/codex/bin/codex"
    text = dest.read_text(encoding="utf-8")
    assert "glm-secret" not in text and "ds-secret" not in text


def test_from_env_codex_on_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    monkeypatch.delenv("SAM_CODEX_BIN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text("#!/bin/sh\n")
    (bindir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    dest = tmp_path / "config.toml"
    assert main(["init", "--from-env", "--path", str(dest)]) == 0
    settings = config.load(extra=dest)
    assert "codex" in settings.providers
    assert settings.providers["codex"].binary is None


def test_refuses_overwrite_without_force(tmp_path: Path, capsys) -> None:
    dest = tmp_path / "config.toml"
    assert main(["init", "--yes", "--from", "glm", "--path", str(dest)]) == 0
    capsys.readouterr()
    before = dest.read_text(encoding="utf-8")
    assert main(["init", "--yes", "--from", "codex", "--path", str(dest)]) == 1
    assert dest.read_text(encoding="utf-8") == before
    assert main(["init", "--yes", "--from", "codex", "--path", str(dest), "--force"]) == 0
    assert "codex" in dest.read_text(encoding="utf-8")
