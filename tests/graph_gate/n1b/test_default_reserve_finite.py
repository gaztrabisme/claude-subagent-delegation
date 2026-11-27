"""N4 fix: `[graph].default_reserve_usd` must be a finite positive number."""

from __future__ import annotations

import pytest

from tests.conftest import make_settings


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_default_reserve_is_rejected(tmp_path, value):
    from subagent.config import ConfigError
    from subagent.graph.server import settings_from

    with pytest.raises((ConfigError, ValueError)):
        settings_from(make_settings(tmp_path), {"default_reserve_usd": value})
