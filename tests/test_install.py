"""Project-scoped installation for the four supported harnesses."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from subagent import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.delenv("SUBAGENT_CONFIG", raising=False)


def _run(tmp_path: Path, harness: str, *options: str) -> tuple[int, Path]:
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    return cli.main(["install", "--for", harness, "--target-dir", str(target), *options]), target


def _json_config(target: Path, path: str = ".mcp.json") -> dict:
    return json.loads((target / path).read_text())


def _toml_config(target: Path) -> dict:
    return tomllib.loads((target / ".codex/config.toml").read_text())


def test_install_claude_writes_skill_plan_goal_and_timeout(tmp_path, capsys):
    code, target = _run(tmp_path, "claude")

    assert code == 0
    skill = target / ".claude/skills/delegate/SKILL.md"
    plan = target / ".claude/commands/plan.md"
    goal = target / ".claude/commands/goal.md"
    assert skill.read_text() == (ROOT / "skills/delegate/SKILL.md").read_text()
    assert "## Goal block" in plan.read_text()
    assert "Do not implement" in plan.read_text()
    assert "GOAL.md" in goal.read_text()
    assert "only the goal block" in goal.read_text()
    assert _json_config(target)["mcpServers"]["subagent"]["timeout"] == 1_800_000
    output = capsys.readouterr().out
    for path in (skill, plan, goal, target / ".mcp.json"):
        assert str(path) in output
    assert "Timeout: 1800 seconds" in output


def test_install_codex_writes_instruction_and_timeout(tmp_path):
    code, target = _run(tmp_path, "codex")

    assert code == 0
    assert (target / ".agents/skills/delegate/SKILL.md").is_file()
    assert (target / ".codex/prompts/plan.md").is_file()
    assert (target / ".codex/prompts/goal.md").is_file()
    assert _toml_config(target)["mcp_servers"]["subagent"]["tool_timeout_sec"] == 1800


def test_install_gemini_writes_instruction_and_timeout(tmp_path):
    code, target = _run(tmp_path, "gemini")

    assert code == 0
    instruction = target / ".gemini/GEMINI.md"
    assert ".agents/skills/delegate/SKILL.md" in instruction.read_text()
    assert (target / ".gemini/commands/plan.toml").is_file()
    assert (target / ".gemini/commands/goal.toml").is_file()
    command = tomllib.loads((target / ".gemini/commands/plan.toml").read_text())
    assert set(command) == {"description", "prompt"}
    assert "## Goal block" in command["prompt"]
    assert _json_config(target, ".gemini/settings.json")["mcpServers"]["subagent"]["timeout"] == 1_800_000


def test_install_copilot_writes_instruction_and_timeout(tmp_path):
    code, target = _run(tmp_path, "copilot")

    assert code == 0
    instruction = target / ".github/copilot-instructions.md"
    assert ".agents/skills/delegate/SKILL.md" in instruction.read_text()
    plan = target / ".github/prompts/plan.prompt.md"
    goal = target / ".github/prompts/goal.prompt.md"
    assert "name: plan" in plan.read_text()
    assert "name: goal" in goal.read_text()
    server = _json_config(target)["mcpServers"]["subagent"]
    assert server == {
        "type": "local",
        "command": "subagent-mcp",
        "args": [],
        "tools": ["*"],
        "timeout": 1_800_000,
    }


def test_install_claude_registers_subagent_timeout_milliseconds(tmp_path):
    code, target = _run(tmp_path, "claude", "--timeout", "75")

    assert code == 0
    assert _json_config(target)["mcpServers"]["subagent"]["timeout"] == 75_000


def test_install_codex_registers_subagent_timeout_seconds(tmp_path):
    code, target = _run(tmp_path, "codex", "--timeout", "75")

    assert code == 0
    assert _toml_config(target)["mcp_servers"]["subagent"] == {
        "command": "subagent-mcp",
        "tool_timeout_sec": 75,
    }


def test_install_gemini_registers_subagent_timeout_milliseconds(tmp_path):
    code, target = _run(tmp_path, "gemini", "--timeout", "75")

    assert code == 0
    assert _json_config(target, ".gemini/settings.json")["mcpServers"]["subagent"]["timeout"] == 75_000


def test_install_copilot_registers_subagent_timeout_milliseconds(tmp_path):
    code, target = _run(tmp_path, "copilot", "--timeout", "90")

    assert code == 0
    assert _json_config(target)["mcpServers"]["subagent"]["timeout"] == 90_000


def test_install_target_dir_overrides_environment(tmp_path, monkeypatch):
    env_target = tmp_path / "from-environment"
    env_target.mkdir()
    selected = tmp_path / "selected"
    selected.mkdir()
    monkeypatch.setenv("SUBAGENT_INSTALL_ROOT", str(env_target))

    assert cli.main(["install", "--for", "claude", "--target-dir", str(selected)]) == 0
    assert (selected / ".mcp.json").is_file()
    assert not (env_target / ".mcp.json").exists()


def test_install_uses_environment_target(tmp_path, monkeypatch):
    target = tmp_path / "environment-target"
    target.mkdir()
    monkeypatch.setenv("SUBAGENT_INSTALL_ROOT", str(target))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["install", "--for", "codex"]) == 0
    assert (target / ".codex/config.toml").is_file()
    assert not (tmp_path / ".codex/config.toml").exists()


def test_install_uses_global_root_when_no_target_is_set(tmp_path, monkeypatch):
    target = tmp_path / "root-target"
    target.mkdir()
    monkeypatch.delenv("SUBAGENT_INSTALL_ROOT", raising=False)

    assert cli.main(["--root", str(target), "install", "--for", "codex"]) == 0
    assert (target / ".codex/config.toml").is_file()


def test_install_uses_only_temporary_target(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    target = tmp_path / "target"
    cwd.mkdir()
    target.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("SUBAGENT_INSTALL_ROOT", str(target))

    assert cli.main(["install", "--for", "gemini"]) == 0
    assert (target / ".gemini/settings.json").is_file()
    assert list(cwd.iterdir()) == []


def test_install_preserves_unrelated_mcp_entries(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    original = {
        "custom": {"kept": True},
        "mcpServers": {"other": {"command": "other-mcp", "args": ["--keep"]}},
    }
    (target / ".mcp.json").write_text(json.dumps(original, indent=2))

    assert cli.main(["install", "--for", "claude", "--target-dir", str(target)]) == 0
    merged = _json_config(target)
    assert merged["custom"] == original["custom"]
    assert merged["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert "subagent" in merged["mcpServers"]


def test_install_existing_server_requires_force(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    config_path = target / ".mcp.json"
    original = '{"mcpServers":{"subagent":{"command":"existing"}}}\n'
    config_path.write_text(original)

    assert cli.main(["install", "--for", "claude", "--target-dir", str(target)]) == 1
    assert capsys.readouterr().err == "error: MCP server 'subagent' already exists\n"
    assert config_path.read_text() == original
    assert not (target / ".claude/skills/delegate/SKILL.md").exists()


def test_install_owned_file_collision_leaves_every_target_unchanged(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    instruction = target / ".github/copilot-instructions.md"
    instruction.parent.mkdir()
    instruction.write_text("keep this instruction\n")
    config_path = target / ".mcp.json"
    original_config = '{"mcpServers":{"other":{"command":"other-mcp"}}}\n'
    config_path.write_text(original_config)

    assert cli.main([
        "install", "--for", "copilot", "--target-dir", str(target)
    ]) == 1
    assert f"error: file '{instruction}' already exists; use --force to overwrite" in (
        capsys.readouterr().err
    )
    assert instruction.read_text() == "keep this instruction\n"
    assert config_path.read_text() == original_config
    assert not (target / ".agents/skills/delegate/SKILL.md").exists()


def test_install_force_replaces_managed_content_only(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    config_path = target / ".mcp.json"
    original = {
        "mcpServers": {
            "other": {"command": "other-mcp"},
            "subagent": {"command": "old"},
        }
    }
    config_path.write_text(json.dumps(original, indent=2))
    skill_path = target / ".claude/skills/delegate/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("old skill\n")

    assert cli.main([
        "install", "--for", "claude", "--target-dir", str(target), "--timeout", "45", "--force"
    ]) == 0
    merged = _json_config(target)
    assert merged["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert merged["mcpServers"]["subagent"]["timeout"] == 45_000
    assert skill_path.read_text() == (ROOT / "skills/delegate/SKILL.md").read_text()


@pytest.mark.parametrize(
    "original",
    [
        '[mcp_servers]\nother = { command = "other-mcp" }\n'
        'subagent = { command = "old" }\n',
        'mcp_servers = { other = { command = "other-mcp" }, '
        'subagent = { command = "old" } }\n',
    ],
)
def test_install_force_replaces_inline_toml_server_and_preserves_neighbor(tmp_path, original):
    target = tmp_path / "target"
    target.mkdir()
    config_path = target / ".codex/config.toml"
    config_path.parent.mkdir()
    config_path.write_text(original)

    assert cli.main([
        "install", "--for", "codex", "--target-dir", str(target), "--force", "--timeout", "75"
    ]) == 0
    servers = _toml_config(target)["mcp_servers"]
    assert servers["other"] == {"command": "other-mcp"}
    assert servers["subagent"] == {"command": "subagent-mcp", "tool_timeout_sec": 75}


def test_install_config_write_failure_reports_partial_files(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    target.mkdir()
    registration = target / ".mcp.json"
    write_text = Path.write_text

    def fail_registration(path: Path, data: str, *args, **kwargs):
        if path == registration:
            raise OSError("permission denied")
        return write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_registration)
    assert cli.main(["install", "--for", "claude", "--target-dir", str(target)]) == 1
    error = capsys.readouterr().err
    assert f"error: cannot update '{registration}': permission denied" in error
    assert "Files already written:" in error
    assert (target / ".claude/skills/delegate/SKILL.md").is_file()


def test_install_copilot_rejects_timeout_below_native_minimum(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(SystemExit) as exc:
        cli.main([
            "install", "--for", "copilot", "--target-dir", str(target), "--timeout", "59"
        ])

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "usage:" in error
    assert "at least 60 seconds" in error
    assert list(target.iterdir()) == []


def test_install_invalid_json_leaves_targets_unchanged(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    config_path = target / ".mcp.json"
    config_path.write_text("{invalid\n")

    assert cli.main(["install", "--for", "claude", "--target-dir", str(target)]) == 1
    assert f"error: cannot update '{config_path}':" in capsys.readouterr().err
    assert config_path.read_text() == "{invalid\n"
    assert not (target / ".claude/commands/plan.md").exists()


def test_install_default_timeout_uses_project_run_timeout(tmp_path):
    target = tmp_path / "target"
    (target / ".subagent").mkdir(parents=True)
    (target / ".subagent/config.toml").write_text("[core]\nrun_timeout = 240\n")

    assert cli.main(["install", "--for", "claude", "--target-dir", str(target)]) == 0
    assert _json_config(target)["mcpServers"]["subagent"]["timeout"] == 240_000
