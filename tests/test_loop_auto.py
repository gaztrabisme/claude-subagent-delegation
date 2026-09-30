"""The S1 auto-delegation setting is parsed and exposed by doctor."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from subagent import config
from subagent.cli import main
from subagent.config import ConfigError
from tests.conftest import write_config


def _config_file(tmp_path: Path, monkeypatch, fake_codex, *, auto: str | None = None) -> Path:
    tables = {
        "core": {"workspace": str(tmp_path), "default_provider": "codex"},
        "providers": {
            "codex": {"driver": "codex", "binary": os.environ["SUBAGENT_TEST_CODEX_BIN"]}
        },
    }
    if auto is not None:
        tables["loop"] = {"auto": auto}
    path = write_config(tmp_path / "config.toml", tables)
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    return path


def test_loop_auto_defaults_to_ask(tmp_path: Path) -> None:
    path = write_config(tmp_path / "default.toml", {
        "providers": {"codex": {"driver": "codex"}},
    })

    assert config.load(extra=path).loop.auto == "ask"


@pytest.mark.parametrize("value", ["ask", "always", "never"])
def test_loop_auto_accepts_ask_always_never(tmp_path: Path, value: str) -> None:
    path = write_config(tmp_path / f"{value}.toml", {
        "loop": {"auto": value},
        "providers": {"codex": {"driver": "codex"}},
    })

    assert config.load(extra=path).loop.auto == value


def test_loop_auto_rejects_other_values(tmp_path: Path, capsys) -> None:
    path = write_config(tmp_path / "invalid.toml", {
        "loop": {"auto": "sometimes"},
        "providers": {"codex": {"driver": "codex"}},
    })
    with pytest.raises(ConfigError) as exc_info:
        config.load(extra=path)
    assert str(exc_info.value) == "[loop].auto must be 'ask', 'always' or 'never'"


def test_loop_auto_is_reported_in_doctor_json(tmp_path: Path, monkeypatch, fake_codex, capsys) -> None:
    _config_file(tmp_path, monkeypatch, fake_codex, auto="always")

    assert main(["doctor", "--no-probe", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["loop_auto"] == "always"


def test_review_b2_loop_auto_skill_gate_covers_never_and_bad_json() -> None:
    skill = Path(__file__).resolve().parents[1] / "skills/delegate/SKILL.md"
    text = " ".join(skill.read_text(encoding="utf-8").split())

    assert "`never`: keep the work with the orchestrator and do not start a worker." in text
    assert "report a configuration error and stop before delegation." in text
    assert "Never infer `always` from missing or malformed output." in text
