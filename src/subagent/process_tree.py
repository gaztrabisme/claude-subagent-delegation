"""Read a process tree without spawning a helper where the OS exposes it.

Both the verification kill path and the egress proxy's pid allow-list need the
live descendants of one pid. On macOS libproc's direct-children API is the
cheap and reliable source; Linux exposes procfs; `ps` is only the last resort,
because a sandboxed process cannot always execute it.
"""

from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path


def descendant_pids(root_pid: int) -> set[int]:
    """Every descendant of `root_pid`, read before the parent is killed.

    A grandchild may call setsid itself: it leaves the parent's process group
    but keeps its parent id. Ask the OS directly where it exposes the tree;
    `ps` is only the fallback, because a sandboxed verification parent cannot
    always execute it.
    """
    if Path("/proc").is_dir():
        return children_map(root_pid, proc_children())
    libproc = libproc_tree(root_pid)
    if libproc is not None:
        return libproc
    return children_map(root_pid, ps_children() or {})


def children_map(root_pid: int, children: dict[int, list[int]]) -> set[int]:
    """Walk a pid -> children map from `root_pid` down."""
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def proc_children() -> dict[int, list[int]]:
    """A pid -> children map from Linux procfs."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    ppid = int(line.split()[1])
                except (IndexError, ValueError):
                    break
                children.setdefault(ppid, []).append(int(entry.name))
                break
    return children


def libproc_tree(root_pid: int) -> set[int] | None:
    """Walk macOS libproc's direct-children API without spawning a process."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        fn = libproc.proc_listchildpids
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    capacity = 1024
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        while True:
            buffer = (ctypes.c_int * capacity)()
            count = fn(parent, buffer, ctypes.sizeof(buffer))
            if count < 0:
                return None
            if count < capacity:
                for index in range(count):
                    child = buffer[index]
                    if child > 0 and child not in found:
                        found.add(child)
                        pending.append(child)
                break
            capacity *= 2
    return found


def ps_children() -> dict[int, list[int]] | None:
    """A pid -> children map from `ps`, when the platform has no better API."""
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    children: dict[int, list[int]] = {}
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children
