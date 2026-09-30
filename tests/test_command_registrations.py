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
        "subagent.commands.providers",
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
        (["provider", "list"], "subagent.commands.providers"),
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
