"""What a provider is: its configuration, its driver interface, its process.

A provider is one backend a delegation can run on plus the driver that talks
to it. `ProviderConfig` is the configuration block `[providers.<name>]` builds
(config.py owns the parsing); `Provider` is the driver object that turns it
into a child process.

Nothing here reads the environment except an api_key_env name a provider
declares: every other value comes from the config file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import nullcontext
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
DRIVER_COPILOT = "copilot"
DRIVER_GEMINI = "gemini"
DRIVER_GROK = "grok"
DRIVER_OMP = "omp"
DRIVER_ANTIGRAVITY = "antigravity"

# Every driver name the config file accepts. The ones with no module yet
# refuse at boot, not at load: a config naming one is valid, it just cannot
# run on this build.
DRIVERS = (
    DRIVER_CLAUDE, DRIVER_CODEX, DRIVER_COPILOT, DRIVER_GEMINI, DRIVER_GROK,
    DRIVER_ANTIGRAVITY, DRIVER_OMP,
)

NOT_PORTED = "driver not yet ported"

# A vendor label groups providers that share a refusal dialect (router keys
# its refusal codes on it). Derived when the config does not say.
VENDOR_BY_DRIVER = {
    DRIVER_CODEX: "codex",
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
    poll: poll(session) returns an outcome snapshot or None. Hooks must bound
        their own I/O; neither hook may approve a human gate.
    """

    run_context: bool = False
    native_outcome: bool = False
    native_verification: bool = False
    native_usage: bool = False
    cancel: bool = False
    poll: bool = False


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

    def cancel(self, session: Session, *, reason: str, kind: str) -> None:
        ...

    def poll(self, session: Session) -> dict[str, Any] | None:
        ...


class Process:
    """One child subprocess, read as a stream of JSON events.

    Subclasses decide what the stream means: `events()` is theirs. This holds
    the parts every driver shares -- spawning, draining stderr beside stdout
    (an unread stderr pipe fills up and blocks the child), and killing the
    whole process group.
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
                env=self.env,
                stdin=child_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
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

    def kill(self) -> None:
        with self._cancel_fence or nullcontext():
            if self._cancel_fence is not None:
                self._cancelled = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()


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
