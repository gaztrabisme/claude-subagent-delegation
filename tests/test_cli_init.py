"""`subagent init` writes loadable configs and never a key value."""

from __future__ import annotations

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


def test_refuses_overwrite_without_force(tmp_path: Path, capsys) -> None:
    dest = tmp_path / "config.toml"
    assert main(["init", "--yes", "--from", "glm", "--path", str(dest)]) == 0
    capsys.readouterr()
    before = dest.read_text(encoding="utf-8")
    assert main(["init", "--yes", "--from", "codex", "--path", str(dest)]) == 1
    assert dest.read_text(encoding="utf-8") == before
    assert main(["init", "--yes", "--from", "codex", "--path", str(dest), "--force"]) == 0
    assert "codex" in dest.read_text(encoding="utf-8")
