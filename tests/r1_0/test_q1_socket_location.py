"""Q1 acceptance tests: approval socket location, permissions, and validation."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

import anyio
import pytest

from subagent import config
from subagent.config import ConfigError
from subagent.guard.supervisor import Supervisor

from ._helpers import REPO_ROOT


def _serve_once(supervisor: Supervisor) -> tuple[int, int]:
    """Start `Supervisor.serve`, inspect socket & parent modes, and shut down cleanly."""
    observed: dict[str, int] = {}

    async def _run() -> None:
        async with anyio.create_task_group() as tg:
            await tg.start(supervisor.serve)
            sock = Path(supervisor.socket_path)
            observed["sock_mode"] = stat.S_IMODE(sock.stat().st_mode)
            observed["parent_mode"] = stat.S_IMODE(sock.parent.stat().st_mode)
            tg.cancel_scope.cancel()

    try:
        anyio.run(_run)
    finally:
        supervisor.cleanup()
    return observed["parent_mode"], observed["sock_mode"]


def test_q1_default_socket_under_session_root_with_secure_perms(tmp_path: Path) -> None:
    """Q1: default socket is under session_root (never temp dirs), parent 0700, socket 0600."""
    session_root = REPO_ROOT / f".q{os.getpid() % 1000}"
    shutil.rmtree(session_root, ignore_errors=True)
    session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg_path = tmp_path / "q1-default.toml"
    cfg_path.write_text(
        f'[core]\nsession_root = "{session_root}"\n',
        encoding="utf-8",
    )
    try:
        settings = config.load(extra=cfg_path)
        sock = Path(settings.approval_socket)
        assert sock.resolve().is_relative_to(settings.session_root.resolve()), (
            f"default approval_socket {sock} must live under session_root {settings.session_root}"
        )
        forbidden_roots = (
            Path(tempfile.gettempdir()),
            Path("/tmp"),
            Path("/private/tmp"),
            Path("/var/folders"),
            Path("/private/var/folders"),
        )
        for root in forbidden_roots:
            for candidate in (sock, sock.resolve()):
                assert not candidate.is_relative_to(root), (
                    f"default approval_socket {candidate} must not be under {root}"
                )

        supervisor = Supervisor(settings)
        parent_mode, sock_mode = _serve_once(supervisor)
        assert not sock.parent.is_symlink()
        assert sock.parent.stat().st_uid == os.getuid()
        assert parent_mode == 0o700
        assert sock_mode == 0o600
    finally:
        shutil.rmtree(session_root, ignore_errors=True)


def test_q1_too_long_session_root_raises_config_error_without_temp_fallback(
    tmp_path: Path,
) -> None:
    """Q1: a too-long session_root raises ConfigError instead of falling back to /tmp."""
    long_session_root = tmp_path / ("s" * 100)
    cfg_path = tmp_path / "q1-long.toml"
    cfg_path.write_text(
        f'[core]\nsession_root = "{long_session_root}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        config.load(extra=cfg_path)


def test_q1_configured_socket_refuses_0777_parent_dir(tmp_path_factory) -> None:
    """Q1: a pre-existing socket parent directory with mode 0777 is refused."""
    base = tmp_path_factory.mktemp("q1m")
    sock_dir = base / "d"
    sock_dir.mkdir(mode=0o777)
    sock_dir.chmod(0o777)
    assert stat.S_IMODE(sock_dir.stat().st_mode) == 0o777

    cfg_path = base / "q1-777.toml"
    cfg_path.write_text(
        f'[core]\nsession_root = "{base / "s"}"\n'
        f'[guard]\napproval_socket = "{sock_dir / "a.sock"}"\n',
        encoding="utf-8",
    )
    with pytest.raises((ConfigError, OSError, RuntimeError, ValueError)):
        settings = config.load(extra=cfg_path)
        _serve_once(Supervisor(settings))


def test_q1_configured_socket_refuses_symlink_parent_dir(tmp_path_factory) -> None:
    """Q1: a pre-existing socket parent directory that is a symlink is refused."""
    base = tmp_path_factory.mktemp("q1l")
    real_dir = base / "r"
    real_dir.mkdir(mode=0o700)
    real_dir.chmod(0o700)
    link_dir = base / "l"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    cfg_path = base / "q1-sym.toml"
    cfg_path.write_text(
        f'[core]\nsession_root = "{base / "s"}"\n'
        f'[guard]\napproval_socket = "{link_dir / "a.sock"}"\n',
        encoding="utf-8",
    )
    with pytest.raises((ConfigError, OSError, RuntimeError, ValueError)):
        settings = config.load(extra=cfg_path)
        _serve_once(Supervisor(settings))


def test_q1_configured_socket_missing_parent_created_with_0700(tmp_path_factory) -> None:
    """Q1: a configured approval_socket creates a missing parent dir with mode 0700."""
    base = tmp_path_factory.mktemp("q1c")
    sock_dir = base / "d"
    sock_path = sock_dir / "a.sock"
    cfg_path = base / "q1-create.toml"
    cfg_path.write_text(
        f'[core]\nsession_root = "{base / "s"}"\n'
        f'[guard]\napproval_socket = "{sock_path}"\n',
        encoding="utf-8",
    )
    old_umask = os.umask(0o022)
    try:
        settings = config.load(extra=cfg_path)
        parent_mode, sock_mode = _serve_once(Supervisor(settings))
        assert parent_mode == 0o700
        assert sock_mode == 0o600
    finally:
        os.umask(old_umask)
