"""N1-4: `[graph].default_reserve_usd` must be positive."""

from __future__ import annotations

import pytest

from tests.conftest import make_settings


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_non_positive_default_reserve_is_rejected(tmp_path, value):
    from subagent.config import ConfigError
    from subagent.graph.server import settings_from

    with pytest.raises((ConfigError, ValueError)) as err:
        settings_from(make_settings(tmp_path), {"default_reserve_usd": value})
    assert "default_reserve_usd" in str(err.value)


@pytest.mark.parametrize("value", [0.01, 0.5, 2])
def test_positive_default_reserve_is_accepted(tmp_path, value):
    from subagent.graph.server import settings_from

    gs = settings_from(make_settings(tmp_path), {"default_reserve_usd": value})
    assert gs.default_reserve_usd == float(value)
