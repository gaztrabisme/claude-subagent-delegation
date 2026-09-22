"""The lane registry: every backend a delegation can run on.

A lane is one provider plus the driver that talks to it. The `claude` driver
runs `claude -p` against an Anthropic-compatible endpoint; the `codex` driver
runs `codex exec`. Knobs resolve per lane from the environment:
SAM_<LANE>_<KNOB>, then the global SAM_<KNOB> (numeric knobs only), then the
lane default.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DRIVER_CLAUDE = "claude"
DRIVER_CODEX = "codex"

FALLBACK_MODES = ("full", "local", "none")

# Cloud-lane numeric defaults. These are the values Settings has always used
# when the matching SAM_ variable is unset.
CLOUD_MAX_AGENTS = 4
CLOUD_COMPACT_WINDOW = 1_000_000
CLOUD_MAX_STEPS = 40
CLOUD_RUN_TIMEOUT = 1800.0
CLOUD_IDLE_TIMEOUT = 900.0

OMLX_SETTINGS = Path(".omlx") / "settings.json"


@dataclass(frozen=True, slots=True)
class Lane:
    """One backend a child can run on, with its resolved knobs.

    base_url None means the address is not fixed: bppc's (resolver "bppc")
    is found at dispatch by the health gate, and codex needs none because the
    Codex CLI owns its own connection. model None means the driver's own
    configured model.
    """

    name: str
    driver: str
    provider: str
    base_url: str | None
    model: str | None
    api_key_envs: tuple[str, ...]
    default_api_key: str | None
    local: bool
    send_sampling: bool
    max_agents: int
    compact_window: int
    max_steps: int
    run_timeout: float
    idle_timeout: float
    health_url: str | None
    resolver: str | None = None
    # A request adapter (adapter.ADAPTERS) the child's requests pass through.
    adapter: str | None = None

    def api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """The first set key in `api_key_envs`, else `default_api_key`."""
        source = os.environ if env is None else env
        for name in self.api_key_envs:
            raw = source.get(name)
            if raw and raw.strip():
                return raw.strip()
        return self.default_api_key

    def unavailable(self) -> str | None:
        """Why this build cannot run a child on this lane, or None."""
        if self.driver == DRIVER_CODEX:
            return None  # the Codex CLI owns its own connection
        if self.driver != DRIVER_CLAUDE:
            return f"lane {self.name!r} ({self.driver} driver) is not available in this build"
        if not self.base_url and not self.resolver:
            return f"lane {self.name!r} has no base URL and is not available in this build"
        return None

    def as_dict(self) -> dict[str, object]:
        """Public view for `list`. Names the key variables, never their values."""
        return {
            "name": self.name,
            "driver": self.driver,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
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
        }


def _raw(env: Mapping[str, str], lane: str, knob: str, *, global_too: bool) -> str | None:
    names = [f"SAM_{lane.upper()}_{knob}"]
    if global_too:
        names.append(f"SAM_{knob}")
    for name in names:
        raw = env.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


def _int(env: Mapping[str, str], lane: str, knob: str, default: int) -> int:
    raw = _raw(env, lane, knob, global_too=True)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"SAM_{lane.upper()}_{knob} / SAM_{knob} must be positive, got {raw!r}")
    return value


def _float(env: Mapping[str, str], lane: str, knob: str, default: float) -> float:
    raw = _raw(env, lane, knob, global_too=True)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"SAM_{lane.upper()}_{knob} / SAM_{knob} must be positive, got {raw!r}")
    return value


def _text(env: Mapping[str, str], lane: str, knob: str, default: str | None) -> str | None:
    # Per lane only. A global SAM_MODEL or SAM_BASE_URL would point every lane
    # at one backend's model or address.
    raw = _raw(env, lane, knob, global_too=False)
    return raw if raw is not None else default


def omlx_settings_key(home: Path | None = None) -> str | None:
    """auth.api_key from ~/.omlx/settings.json. Missing file or key is None."""
    path = (home or Path.home()) / OMLX_SETTINGS
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    auth = data.get("auth") if isinstance(data, dict) else None
    key = auth.get("api_key") if isinstance(auth, dict) else None
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def _lane(
    env: Mapping[str, str],
    *,
    name: str,
    driver: str,
    provider: str,
    base_url: str | None,
    model: str | None,
    api_key_envs: tuple[str, ...],
    default_api_key: str | None = None,
    local: bool = False,
    send_sampling: bool = True,
    max_agents: int = CLOUD_MAX_AGENTS,
    compact_window: int = CLOUD_COMPACT_WINDOW,
    max_steps: int = CLOUD_MAX_STEPS,
    run_timeout: float = CLOUD_RUN_TIMEOUT,
    idle_timeout: float = CLOUD_IDLE_TIMEOUT,
    health_url: str | None = None,
    resolver: str | None = None,
    adapter: str | None = None,
) -> Lane:
    url = _text(env, name, "BASE_URL", base_url)
    # SAM_<LANE>_ADAPTER=none turns a lane's default adapter off.
    adapter = _text(env, name, "ADAPTER", adapter)
    if adapter is not None and adapter.lower() in ("none", "off"):
        adapter = None
    return Lane(
        name=name,
        driver=driver,
        provider=provider,
        base_url=url.rstrip("/") if url else None,
        model=_text(env, name, "MODEL", model),
        api_key_envs=api_key_envs,
        default_api_key=default_api_key,
        local=local,
        send_sampling=send_sampling,
        max_agents=_int(env, name, "MAX_AGENTS", max_agents),
        compact_window=_int(env, name, "COMPACT_WINDOW", compact_window),
        max_steps=_int(env, name, "MAX_STEPS", max_steps),
        run_timeout=_float(env, name, "RUN_TIMEOUT", run_timeout),
        idle_timeout=_float(env, name, "IDLE_TIMEOUT", idle_timeout),
        health_url=health_url,
        resolver=resolver,
        adapter=adapter,
    )


def load_lanes(env: Mapping[str, str]) -> dict[str, Lane]:
    """Build the five lanes from `env`, in chain order."""
    lanes = [
        _lane(
            env,
            name="codex",
            driver=DRIVER_CODEX,
            provider="openai",
            base_url=None,
            model=None,
            api_key_envs=(),
        ),
        _lane(
            env,
            name="deepseek",
            driver=DRIVER_CLAUDE,
            provider="deepseek",
            base_url="https://api.deepseek.com/anthropic",
            model="deepseek-v4-pro",
            api_key_envs=("DEEPSEEK_API_KEY",),
        ),
        _lane(
            env,
            name="glm",
            driver=DRIVER_CLAUDE,
            provider="zai",
            base_url="https://api.z.ai/api/anthropic",
            model="glm-5.3-flash[1m]",
            api_key_envs=("GLM_API_KEY", "ZAI_API_KEY"),
        ),
        _lane(
            env,
            name="bppc",
            driver=DRIVER_CLAUDE,
            provider="local-llamacpp",
            base_url=None,
            model="qwen3.8-27b",
            api_key_envs=("SAM_BPPC_API_KEY",),
            default_api_key="local",
            local=True,
            max_agents=1,
            compact_window=40960,
            resolver="bppc",
            # llama.cpp's Qwen3.8 template refuses a system message after the
            # first user turn, which Claude Code sends.
            adapter="fold_system",
        ),
        _lane(
            env,
            name="omlx",
            driver=DRIVER_CLAUDE,
            provider="local-omlx",
            base_url="http://127.0.0.1:8000",
            model="Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B",
            api_key_envs=("SAM_OMLX_API_KEY",),
            default_api_key=omlx_settings_key(),
            local=True,
            send_sampling=False,
            # Measured on this Mac: aggregate output tok/s 20.1 at 1 child,
            # 18.3 at 2, 7.6 at 4; p90 TTFT 3.9 s -> 181 s at 4.
            max_agents=1,
            compact_window=98304,
            health_url="http://127.0.0.1:8000/api/status",
        ),
    ]
    return {lane.name: lane for lane in lanes}
