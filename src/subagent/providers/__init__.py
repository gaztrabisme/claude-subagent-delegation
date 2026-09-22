"""The drivers a provider can be run on, and how to find one by name."""

from __future__ import annotations

from typing import Any

from .base import (
    DRIVER_CLAUDE,
    DRIVER_CODEX,
    DRIVERS,
    HealthSpec,
    NotPorted,
    PricingSpec,
    ProbeSpec,
    Process,
    Provider,
    ProviderConfig,
    Session,
    Translator,
)

_CACHE: dict[str, Any] = {}


def for_driver(name: str) -> Any:
    """The driver object for `name`. KeyError when there is no such driver.

    The modules are imported on first use: `runs` imports this package, and
    the drivers import `runs`-adjacent modules back.
    """
    if name in _CACHE:
        return _CACHE[name]
    if name == DRIVER_CLAUDE:
        from .claude import CLAUDE_PROVIDER as provider
    elif name == DRIVER_CODEX:
        from .codex import CODEX_PROVIDER as provider
    elif name in DRIVERS:
        provider = NotPorted(name)
    else:
        raise KeyError(f"unknown driver {name!r}; expected one of {', '.join(DRIVERS)}")
    _CACHE[name] = provider
    return provider


__all__ = [
    "DRIVERS",
    "DRIVER_CLAUDE",
    "DRIVER_CODEX",
    "HealthSpec",
    "PricingSpec",
    "ProbeSpec",
    "Process",
    "Provider",
    "ProviderConfig",
    "Session",
    "Translator",
    "for_driver",
]
