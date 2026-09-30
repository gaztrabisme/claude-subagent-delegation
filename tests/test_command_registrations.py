"""S1 installs command seams while leaving the existing init wizard usable."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest

from subagent import config
from subagent.cli import main


@pytest.mark.parametrize(
    "module_name",
    [
        "subagent.commands.init",
        "subagent.commands.install",
    ],
)
def test_command_registration_stubs_are_importable_and_exit_two(
    module_name: str, capsys
) -> None:
    module = importlib.import_module(module_name)

    assert module.run(argparse.Namespace()) == 2
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert len(lines) == 1
    assert "not implemented" in lines[0].lower()


@pytest.mark.parametrize(
    ("argv", "module_name"),
    [
        (["install", "--for", "codex"], "subagent.commands.install"),
    ],
)
def test_command_registration_routes_cli_to_stubs(
    argv: list[str], module_name: str, capsys
) -> None:
    importlib.import_module(module_name)

    assert main(argv) == 2
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert len(lines) == 1
    assert "not implemented" in lines[0].lower()


def test_command_registration_routes_provider_commands(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    destination = tmp_path / "xdg" / "subagent" / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(
        '[core]\ndefault_provider = "one"\n\n'
        '[providers.one]\ndriver = "codex"\nmodel = "model-one"\n\n'
        '[providers.two]\ndriver = "codex"\nmodel = "model-two"\n',
        encoding="utf-8",
    )

    assert main(["provider", "list", "--json"]) == 0
    listed = capsys.readouterr().out
    assert '"one"' in listed and '"two"' in listed

    assert main(["use", "two"]) == 0
    assert "default provider set to 'two'" in capsys.readouterr().out
    assert config.load(extra=destination).default_provider == "two"


def test_command_registration_keeps_existing_init_working(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    destination = tmp_path / "config.toml"

    assert main([
        "init", "--yes", "--from", "codex", "--path", str(destination)
    ]) == 0
    output = capsys.readouterr().out
    assert f"wrote {destination}" in output
    assert config.load(extra=destination).providers
