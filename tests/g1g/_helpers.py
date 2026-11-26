"""Shared offline helpers for the G1g acceptance tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ..conftest import make_settings
from ..r6._helpers import init_git_repo

DOCTOR_HEAD = [sys.executable, "-m", "subagent", "doctor", "--provider"]


def providers_table() -> dict[str, dict[str, Any]]:
    """Config order: alpha (base_url), nolane (no route), beta (network_allow),
    loc (local, no route), gamma (base_url)."""
    cloud = {"driver": "claude", "model": "m", "api_key_env": "GLM_API_KEY", "vendor": "zai"}
    return {
        "alpha": {**cloud, "base_url": "https://alpha.example/api"},
        "nolane": dict(cloud),
        "beta": {**cloud, "network_allow": ["beta.example:443"]},
        "loc": {**cloud, "local": True, "api_key": "local"},
        "gamma": {**cloud, "base_url": "https://gamma.example/api"},
    }


def routed_table() -> dict[str, dict[str, Any]]:
    table = providers_table()
    del table["nolane"]
    return table


class FakeRunner:
    """Runs real git/tar, answers uv and doctor calls from a table."""

    def __init__(self, settings=None, *, doctor=None, fail_uv: bool = False):
        self.settings = settings
        self.doctor = doctor or {}  # name -> (rc, stdout, stderr) or an exception
        self.fail_uv = fail_uv
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.served_at_probe: list[bool] = []
        self.installed_before_probe: list[bool] = []

    def __call__(self, argv, **kwargs):
        cmd = [str(part) for part in argv]
        self.calls.append(cmd)
        self.kwargs.append(kwargs)
        if Path(cmd[0]).name in ("git", "tar"):
            return subprocess.run(cmd, **kwargs)
        if cmd[:4] == DOCTOR_HEAD[:1] + ["-m", "subagent", "doctor"]:
            name = cmd[cmd.index("--provider") + 1]
            if self.settings is not None:
                self.served_at_probe.append(
                    (Path(self.settings.session_root) / "served.json").exists()
                )
            self.installed_before_probe.append(any(
                "install" in c for c in self.calls if len(c) > 1 and c[1] == "pip"
            ))
            outcome = self.doctor.get(name, (0, "", ""))
            if isinstance(outcome, BaseException):
                raise outcome
            rc, out, err = outcome
            return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)
        rc = 1 if self.fail_uv else 0
        return subprocess.CompletedProcess(
            cmd, rc, stdout="", stderr="install failed" if self.fail_uv else ""
        )

    def doctor_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "doctor" in c and c[:1] == [sys.executable]]

    def probed(self) -> list[str]:
        return [c[c.index("--provider") + 1] for c in self.doctor_calls()]

    def installs(self) -> list[list[str]]:
        return [c for c in self.calls if "pip" in c or "venv" in c]


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    return repo, init_git_repo(repo, {"pkg.txt": "v1\n"})


def settings_for(tmp_path: Path, table=None):
    return make_settings(tmp_path, table if table is not None else routed_table())
