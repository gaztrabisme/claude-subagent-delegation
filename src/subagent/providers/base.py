"""What a provider is: its configuration, its driver interface, its process.

A provider is one backend a delegation can run on plus the driver that talks
to it. `ProviderConfig` is the configuration block `[providers.<name>]` builds
(config.py owns the parsing); `Provider` is the driver object that turns it
into a child process.

Nothing here reads the environment except an api_key_env name a provider
declares: every other value comes from the config file.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Settings
    from ..goal import Goal
    from ..router import Refusal

DRIVER_CLAUDE = "claude"
DRIVER_CODEX = "codex"
DRIVER_DSH = "dsh"
DRIVER_COPILOT = "copilot"
DRIVER_GEMINI = "gemini"
DRIVER_GROK = "grok"
DRIVER_OMP = "omp"
DRIVER_EPI = "epi"
DRIVER_ANTIGRAVITY = "antigravity"

# Every driver name the config file accepts. The ones with no module yet
# refuse at boot, not at load: a config naming one is valid, it just cannot
# run on this build.
DRIVERS = (
    DRIVER_CLAUDE, DRIVER_CODEX, DRIVER_DSH, DRIVER_COPILOT, DRIVER_GEMINI, DRIVER_GROK,
    DRIVER_ANTIGRAVITY, DRIVER_OMP, DRIVER_EPI,
)

NOT_PORTED = "driver not yet ported"

# A vendor label groups providers that share a refusal dialect (router keys
# its refusal codes on it). Derived when the config does not say.
VENDOR_BY_DRIVER = {
    DRIVER_EPI: "epi",
    DRIVER_CODEX: "codex",
    DRIVER_DSH: "deepseek",
    DRIVER_COPILOT: "copilot",
    DRIVER_GROK: "grok",
    DRIVER_OMP: "omp",
    DRIVER_ANTIGRAVITY: "gemini",
}
VENDOR_BY_HOST = {
    "api.z.ai": "zai",
    "api.deepseek.com": "deepseek",
}
VENDOR_NONE = "none"


def derive_vendor(driver: str, base_url: str | None) -> str:
    """The vendor label for a provider that declares none."""
    by_driver = VENDOR_BY_DRIVER.get(driver)
    if by_driver:
        return by_driver
    host = urlsplit(base_url or "").hostname or ""
    return VENDOR_BY_HOST.get(host.lower(), VENDOR_NONE)


@dataclass(frozen=True, slots=True)
class HealthSpec:
    """`[providers.<name>.health]`: the gate run before a child is dispatched.

    kind "none" is no gate at all (the cloud providers). "http" and "omlx" GET
    `url`; "llamacpp" tries `candidates` (base URLs) in order, plus whatever
    `resolve_cmd` prints, and the first that answers wins.
    """

    kind: str = "none"
    url: str | None = None
    candidates: tuple[str, ...] = ()
    resolve_cmd: tuple[str, ...] = ()
    warm: bool = False
    warm_path: str = "/v1/models"
    cold_load_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """`[providers.<name>.probe]`: what capacity telemetry samples, and when
    it would refuse a dispatch (`admit`)."""

    gpu_cmd: tuple[str, ...] = ()
    metrics_url: str | None = None
    slots_url: str | None = None
    host_cmd: tuple[str, ...] = ()
    host_parser: str | None = None
    admit: Mapping[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.gpu_cmd or self.metrics_url or self.slots_url or self.host_parser)


@dataclass(frozen=True, slots=True)
class PricingSpec:
    """`[providers.<name>.pricing]`: how a run on this provider is costed."""

    kind: str = "none"
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """One configured backend, with the knobs a child runs with.

    base_url None means the address is not fixed: a health spec with
    candidates resolves it at dispatch, and the codex driver needs none
    because the Codex CLI owns its own connection. model None means the
    driver's own configured model.
    """

    name: str
    driver: str
    vendor: str
    base_url: str | None = None
    model: str | None = None
    api_key_envs: tuple[str, ...] = ()
    # A literal key for a backend that needs one but has no secret (a local
    # llama.cpp takes any bearer token). Never a real credential.
    api_key_default: str | None = None
    local: bool = False
    send_sampling: bool = True
    max_agents: int = 4
    compact_window: int = 1_000_000
    max_steps: int = 40
    run_timeout: float = 1800.0
    idle_timeout: float = 900.0
    adapter: str | None = None
    binary: str | None = None
    experimental: bool = False
    health: HealthSpec = field(default_factory=HealthSpec)
    probe: ProbeSpec = field(default_factory=ProbeSpec)
    pricing: PricingSpec = field(default_factory=PricingSpec)
    # An optional native thinking override; each driver defines its supported
    # value mapping while unsupported values retain its ordinary default.
    thinking: str | None = None
    # Every key in the provider's config table this module does not recognise.
    # Drivers read their own extras from it: copilot's builtin_mcps, loop and
    # extra_args, grok's hooks.
    extra: dict[str, Any] = field(default_factory=dict)
    effort: str | None = None
    # Optional OS-enforced child read/write exclusions, expanded by config.py.
    deny_read: tuple[str, ...] = ()
    # Exact proxy destinations permitted for this provider lane. Host-only
    # entries default to TLS port 443; wildcards cover subdomains only.
    network_allow: tuple[str, ...] = ()

    def api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """The first set key in `api_key_envs`, else `api_key_default`."""
        source = os.environ if env is None else env
        for name in self.api_key_envs:
            raw = source.get(name)
            if raw and raw.strip():
                return raw.strip()
        return self.api_key_default

    def unavailable(self) -> str | None:
        """Why this build cannot run a child on this provider, or None."""
        if self.extra.get("auth") == "login":
            return None
        if self.driver != DRIVER_CLAUDE:
            # codex, copilot, grok and gemini CLIs own their own connection;
            # their drivers check the binary at boot.
            return None
        if not self.base_url and not self.health.candidates and not self.health.resolve_cmd:
            return f"provider {self.name!r} has no base URL and is not available in this build"
        return None

    def as_dict(self) -> dict[str, object]:
        """Public view for `list`. Names the key variables, never their values."""
        view = {
            "name": self.name,
            "driver": self.driver,
            "vendor": self.vendor,
            "base_url": self.base_url,
            "model": self.model,
            "effort": self.effort,
            "thinking": self.thinking,
            "local": self.local,
            "available": self.unavailable() is None,
            "api_key_envs": list(self.api_key_envs),
            "has_api_key": bool(self.api_key()),
            "max_agents": self.max_agents,
            "compact_window": self.compact_window,
            "max_steps": self.max_steps,
            "run_timeout": self.run_timeout,
            "idle_timeout": self.idle_timeout,
            "adapter": self.adapter,
            "experimental": self.experimental,
        }
        if self.extra.get("auth") == "login":
            view["auth"] = "login"
        return view


@dataclass
class Session:
    """Per-agent state one provider keeps between turns.

    `home` is the per-agent configuration directory the driver wrote
    (claude-home, codex-home); `session_id` is whatever the backend calls a
    resumable thread, learned from its own events.
    """

    provider: str
    home: Path | None = None
    session_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Translator(Protocol):
    """Native events in, claude stream-json events out."""

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class RunContext:
    """Outer-run inputs, unchanged across retries of that run.

    `deadline` is an absolute Unix timestamp, not a duration. `goal` is the
    existing parsed goal object. A continue gets a fresh `request_id`.
    """

    verification: str | None
    goal: Goal | None
    request_id: str
    deadline: float | None
    # Original caller inputs. Defaults keep existing hook providers compatible.
    # In particular, an outer config's model and default fallback are not
    # instructions to reroute inside another service.
    task: str | None = None
    instructions: str | None = None
    name: str | None = None
    model: str | None = None
    fallback: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Explicit opt-ins. Drivers without this declaration retain all defaults.

    run_context: spawn accepts the keyword-only `context` argument.
    native_outcome: result events carry an `outcome` dict in Run.detail shape.
    native_verification: that dict's checks are authoritative; no local checks
        or distillation run. A missing outcome fails rather than claiming success.
    native_usage: usage and cost are service snapshots, never stream-metered
        or repriced. Preserve top-level credits, credits_unit and cash_usd.
    cancel: cancel(session, *, reason, kind) cancels the service job before kill.
        It may return a final native snapshot for cancellation accounting.
    poll: poll(session) returns an outcome snapshot or None. Hooks must bound
        their own I/O; neither hook may approve a human gate.
    transcript: transcript(session, *, run_id, limit, raw) fetches activity for
        a native run. Native run identities must remain in that run's extra.
    metadata: metadata(settings, cfg) returns read-only provider extras for list.
    """

    run_context: bool = False
    native_outcome: bool = False
    native_verification: bool = False
    native_usage: bool = False
    cancel: bool = False
    poll: bool = False
    transcript: bool = False
    metadata: bool = False
    # MCP waits for admission or a pre-admission argument error, even with
    # wait_seconds=0. Cancellation must be confirmed before releasing guards.
    admission_barrier: bool = False
    confirmed_cancel: bool = False


def capabilities(provider: Any) -> ProviderCapabilities:
    return getattr(provider, "capabilities", ProviderCapabilities())


class Provider(Protocol):
    """The driver interface. One instance per driver, shared by every agent."""

    name: str
    needs_api_key: bool
    prompt_on_stdin: bool

    def guard(self, settings: Settings, cfg: ProviderConfig) -> str:
        ...

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        ...

    def argv(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> list[str]:
        ...

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session
    ) -> dict[str, str]:
        ...

    def translator(self, session: Session) -> Translator | None:
        ...

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> Process:
        ...

    def refusal(
        self, cfg: ProviderConfig, events: list[dict[str, Any]]
    ) -> Refusal | None:
        ...


class HookProvider(Provider, Protocol):
    """Optional extension. Only declared capabilities are invoked at runtime.

    Native result events are {type: "result", session_id: ..., outcome: {...}}.
    Poll returns the same outcome dict, not a stream event. Its fields are a
    snapshot, so a resumed snapshot must clear needs_human and human_action.
    Outer run/agent/provider identities remain the registry's identities; put
    service identities and routing metadata inside `extra` or Session.data.
    """

    capabilities: ProviderCapabilities

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
        *,
        context: RunContext,
    ) -> Process:
        ...

    def cancel(self, session: Session, *, reason: str, kind: str) -> dict[str, Any] | None:
        ...

    def poll(self, session: Session) -> dict[str, Any] | None:
        ...


_TREE_MARKER_ENV = "SUBAGENT_PROCESS_TREE_MARKER"
_MAC_PROC_PIDTBSDINFO = 3
_MAC_KERN_PROCARGS2 = 49
_MAC_LIBPROC: Any = None
_MAC_LIBC: Any = None


def _mac_library(name: str) -> Any:
    global _MAC_LIBPROC, _MAC_LIBC
    if name == "proc":
        if _MAC_LIBPROC is None:
            library_path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
            _MAC_LIBPROC = ctypes.CDLL(library_path, use_errno=True)
        return _MAC_LIBPROC
    if _MAC_LIBC is None:
        _MAC_LIBC = ctypes.CDLL(None, use_errno=True)
    return _MAC_LIBC


class _MacProcBsdInfo(ctypes.Structure):
    """The stable proc_bsdinfo prefix exported by macOS libproc."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("pbi_rfu", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _mac_process_info(pid: int) -> dict[str, Any] | None:
    """Read pid, parent, uid, and a PID-reuse-safe start identity via libproc."""
    try:
        libproc = _mac_library("proc")
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _MacProcBsdInfo()
        size = proc_pidinfo(
            pid, _MAC_PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if size != ctypes.sizeof(info) or info.pbi_pid != pid:
            return None
        return {
            "pid": pid,
            "ppid": int(info.pbi_ppid),
            "uid": int(info.pbi_uid),
            "status": int(info.pbi_status),
            "start": (int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)),
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _mac_process_has_marker(pid: int, marker: str) -> bool:
    """Read a same-user process environment through KERN_PROCARGS2."""
    try:
        libc = _mac_library("libc")
        sysctl = libc.sysctl
        sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, _MAC_KERN_PROCARGS2, pid)
        length = ctypes.c_size_t(0)
        if sysctl(mib, 3, None, ctypes.byref(length), None, 0) != 0 or not length.value:
            return False
        data = ctypes.create_string_buffer(length.value)
        if sysctl(mib, 3, data, ctypes.byref(length), None, 0) != 0:
            return False
        wanted = f"{_TREE_MARKER_ENV}={marker}".encode("ascii")
        return wanted in data.raw[:length.value].split(b"\0")
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _linux_process_info(pid: int, marker: str) -> dict[str, Any] | None:
    """Read one process from procfs, including its birth tick and environment marker."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        info = {
            "pid": pid,
            "ppid": int(tail[1]),
            "state": tail[0],
            "start": tail[19],
            "uid": Path(f"/proc/{pid}").stat().st_uid,
            "marker": False,
        }
        wanted = f"{_TREE_MARKER_ENV}={marker}".encode("ascii")
        info["marker"] = wanted in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        return info
    except (
        FileNotFoundError, PermissionError, ProcessLookupError, OSError, ValueError, IndexError
    ):
        return None


def _process_snapshot(marker: str) -> dict[int, dict[str, Any]]:
    """Find processes carrying this run marker and their parentage."""
    snapshot: dict[int, dict[str, Any]] = {}
    if sys.platform == "darwin":
        try:
            libproc = _mac_library("proc")
            proc_listallpids = libproc.proc_listallpids
            proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
            proc_listallpids.restype = ctypes.c_int
            count = max(0, proc_listallpids(None, 0))
            pids = (ctypes.c_int * max(count, 1))()
            count = max(0, proc_listallpids(pids, ctypes.sizeof(pids)))
            for pid in pids[:count]:
                if pid <= 0:
                    continue
                info = _mac_process_info(pid)
                if info is None or info.get("uid") != getattr(os, "getuid", lambda: -1)():
                    continue
                info["marker"] = _mac_process_has_marker(pid, marker)
                snapshot[pid] = info
        except (AttributeError, OSError, TypeError, ValueError):
            return {}
    elif Path("/proc").is_dir():
        try:
            pids = (int(item.name) for item in Path("/proc").iterdir() if item.name.isdigit())
            for pid in pids:
                info = _linux_process_info(pid, marker)
                if info is not None:
                    snapshot[pid] = info
        except OSError:
            return {}
    return snapshot


def _process_identity(pid: int) -> Any | None:
    """Return a process birth identity, avoiding pid-only signalling."""
    if sys.platform == "darwin":
        info = _mac_process_info(pid)
        return info["start"] if info is not None else None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[19]
    except (
        FileNotFoundError, PermissionError, ProcessLookupError, OSError, ValueError, IndexError
    ):
        return None


class Process:
    """One child subprocess, read as a stream of JSON events.

    Subclasses decide what the stream means: `events()` is theirs. This holds
    the parts every driver shares -- spawning, draining stderr beside stdout
    (an unread stderr pipe fills up and blocks the child), and tracking the
    complete process tree across process-group changes.
    """

    def __init__(
        self, argv: list[str], env: dict[str, str], cwd: str, prompt: str | None = None
    ):
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.prompt = prompt
        self._proc: subprocess.Popen[str] | None = None
        self._cancel_fence: threading.Lock | None = None
        self._cancelled = False
        self._tree_marker = secrets.token_hex(32)
        self._tree_tracked: dict[int, Any] = {}
        self._tree_lock = threading.RLock()
        self._tree_finished = threading.Event()
        self._tree_monitor: threading.Thread | None = None

    def enable_cancel_fence(self) -> None:
        """Opt-in clients cannot start after kill, even before Popen exists.

        The run layer enables this before publishing a hook provider's process.
        Legacy processes keep their original start and kill behavior.
        """
        self._cancel_fence = threading.Lock()

    def _start(self, stdin: bool = False, *, closed_stdin: bool = False) -> subprocess.Popen[str]:
        """Start the child. `stdin` opens a pipe to write the prompt on;
        `closed_stdin` hands it /dev/null (for a CLI that would otherwise
        read the parent's stdin -- the MCP transport -- to EOF); neither
        inherits the parent's."""
        if stdin:
            child_stdin: int | None = subprocess.PIPE
        elif closed_stdin:
            child_stdin = subprocess.DEVNULL
        else:
            child_stdin = None
        with self._cancel_fence or nullcontext():
            if self._cancelled:
                raise RuntimeError("provider client cancelled before start")
            self._proc = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                env={**self.env, _TREE_MARKER_ENV: self._tree_marker},
                stdin=child_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            self._tree_tracked[self._proc.pid] = _process_identity(self._proc.pid)
            self._tree_monitor = threading.Thread(
                target=self._watch_tree, args=(self._proc,),
                name=f"subagent-tree-{self._proc.pid}", daemon=True,
            )
            self._tree_monitor.start()
            return self._proc

    def events(self) -> Iterator[dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen[str], buf: list[str]) -> None:
        """Read all of the child's stderr into `buf`; runs beside stdout."""
        from ..config import log

        if proc.stderr is None:
            return
        try:
            buf.append(proc.stderr.read())
        except (ValueError, OSError) as exc:  # pipe torn down by kill()
            log.debug("stderr drain ended: %s", exc)

    def _drain_thread(self, proc: subprocess.Popen[str], buf: list[str]) -> threading.Thread:
        thread = threading.Thread(
            target=self._drain_stderr, args=(proc, buf), daemon=True
        )
        thread.start()
        return thread

    @staticmethod
    def _json_lines(stream: Any) -> Iterator[dict[str, Any]]:
        from ..config import log

        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-json child line: %s", line[:200])
                continue
            if isinstance(event, dict):
                yield event

    def _remember_tree(self, snapshot: dict[int, dict[str, Any]]) -> set[int]:
        """Remember marked processes and descendants with their birth identities."""
        valid = {
            pid for pid, identity in self._tree_tracked.items()
            if identity is not None and pid in snapshot
            and snapshot[pid].get("start") == identity
        }
        owned = {pid for pid, info in snapshot.items() if info.get("marker")}
        owned.update(valid)
        changed = True
        while changed:
            changed = False
            for pid, info in snapshot.items():
                if pid not in owned and info.get("ppid") in owned:
                    owned.add(pid)
                    changed = True
        for pid in owned:
            identity = snapshot[pid].get("start")
            if identity is not None:
                self._tree_tracked[pid] = identity
        return owned

    def _live_owned(self, snapshot: dict[int, dict[str, Any]]) -> list[tuple[int, Any]]:
        owned = self._remember_tree(snapshot)
        live = []
        for pid in owned:
            info = snapshot.get(pid)
            identity = self._tree_tracked.get(pid)
            if info is None or identity is None or info.get("start") != identity:
                continue
            if info.get("state") == "Z" or info.get("status") == 5:
                continue
            live.append((pid, identity))
        return live

    @staticmethod
    def _signal_identity(pid: int, identity: Any, sig: int) -> None:
        """Signal only the process instance whose start identity was recorded."""
        try:
            if sys.platform.startswith("linux") and hasattr(os, "pidfd_open"):
                pidfd = os.pidfd_open(pid)
                try:
                    if _process_identity(pid) != identity:
                        return
                    send = getattr(signal, "pidfd_send_signal", None)
                    if send is not None:
                        send(pidfd, sig, None, 0)
                    else:
                        os.kill(pid, sig)
                finally:
                    os.close(pidfd)
                return
            if _process_identity(pid) != identity:
                return
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError, ValueError):
            pass

    def _terminate_owned_tree(self, grace: float = 0.8) -> None:
        """Terminate every still-live process identified as part of this run."""
        with self._tree_lock:
            if self._tree_finished.is_set():
                return
            proc = self._proc
            if (proc is not None and proc.poll() is None
                    and _process_identity(proc.pid) == self._tree_tracked.get(proc.pid)):
                # Popen created a new session, so this group contains only the
                # CLI and descendants that have not called setsid().
                with suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(proc.pid, signal.SIGTERM)
            snapshot = _process_snapshot(self._tree_marker)
            processes = self._live_owned(snapshot)
            for pid, identity in processes:
                self._signal_identity(pid, identity, signal.SIGTERM)

            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                time.sleep(0.05)
                snapshot = _process_snapshot(self._tree_marker)
                processes = self._live_owned(snapshot)
                if not processes:
                    break
            if processes:
                # Recheck identity immediately before SIGKILL; a completed pid
                # may have been recycled while the grace period elapsed.
                for pid, identity in processes:
                    self._signal_identity(pid, identity, signal.SIGKILL)
            self._tree_finished.set()

    def _watch_tree(self, proc: subprocess.Popen[str]) -> None:
        """Track descendants during the run and close inherited pipes at exit."""
        while proc.poll() is None:
            with suppress(Exception):  # noqa: BLE001 - cleanup must not break event collection
                self._remember_tree(_process_snapshot(self._tree_marker))
            time.sleep(0.12)
        self._terminate_owned_tree()

    def kill(self) -> None:
        with self._cancel_fence or nullcontext():
            if self._cancel_fence is not None:
                self._cancelled = True
            proc = self._proc
        if proc is None:
            return
        self._terminate_owned_tree()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # The process identity remains owned by Popen until it is reaped.
            if _process_identity(proc.pid) is not None:
                proc.kill()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1)


class NotPorted:
    """A driver named in the config that this build cannot run.

    Its boot fails with a plain reason, so a chain hop on it is skipped like
    any other unavailable provider instead of crashing the walk.
    """

    needs_api_key = False
    prompt_on_stdin = False

    def __init__(self, name: str):
        self.name = name

    def guard(self, settings: Settings, cfg: ProviderConfig) -> str:
        return "none"

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        raise NotImplementedError(NOT_PORTED)

    def argv(self, *args: Any, **kwargs: Any) -> list[str]:
        raise NotImplementedError(NOT_PORTED)

    def env(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        raise NotImplementedError(NOT_PORTED)

    def translator(self, session: Session) -> Translator | None:
        return None

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        return None
