"""Acceptance tests for R6(e): `subagent doctor` workspace acceptance check."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from subagent import config
from subagent.cli import main


def _write_codex_config(tmp_path: Path, monkeypatch, binary: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[core]\ndefault_provider = "codex"\n\n'
        '[providers.codex]\ndriver = "codex"\n'
        f'binary = "{binary}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg))
    return cfg


def _isolate_server_paths(tmp_path: Path, monkeypatch) -> None:
    pinned = tmp_path / "outside-pinned"
    hook = pinned / "guard" / "approval_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook\n", encoding="utf-8")
    prefix = pinned / "venv"
    prefix.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "APPROVAL_HOOK", hook)
    monkeypatch.setattr(sys, "prefix", str(prefix))


def test_e_doctor_json_accepted_workspace(tmp_path: Path, monkeypatch, fake_codex, capsys):
    _isolate_server_paths(tmp_path, monkeypatch)
    _write_codex_config(tmp_path, monkeypatch, os.environ["SUBAGENT_TEST_CODEX_BIN"])
    ws = tmp_path / "accepted-ws"
    ws.mkdir()

    assert main(["--root", str(ws), "doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert "workspace" in data
    assert Path(data["workspace"]["path"]).resolve() == ws.resolve()
    assert data["workspace"]["accepted"] is True
    assert data["workspace"]["reason"] is None

    # Default workspace without --root uses cwd.
    monkeypatch.chdir(ws)
    assert main(["doctor", "--json"]) == 0
    cwd_data = json.loads(capsys.readouterr().out)
    assert Path(cwd_data["workspace"]["path"]).resolve() == ws.resolve()
    assert cwd_data["workspace"]["accepted"] is True


def test_e_doctor_json_refused_workspace_exits_0(
    tmp_path: Path, monkeypatch, fake_codex, capsys
):
    _isolate_server_paths(tmp_path, monkeypatch)
    _write_codex_config(tmp_path, monkeypatch, os.environ["SUBAGENT_TEST_CODEX_BIN"])
    ws = tmp_path / "refused-ws"
    hook = ws / "src" / "subagent" / "guard" / "approval_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVAL_HOOK", hook)

    rc = main(["--root", str(ws), "doctor", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert "workspace" in data
    assert Path(data["workspace"]["path"]).resolve() == ws.resolve()
    assert data["workspace"]["accepted"] is False
    assert isinstance(data["workspace"]["reason"], str)
    assert "guard hook" in data["workspace"]["reason"].lower()


def test_e_doctor_no_probe_includes_workspace(tmp_path: Path, monkeypatch, capsys):
    _isolate_server_paths(tmp_path, monkeypatch)
    _write_codex_config(tmp_path, monkeypatch, "/nonexistent/codex")
    ws = tmp_path / "no-probe-ws"
    ws.mkdir()

    assert main(["--root", str(ws), "doctor", "--no-probe", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "workspace" in data
    assert Path(data["workspace"]["path"]).resolve() == ws.resolve()
    assert data["workspace"]["accepted"] is True
    assert data["workspace"]["reason"] is None


def test_e_doctor_table_prints_workspace_line(tmp_path: Path, monkeypatch, capsys):
    _isolate_server_paths(tmp_path, monkeypatch)
    _write_codex_config(tmp_path, monkeypatch, "/nonexistent/codex")
    ws = tmp_path / "table-ws"
    ws.mkdir()

    assert main(["--root", str(ws), "doctor", "--no-probe"]) == 0
    accepted_lines = capsys.readouterr().out.splitlines()
    assert "workspace: accepted" in accepted_lines

    hook = ws / "src" / "subagent" / "guard" / "approval_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("# hook\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVAL_HOOK", hook)

    assert main(["--root", str(ws), "doctor", "--no-probe"]) == 0
    refused_lines = capsys.readouterr().out.splitlines()
    matching = [line for line in refused_lines if line.startswith("workspace: refused: ")]
    assert len(matching) == 1
    assert "guard hook" in matching[0].lower()
