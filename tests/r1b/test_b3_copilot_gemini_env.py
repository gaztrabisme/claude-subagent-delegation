"""B3 acceptance tests: Copilot and Gemini CLI drivers build env from Settings.base_child_env()."""

from __future__ import annotations

import secrets
from pathlib import Path

from subagent.providers import copilot as copilot_driver
from subagent.providers import gemini as gemini_driver
from subagent.providers.base import ProviderConfig, Session
from tests.conftest import make_settings


def _copilot_cfg(**extra) -> ProviderConfig:
    return ProviderConfig(name="copilot", driver="copilot", vendor="copilot", extra=extra)


def _gemini_cfg(**kw) -> ProviderConfig:
    return ProviderConfig(name="gemini", driver="gemini", vendor="gemini", **kw)


def test_b3_copilot_env_uses_allowlist_and_excludes_secrets(tmp_path: Path, monkeypatch) -> None:
    """B3.1: CopilotProvider.env() starts from base_child_env() and strips canary, undeclared GH_TOKEN, and AWS_SECRET_ACCESS_KEY."""
    canary = f"canary-{secrets.token_hex(8)}"
    gh_tok = f"gh-{secrets.token_hex(8)}"
    aws_secret = f"aws-{secrets.token_hex(8)}"
    glm_key = f"glm-{secrets.token_hex(8)}"
    unlisted = f"unlisted-{secrets.token_hex(8)}"
    safe_pass = f"safe-{secrets.token_hex(6)}"

    monkeypatch.setenv("SECRET_CANARY_TOKEN", canary)
    monkeypatch.setenv("GH_TOKEN", gh_tok)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", aws_secret)
    monkeypatch.setenv("GLM_API_KEY", glm_key)
    monkeypatch.setenv("UNALLOWLISTED_COPILOT_VAR", unlisted)
    monkeypatch.setenv("SAFE_COPILOT_PASSTHROUGH", safe_pass)

    settings = make_settings(tmp_path, child_env_passthrough=["SAFE_COPILOT_PASSTHROUGH"])
    env = copilot_driver.COPILOT_PROVIDER.env(
        settings, "a1", _copilot_cfg(), Session(provider="copilot")
    )

    for forbidden in (
        "SECRET_CANARY_TOKEN",
        "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GLM_API_KEY",
        "UNALLOWLISTED_COPILOT_VAR",
    ):
        assert forbidden not in env, f"{forbidden} leaked into CopilotProvider.env()"

    assert "PATH" in env
    assert "HOME" in env
    assert env.get("SAFE_COPILOT_PASSTHROUGH") == safe_pass


def test_b3_copilot_env_includes_declared_api_key(tmp_path: Path, monkeypatch) -> None:
    """B3.2: CopilotProvider.env() includes the provider's own declared api_key_env when configured."""
    copilot_key = f"copilot-key-{secrets.token_hex(8)}"
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", copilot_key)
    monkeypatch.setenv("SECRET_CANARY_TOKEN", f"canary-{secrets.token_hex(8)}")
    monkeypatch.setenv("GH_TOKEN", f"gh-{secrets.token_hex(8)}")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", f"aws-{secrets.token_hex(8)}")

    settings = make_settings(
        tmp_path,
        providers={
            "copilot": {
                "driver": "copilot",
                "vendor": "copilot",
                "api_key_env": "COPILOT_GITHUB_TOKEN",
            },
        },
    )
    cfg = settings.providers["copilot"]
    env = copilot_driver.COPILOT_PROVIDER.env(settings, "a1", cfg, Session(provider="copilot"))

    assert env.get("COPILOT_GITHUB_TOKEN") == copilot_key
    assert "SECRET_CANARY_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_b3_gemini_env_uses_allowlist_and_excludes_secrets(tmp_path: Path, monkeypatch) -> None:
    """B3.3: GeminiProvider.env() starts from base_child_env() and strips canary, undeclared GH_TOKEN, and AWS_SECRET_ACCESS_KEY."""
    canary = f"canary-{secrets.token_hex(8)}"
    gh_tok = f"gh-{secrets.token_hex(8)}"
    aws_secret = f"aws-{secrets.token_hex(8)}"
    glm_key = f"glm-{secrets.token_hex(8)}"
    unlisted = f"unlisted-{secrets.token_hex(8)}"
    safe_pass = f"safe-{secrets.token_hex(6)}"

    monkeypatch.setenv("SECRET_CANARY_TOKEN", canary)
    monkeypatch.setenv("GH_TOKEN", gh_tok)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", aws_secret)
    monkeypatch.setenv("GLM_API_KEY", glm_key)
    monkeypatch.setenv("UNALLOWLISTED_GEMINI_VAR", unlisted)
    monkeypatch.setenv("SAFE_GEMINI_PASSTHROUGH", safe_pass)

    settings = make_settings(tmp_path, child_env_passthrough=["SAFE_GEMINI_PASSTHROUGH"])
    env = gemini_driver.GEMINI_PROVIDER.env(
        settings, "a1", _gemini_cfg(experimental=True), Session(provider="gemini")
    )

    for forbidden in (
        "SECRET_CANARY_TOKEN",
        "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GLM_API_KEY",
        "UNALLOWLISTED_GEMINI_VAR",
    ):
        assert forbidden not in env, f"{forbidden} leaked into GeminiProvider.env()"

    assert "PATH" in env
    assert "HOME" in env
    assert env.get("SAFE_GEMINI_PASSTHROUGH") == safe_pass


def test_b3_gemini_env_includes_declared_api_key(tmp_path: Path, monkeypatch) -> None:
    """B3.4: GeminiProvider.env() includes the provider's own declared api_key_env when configured."""
    gemini_key = f"gemini-key-{secrets.token_hex(8)}"
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("SECRET_CANARY_TOKEN", f"canary-{secrets.token_hex(8)}")
    monkeypatch.setenv("GH_TOKEN", f"gh-{secrets.token_hex(8)}")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", f"aws-{secrets.token_hex(8)}")

    settings = make_settings(
        tmp_path,
        providers={
            "gemini": {
                "driver": "gemini",
                "vendor": "gemini",
                "api_key_env": "GEMINI_API_KEY",
                "experimental": True,
            },
        },
    )
    cfg = settings.providers["gemini"]
    env = gemini_driver.GEMINI_PROVIDER.env(settings, "a1", cfg, Session(provider="gemini"))

    assert env.get("GEMINI_API_KEY") == gemini_key
    assert "SECRET_CANARY_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
