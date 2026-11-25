"""Optional macOS Seatbelt restrictions inherited by a child and its descendants."""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Character devices an ordinary process opens for writing (shell redirects,
# subprocess DEVNULL, terminal probes, dtrace helpers, /dev/stdout aliases).
# A write-confined profile keeps exactly these writable, not the rest of /dev.
STANDARD_DEVICE_LITERALS = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper")
STANDARD_DEVICE_SUBPATHS = ("/dev/fd",)

# Ways out of a Seatbelt profile that `(allow default)` leaves open: another
# process started on the child's behalf runs outside the profile (and outside
# the run's process tree). An A9 profile denies sending Apple Events, the
# LaunchServices, ServiceManagement and background-download services, and
# running the three system tools that drive them.
IPC_ESCAPE_MACH_SERVICES = (
    "com.apple.coreservices.launchservicesd",  # LaunchServices: open apps, URLs, files.
    "com.apple.lsd.open",  # LaunchServices open requests.
    "com.apple.lsd.modifydb",  # LaunchServices handler registration (persistence).
    "com.apple.xpc.smd",  # ServiceManagement: launchd job and login-item submission.
    "com.apple.nsurlsessiond",  # Background URL sessions run by the daemon, not the child.
    "com.apple.nsurlsessiond.NSURLSessionProxyService",  # The same daemon's proxy service.
)
IPC_ESCAPE_EXECUTABLES = (
    "/bin/launchctl",  # Submits or bootstraps launchd jobs.
    "/usr/bin/open",  # Launches apps and URLs through LaunchServices.
    "/usr/bin/osascript",  # Sends Apple Events to other apps.
)

# R1a round 6 (H1, H4): every A9 profile writes only the workspace, the
# agent's own directory `<session_root>/agents/<agent_id>/` (its CLI homes,
# temp and cache) and the standard devices. A write root anywhere else under
# the owner's home is refused unless it is listed here, per driver, with the
# reason the CLI cannot run without it (proved by a live run, not assumed).
# Each entry is a residual in the round's report. None is needed today: the
# login credentials are copied (claude) or linked/read in place read-only
# (codex auth.json, dsh .credentials.yaml).
OWNER_HOME_EXCEPTIONS: dict[str, list[tuple[str, str]]] = {
    "claude": [],
    "codex": [],
    "omp": [],
    "dsh": [],
}

# The loopback port of the agent's egress proxy while its driver builds argv.
_EGRESS_PORT: ContextVar[int | None] = ContextVar("subagent_egress_port", default=None)


@contextmanager
def egress_port(port: int | None) -> Iterator[None]:
    """Expose the agent's egress proxy port to a driver that builds its own profile."""
    token = _EGRESS_PORT.set(port)
    try:
        yield
    finally:
        _EGRESS_PORT.reset(token)


def loopback_port(base_url: str | None) -> int | None:
    """The port of a loopback `base_url` (a local model server), else None."""
    parsed = urlsplit(base_url or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return port or (443 if parsed.scheme.lower() == "https" else 80)


def network_ports(base_url: str | None) -> list[int]:
    """Loopback TCP ports an agent's child may reach: its proxy and a local base_url."""
    ports = [port for port in (_EGRESS_PORT.get(), loopback_port(base_url)) if port]
    return list(dict.fromkeys(ports))


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
    read_allow_subpaths: Sequence[str] = (),
    network_loopback_ports: Sequence[int] | None = None,
    network_unix_sockets: Sequence[str] = (),
    read_deny_subpaths: Sequence[str] = (),
    write_allow_devices: bool = False,
    deny_ipc_escapes: bool = False,
) -> str:
    """Restrict writes, deny excluded realpaths, and prevent ancestor renames.

    `read_deny_subpaths` denies reads only (writes stay governed by the write
    rules); `read_allow_subpaths` re-allows reads below one of them. A network
    policy (`network_loopback_ports` not None, or unix sockets) denies all
    outbound network except loopback TCP on those ports and those socket paths.
    """
    rules = ["(version 1)", "(allow default)"]
    if write_allow is not None:
        rules.append('(deny file-write* (subpath "/"))')
        allowed = [_filter(path, "subpath") for path in normalize_paths(write_allow)]
        if allowed:
            rules.append(f'(allow file-write* {" ".join(allowed)})')
    if write_allow is not None and write_allow_devices:
        devices = [
            *(_filter(path, "literal") for path in STANDARD_DEVICE_LITERALS),
            *(_filter(path, "subpath") for path in STANDARD_DEVICE_SUBPATHS),
        ]
        rules.append(f'(allow file-write* {" ".join(devices)})')
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
    read_denied = normalize_paths(read_deny_subpaths)
    for path in read_denied:
        rules.append(f'(deny file-read* {_filter(path, "subpath")})')
        # Renaming the denied directory would move its contents out from under
        # the rule; pin the directory itself as well as its ancestors.
        rules.append(f'(deny file-write* {_filter(path, "literal")})')
    for path in normalize_paths(read_allow):
        rules.append(f'(allow file-read* {_filter(path, "literal")})')
    for path in normalize_paths(read_metadata_allow):
        rules.append(f'(allow file-read-metadata {_filter(path, "literal")})')
    for path in normalize_paths(read_allow_subpaths):
        rules.append(f'(allow file-read* {_filter(path, "subpath")})')
    for path in normalize_paths(write_allow_exact):
        rules.append(f'(allow file-write* {_filter(path, "literal")})')
    ancestors = dict.fromkeys(
        str(parent) for path in (*denied, *write_paths, *read_denied)
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
    if network_loopback_ports is not None or network_unix_sockets:
        rules.append("(deny network-outbound)")
        for port in dict.fromkeys(int(port) for port in (network_loopback_ports or ())):
            if not 1 <= port <= 65535:
                raise ValueError("network loopback ports must be between 1 and 65535")
            rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
        for path in normalize_paths(network_unix_sockets):
            # Seatbelt's unix-socket filter is path scoped; it does not grant
            # access to other local sockets.
            rules.append(
                f'(allow network-outbound (remote unix-socket {_filter(path, "literal")}))'
            )
    if deny_ipc_escapes:
        rules.append("(deny appleevent-send)")
        services = " ".join(f'(global-name "{name}")' for name in IPC_ESCAPE_MACH_SERVICES)
        rules.append(f"(deny mach-lookup {services})")
        programs = " ".join(_filter(path, "literal") for path in IPC_ESCAPE_EXECUTABLES)
        rules.append(f"(deny process-exec {programs})")
    return "\n".join(rules)


def check_platform(paths: Sequence[str]) -> None:
    if paths and sys.platform != "darwin":
        raise RuntimeError("deny_read requires macOS sandbox-exec; refusing an unsandboxed child")


def wrap_argv(
    argv: list[str], paths: Sequence[str], write_allow: Sequence[str] | None = None,
    write_deny: Sequence[str] = (), *,
    read_allow: Sequence[str] = (), read_metadata_allow: Sequence[str] = (),
    write_allow_exact: Sequence[str] = (), write_allow_subpaths: Sequence[str] = (),
    read_allow_subpaths: Sequence[str] = (),
    network_loopback_ports: Sequence[int] | None = None,
    network_unix_sockets: Sequence[str] = (),
    read_deny_subpaths: Sequence[str] = (),
    write_allow_devices: bool = False,
    deny_ipc_escapes: bool = False,
) -> list[str]:
    """Pass arguments directly to sandbox-exec; never fall back on sandbox failure."""
    network_policy = network_loopback_ports is not None or bool(network_unix_sockets)
    if not paths and not network_policy:
        return argv
    check_platform(paths if paths else ("network-policy",))
    return [
        SANDBOX_EXEC, "-p",
        build_profile(paths, write_allow, write_deny, read_allow=read_allow,
                      read_metadata_allow=read_metadata_allow,
                      write_allow_exact=write_allow_exact,
                      write_allow_subpaths=write_allow_subpaths,
                      read_allow_subpaths=read_allow_subpaths,
                      network_loopback_ports=network_loopback_ports,
                      network_unix_sockets=network_unix_sockets,
                      read_deny_subpaths=read_deny_subpaths,
                      write_allow_devices=write_allow_devices,
                      deny_ipc_escapes=deny_ipc_escapes),
        *argv,
    ]


def _inside(path: str | Path, root: str | Path) -> bool:
    path, root = os.path.abspath(path), os.path.abspath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_write_roots(
    driver: str, roots: Sequence[str | Path], *, workspace: str | Path, agent_dir: str | Path,
) -> None:
    """Refuse a write root outside the H1 allowlist (workspace, agent dir, devices, H4)."""
    devices = (*STANDARD_DEVICE_LITERALS, *STANDARD_DEVICE_SUBPATHS)
    listed = [path for path, _reason in OWNER_HOME_EXCEPTIONS.get(driver, [])]
    for root in roots:
        spellings = {os.path.abspath(Path(root).expanduser()),
                     str(Path(root).expanduser().resolve())}
        for spelling in spellings:
            if spelling in devices:
                continue
            if any(_inside(spelling, base) for base in (
                workspace, Path(workspace).resolve(), agent_dir, Path(agent_dir).resolve(),
            )):
                continue
            if any(_inside(spelling, os.path.expanduser(p)) for p in listed):
                continue
            raise ValueError(
                f"{driver} write root {spelling} is outside the workspace and the agent "
                "directory and is not listed in OWNER_HOME_EXCEPTIONS"
            )


def agent_profile_kwargs(
    *, session_root: Path, agent_id: str, approval_socket: str, base_url: str | None,
    write_roots: Sequence[str], write_deny: Sequence[str] = (),
    read_allow: Sequence[str] = (), read_metadata_allow: Sequence[str] = (),
    write_allow_exact: Sequence[str] = (), write_allow_subpaths: Sequence[str] = (),
    workspace: str | Path | None = None, driver: str | None = None,
) -> dict:
    """`wrap_argv` keywords for a hook driver's one A9 profile.

    Writes are confined to `write_roots` (plus the standard devices); with
    `workspace` given, each root must be the workspace, the agent directory
    or an OWNER_HOME_EXCEPTIONS entry for `driver` (H1, H4), and
    `<workspace>/.codex` is write-denied (N6). The agent's approval key is
    write-denied. The
    approval socket's directory is write-denied (R1-0 Q2); other agents'
    directories under `<session_root>/agents/` are read-denied while this
    agent's own stays readable (A9.3); outbound network is limited to the
    agent's egress proxy, a loopback `base_url` and the approval socket (A10);
    Apple Events, LaunchServices, ServiceManagement, nsurlsessiond and the
    tools that drive them are denied, so no helper process runs the child's
    request outside the profile.
    """
    agents = Path(session_root) / "agents"
    agent_dir = agents / agent_id
    common_deny = [str(agent_dir / "approval.key")]  # The hook's reply key.
    if workspace is not None:
        check_write_roots(
            driver or "unknown", [*write_roots, *write_allow_exact, *write_allow_subpaths],
            workspace=workspace, agent_dir=agent_dir,
        )
        # Codex reads hook trust and project config from <workspace>/.codex
        # (N6, with --dangerously-bypass-hook-trust): no child may write it.
        common_deny.append(str(Path(workspace) / ".codex"))
    socket_deny = approval_socket_write_deny(
        approval_socket, [*write_roots, *write_allow_subpaths, *write_allow_exact],
    )
    return {
        "write_allow": list(write_roots),
        "write_allow_devices": True,
        "write_deny": [*write_deny, *common_deny, socket_deny],
        "read_allow": list(read_allow),
        # Path resolution through agents/ to this agent's own directory needs
        # the directory's metadata, not a listing of its siblings.
        "read_metadata_allow": [*read_metadata_allow, str(agents)],
        "write_allow_exact": list(write_allow_exact),
        "write_allow_subpaths": list(write_allow_subpaths),
        "read_deny_subpaths": [str(agents)],
        "read_allow_subpaths": [str(agents / agent_id)],
        "network_loopback_ports": network_ports(base_url),
        "network_unix_sockets": [approval_socket],
        "deny_ipc_escapes": True,
    }
