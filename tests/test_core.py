from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import Mock

from subagent import config
from subagent.core import InProcessServer

from .conftest import make_settings


def test_in_process_delegate_passes_deny_read_to_registry(tmp_path: Path):
    server = InProcessServer(tmp_path / "sessions", make_settings(tmp_path))
    server.registry = Mock()
    agent = server.registry.create_agent.return_value
    agent.wait_ready.return_value = None
    paths = [str(tmp_path / "private")]

    run = server.delegate(
        provider="glm", task="task", verification="true", workspace=tmp_path,
        name="test", deny_read=paths,
    )

    server.registry.create_agent.assert_called_once_with(
        "test", tmp_path.resolve(), provider="glm", fallback="none", deny_read=paths,
    )
    agent.delegate.assert_called_once_with("task", "true")
    assert run is agent.delegate.return_value


def test_in_process_server_uses_short_socket_for_long_session_root(
    tmp_path: Path, monkeypatch
):
    short_socket_root = Path(__file__).resolve().parents[1] / ".t"
    shutil.rmtree(short_socket_root, ignore_errors=True)
    monkeypatch.setattr(config, "SHORT_SOCKET_ROOT", short_socket_root)
    session_root = tmp_path / ("sessions-" + "x" * 80)
    server = None
    try:
        settings = make_settings(tmp_path)
        server = InProcessServer(session_root, settings)

        assert len(os.fsencode(session_root / "approval.sock")) >= config.UNIX_SOCKET_PATH_MAX
        assert len(os.fsencode(server.settings.approval_socket)) < config.UNIX_SOCKET_PATH_MAX

        server.start()
    finally:
        if server is not None:
            server.stop()
        shutil.rmtree(short_socket_root, ignore_errors=True)
    assert not server._thread.is_alive()
