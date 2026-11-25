"""Optional macOS Seatbelt restrictions inherited by a child and its descendants."""

from __future__ import annotations

import os
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


def _filter(path: str, kind: str) -> str:
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in path):
        raise ValueError("deny_read path contains an unsupported control character")
    escaped = (path.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'({kind} "{escaped}")'


def approval_socket_write_deny(
    socket_path: str | Path, write_allowed_roots: Sequence[str | Path] = (),
) -> str:
    """Deny the socket directory unless that directory contains another writable root."""
    socket = Path(socket_path).expanduser()
    socket_parent = socket.parent.resolve()
    for raw in write_allowed_roots:
        allowed = Path(raw).expanduser().resolve()
        if allowed == socket_parent or allowed.is_relative_to(socket_parent):
            return str(socket)
    return str(socket.parent)


def build_profile(
    deny_paths: Sequence[str], write_allow: Sequence[str] | None = None,
    write_deny: Sequence[str] = (), *,
    read_allow: Sequence[str] = (), read_metadata_allow: Sequence[str] = (),
    write_allow_exact: Sequence[str] = (), write_allow_subpaths: Sequence[str] = (),
) -> str:
    """Restrict writes, deny excluded realpaths, and prevent ancestor renames."""
    rules = ["(version 1)", "(allow default)"]
    if write_allow is not None:
        rules.append('(deny file-write* (subpath "/"))')
        allowed = [_filter(path, "subpath") for path in normalize_paths(write_allow)]
        if allowed:
            rules.append(f'(allow file-write* {" ".join(allowed)})')
    # Later SBPL rules win: exclusions must override even allowed write roots.
    denied = normalize_paths(deny_paths)
    for path in denied:
        for operation in ("file-read*", "file-write*"):
            rules.append(f'(deny {operation} {_filter(path, "subpath")})')
    write_paths: dict[str, None] = {}
    for raw in write_deny:
        resolved = normalize_paths([raw])[0]
        # Protect replacing a symlink as well as writing through its target.
        spelling = os.path.abspath(Path(raw).expanduser())
        write_paths.update(dict.fromkeys((spelling, resolved)))
    for path in write_paths:
        for kind in ("literal", "subpath"):
            rules.append(f'(deny file-write* {_filter(path, kind)})')
    # Narrow exceptions follow subtree denials. Literal filters cannot grant
    # access to siblings or descendants of the exception path.
    for path in normalize_paths(read_allow):
        rules.append(f'(allow file-read* {_filter(path, "literal")})')
    for path in normalize_paths(read_metadata_allow):
        rules.append(f'(allow file-read-metadata {_filter(path, "literal")})')
    for path in normalize_paths(write_allow_exact):
        rules.append(f'(allow file-write* {_filter(path, "literal")})')
    ancestors = dict.fromkeys(
        str(parent) for path in (*denied, *write_paths)
        for parent in Path(path).parents if parent != Path("/")
    )
    for ancestor in ancestors:
        # Literal only: siblings within an allowed ancestor remain writable.
        # Pin write-denied files too, so renaming CODEX_HOME cannot replace hooks.
        rules.append(f'(deny file-write* {_filter(ancestor, "literal")})')
    # A narrow worker scratch area may be nested below a configured socket
    # parent; it cannot cover the socket file or its direct siblings.
    for path in normalize_paths(write_allow_subpaths):
        rules.append(f'(allow file-write* {_filter(path, "subpath")})')
    return "\n".join(rules)


def check_platform(paths: Sequence[str]) -> None:
    if paths and sys.platform != "darwin":
        raise RuntimeError("deny_read requires macOS sandbox-exec; refusing an unsandboxed child")


def wrap_argv(
    argv: list[str], paths: Sequence[str], write_allow: Sequence[str] | None = None,
    write_deny: Sequence[str] = (), *,
    read_allow: Sequence[str] = (), read_metadata_allow: Sequence[str] = (),
    write_allow_exact: Sequence[str] = (), write_allow_subpaths: Sequence[str] = (),
) -> list[str]:
    """Pass arguments directly to sandbox-exec; never fall back on sandbox failure."""
    if not paths:
        return argv
    check_platform(paths)
    return [
        "/usr/bin/sandbox-exec", "-p",
        build_profile(paths, write_allow, write_deny, read_allow=read_allow,
                      read_metadata_allow=read_metadata_allow,
                      write_allow_exact=write_allow_exact,
                      write_allow_subpaths=write_allow_subpaths),
        *argv,
    ]
