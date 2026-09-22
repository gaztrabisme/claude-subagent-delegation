from __future__ import annotations

import json
from pathlib import Path

from subagent.config import Settings

from .conftest import make_settings


def test_result_cap_chars_is_summary_tokens_times_ratio(tmp_path: Path):
    settings = make_settings(tmp_path, summary_tokens=2000, chars_per_token=3.5)
    assert settings.result_cap_chars == 7000


def test_hooks_config_lives_under_session_root_not_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    settings = make_settings(tmp_path, supervisor="agent")
    path = settings.hooks_config("a1")
    assert path.is_relative_to(settings.session_root)
    assert not (home / ".claude" / "settings.json").exists()
    payload = path.read_text()
    assert "PreToolUse" in payload
    assert "approval_hook.py" in payload


def test_child_env_strips_parent_oauth_and_sets_glm(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-oauth")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    settings = make_settings(tmp_path)
    env = settings.child_env("a1")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "glm-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(settings.session_root))


def test_from_env_reads_sam_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAM_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SAM_GLM_MODEL", "glm-5.3[1m]")
    monkeypatch.setenv("GLM_API_KEY", "from-env")
    settings = Settings.from_env()
    assert settings.workspace == tmp_path.resolve()
    assert settings.lane("glm").model == "glm-5.3[1m]"
    assert settings.lane().api_key() == "from-env"


def test_compact_window_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAM_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SAM_COMPACT_WINDOW", "40960")
    monkeypatch.setenv("GLM_API_KEY", "k")
    settings = Settings.from_env()
    assert settings.compact_window == 40960
    env = settings.child_env("a1")
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "40960"
    payload = json.loads(settings.hooks_config("a1").read_text())
    assert payload["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "40960"
    assert payload["autoCompactWindow"] == 40960


def test_loop_strikes_default_is_eight(monkeypatch):
    """At 3, a child re-running its tests between edits would be killed."""
    monkeypatch.delenv("SAM_LOOP_STRIKES", raising=False)
    assert Settings.from_env().loop_strikes == 8
