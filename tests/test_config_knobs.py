"""Configuration shape, ranges and driver support for the S1 knobs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subagent import config
from subagent.cli import main
from subagent.config import ConfigError
from tests.conftest import write_config


def _tables(*, core=None, loop=None, providers=None):
    return {
        "core": core or {},
        "loop": loop or {},
        "providers": providers or {"codex": {"driver": "codex"}},
    }


def _load(tmp_path: Path, *, core=None, loop=None, providers=None, name="knobs.toml"):
    path = write_config(tmp_path / name, _tables(core=core, loop=loop, providers=providers))
    return config.load(extra=path), path


def test_config_knobs_values_and_types_validate(tmp_path: Path) -> None:
    defaults, _ = _load(tmp_path)
    assert defaults.loop.auto == "ask"
    assert defaults.loop.test_output_cap is None
    assert defaults.loop.parallel_tool_calls is False
    assert defaults.loop.no_narration is False
    assert defaults.loop.delegate_test_writer is False
    assert defaults.compact_window == 1_000_000
    assert defaults.provider("codex").compact_window == 1_000_000
    assert defaults.provider("codex").thinking is None

    settings, _ = _load(
        tmp_path,
        core={"compact_window": 120_000},
        loop={
            "auto": "always",
            "test_output_cap": 420,
            "parallel_tool_calls": True,
            "no_narration": True,
            "delegate_test_writer": True,
            "test_writer": {"provider": "writer"},
        },
        providers={
            "codex": {
                "driver": "codex",
                "compact_window": 64_000,
                "effort": "xhigh",
                "thinking": "low",
            },
            "writer": {"driver": "codex"},
        },
        name="set.toml",
    )
    assert settings.loop.auto == "always"
    assert settings.loop.test_output_cap == 420
    assert settings.loop.parallel_tool_calls is True
    assert settings.loop.no_narration is True
    assert settings.loop.delegate_test_writer is True
    assert settings.compact_window == 120_000
    assert settings.provider("codex").compact_window == 64_000
    assert settings.provider("codex").thinking == "low"
    assert settings.provider("codex").effort == "xhigh"

    inherited, _ = _load(
        tmp_path,
        core={"compact_window": 80_000},
        providers={"codex": {"driver": "codex"}},
        name="inherit.toml",
    )
    assert inherited.provider("codex").compact_window == 80_000


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        (
            _tables(loop={"test_output_cap": 0}),
            "[loop].test_output_cap must be a positive integer",
        ),
        (
            _tables(loop={"test_output_cap": -2}),
            "[loop].test_output_cap must be a positive integer",
        ),
        (
            _tables(loop={"test_output_cap": 1.5}),
            "[loop].test_output_cap must be a positive integer",
        ),
        (
            _tables(loop={"test_output_cap": True}),
            "[loop].test_output_cap must be a positive integer",
        ),
        (
            _tables(core={"compact_window": 0}),
            "[core].compact_window must be a positive integer",
        ),
        (
            _tables(core={"compact_window": 2.5}),
            "[core].compact_window must be a positive integer",
        ),
        (
            _tables(providers={"codex": {"driver": "codex", "compact_window": -1}}),
            "[providers.codex].compact_window must be a positive integer",
        ),
        (
            _tables(providers={"codex": {"driver": "codex", "compact_window": 1.5}}),
            "[providers.codex].compact_window must be a positive integer",
        ),
        (
            _tables(loop={"auto": "sometimes"}),
            "[loop].auto must be 'ask', 'always' or 'never'",
        ),
        (
            _tables(providers={"codex": {"driver": "codex", "thinking": "medium"}}),
            "provider 'codex' thinking must be 'off' or 'low'",
        ),
        (
            _tables(providers={"codex": {"driver": "codex", "thinking": 1}}),
            "provider 'codex' thinking must be 'off' or 'low'",
        ),
    ],
)
def test_config_knobs_values_and_types_reject_invalid_values(
    tmp_path: Path, tables: dict, message: str
) -> None:
    path = write_config(tmp_path / "invalid.toml", tables)

    with pytest.raises(ConfigError) as exc_info:
        config.load(extra=path)

    assert str(exc_info.value) == message


@pytest.mark.parametrize(
    "key",
    ["parallel_tool_calls", "no_narration", "delegate_test_writer"],
)
@pytest.mark.parametrize("value", [1, "true"])
def test_config_knobs_boolean_values_reject_non_booleans(
    tmp_path: Path, key: str, value
) -> None:
    path = write_config(tmp_path / "invalid-bool.toml", _tables(loop={key: value}))

    with pytest.raises(ConfigError) as exc_info:
        config.load(extra=path)

    assert str(exc_info.value) == f"[loop].{key} must be a boolean"


@pytest.mark.parametrize(
    ("driver", "thinking"),
    [("omp", "off"), ("omp", "low"), ("codex", "low"), ("antigravity", "low")],
)
def test_config_knobs_thinking_support_matrix(
    tmp_path: Path, driver: str, thinking: str
) -> None:
    settings, _ = _load(
        tmp_path,
        providers={"worker": {"driver": driver, "thinking": thinking, "effort": "xhigh"}},
    )

    assert settings.provider("worker").thinking == thinking
    assert settings.provider("worker").effort == "xhigh"


@pytest.mark.parametrize(
    ("driver", "thinking"),
    [("codex", "off"), ("antigravity", "off"), ("claude", "low")],
)
def test_config_knobs_unsupported_thinking_warns_and_uses_native_default(
    tmp_path: Path, monkeypatch, driver: str, thinking: str, capsys
) -> None:
    settings, path = _load(
        tmp_path,
        providers={"worker": {"driver": driver, "thinking": thinking}},
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    expected = (
        f"warning: provider 'worker' driver '{driver}' does not support thinking = "
        f"'{thinking}'; provider default used."
    )

    assert main(["doctor", "--no-probe", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert next(row for row in data["providers"] if row["name"] == "worker")["warning"] == expected

    assert main(["doctor", "--no-probe"]) == 0
    captured = capsys.readouterr()
    assert expected in captured.err
    # The configured value is retained for diagnostics; the unsupported native option is ignored.
    assert settings.provider("worker").thinking == thinking


def test_config_knobs_delegate_test_writer_requires_provider_and_review(
    tmp_path: Path,
) -> None:
    for loop in (
        {"delegate_test_writer": True},
        {
            "delegate_test_writer": True,
            "review_tests": False,
            "test_writer": {"provider": "writer"},
        },
    ):
        path = write_config(tmp_path / "invalid-writer.toml", _tables(
            loop=loop,
            providers={"codex": {"driver": "codex"}, "writer": {"driver": "codex"}},
        ))
        with pytest.raises(ConfigError) as exc_info:
            config.load(extra=path)
        assert str(exc_info.value) == (
            "[loop].delegate_test_writer requires [loop.test_writer].provider and "
            "[loop].review_tests = true"
        )

    valid, _ = _load(
        tmp_path,
        loop={"delegate_test_writer": True, "test_writer": {"provider": "writer"}},
        providers={"codex": {"driver": "codex"}, "writer": {"driver": "codex"}},
        name="valid-writer.toml",
    )
    assert valid.loop.delegate_test_writer is True
    assert valid.loop.test_writer.provider == "writer"
