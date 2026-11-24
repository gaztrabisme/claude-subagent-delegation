"""Acceptance test for default max_agents on auth = "login" providers."""

from __future__ import annotations

from pathlib import Path

import pytest

from subagent import config
from tests.conftest import write_config


@pytest.mark.parametrize(
    ("case_id", "core_table", "provider_table", "expected"),
    [
        pytest.param(
            "login_default",
            {"max_agents": 6},
            {"driver": "claude", "auth": "login"},
            1,
            id="login-default",
        ),
        pytest.param(
            "login_explicit",
            {"max_agents": 6},
            {"driver": "claude", "auth": "login", "max_agents": 2},
            2,
            id="login-explicit",
        ),
        pytest.param(
            "non_login_inherits_core",
            {"max_agents": 6},
            {
                "driver": "claude",
                "base_url": "https://api.example.test",
                "api_key_env": "GLM_API_KEY",
            },
            6,
            id="non-login-inherits-core",
        ),
    ],
)
def test_login_max_agents_default(
    tmp_path: Path,
    case_id: str,
    core_table: dict,
    provider_table: dict,
    expected: int,
) -> None:
    cfg_path = write_config(
        tmp_path / f"{case_id}.toml",
        {"core": core_table, "providers": {"worker": provider_table}},
    )
    settings = config.load(extra=cfg_path)
    assert settings.provider("worker").max_agents == expected

    if case_id == "login_default":
        default_core_path = write_config(
            tmp_path / f"{case_id}-omitted-core.toml",
            {"providers": {"worker": provider_table}},
        )
        assert config.load(extra=default_core_path).provider("worker").max_agents == 1
