"""Server settings, resolved once from the environment at startup."""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .lanes import Lane, load_lanes

log = logging.getLogger("sam")

APPROVAL_HOOK = Path(__file__).parent / "runtime" / "approval_hook.py"
DEFAULT_LANE = "glm"


def configure_logging(level: str) -> None:
    """Attach a stderr handler. Safe to call more than once."""
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("sam %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False


def _count_env(name: str, default: int) -> int:
    """A count that may be 0 (0 turns the feature off)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be 0 or more, got {raw!r}")
    return value


def _int_env(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _float_env(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Server-wide settings. Per-backend values live on the lanes.

    The numeric knobs here (max_agents, max_steps, timeouts, compact_window)
    are the global SAM_ values; each lane resolves its own from them, so a
    lane's copy is the one a child actually runs with. max_agents is also the
    cap on live agents across all lanes.
    """

    workspace: Path
    session_root: Path
    claude_bin: str
    max_agents: int
    transcript_limit: int
    summary_tokens: int
    idle_timeout: float
    run_timeout: float
    turn_token_budget: int | None
    loop_strikes: int
    supervisor: str
    supervisor_cmd: str
    supervisor_timeout: float
    approval_socket: str
    log_level: str
    max_steps: int
    verify_timeout: float
    chars_per_token: float
    run_archive: int
    trace: str | None
    compact_window: int
    rate_limit_retries: int
    rate_limit_backoff: float
    throttle_backoff: float
    default_lane: str = DEFAULT_LANE
    lanes: dict[str, Lane] = field(default_factory=dict)
    # Lane memory: how long a refusal with no reset time keeps a lane closed.
    balance_close_hours: float = 6.0
    throttle_close_minutes: float = 15.0
    # Extra time a run gets when oMLX reports no model loaded yet.
    omlx_cold_load_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> Settings:
        workspace = Path(os.environ.get("SAM_WORKSPACE") or Path.cwd()).expanduser().resolve()
        # Outside every workspace by default: it holds the per-agent settings
        # file that installs the guard hook, and a child that could write it
        # could remove its own guard. The guard also refuses writes under it
        # wherever it is (guard.protect).
        session_root = Path(
            os.environ.get("SAM_SESSION_ROOT") or (Path.home() / ".subagent-mcp" / "sessions")
        ).expanduser().resolve()
        return cls(
            workspace=workspace,
            session_root=session_root,
            claude_bin=os.environ.get("SAM_CLAUDE_BIN") or "claude",
            max_agents=_int_env("SAM_MAX_AGENTS", 4) or 4,
            transcript_limit=_int_env("SAM_TRANSCRIPT_LIMIT", 400) or 400,
            summary_tokens=_int_env("SAM_SUMMARY_TOKENS", 2000) or 2000,
            idle_timeout=_float_env("SAM_IDLE_TIMEOUT", 900.0) or 900.0,
            run_timeout=_float_env("SAM_RUN_TIMEOUT", 1800.0) or 1800.0,
            turn_token_budget=_int_env("SAM_TURN_TOKEN_BUDGET", None),
            # 8, not 3: a child re-running its tests between edits repeats one
            # call legitimately, and a live loop detector would kill it.
            loop_strikes=_int_env("SAM_LOOP_STRIKES", 8) or 8,
            supervisor=os.environ.get("SAM_SUPERVISOR") or "auto",
            supervisor_cmd=os.environ.get("SAM_SUPERVISOR_CMD")
            or "claude -p --model sonnet",
            supervisor_timeout=_float_env("SAM_SUPERVISOR_TIMEOUT", 120.0) or 120.0,
            approval_socket=os.environ.get("SAM_APPROVAL_SOCKET")
            or str(Path(tempfile.gettempdir()) / f"sam-approval-{os.getpid()}.sock"),
            log_level=os.environ.get("SAM_LOG_LEVEL") or "info",
            max_steps=_int_env("SAM_MAX_STEPS", 40) or 40,
            verify_timeout=_float_env("SAM_VERIFY_TIMEOUT", 300.0) or 300.0,
            chars_per_token=_float_env("SAM_CHARS_PER_TOKEN", 3.5) or 3.5,
            run_archive=_int_env("SAM_RUN_ARCHIVE", 200) or 200,
            trace=os.environ.get("SAM_TRACE"),
            compact_window=_int_env("SAM_COMPACT_WINDOW", 1_000_000) or 1_000_000,
            # Transient z.ai rate limits (HTTP 429/529) are retried with
            # exponential backoff: backoff * 2**attempt, capped at 300s.
            rate_limit_retries=_count_env("SAM_RATE_LIMIT_RETRIES", 3),
            rate_limit_backoff=_float_env("SAM_RATE_LIMIT_BACKOFF", 5.0) or 5.0,
            # z.ai fair-use throttle (code 1313) clears in minutes: 60s, then
            # doubled per attempt, capped at 900s. Plan quota (1308/1310) is
            # never retried.
            throttle_backoff=_float_env("SAM_THROTTLE_BACKOFF", 60.0) or 60.0,
            default_lane=os.environ.get("SAM_DEFAULT_LANE") or DEFAULT_LANE,
            lanes=load_lanes(os.environ),
            balance_close_hours=_float_env("SAM_BALANCE_CLOSE_HOURS", 6.0) or 6.0,
            throttle_close_minutes=_float_env("SAM_THROTTLE_CLOSE_MINUTES", 15.0) or 15.0,
            omlx_cold_load_seconds=_float_env("SAM_OMLX_COLD_LOAD_SECONDS", 120.0) or 120.0,
        )

    def lane(self, name: str | None = None) -> Lane:
        """The named lane, or the default one. KeyError if there is none."""
        return self.lanes[name or self.default_lane]

    @property
    def result_cap_chars(self) -> int:
        return max(200, int(self.summary_tokens * self.chars_per_token))

    def agent_home(self, agent_id: str) -> Path:
        path = self.session_root / "agents" / agent_id / "claude-home"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def hooks_config(
        self, agent_id: str, lane: Lane | None = None, model: str | None = None
    ) -> Path:
        """Per-agent Claude Code settings.json with the PreToolUse hook.

        `lane` defaults to the default lane and `model` to that lane's model.
        No sampling setting (temperature, top_p, top_k) is written here for
        any lane; a lane with send_sampling False depends on that.
        """
        lane = lane or self.lane()
        model = model or lane.model or ""
        path = self.agent_home(agent_id) / "settings.json"
        if self.supervisor == "off":
            hooks: dict = {}
        else:
            command = (
                f"{shlex.quote(sys.executable)} {shlex.quote(str(APPROVAL_HOOK))} "
                f"--agent {shlex.quote(agent_id)}"
            )
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
                "ANTHROPIC_BASE_URL": lane.base_url or "",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(lane.compact_window),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "API_TIMEOUT_MS": "3000000",
            },
            "hooks": hooks,
            "autoCompactWindow": lane.compact_window,
        }
        path.write_text(json.dumps(payload, indent=2))
        return path

    def child_env(
        self, agent_id: str, lane: Lane | None = None, model: str | None = None
    ) -> dict[str, str]:
        """Environment for one `claude -p` subprocess. Isolated from parent OAuth.

        `lane` defaults to the default lane and `model` to that lane's model.
        The key is the lane's: its first set api_key_env, else its default.
        """
        lane = lane or self.lane()
        model = model or lane.model or ""
        env = {k: v for k, v in os.environ.items() if v is not None}
        # The server's own copies of every lane's key; the child gets exactly
        # one, as ANTHROPIC_AUTH_TOKEN, because Claude Code needs it.
        server_keys = {n for known in self.lanes.values() for n in known.api_key_envs}
        for leaked in (
            *sorted(server_keys | set(lane.api_key_envs)),
            "GLM_API_KEY",
            "ZAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        ):
            env.pop(leaked, None)
        key = lane.api_key() or ""
        env.update({
            "CLAUDE_CONFIG_DIR": str(self.agent_home(agent_id)),
            "ANTHROPIC_BASE_URL": lane.base_url or "",
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(lane.compact_window),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "3000000",
            # Every Bash call starts in the workspace. The guard resolves
            # relative paths from there; a cwd carried over from an earlier
            # call's `cd` would make `../x` mean something else.
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
            "SAM_APPROVAL_SOCKET": self.approval_socket,
            "SAM_HOOK_TIMEOUT": str(self.supervisor_timeout + 20),
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
