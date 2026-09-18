"""Server settings, resolved once from the environment at startup."""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gsa")

APPROVAL_HOOK = Path(__file__).parent / "runtime" / "approval_hook.py"
DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_MODEL = "glm-5.3[1m]"
DEFAULT_FLASH_MODEL = "glm-5.3-flash[1m]"


def configure_logging(level: str) -> None:
    """Attach a stderr handler. Safe to call more than once."""
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("gsa %(levelname)s %(name)s: %(message)s"))
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


def resolve_api_key() -> str | None:
    """GLM / Z.ai key. None means inherit-or-fail at spawn, not at import."""
    for name in ("GLM_API_KEY", "ZAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip()
    return None


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str | None
    base_url: str
    model: str
    flash_model: str
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

    @classmethod
    def from_env(cls) -> Settings:
        workspace = Path(os.environ.get("GSA_WORKSPACE") or Path.cwd()).expanduser().resolve()
        # Outside every workspace by default: it holds the per-agent settings
        # file that installs the guard hook, and a child that could write it
        # could remove its own guard. The guard also refuses writes under it
        # wherever it is (guard.protect).
        session_root = Path(
            os.environ.get("GSA_SESSION_ROOT") or (Path.home() / ".glm-subagent" / "sessions")
        ).expanduser().resolve()
        return cls(
            api_key=resolve_api_key(),
            base_url=(os.environ.get("GSA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            model=os.environ.get("GSA_MODEL") or DEFAULT_MODEL,
            flash_model=os.environ.get("GSA_FLASH_MODEL") or DEFAULT_FLASH_MODEL,
            workspace=workspace,
            session_root=session_root,
            claude_bin=os.environ.get("GSA_CLAUDE_BIN") or "claude",
            max_agents=_int_env("GSA_MAX_AGENTS", 4) or 4,
            transcript_limit=_int_env("GSA_TRANSCRIPT_LIMIT", 400) or 400,
            summary_tokens=_int_env("GSA_SUMMARY_TOKENS", 2000) or 2000,
            idle_timeout=_float_env("GSA_IDLE_TIMEOUT", 900.0) or 900.0,
            run_timeout=_float_env("GSA_RUN_TIMEOUT", 1800.0) or 1800.0,
            turn_token_budget=_int_env("GSA_TURN_TOKEN_BUDGET", None),
            # 8, not 3: a child re-running its tests between edits repeats one
            # call legitimately, and a live loop detector would kill it.
            loop_strikes=_int_env("GSA_LOOP_STRIKES", 8) or 8,
            supervisor=os.environ.get("GSA_SUPERVISOR") or "auto",
            supervisor_cmd=os.environ.get("GSA_SUPERVISOR_CMD")
            or "claude -p --model sonnet",
            supervisor_timeout=_float_env("GSA_SUPERVISOR_TIMEOUT", 120.0) or 120.0,
            approval_socket=os.environ.get("GSA_APPROVAL_SOCKET")
            or str(Path(tempfile.gettempdir()) / f"gsa-approval-{os.getpid()}.sock"),
            log_level=os.environ.get("GSA_LOG_LEVEL") or "info",
            max_steps=_int_env("GSA_MAX_STEPS", 40) or 40,
            verify_timeout=_float_env("GSA_VERIFY_TIMEOUT", 300.0) or 300.0,
            chars_per_token=_float_env("GSA_CHARS_PER_TOKEN", 3.5) or 3.5,
            run_archive=_int_env("GSA_RUN_ARCHIVE", 200) or 200,
            trace=os.environ.get("GSA_TRACE"),
            compact_window=_int_env("GSA_COMPACT_WINDOW", 1_000_000) or 1_000_000,
            # Transient z.ai rate limits (HTTP 429/529) are retried with
            # exponential backoff: backoff * 2**attempt, capped at 300s.
            rate_limit_retries=_count_env("GSA_RATE_LIMIT_RETRIES", 3),
            rate_limit_backoff=_float_env("GSA_RATE_LIMIT_BACKOFF", 5.0) or 5.0,
            # z.ai fair-use throttle (code 1313) clears in minutes: 60s, then
            # doubled per attempt, capped at 900s. Plan quota (1308/1310) is
            # never retried.
            throttle_backoff=_float_env("GSA_THROTTLE_BACKOFF", 60.0) or 60.0,
        )

    @property
    def result_cap_chars(self) -> int:
        return max(200, int(self.summary_tokens * self.chars_per_token))

    def agent_home(self, agent_id: str) -> Path:
        path = self.session_root / "agents" / agent_id / "claude-home"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def hooks_config(self, agent_id: str) -> Path:
        """Per-agent Claude Code settings.json with the PreToolUse hook."""
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
                "ANTHROPIC_BASE_URL": self.base_url,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.flash_model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.compact_window),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "API_TIMEOUT_MS": "3000000",
            },
            "hooks": hooks,
            "autoCompactWindow": self.compact_window,
        }
        path.write_text(json.dumps(payload, indent=2))
        return path

    def child_env(self, agent_id: str, api_key: str | None = None) -> dict[str, str]:
        """Environment for one `claude -p` subprocess. Isolated from parent OAuth."""
        env = {k: v for k, v in os.environ.items() if v is not None}
        for leaked in (
            # The server's own copies of the key; the child gets exactly one,
            # as ANTHROPIC_AUTH_TOKEN, because Claude Code needs it.
            "GLM_API_KEY",
            "ZAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        ):
            env.pop(leaked, None)
        key = api_key or self.api_key or ""
        env.update({
            "CLAUDE_CONFIG_DIR": str(self.agent_home(agent_id)),
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.flash_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.compact_window),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "3000000",
            # Every Bash call starts in the workspace. The guard resolves
            # relative paths from there; a cwd carried over from an earlier
            # call's `cd` would make `../x` mean something else.
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
            "GSA_APPROVAL_SOCKET": self.approval_socket,
            "GSA_HOOK_TIMEOUT": str(self.supervisor_timeout + 20),
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
