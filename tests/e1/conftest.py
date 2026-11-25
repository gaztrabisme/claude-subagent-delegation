"""E1 fixtures: the envelope record kinds are new, so the suite-wide schema check
(tests/conftest.py trace_records, which rejects unknown kinds) is replaced here
by a plain collector. tests/e1/test_trace_shape.py pins the new shapes itself."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def trace_records():
    yield []


@pytest.fixture
def make_env(tmp_path, monkeypatch):
    """Factory for a registry over recorded fake spawns; shut down at test end."""
    from ._helpers import Env

    made: list[Env] = []

    def factory(*args, **kwargs):
        env = Env(tmp_path, monkeypatch, *args, **kwargs)
        made.append(env)
        return env

    yield factory
    for env in made:
        env.close()
