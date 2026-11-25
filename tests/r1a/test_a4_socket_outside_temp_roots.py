"""A4 acceptance tests: approval socket outside temp roots and workspace refusal."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from subagent import config
from subagent.runs import Registry, RegistryError
from tests.conftest import make_settings

from ._helpers import REPO_ROOT


@pytest.mark.parametrize(
    "temp_base",
    [
        pytest.param(lambda _tmp: Path(tempfile.gettempdir()), id="gettempdir"),
        pytest.param(lambda _tmp: Path("/tmp"), id="slash-tmp"),
    ],
)
def test_a4_temp_session_root_uses_short_socket_root(
    tmp_path: Path, monkeypatch, temp_base
) -> None:
    """A4: when preferred socket resolves under a temp root, config uses SHORT_SOCKET_ROOT (0700)."""
    # The short root must lie outside every temp root, or config rightly refuses it.
    short_root = REPO_ROOT / f".a4t-{os.getpid()}-{temp_base(tmp_path).name}"
    shutil.rmtree(short_root, ignore_errors=True)
    monkeypatch.setattr(config, "SHORT_SOCKET_ROOT", short_root)

    base = temp_base(tmp_path)
    session_root = base / f"sa-a4-{os.getpid()}"
    cfg_path = tmp_path / "a4.toml"
    cfg_path.write_text(f'[core]\nsession_root = "{session_root}"\n', encoding="utf-8")

    try:
        settings = config.load(extra=cfg_path)
        sock = Path(settings.approval_socket)
        assert sock.resolve().is_relative_to(short_root.resolve()), (
            f"expected socket {sock} under {short_root}, got temp-root socket"
        )
        parent_mode = stat.S_IMODE(sock.parent.lstat().st_mode)
        assert parent_mode == 0o700
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


def test_a4_symlink_into_temp_root_uses_short_socket_root(
    tmp_path: Path, monkeypatch
) -> None:
    """A4: a short preferred socket path whose symlink resolves under a temp root falls back to SHORT_SOCKET_ROOT."""
    short_root = REPO_ROOT / f".a4-{os.getpid()}"
    link_parent = REPO_ROOT / f".l4-{os.getpid()}"
    shutil.rmtree(short_root, ignore_errors=True)
    link_parent.unlink(missing_ok=True)
    monkeypatch.setattr(config, "SHORT_SOCKET_ROOT", short_root)

    link_parent.symlink_to("/tmp", target_is_directory=True)
    sub = f"s4-{os.getpid()}"
    preferred = link_parent / sub / "a.sock"
    assert len(os.fsencode(preferred)) < config.UNIX_SOCKET_PATH_MAX
    try:
        resolved_sock = Path(config._approval_socket_path(preferred))
        assert resolved_sock.resolve().is_relative_to(short_root.resolve())
    finally:
        shutil.rmtree(link_parent / sub, ignore_errors=True)
        link_parent.unlink(missing_ok=True)
        shutil.rmtree(short_root, ignore_errors=True)


def test_a4_registry_create_agent_refuses_workspace_containing_socket_dir(tmp_path: Path) -> None:
    """A4: Registry.create_agent refuses an agent whose workspace contains the approval socket directory."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sock_dir = ws / "sockdir"
    sock_dir.mkdir(mode=0o700)
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        approval_socket=str(sock_dir / "approval.sock"),
    )
    registry = Registry(settings, start_reaper=False)
    try:
        with pytest.raises(RegistryError):
            registry.create_agent("a4", ws, provider="glm")
    finally:
        registry.shutdown()
