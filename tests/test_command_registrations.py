"""S1 installs command seams while leaving the existing init wizard usable."""

from __future__ import annotations

from pathlib import Path

from subagent import config
from subagent.cli import main


def test_command_registration_routes_cli_to_install(
    tmp_path: Path, capsys
) -> None:
    target = tmp_path / "target"

    assert main([
        "install", "--for", "codex", "--target-dir", str(target)
    ]) == 0
    output = capsys.readouterr().out
    assert "Installed for: codex" in output
    assert f"Target: {target}" in output
    assert (target / ".codex/config.toml").is_file()


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
