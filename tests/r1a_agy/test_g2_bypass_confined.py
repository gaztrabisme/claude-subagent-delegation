"""G2 acceptance test: a BypassSandbox child still runs under the inherited outer profile."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ._common import (
    SEATBELT,
    can_nest_sandbox,
    capture_spawn,
    fake_binary,
    make_agy_settings,
    make_home,
)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt runtime check")
def test_g2_real_profile_confines_a_bypassing_child(tmp_path: Path, monkeypatch) -> None:
    """G2: under the real antigravity profile a child cannot write outside, read ~/.ssh or reach the internet."""
    home = make_home(tmp_path, monkeypatch)
    ws, _, settings = make_agy_settings(tmp_path, fake_binary(tmp_path))
    argv, env = capture_spawn(monkeypatch, settings, ws, "agy-g2")
    assert argv[:2] == [SEATBELT, "-p"], argv[:3]
    profile = argv[2]
    proxy_port = int(env["HTTPS_PROXY"].rsplit(":", 1)[1].rstrip("/"))
    if not can_nest_sandbox():
        return  # already sandboxed: the profile text checks live in the G1 tests

    outside = tmp_path / "outside-marker.txt"
    probe = (
        "import errno, pathlib, socket, sys\n"
        f"ws = pathlib.Path({str(ws)!r}); outside = pathlib.Path({str(outside)!r})\n"
        f"ssh = pathlib.Path({str(home / '.ssh' / 'token')!r})\n"
        f"gem = pathlib.Path({str(home / '.gemini' / 'antigravity-cli' / 'token')!r})\n"
        f"proxy_port = {proxy_port}\n"
        "(ws / 'inside.txt').write_text('ok')\n"            # workspace stays writable
        "gem.read_text()\n"                                  # agy's own state stays readable
        "try:\n    outside.write_text('x'); sys.exit(10)\nexcept PermissionError:\n    pass\n"
        "try:\n    ssh.read_text(); sys.exit(11)\nexcept PermissionError:\n    pass\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "try:\n    s.connect(('192.0.2.1', 80)); sys.exit(12)\n"
        "except OSError as exc:\n"
        "    if exc.errno not in (errno.EPERM, errno.EACCES): sys.exit(13)\n"
        "finally:\n    s.close()\n"
        "p = socket.socket(); p.settimeout(2); p.connect(('127.0.0.1', proxy_port)); p.close()\n"
        "sys.exit(0)\n"
    )
    proc = subprocess.run(
        [SEATBELT, "-p", profile, sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=15, cwd=ws,
    )
    assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr!r}"
    assert (ws / "inside.txt").exists() and not outside.exists()
