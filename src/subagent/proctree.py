"""Stop a child started with start_new_session=True, with its whole process tree.

POSIX signals the child's process group. Windows has no process groups to
signal, so it asks taskkill to end the tree (/T), forcefully (/F) for SIGKILL.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def kill_tree(proc: subprocess.Popen, force: bool = False) -> None:
    """Raises ProcessLookupError when the process is already gone."""
    if sys.platform == "win32":
        if proc.poll() is not None:
            raise ProcessLookupError(proc.pid)
        argv = ["taskkill", "/T", "/PID", str(proc.pid)]
        if force:
            argv.insert(1, "/F")
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if force:
            proc.kill()
        return
    os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
