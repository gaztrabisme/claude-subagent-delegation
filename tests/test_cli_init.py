"""`subagent init` writes loadable configs and never a key value."""

from __future__ import annotations

import tomllib
from pathlib import Path

from subagent import config
from subagent.cli import main
from subagent.commands import init as init_command

NAMES = (
    "claude", "copilot", "codex", "glm", "deepseek", "llama.cpp", "omlx", "vllm", "full",
)


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


def test_init_detect_does_not_run_or_write_to_detected_clis(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    marker = tmp_path / "executed"
    contents = f"#!/bin/sh\nprintf ran > {marker}\n"
    for name in init_command.SUBSCRIPTION_CLIS:
        path = binary_dir / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir))

    dest = tmp_path / "config.toml"
    assert main(["init", "--yes", "--from", "glm", "--path", str(dest)]) == 0

    assert not marker.exists()
    assert all((binary_dir / name).read_text(encoding="utf-8") == contents
               for name in init_command.SUBSCRIPTION_CLIS)
    assert dest.is_file()
    capsys.readouterr()


def test_init_detect_offers_installed_clis_without_keys(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    installed = {"claude", "codex"}
    monkeypatch.setattr(
        init_command.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name in installed else None,
    )
    answers = iter(("claude,codex", "", "", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    dest = tmp_path / "config.toml"

    assert main(["init", "--from", "glm", "--path", str(dest)]) == 0

    data = tomllib.loads(dest.read_text(encoding="utf-8"))
    providers = data["providers"]
    assert providers["claude"]["auth"] == "login"
    assert providers["codex"]["driver"] == "codex"
    for name in ("claude", "codex"):
        assert "api_key" not in providers[name]
        assert "api_key_env" not in providers[name]
        assert providers[name]["pricing"] == {"kind": "flat_plan"}
    assert "detected subscription CLIs: claude, codex" in capsys.readouterr().out


def test_init_detect_skips_missing_clis(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(init_command.shutil, "which", lambda _name: None)
    dest = tmp_path / "config.toml"

    assert main(["init", "--yes", "--from", "glm", "--path", str(dest)]) == 0

    providers = tomllib.loads(dest.read_text(encoding="utf-8"))["providers"]
    assert set(providers) == {"glm"}
    assert "No supported subscription CLI detected." in capsys.readouterr().out


def test_init_detect_yes_adds_detected_subscription_tables(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    detected = {"claude", "codex", "copilot", "gemini", "grok"}
    monkeypatch.setattr(
        init_command.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name in detected else None,
    )
    dest = tmp_path / "config.toml"

    assert main(["init", "--yes", "--path", str(dest)]) == 0

    data = tomllib.loads(dest.read_text(encoding="utf-8"))
    for name in detected:
        provider = data["providers"][name]
        assert provider["driver"] == name
        assert provider["pricing"] == {"kind": "flat_plan"}
        assert "api_key" not in provider and "api_key_env" not in provider
    assert data["providers"]["claude"]["auth"] == "login"
    assert "wrote" in capsys.readouterr().out


def test_init_unknown_example_reports_choices_and_exits_two(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(init_command.shutil, "which", lambda _name: None)
    dest = tmp_path / "config.toml"

    assert main(["init", "--yes", "--from", "missing", "--path", str(dest)]) == 2

    error = capsys.readouterr().err.strip()
    assert "error: unknown example 'missing'; examples:" in error
    assert not dest.exists()


def test_init_unwritable_destination_reports_path_and_exits_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(init_command.shutil, "which", lambda _name: None)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("keep", encoding="utf-8")
    dest = blocked_parent / "config.toml"

    assert main(["init", "--yes", "--from", "glm", "--path", str(dest)]) == 1

    error = capsys.readouterr().err.strip()
    assert error.startswith(f"error: cannot write config '{dest}': ")
    assert blocked_parent.read_text(encoding="utf-8") == "keep"
