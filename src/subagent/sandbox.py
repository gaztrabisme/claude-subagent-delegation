"""Optional macOS Seatbelt restrictions inherited by a child and its descendants."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


def normalize_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Freeze canonical paths, rejecting values SBPL cannot safely represent."""
    resolved = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("deny_read paths must be nonempty strings")
        if "\x00" in path:
            raise ValueError("deny_read path contains an unsupported control character")
        path = str(Path(path).expanduser().resolve())
        if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in path):
            raise ValueError("deny_read path contains an unsupported control character")
        resolved.append(path)
    return tuple(dict.fromkeys(resolved))


def build_profile(paths: Sequence[str]) -> str:
    """Deny reading and changing each realpath, including its descendants."""
    rules = ["(version 1)", "(allow default)"]
    for path in normalize_paths(paths):
        escaped = (path.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        for operation in ("file-read*", "file-write*"):
            rules.append(f'(deny {operation} (subpath "{escaped}"))')
    return "\n".join(rules)


def check_platform(paths: Sequence[str]) -> None:
    if paths and sys.platform != "darwin":
        raise RuntimeError("deny_read requires macOS sandbox-exec; refusing an unsandboxed child")


def wrap_argv(argv: list[str], paths: Sequence[str]) -> list[str]:
    """Pass arguments directly to sandbox-exec; never fall back on sandbox failure."""
    if not paths:
        return argv
    check_platform(paths)
    return ["/usr/bin/sandbox-exec", "-p", build_profile(paths), *argv]
