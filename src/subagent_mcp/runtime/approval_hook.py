#!/usr/bin/env python3
"""PreToolUse hook: ask the supervisor before the child runs a tool.

Claude Code invokes this as a local process with the proposed tool call as JSON
on stdin. It asks the MCP server over a unix socket and always emits a
PreToolUse decision on stdout. Exit 0 with empty stdout would allow the call
under bypassPermissions, so silence is not a valid response.

    permissionDecision allow -- run the tool
    permissionDecision deny  -- block; reason is shown to the model

Anything unexpected denies. A hook that cannot reach its supervisor must deny,
never wave the call through.

Standard library only: this runs once per tool call and must stay cheap.
"""

from __future__ import annotations

import json
import os
import socket
import sys

TIMEOUT_S = float(os.environ.get("SAM_HOOK_TIMEOUT", "150"))


def agent_id() -> str:
    """The agent this hook belongs to, from `--agent <id>` on its command line."""
    argv = sys.argv[1:]
    if "--agent" in argv:
        index = argv.index("--agent")
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def emit(decision: str, reason: str, *, code: int) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()
    if decision != "allow":
        sys.stderr.write(reason.rstrip() + "\n")
    sys.exit(code)


def block(reason: str) -> None:
    emit("deny", reason, code=2)


def main() -> None:
    socket_path = os.environ.get("SAM_APPROVAL_SOCKET")
    if not socket_path:
        block("blocked: SAM_APPROVAL_SOCKET is not set, so no supervisor can be reached")

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError) as exc:
        block(f"blocked: hook could not read its input ({exc})")

    request = {
        "tool_name": payload.get("tool_name") or payload.get("toolName") or "",
        "tool_input": payload.get("tool_input") or payload.get("toolInput") or {},
        "workspace": payload.get("cwd") or os.getcwd(),
        "agent_id": agent_id(),
    }

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT_S)
            sock.connect(socket_path)
            sock.sendall((json.dumps(request) + "\n").encode())
            chunks = []
            while b"\n" not in b"".join(chunks):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        reply = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode())
    except (OSError, ValueError) as exc:
        block(f"blocked: supervisor unreachable ({type(exc).__name__}: {exc})")

    reason = reply.get("reason") or "blocked by the supervisor"
    if reply.get("action") == "allow":
        emit("allow", reason, code=0)
    block(reason)


if __name__ == "__main__":
    main()
