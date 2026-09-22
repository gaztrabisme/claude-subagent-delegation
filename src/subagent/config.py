"""Server settings, read from TOML config files at startup.

Three files are layered, each overriding the one before, tables merged key by
key: `$XDG_CONFIG_HOME/subagent/config.toml` (default
`~/.config/subagent/config.toml`), `<project_root>/.subagent/config.toml`, and
whatever `$SUBAGENT_CONFIG` names.

Configuration is the file. The only environment variables this server reads
are `SUBAGENT_CONFIG`, the `api_key_env` names the providers declare, and the
two plumbing variables it sets on its own children
(`SUBAGENT_APPROVAL_SOCKET`, `SUBAGENT_HOOK_TIMEOUT`). There are no built-in
providers: a config with no `[providers]` table can run nothing.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import adapter
from .providers.base import (
    DRIVERS,
    HealthSpec,
    PricingSpec,
    ProbeSpec,
    ProviderConfig,
    derive_vendor,
)

log = logging.getLogger("sam")

APPROVAL_HOOK = Path(__file__).parent / "guard" / "approval_hook.py"

CONFIG_ENV = "SUBAGENT_CONFIG"
CONFIG_NAME = "config.toml"
PROJECT_DIR = ".subagent"

# Names the child must never inherit from this server, whatever the providers
# declare: the parent's own Claude credentials and endpoint.
FIXED_LEAKED_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
)


class ConfigError(RuntimeError):
    """A config file this server cannot run with. The message says why."""


def configure_logging(level: str) -> None:
    """Attach a stderr handler. Safe to call more than once."""
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("sam %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False


# --- reading and merging -------------------------------------------------------


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """`over` on top of `base`, tables merged key by key (loop.common)."""
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            out[key] = _merge(dict(current), value)
        else:
            out[key] = value
    return out


def _read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def config_paths(project_root: Path | None = None, extra: Path | None = None) -> list[Path]:
    """The config files that are layered, in order, whether or not they exist."""
    home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".config"
    paths = [base / "subagent" / CONFIG_NAME]
    if project_root is not None:
        paths.append(Path(project_root).expanduser() / PROJECT_DIR / CONFIG_NAME)
    named = os.environ.get(CONFIG_ENV)
    if named and named.strip():
        paths.append(Path(named.strip()).expanduser())
    if extra is not None:
        paths.append(Path(extra))
    return paths


def read_config(project_root: Path | None = None, extra: Path | None = None) -> dict[str, Any]:
    """The merged config table from every layer that exists."""
    data: dict[str, Any] = {}
    for path in config_paths(project_root, extra):
        if path.exists():
            data = _merge(data, _read(path))
    return data


# --- typed getters -------------------------------------------------------------


def _table(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] must be a table, got {type(value).__name__}")
    return dict(value)


def _str(table: Mapping[str, Any], key: str, default: str | None) -> str | None:
    raw = table.get(key, default)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError(f"{key} must be a string, got {raw!r}")
    return raw


def _bool(table: Mapping[str, Any], key: str, default: bool) -> bool:
    raw = table.get(key, default)
    if not isinstance(raw, bool):
        raise ConfigError(f"{key} must be true or false, got {raw!r}")
    return raw


def _num(table: Mapping[str, Any], key: str, default: float | None,
         *, positive: bool = True) -> float | None:
    raw = table.get(key, default)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{key} must be a number, got {raw!r}")
    if positive and raw <= 0:
        raise ConfigError(f"{key} must be positive, got {raw!r}")
    if not positive and raw < 0:
        raise ConfigError(f"{key} must be 0 or more, got {raw!r}")
    return float(raw)


def _int(table: Mapping[str, Any], key: str, default: int | None,
         *, positive: bool = True) -> int | None:
    value = _num(table, key, default, positive=positive)
    return None if value is None else int(value)


def _strings(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """A key given either as one string or as a list of strings."""
    raw = table.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
        return tuple(v for v in raw if v.strip())
    raise ConfigError(f"{key} must be a string or a list of strings, got {raw!r}")


def _path(raw: str | None, default: Path) -> Path:
    return (Path(raw).expanduser() if raw else default).resolve()


# --- providers -----------------------------------------------------------------


def _health(table: Mapping[str, Any]) -> HealthSpec:
    spec = _table(table, "health")
    kind = _str(spec, "kind", "none") or "none"
    return HealthSpec(
        kind=kind,
        url=_str(spec, "url", None),
        candidates=_strings(spec, "candidates"),
        resolve_cmd=_strings(spec, "resolve_cmd"),
        warm=_bool(spec, "warm", False),
        warm_path=_str(spec, "warm_path", "/v1/models") or "/v1/models",
        cold_load_seconds=_num(spec, "cold_load_seconds", 0.0, positive=False) or 0.0,
    )


def _probe(table: Mapping[str, Any]) -> ProbeSpec:
    spec = _table(table, "probe")
    admit = _table(spec, "admit")
    return ProbeSpec(
        gpu_cmd=_strings(spec, "gpu_cmd"),
        metrics_url=_str(spec, "metrics_url", None),
        slots_url=_str(spec, "slots_url", None),
        host_cmd=_strings(spec, "host_cmd"),
        host_parser=_str(spec, "host_parser", None),
        admit=admit,
    )


def _pricing(table: Mapping[str, Any]) -> PricingSpec:
    spec = _table(table, "pricing")
    kind = _str(spec, "kind", "none") or "none"
    values = {k: v for k, v in spec.items() if k != "kind"}
    return PricingSpec(kind=kind, values=values)


def _provider(name: str, table: Mapping[str, Any], core: Mapping[str, Any]) -> ProviderConfig:
    driver = _str(table, "driver", None)
    if not driver:
        raise ConfigError(f"provider {name!r} has no driver")
    if driver not in DRIVERS:
        raise ConfigError(
            f"provider {name!r} has unknown driver {driver!r}; "
            f"expected one of {', '.join(DRIVERS)}"
        )
    base_url = _str(table, "base_url", None)
    if base_url:
        base_url = base_url.rstrip("/")
    adapter_name = _str(table, "adapter", None)
    if adapter_name and adapter_name.lower() in ("none", "off"):
        adapter_name = None
    return ProviderConfig(
        name=name,
        driver=driver,
        vendor=_str(table, "vendor", None) or derive_vendor(driver, base_url),
        base_url=base_url,
        model=_str(table, "model", None),
        api_key_envs=_strings(table, "api_key_env"),
        api_key_default=_str(table, "api_key", None),
        local=_bool(table, "local", False),
        send_sampling=_bool(table, "send_sampling", True),
        max_agents=int(_int(table, "max_agents", core["max_agents"])),
        compact_window=int(_int(table, "compact_window", core["compact_window"])),
        max_steps=int(_int(table, "max_steps", core["max_steps"])),
        run_timeout=float(_num(table, "run_timeout", core["run_timeout"])),
        idle_timeout=float(_num(table, "idle_timeout", core["idle_timeout"])),
        adapter=adapter_name,
        binary=_str(table, "binary", None),
        experimental=_bool(table, "experimental", False),
        health=_health(table),
        probe=_probe(table),
        pricing=_pricing(table),
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """Server-wide settings. Per-backend values live on the providers.

    The numeric knobs here (max_agents, max_steps, timeouts, compact_window)
    are the `[core]` defaults each provider resolves its own from, so a
    provider's copy is the one a child actually runs with. max_agents is also
    the cap on live agents across all providers.
    """

    workspace: Path
    session_root: Path
    max_agents: int = 4
    transcript_limit: int = 400
    summary_tokens: int = 2000
    idle_timeout: float = 900.0
    run_timeout: float = 1800.0
    turn_token_budget: int | None = None
    # 8, not 3: a child re-running its tests between edits repeats one call
    # legitimately, and a tighter loop detector would kill it.
    loop_strikes: int = 8
    supervisor: str = "auto"
    supervisor_cmd: str | None = None
    supervisor_timeout: float = 120.0
    allow_unguarded: bool = False
    approval_socket: str = ""
    log_level: str = "info"
    max_steps: int = 40
    verify_timeout: float = 300.0
    chars_per_token: float = 3.5
    run_archive: int = 200
    trace: str | None = None
    compact_window: int = 1_000_000
    # Transient rate limits (HTTP 429/529) are retried with exponential
    # backoff: backoff * 2**attempt, capped at 300s.
    rate_limit_retries: int = 3
    rate_limit_backoff: float = 5.0
    # A fair-use throttle clears in minutes: 60s, then doubled per attempt,
    # capped at 900s. Plan quota is never retried.
    throttle_backoff: float = 60.0
    sample_seconds: float = 10.0
    default_provider: str = ""
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    fallback_chain: tuple[str, ...] = ()
    pricing: Mapping[str, Any] = field(default_factory=dict)
    loop: Mapping[str, Any] = field(default_factory=dict)
    instructions: str = ""
    # Provider memory: how long a refusal with no reset time keeps one closed.
    balance_close_hours: float = 6.0
    throttle_close_minutes: float = 15.0
    # Config problems that are not fatal (a provider with no key in the
    # environment); `doctor` and the server banner print them.
    warnings: tuple[str, ...] = ()

    # --- providers -------------------------------------------------------

    def require_providers(self) -> None:
        """Raise unless at least one provider is configured."""
        if not self.providers:
            raise ConfigError("no providers configured; run `subagent init`")

    def provider(self, name: str | None = None) -> ProviderConfig:
        """The named provider, or the default one. KeyError if there is none."""
        return self.providers[name or self.default_provider]

    def chain(self, primary: str, mode: str | None = "full") -> list[ProviderConfig]:
        """The providers a delegation may run on, in order (router.chain)."""
        from . import router

        names = router.chain(
            primary, mode if mode is not None else "full",
            list(self.fallback_chain), self.providers,
        )
        return [self.providers[n] for n in names if n in self.providers]

    def cold_load_seconds(self, name: str) -> float:
        """Deadline extension for a cold-load run on `name`."""
        cfg = self.providers.get(name)
        return cfg.health.cold_load_seconds if cfg else 0.0

    # --- derived ---------------------------------------------------------

    @property
    def result_cap_chars(self) -> int:
        return max(200, int(self.summary_tokens * self.chars_per_token))

    def agent_home(self, agent_id: str) -> Path:
        path = self.session_root / "agents" / agent_id / "claude-home"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def guard_hook_command(self, agent_id: str, *extra: str) -> str:
        """The PreToolUse guard hook command line for one agent, any driver."""
        parts = [sys.executable, str(APPROVAL_HOOK), "--agent", agent_id, *extra]
        return " ".join(shlex.quote(p) for p in parts)

    @property
    def leaked_keys(self) -> tuple[str, ...]:
        """Key variables this server holds that no child may inherit."""
        declared = {n for cfg in self.providers.values() for n in cfg.api_key_envs}
        return (*sorted(declared), *FIXED_LEAKED_KEYS)

    def hooks_config(
        self, agent_id: str, cfg: ProviderConfig | None = None, model: str | None = None
    ) -> Path:
        """Per-agent Claude Code settings.json with the PreToolUse hook.

        `cfg` defaults to the default provider and `model` to its model. No
        sampling setting (temperature, top_p, top_k) is written here for any
        provider; one with send_sampling False depends on that.
        """
        cfg = cfg or self.provider()
        model = model or cfg.model or ""
        path = self.agent_home(agent_id) / "settings.json"
        if self.supervisor == "off":
            hooks: dict = {}
        else:
            command = self.guard_hook_command(agent_id)
            hooks = {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                                "timeout": int(self.supervisor_timeout + 30),
                            }
                        ],
                    }
                ]
            }
        payload = {
            "env": {
                "ANTHROPIC_BASE_URL": adapter.child_base_url(cfg) or "",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(cfg.compact_window),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "API_TIMEOUT_MS": "3000000",
            },
            "hooks": hooks,
            "autoCompactWindow": cfg.compact_window,
        }
        path.write_text(json.dumps(payload, indent=2))
        return path

    def child_env(
        self, agent_id: str, cfg: ProviderConfig | None = None, model: str | None = None
    ) -> dict[str, str]:
        """Environment for one `claude -p` subprocess. Isolated from parent OAuth.

        `cfg` defaults to the default provider and `model` to its model. The
        key is that provider's: its first set api_key_env, else its default.
        """
        cfg = cfg or self.provider()
        model = model or cfg.model or ""
        env = {k: v for k, v in os.environ.items() if v is not None}
        # The server's own copies of every provider's key; the child gets
        # exactly one, as ANTHROPIC_AUTH_TOKEN, because Claude Code needs it.
        for leaked in self.leaked_keys:
            env.pop(leaked, None)
        key = cfg.api_key() or ""
        env.update({
            "CLAUDE_CONFIG_DIR": str(self.agent_home(agent_id)),
            "ANTHROPIC_BASE_URL": adapter.child_base_url(cfg) or "",
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(cfg.compact_window),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "3000000",
            # Every Bash call starts in the workspace. The guard resolves
            # relative paths from there; a cwd carried over from an earlier
            # call's `cd` would make `../x` mean something else.
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
            "SUBAGENT_APPROVAL_SOCKET": self.approval_socket,
            "SUBAGENT_HOOK_TIMEOUT": str(self.supervisor_timeout + 20),
        })
        return env

    def workspace_refusal(self, workspace: Path) -> str | None:
        """Why `workspace` cannot be delegated into, or None.

        The hook the guard runs as is `sys.executable APPROVAL_HOOK`. A
        workspace containing either makes the guard's own code writable by the
        child it guards.
        """
        root = Path(workspace).resolve()
        owned = (("the guard hook", APPROVAL_HOOK), ("this server's Python", Path(sys.prefix)))
        for label, path in owned:
            try:
                Path(path).resolve().relative_to(root)
            except ValueError:
                continue
            return (
                f"workspace {root} contains {label} ({path}); a child there could "
                "rewrite its own guard. Delegate into a subdirectory or a worktree."
            )
        return None

    def resolve_workspace(self, requested: str | None) -> Path:
        if not requested:
            return self.workspace
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()


def _validate(settings: Settings, loop: Mapping[str, Any]) -> None:
    """Every config error that is about more than one table."""
    known = set(settings.providers)
    if settings.default_provider and settings.default_provider not in known:
        raise ConfigError(
            f"[core].default_provider {settings.default_provider!r} is not a declared "
            f"provider; declared: {', '.join(sorted(known)) or '(none)'}"
        )
    for name in settings.fallback_chain:
        if name not in known:
            raise ConfigError(
                f"[fallback].chain names {name!r}, which is not a declared provider"
            )
    for where, table in _loop_targets(loop):
        name = table.get("provider")
        if isinstance(name, str) and name and name not in known:
            raise ConfigError(f"{where} names provider {name!r}, which is not declared")
    if settings.supervisor == "agent" and not (settings.supervisor_cmd or "").strip():
        raise ConfigError('[guard].supervisor = "agent" needs [guard].supervisor_cmd')


def _loop_targets(loop: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """The `[loop]` tables that name a provider, with a label for the error."""
    found: list[tuple[str, Mapping[str, Any]]] = []
    tiers = loop.get("tiers")
    if isinstance(tiers, Mapping):
        for tier, table in tiers.items():
            if isinstance(table, Mapping):
                found.append((f"[loop.tiers.{tier}]", table))
    for key in ("review", "test_writer"):
        table = loop.get(key)
        if isinstance(table, Mapping):
            found.append((f"[loop.{key}]", table))
    return found


def load(project_root: Path | None = None, extra: Path | None = None) -> Settings:
    """Settings from the layered config files. ConfigError says what is wrong."""
    data = read_config(project_root, extra)
    core = _table(data, "core")
    guard = _table(data, "guard")
    telemetry = _table(data, "telemetry")
    fallback = _table(data, "fallback")
    pricing = _table(data, "pricing")
    loop = _table(data, "loop")
    provider_tables = _table(data, "providers")

    defaults = {
        "max_agents": _int(core, "max_agents", 4),
        "compact_window": _int(core, "compact_window", 1_000_000),
        "max_steps": _int(core, "max_steps", 40),
        "run_timeout": _num(core, "run_timeout", 1800.0),
        "idle_timeout": _num(core, "idle_timeout", 900.0),
    }
    providers: dict[str, ProviderConfig] = {}
    warnings: list[str] = []
    for name, table in provider_tables.items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"[providers.{name}] must be a table")
        cfg = _provider(str(name), table, defaults)
        providers[cfg.name] = cfg
        if cfg.api_key_envs and not cfg.api_key():
            warnings.append(
                f"provider {cfg.name}: no API key in this server's environment; "
                f"set one of {', '.join(cfg.api_key_envs)}"
            )

    # Outside every workspace by default: the session root holds the per-agent
    # settings file that installs the guard hook, and a child that could write
    # it could remove its own guard.
    settings = Settings(
        workspace=_path(_str(core, "workspace", None), Path.cwd()),
        session_root=_path(
            _str(core, "session_root", None), Path.home() / ".subagent" / "sessions"
        ),
        max_agents=int(defaults["max_agents"]),
        transcript_limit=int(_int(core, "transcript_limit", 400)),
        summary_tokens=int(_int(core, "summary_tokens", 2000)),
        idle_timeout=float(defaults["idle_timeout"]),
        run_timeout=float(defaults["run_timeout"]),
        turn_token_budget=_int(core, "turn_token_budget", None),
        loop_strikes=int(_int(core, "loop_strikes", 8)),
        supervisor=_str(guard, "supervisor", "auto") or "auto",
        supervisor_cmd=_str(guard, "supervisor_cmd", None),
        supervisor_timeout=float(_num(guard, "supervisor_timeout", 120.0)),
        allow_unguarded=_bool(guard, "allow_unguarded", False),
        approval_socket=_str(guard, "approval_socket", None)
        or str(Path(tempfile.gettempdir()) / f"subagent-approval-{os.getpid()}.sock"),
        log_level=_str(core, "log_level", "info") or "info",
        max_steps=int(defaults["max_steps"]),
        verify_timeout=float(_num(core, "verify_timeout", 300.0)),
        chars_per_token=float(_num(core, "chars_per_token", 3.5)),
        run_archive=int(_int(core, "run_archive", 200)),
        trace=_str(core, "trace", None),
        compact_window=int(defaults["compact_window"]),
        rate_limit_retries=int(_int(core, "rate_limit_retries", 3, positive=False)),
        rate_limit_backoff=float(_num(core, "rate_limit_backoff", 5.0)),
        throttle_backoff=float(_num(core, "throttle_backoff", 60.0)),
        sample_seconds=float(_num(telemetry, "sample_seconds", 10.0)),
        default_provider=_str(core, "default_provider", None)
        or (next(iter(providers)) if providers else ""),
        providers=providers,
        fallback_chain=_strings(fallback, "chain"),
        pricing=pricing,
        loop=loop,
        instructions=_str(core, "instructions", "") or "",
        balance_close_hours=float(_num(core, "balance_close_hours", 6.0)),
        throttle_close_minutes=float(_num(core, "throttle_close_minutes", 15.0)),
        warnings=tuple(warnings),
    )
    _validate(settings, loop)
    return settings
