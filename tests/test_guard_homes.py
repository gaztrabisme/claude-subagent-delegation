"""Provider credential homes are denied only when that driver is configured."""

from __future__ import annotations

from pathlib import Path

import pytest

from subagent.config import Settings
from subagent.guard import classify
from subagent.providers.base import ProviderConfig


@pytest.fixture(autouse=True)
def _reset_provider_homes(monkeypatch):
    monkeypatch.setattr(classify, "_provider_homes", ())


def _settings(*providers: tuple[str, str, str]) -> Settings:
    return Settings(
        workspace=Path("."),
        session_root=Path("/tmp/subagent-test-sessions"),
        providers={
            name: ProviderConfig(name=name, driver=driver, vendor=vendor)
            for name, driver, vendor in providers
        },
    )


def _home(name: str) -> Path:
    return Path.home() / name


def test_codex_home_denied_only_when_codex_configured():
    codex_home = _home(".codex")
    classify.protect_provider_homes(_settings(("codex", "codex", "codex")))
    assert classify.is_sensitive(codex_home) is not None
    classify.protect_provider_homes(_settings(("glm", "claude", "zai")))
    assert classify.is_sensitive(codex_home) is None


def test_omlx_home_denied_only_when_omlx_configured():
    omlx_home = _home(".omlx")
    classify.protect_provider_homes(_settings(("omlx", "claude", "omlx")))
    assert classify.is_sensitive(omlx_home) is not None
    classify.protect_provider_homes(_settings(("glm", "claude", "zai")))
    assert classify.is_sensitive(omlx_home) is None


def test_subagent_home_is_always_sensitive():
    assert classify.is_sensitive(_home(".subagent")) is not None


def test_removed_homes_are_not_sensitive():
    assert classify.is_sensitive(_home(".glm-subagent")) is None
    assert classify.is_sensitive(_home(".subagent-mcp")) is None
