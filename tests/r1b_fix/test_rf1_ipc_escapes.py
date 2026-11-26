"""RF1 (F1): verification denies exec of the macOS IPC escape binaries."""

from __future__ import annotations

import sys

import pytest

from tests.r1b_fix._helpers import HEADER, run_script

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_SCRIPT = HEADER + """
import subprocess
CMDS = {
    "open": ["/usr/bin/open", "-h"],
    "launchctl": ["/bin/launchctl", "version"],
    "osascript": ["/usr/bin/osascript", "-e", "return 1"],
}
for name, argv in CMDS.items():
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=8)
        R[name] = {"rc": p.returncode, "out": (p.stdout or "") + (p.stderr or "")}
    except PermissionError as exc:
        R[name] = {"denied_exc": f"{type(exc).__name__}:{exc.errno}"}
    except OSError as exc:
        R[name] = {"oserror": f"{type(exc).__name__}:{exc.errno}"}
done()
"""


def _denied(entry: dict) -> bool:
    return (
        "denied_exc" in entry
        or entry.get("rc") == 126
        or "Operation not permitted" in entry.get("out", "")
    )


@pytest.mark.parametrize("name", ["open", "launchctl", "osascript"])
def test_rf1_exec_of_ipc_escape_is_refused(tmp_path, name: str) -> None:
    """RF1: under run_verification on macOS, exec of /usr/bin/open, launchctl, osascript is refused."""
    _, result, data = run_script(tmp_path, _SCRIPT)
    assert result.sandboxed is True
    assert name in data, f"probe did not record {name}: {data}"
    assert _denied(data[name]), f"{name} was allowed to run under verification: {data[name]}"
