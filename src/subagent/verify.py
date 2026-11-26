"""Run the caller's acceptance command and decide whether the work is done.

`finish_reason == 'completed'` means the child's turn ended cleanly. It does not
mean the task is right, and an agent declaring victory prematurely is a primary
failure mode of long-running harnesses. So the caller states the command that
proves the work, and this module runs it -- the server, not the child. A child
reporting its own test results is a claim; an exit code is a fact.

The command is classified through the same `guard` policy the child's own calls
go through before it executes. The caller is another agent and can be prompt
injected, so "the caller asked for it" is not authorization on its own. Only a
command the classifier positively ALLOWs runs; escalate and deny both block.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sandbox
from .config import APPROVAL_HOOK, Settings, log
from .guard.classify import (
    ALLOW,
    _git_metadata_roots,
    classify_verification,
    provider_guard_context,
)

OUTPUT_TAIL_CHARS = 2000


@dataclass
class VerificationResult:
    command: str
    passed: bool
    reason: str
    exit_code: int | None = None
    output: str = ""
    counts: dict[str, int] | None = None
    duration_seconds: float = 0.0
    # True only when the macOS Seatbelt profile was actually applied.
    sandboxed: bool = False

    @property
    def output_tail(self) -> str:
        return (
            self.output[-OUTPUT_TAIL_CHARS:]
            if len(self.output) > OUTPUT_TAIL_CHARS
            else self.output
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "command": self.command,
            "passed": self.passed,
            "reason": self.reason,
            "duration_seconds": round(self.duration_seconds, 1),
            "sandboxed": self.sandboxed,
        }
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.counts is not None:
            out["counts"] = self.counts
        if self.output_tail:
            out["output_tail"] = self.output_tail
        return out


def run_verification(
    command: str, workspace: Path, settings: Settings, budget: float | None = None
) -> VerificationResult:
    """Classify, then execute, then report. Never raises.

    `budget` is the seconds left on the run's own deadline. Without it a slow
    command can still be running when the reaper decides the run is overdue,
    and the reaper's kill does not reach a subprocess this thread is blocked in
    -- so the run would outlive its own agent. The shorter of the two wins.
    """
    timeout = settings.verify_timeout
    if budget is not None:
        timeout = max(0.0, min(timeout, budget))
    command = (command or "").strip()
    if not command:
        return VerificationResult(command, False, "no verification command was given")

    try:
        guard_context = provider_guard_context(settings)
    except Exception as exc:  # noqa: BLE001 - missing protections must block execution
        log.warning("verification blocked because guard context could not be built", exc_info=True)
        return VerificationResult(
            command,
            False,
            f"blocked before execution: guard context unavailable ({type(exc).__name__})",
        )
    verdict = classify_verification(command, workspace, context=guard_context)
    if verdict.action != ALLOW:
        log.warning("verification blocked (%s): %s", verdict.action, verdict.reason)
        return VerificationResult(
            command, False, f"blocked before execution: {verdict.reason}"
        )

    started = time.time()
    # The child runs on the runtime allowlist, never on the server's whole
    # environment: a verification shell is another child and sees no secrets.
    env = settings.base_child_env()
    shared_temp = Path(tempfile.gettempdir())
    try:
        # A caller may point TMPDIR at a fresh directory for one call. Such a
        # root holds no other session's files; keep it writable only while it
        # was empty before this call, so a command that writes to its
        # configured TMPDIR keeps working. The child's TMPDIR is still the
        # private directory below.
        legacy_temp = shared_temp if _is_empty_directory(shared_temp) else None
        # F3: one private temp root per call, under the shared temp. The child's
        # TMPDIR points at it and nothing else in the shared temp is writable.
        private_temp = Path(tempfile.mkdtemp(prefix="subagent-verify-"))
    except OSError as exc:
        return VerificationResult(
            command,
            False,
            f"could not create a private temp directory: {type(exc).__name__}: {exc}",
            duration_seconds=time.time() - started,
        )
    try:
        env["TMPDIR"] = str(private_temp)
        sandboxed = False
        argv: list[str] = ["/bin/sh", "-c", command]
        if sys.platform == "darwin":
            try:
                argv = sandbox.wrap_argv(
                    argv,
                    _verification_read_deny(settings),
                    write_allow=_verification_write_roots(
                        workspace, private_temp, legacy_temp
                    ),
                    write_deny=_verification_write_deny(workspace),
                    read_deny_subpaths=_verification_read_deny_subpaths(settings),
                    write_allow_devices=True,
                    # A verification command may start a loopback server on an
                    # ephemeral port; non-loopback stays denied. Another agent's
                    # live egress proxy is a loopback route to the network, so
                    # each of those ports is denied after the loopback allow.
                    network_loopback_ports=[],
                    network_loopback_any=True,
                    network_deny_ports=sorted(sandbox.live_egress_ports()),
                    deny_ipc_escapes=True,
                )
            except (OSError, ValueError) as exc:
                return VerificationResult(
                    command,
                    False,
                    f"could not apply the macOS verification sandbox: "
                    f"{type(exc).__name__}: {exc}",
                    duration_seconds=time.time() - started,
                )
            sandboxed = argv[:1] == [sandbox.SANDBOX_EXEC]
        try:
            # Executed through a shell because an acceptance command is written
            # as a shell line; every segment of it was classified above. Its own
            # session keeps the process tree killable on timeout.
            proc = subprocess.Popen(  # noqa: S603 - classified shell path, no shell=True
                argv,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return VerificationResult(
                command,
                False,
                f"could not run: {type(exc).__name__}: {exc}",
                duration_seconds=time.time() - started,
                sandboxed=sandboxed,
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            stdout, stderr = "", ""
            with suppress(subprocess.TimeoutExpired):
                stdout, stderr = proc.communicate(timeout=5)
            limit = (
                f"[core].verify_timeout ({settings.verify_timeout:g}s)"
                if timeout >= settings.verify_timeout
                else f"the run's remaining deadline ({timeout:g}s)"
            )
            return VerificationResult(
                command,
                False,
                f"timed out after {limit}",
                duration_seconds=time.time() - started,
                output=((stdout or "") + (stderr or "")).strip(),
                sandboxed=sandboxed,
            )

        duration = time.time() - started
        output = ((stdout or "") + (stderr or "")).strip()
        from .loop.detect import parse_test_counts

        counts = parse_test_counts(output)
        if proc.returncode == 0:
            return VerificationResult(
                command, True, "exit 0", 0, output, counts, duration, sandboxed=sandboxed
            )
        return VerificationResult(
            command,
            False,
            f"exited {proc.returncode}",
            proc.returncode,
            output,
            counts,
            duration,
            sandboxed=sandboxed,
        )
    finally:
        # Every return path above (success, timeout, spawn error, sandbox error)
        # passes through here. A failed removal must not mask the result.
        shutil.rmtree(private_temp, ignore_errors=True)


def _verification_read_deny(settings: Settings) -> list[str]:
    """Union of every configured provider's deny_read (F4)."""
    paths = [
        str(path)
        for cfg in settings.providers.values()
        for path in cfg.deny_read
    ]
    return list(dict.fromkeys(paths))


def _verification_read_deny_subpaths(settings: Settings) -> list[str]:
    """Other agents' session files and the approval socket's directory (F4)."""
    paths = [str(Path(settings.session_root) / "agents")]
    if settings.approval_socket:
        socket_dir = Path(settings.approval_socket).expanduser().parent
        if socket_dir != Path("/"):
            paths.append(str(socket_dir))
    return list(dict.fromkeys(paths))


def _verification_write_deny(workspace: Path) -> list[str]:
    """The workspace's git state, as the child's own profile denies it (F2)."""
    workspace = Path(workspace)
    return [
        str(workspace / ".git"),
        *(str(root) for root in sorted(_git_metadata_roots(workspace))),
        str(APPROVAL_HOOK),
    ]


def _is_empty_directory(path: Path) -> bool:
    """True only for an existing directory with no entries at all."""
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def _verification_write_roots(
    workspace: Path, private_temp: Path, legacy_temp: Path | None = None
) -> list[str]:
    """The workspace and the call's private temp dir; nothing else is writable.

    `legacy_temp` is a caller-supplied shared temp root that was empty before
    this call. The child's `TMPDIR` still points at `private_temp`; the extra
    root only keeps a command that writes to its configured TMPDIR working.
    """
    roots = [str(Path(workspace).resolve()), str(private_temp.resolve())]
    if legacy_temp is not None:
        roots.append(str(legacy_temp.resolve()))
    return list(dict.fromkeys(roots))


def _descendant_pids(root_pid: int) -> set[int]:
    """Every descendant of `root_pid`, read before the parent is killed.

    A grandchild may call setsid itself: it leaves the parent's process group
    but keeps its parent id. Ask the OS directly where it exposes the tree;
    `ps` is only the fallback, because a sandboxed verification parent cannot
    always execute it.
    """
    if Path("/proc").is_dir():
        return _children_map(root_pid, _proc_children())
    libproc = _libproc_tree(root_pid)
    if libproc is not None:
        return libproc
    return _children_map(root_pid, _ps_children() or {})


def _children_map(root_pid: int, children: dict[int, list[int]]) -> set[int]:
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


def _proc_children() -> dict[int, list[int]]:
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


def _libproc_tree(root_pid: int) -> set[int] | None:
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


def _ps_children() -> dict[int, list[int]] | None:
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


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the command's process group and every descendant it has now."""
    descendants = _descendant_pids(proc.pid)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    for pid in {proc.pid, *descendants}:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
