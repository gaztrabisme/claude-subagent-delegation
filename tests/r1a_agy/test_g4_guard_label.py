"""G4 acceptance tests: the antigravity guard label and doctor output are honest."""

from __future__ import annotations

import json

from subagent import cli, config, sandbox
from subagent.providers import for_driver

from ._common import PROVIDER, fake_binary, make_agy_settings, make_home


def test_g4_guard_reports_sandbox_on_macos(tmp_path, monkeypatch) -> None:
    """G4: AntigravityProvider.guard() is "sandbox" (not "none") on macOS."""
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    make_home(tmp_path, monkeypatch)
    _, _, settings = make_agy_settings(tmp_path, fake_binary(tmp_path))
    assert for_driver("antigravity").guard(settings, settings.provider(PROVIDER)) == "sandbox"


def test_g4_doctor_shows_server_profile_as_boundary(tmp_path, monkeypatch, capsys) -> None:
    """G4: doctor's antigravity row reports the server profile as its boundary, not agy's own sandbox."""
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    make_home(tmp_path, monkeypatch)
    binary = fake_binary(tmp_path)
    cfg_path = tmp_path / "doctor.toml"
    cfg_path.write_text(
        '[providers.gemini]\ndriver = "antigravity"\nvendor = "gemini"\n'
        f'binary = "{binary}"\nexperimental = true\n'
        'network_allow = ["generativelanguage.googleapis.com"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg_path))
    assert cli.main(["doctor", "--no-probe", "--json"]) == 0
    row = {r["name"]: r for r in json.loads(capsys.readouterr().out)["providers"]}[PROVIDER]
    skip = {"name", "binary", "binary_path", "binary_ok", "driver", "vendor"}
    values: list[str] = []

    def walk(v):
        if isinstance(v, str):
            values.append(v.lower())
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk({k: v for k, v in row.items() if k not in skip})
    assert any(v == "sandbox" or "server profile" in v or "sandbox-exec" in v for v in values), row
