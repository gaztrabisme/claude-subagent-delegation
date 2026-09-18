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
import re
import shlex
import socket
import sys

TIMEOUT_S = float(os.environ.get("SAM_HOOK_TIMEOUT", "150"))

# Codex (0.153.4) sends the same stdin keys as Claude Code (tool_name,
# tool_input, cwd) plus turn_id, but rejects `permissionDecision: allow` as
# unsupported: it allows on exit 0 with no output. Its tool names differ too.
CODEX_SHELL_TOOLS = frozenset({
    "exec_command", "shell", "shell_command", "local_shell", "container.exec", "unified_exec",
})
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)


def agent_id() -> str:
    """The agent this hook belongs to, from `--agent <id>` on its command line."""
    argv = sys.argv[1:]
    if "--agent" in argv:
        index = argv.index("--agent")
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def option(name: str) -> str:
    """The value after `name` on this hook's command line, or ""."""
    argv = sys.argv[1:]
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def is_codex(payload: dict) -> bool:
    """Codex installs this hook with `--dialect codex`; its payload also has turn_id."""
    return option("--dialect") == "codex" or "turn_id" in payload


def requests(tool_name: str, tool_input, codex: bool) -> list[tuple[str, object]]:
    """The (tool_name, tool_input) pairs to ask the supervisor about.

    A Claude Code call is asked about as is. A Codex shell call becomes Bash
    with a string command; an apply_patch becomes one Edit per file it touches.
    """
    if not codex:
        return [(tool_name, tool_input)]
    if tool_name in CODEX_SHELL_TOOLS:
        command = tool_input.get("command") if isinstance(tool_input, dict) else tool_input
        if command is None and isinstance(tool_input, dict):
            command = tool_input.get("cmd")
        if isinstance(command, list):
            command = shlex.join(str(part) for part in command)
        return [("Bash", {"command": command} if command is not None else {})]
    if tool_name == "apply_patch":
        patch = tool_input
        if isinstance(tool_input, dict):
            patch = tool_input.get("command") or tool_input.get("input") or tool_input.get("patch")
        paths = _PATCH_PATH.findall(patch) if isinstance(patch, str) else []
        if paths:
            return [("Edit", {"file_path": path}) for path in dict.fromkeys(paths)]
    return [(tool_name, tool_input)]


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


def ask(socket_path: str, request: dict) -> dict:
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
    if not isinstance(reply, dict):
        raise ValueError("supervisor reply is not an object")
    return reply


def main() -> None:
    socket_path = os.environ.get("SAM_APPROVAL_SOCKET")
    if not socket_path:
        block("blocked: SAM_APPROVAL_SOCKET is not set, so no supervisor can be reached")

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError) as exc:
        block(f"blocked: hook could not read its input ({exc})")

    if not isinstance(payload, dict):
        block("blocked: hook input is not a JSON object")
    codex = is_codex(payload)
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    workspace = payload.get("cwd") or os.getcwd()

    reason = "allowed"
    for name, args in requests(tool_name, tool_input, codex):
        request = {
            "tool_name": name,
            "tool_input": args,
            "workspace": workspace,
            "agent_id": agent_id(),
        }
        try:
            reply = ask(socket_path, request)
        except (OSError, ValueError) as exc:
            block(f"blocked: supervisor unreachable ({type(exc).__name__}: {exc})")
        reason = reply.get("reason") or "blocked by the supervisor"
        if reply.get("action") != "allow":
            block(reason)
    if codex:
        # Exit 0 with no output is Codex's allow; it rejects an explicit one.
        sys.exit(0)
    emit("allow", reason, code=0)


if __name__ == "__main__":
    main()
