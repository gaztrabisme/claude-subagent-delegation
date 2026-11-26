"""B2 acceptance tests: verification runs under Seatbelt on macOS and reports sandboxed."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

from subagent.verify import run_verification
from tests.conftest import make_settings

_NET_NON_LOOPBACK_SCRIPT = """\
import contextlib
import errno
import json
import socket
from pathlib import Path

outcome = {}
try:
    with socket.create_connection(("192.0.2.1", 80), timeout=3.0):
        outcome = {"status": "connected"}
except PermissionError as exc:
    outcome = {"status": "permission_error", "type": type(exc).__name__, "errno": exc.errno}
except TimeoutError as exc:
    outcome = {"status": "timeout", "type": type(exc).__name__}
except OSError as exc:
    if exc.errno in (errno.EPERM, errno.EACCES):
        outcome = {"status": "permission_error", "type": type(exc).__name__, "errno": exc.errno}
    else:
        outcome = {"status": "other_oserror", "type": type(exc).__name__, "errno": exc.errno, "msg": str(exc)}

Path("net_result.json").write_text(json.dumps(outcome), encoding="utf-8")
raise SystemExit(0 if outcome.get("status") == "permission_error" else 1)
"""

_LOOPBACK_SCRIPT = """\
import socket
import threading

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]

def serve():
    conn, _ = srv.accept()
    with conn:
        data = conn.recv(16)
        conn.sendall(data.upper())

t = threading.Thread(target=serve, daemon=True)
t.start()
with socket.create_connection(("127.0.0.1", port), timeout=3.0) as cli:
    cli.sendall(b"ping")
    reply = cli.recv(16)
t.join(timeout=2.0)
srv.close()
assert reply == b"PING", reply
"""

_WRITE_CONFINEMENT_SCRIPT = """\
import json
from pathlib import Path

targets = json.loads(Path("targets.json").read_text(encoding="utf-8"))
ws = Path.cwd()
allowed_tmp = Path(targets["allowed_tmp"])
outside_dir = Path(targets["outside_dir"])

(ws / "inside.txt").write_text("ok", encoding="utf-8")
import os
own_tmp = Path(os.environ["TMPDIR"])
(own_tmp / "temp.txt").write_text("ok", encoding="utf-8")
Path("own_tmp.txt").write_text(str(own_tmp), encoding="utf-8")

outside_blocked = False
outside_error = None
try:
    (outside_dir / "escape.txt").write_text("escaped", encoding="utf-8")
except PermissionError as exc:
    outside_blocked = True
    outside_error = f"{type(exc).__name__}:{exc.errno}"
except OSError as exc:
    outside_error = f"{type(exc).__name__}:{exc.errno}"

Path("write_result.json").write_text(
    json.dumps({"outside_blocked": outside_blocked, "outside_error": outside_error}),
    encoding="utf-8",
)
raise SystemExit(0 if outside_blocked else 1)
"""

_PROCESS_TREE_SCRIPT = """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    start_new_session=True,
)
tmp = Path("tree.json.tmp")
tmp.write_text(
    json.dumps({"pid": os.getpid(), "sid": os.getsid(0), "child_pid": child.pid}),
    encoding="utf-8",
)
os.replace(tmp, "tree.json")
time.sleep(30)
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_b2_macos_denies_non_loopback_network_with_permission_error(tmp_path: Path) -> None:
    """B2.1: on macOS, verification denies non-loopback network (192.0.2.1:80) with PermissionError, not timeout."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "net_check.py").write_text(_NET_NON_LOOPBACK_SCRIPT, encoding="utf-8")

    settings = make_settings(tmp_path, verify_timeout=10.0)
    result = run_verification("python3 net_check.py", ws, settings)

    outcome = json.loads((ws / "net_result.json").read_text(encoding="utf-8"))
    assert outcome.get("status") == "permission_error", f"expected permission error, got {outcome}"
    assert outcome.get("errno") in (errno.EPERM, errno.EACCES)
    assert result.passed is True
    assert result.sandboxed is True


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_b2_macos_allows_loopback_server_and_sets_sandboxed(tmp_path: Path) -> None:
    """B2.2: on macOS, verification allows connecting to a loopback server it started and sets sandboxed=True."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "loopback_check.py").write_text(_LOOPBACK_SCRIPT, encoding="utf-8")

    settings = make_settings(tmp_path, verify_timeout=10.0)
    result = run_verification("python3 loopback_check.py", ws, settings)

    assert result.passed is True, result.reason
    assert result.sandboxed is True


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_b2_macos_allows_workspace_and_temp_writes_but_denies_outside(
    tmp_path: Path, monkeypatch
) -> None:
    """B2.3: on macOS, verification write-allows workspace and temp dir and denies writes outside."""
    ws = tmp_path / "ws"
    allowed_tmp = tmp_path / "sandbox_tmp"
    outside_dir = tmp_path / "outside_dir"
    ws.mkdir()
    allowed_tmp.mkdir()
    outside_dir.mkdir()

    monkeypatch.setenv("TMPDIR", str(allowed_tmp))
    monkeypatch.setattr(tempfile, "tempdir", None)

    (ws / "targets.json").write_text(
        json.dumps({"allowed_tmp": str(allowed_tmp), "outside_dir": str(outside_dir)}),
        encoding="utf-8",
    )
    (ws / "write_check.py").write_text(_WRITE_CONFINEMENT_SCRIPT, encoding="utf-8")
    settings = make_settings(tmp_path, verify_timeout=10.0)
    result = run_verification("python3 write_check.py", ws, settings)

    assert result.passed is True, result.reason
    assert result.sandboxed is True
    assert (ws / "inside.txt").read_text(encoding="utf-8") == "ok"
    own_tmp = Path((ws / "own_tmp.txt").read_text(encoding="utf-8"))
    assert own_tmp.resolve() != allowed_tmp.resolve()
    assert not (allowed_tmp / "temp.txt").exists()
    assert not (outside_dir / "escape.txt").exists()


def test_b2_runs_in_new_session_and_kills_process_tree_on_timeout(tmp_path: Path) -> None:
    """B2.4: verification runs in its own session (start_new_session=True) and kills its process tree on timeout."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "tree_sleep.py").write_text(_PROCESS_TREE_SCRIPT, encoding="utf-8")

    settings = make_settings(tmp_path, verify_timeout=1.5)
    pids_to_reap: list[int] = []
    try:
        result = run_verification("python3 tree_sleep.py", ws, settings)
        assert result.passed is False
        assert "timed out" in result.reason

        tree_file = ws / "tree.json"
        assert tree_file.is_file(), "verification script did not start before timeout"
        data = json.loads(tree_file.read_text(encoding="utf-8"))
        pids_to_reap.extend([data["pid"], data["child_pid"]])

        assert data["sid"] != os.getsid(os.getpid()), "expected verification to run in a new session"

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(_pid_alive(p) for p in pids_to_reap):
            time.sleep(0.05)

        survivors = [p for p in pids_to_reap if _pid_alive(p)]
        assert not survivors, f"process tree survived timeout: {survivors}"
    finally:
        for pid in pids_to_reap:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_b2_off_macos_runs_unsandboxed_and_reports_sandboxed_false(
    tmp_path: Path, monkeypatch
) -> None:
    """B2.5: off macOS, verification runs without sandbox-exec and VerificationResult.sandboxed is False."""
    monkeypatch.setattr(sys, "platform", "linux")

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.py").write_text("print('ok')\n", encoding="utf-8")

    settings = make_settings(tmp_path, verify_timeout=10.0)
    result = run_verification("python3 ok.py", ws, settings)

    assert result.passed is True, result.reason
    assert result.sandboxed is False
