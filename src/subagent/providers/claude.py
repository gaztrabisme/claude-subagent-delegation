"""The claude driver: one ``claude -p`` subprocess per turn.

It runs Claude Code against any Anthropic-compatible endpoint, so every
provider whose backend speaks that wire protocol (z.ai, DeepSeek, llama.cpp,
vLLM, oMLX) uses this driver and differs only in its config block.

A driver is stateless; per-agent state (the hooks file, the config directory)
lives under the agent's session directory and is carried by a `Session`.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Settings, log
from ..router import (
    ZAI_QUOTA_CODES,
    ZAI_THROTTLE_CODE,
    clip,
    code_evidence,
    zai_code,
    zai_reset,
)
from .base import DRIVER_CLAUDE, Process, ProviderConfig, Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..router import Refusal

GUARD = "hook"

# The child's built-in tools. Bash, Read and Edit are what it had under
# --bare; Write, Grep and Glob are file tools the guard already classifies.
CHILD_TOOLS = "Bash,Read,Edit,Write,Grep,Glob"

DEFAULT_BINARY = "claude"


# Files and directories Claude Code loads from a config directory as settings,
# instructions, hooks or code. A child must never write them: in the owner's
# directory they would run in the owner's own (unsandboxed) sessions, and in
# the per-agent home in this agent's later turns.
CLAUDE_AUTOLOAD_NAMES = (
    "settings.json", "settings.local.json", "CLAUDE.md", "CLAUDE.local.md",
    "keybindings.json", "hooks", "agents", "commands", "skills", "plugins",
    "output-styles", "statusline",
)
# More of the owner's config directory that changes what the owner's sessions
# run: shell snapshots are sourced before every Bash call, session-env is
# applied to it, ide/ lock files point sessions at an MCP endpoint, local/ holds
# a local install of the CLI, and .claude.json carries MCP servers.
CLAUDE_OWNER_CONTROL_NAMES = (
    "shell-snapshots", "session-env", "ide", "local", "downloads", ".claude.json",
)


def _owner_claude_config() -> tuple[Path, bool]:
    """The owner's Claude config directory and whether CLAUDE_CONFIG_DIR names it."""
    config = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config:
        return Path(config).expanduser(), True
    return Path.home() / ".claude", False


def _project_dir_names(cwd: Path) -> list[str]:
    """Claude Code's projects/ entry for a working directory (both spellings)."""
    spellings = dict.fromkeys((str(cwd), str(Path(cwd).resolve())))
    return [re.sub(r"[^A-Za-z0-9]", "-", spelling) for spelling in spellings]


def sandbox_write_roots(settings: Settings, agent_id: str, cfg: ProviderConfig,
                        cwd: Path) -> list[str]:
    """The claude child's write roots under its macOS profile."""
    roots = [
        str(cwd),  # Project files are the worker's deliverable.
        str(settings.session_root / "agents" / agent_id),  # Own hook and session state.
        str(settings.agent_home(agent_id)),  # Claude's isolated config directory.
        str(settings.session_root / "agents" / agent_id / "tmp"),  # Private CLI scratch.
    ]
    if cfg.extra.get("auth") == "login":
        # The login child runs on the owner's config directory. It may write
        # only its own state there, never what the owner's sessions load.
        config, _ = _owner_claude_config()
        roots.extend(
            str(config / "projects" / name)  # This workspace's session transcripts.
            for name in _project_dir_names(cwd)
        )
        roots.extend([
            str(config / "todos"),  # Per-session todo lists.
            str(config / "statsig"),  # Feature-flag cache.
            str(config / "debug"),  # Debug logs.
            str(config / ".credentials.json"),  # OAuth refresh where no Keychain is used.
        ])
    return list(dict.fromkeys(roots))


def sandbox_write_deny(settings: Settings, agent_id: str, cfg: ProviderConfig,
                       cwd: Path) -> list[str]:
    """Paths the claude child may not write, even below one of its write roots."""
    home = settings.agent_home(agent_id)
    denied = [str(home / name) for name in CLAUDE_AUTOLOAD_NAMES]
    if cfg.extra.get("auth") == "login":
        config, named = _owner_claude_config()
        denied.extend(str(config / name)
                      for name in (*CLAUDE_AUTOLOAD_NAMES, *CLAUDE_OWNER_CONTROL_NAMES))
        if not named:
            # Without CLAUDE_CONFIG_DIR the global state file sits in the home.
            denied.extend(str(Path.home() / name)
                          for name in (".claude.json", ".claude.json.backup"))
    return list(dict.fromkeys(denied))


# Claude Code 2.1.261 prints this harmless warning to stderr on every request
# (the wrapper passes the raw model name; the env maps only the aliases).
# It is never the cause of a failure, so it is stripped from every message.
MODEL_WARNING = "[claude-code:unrecognized_model]"

# Anthropic-compatible rate-limit wording. 429 = rate limit, 529 = provider
# overloaded; "quota"/"Insufficient Balance" are billing limits that behave
# the same way (transient, retryable).
_RATE_LIMIT_WORDS = (
    "rate_limit",
    "rate limit",
    "overloaded",
    "insufficient balance",
    "quota",
)
_AUTH_WORDS = ("invalid api key", "invalid_api_key", "unauthorized", "authentication")
_HTTP_RATE_RE = re.compile(r"\b(?:429|529)\b")
_HTTP_AUTH_RE = re.compile(r"\b(?:401|403)\b")


def _strip_model_warning(stderr: str) -> str:
    """Drop cosmetic unrecognized-model warning lines; keep the real cause."""
    kept = [ln for ln in (stderr or "").splitlines() if MODEL_WARNING not in ln]
    return "\n".join(kept).strip()


def _rate_limit_line(text: str) -> str:
    """The last line of `text` that actually mentions a rate limit, if any."""
    for line in reversed(text.splitlines()):
        low = line.lower()
        if _HTTP_RATE_RE.search(line) or any(w in low for w in _RATE_LIMIT_WORDS):
            return line.strip()
    return ""


def _event_error_text(event: dict | None) -> str:
    """The text a result event itself reports as an error, or "".

    Only fields the CLI fills when a turn actually failed: `error`, and
    `result` when `is_error` is set (Claude Code puts the API error there --
    status, message and retry info). Everything else in a result event --
    `subtype`, usage, cost, durations, the model's answer -- is metadata or
    payload, never a diagnostic, so it is not searched for failure keywords:
    a `subtype: success` event whose answer text merely mentions 429/quota
    does not make the run rate-limited.
    """
    if not isinstance(event, dict):
        return ""
    parts: list[str] = []
    err = event.get("error")
    if isinstance(err, str) and err.strip():
        parts.append(err)
    if event.get("is_error"):
        res = event.get("result")
        if isinstance(res, str) and res.strip():
            parts.append(res)
    return "\n".join(parts)


def exit_event(code: int, stderr: str, last_result_event: dict | None) -> dict[str, Any]:
    """The synthetic result event for a non-zero ``claude -p`` exit."""
    kind, message = classify_exit(code, stderr, last_result_event)
    event: dict[str, Any] = {
        "type": "result",
        "is_error": True,
        "result": "",
        "error": message,
        # Honest kind so the run loop can decide to retry.
        "error_kind": kind,
    }
    if kind == "rate_limited":
        found = zai_code(code_evidence(stderr, last_result_event))
        if found:
            event["zai_code"] = found
    return event


def classify_exit(code: int, stderr: str, last_result_event: dict | None) -> tuple[str, str]:
    """Honest ``(kind, message)`` for a non-zero ``claude -p`` exit.

    kind is one of:
      - ``rate_limited``: transient upstream limit (429/529/quota/overloaded)
        reported by stderr or by the last result event's own error fields;
        the run loop retries these.
      - ``auth``: credential failure (401/403/invalid key).
      - ``cli_error``: anything else. Not retried.
    """
    if code in (0, None):
        # Defensive: a clean exit is not an error; events() never classifies it.
        return "cli_error", ""
    cleaned = _strip_model_warning(stderr)
    # Evidence is stderr plus only what the event reports as an error.
    # Scanning the whole event was a latent false positive (a successful
    # answer merely mentioning 429 would have tripped the class).
    event_error = _event_error_text(last_result_event)
    haystack = f"{cleaned}\n{event_error}".lower()
    if _HTTP_RATE_RE.search(haystack) or any(w in haystack for w in _RATE_LIMIT_WORDS):
        # The actual limit text -- status, message or retry info, whichever
        # stream carried it. Never the event's `subtype`: metadata like
        # "success" is how "rate_limited: success" labels happened.
        detail = _rate_limit_line(cleaned) or _rate_limit_line(event_error)
        detail = detail or "no rate-limit detail in stderr or result event"
        evidence = code_evidence(cleaned, last_result_event)
        found = zai_code(evidence)
        if found in ZAI_QUOTA_CODES:
            reset = zai_reset(evidence)
            when = f", resets at {reset}" if reset else ", reset time not given"
            return "rate_limited", (
                f"rate_limited: z.ai plan quota exhausted ({found}{when}); "
                f"not retried: {clip(detail, 300)}"
            )
        if found == ZAI_THROTTLE_CODE:
            return "rate_limited", (
                "rate_limited: z.ai fair-use throttle (1313); run fewer children at "
                f"once: {clip(detail, 300)}"
            )
        return "rate_limited", f"rate_limited: {clip(detail, 300)}"
    tail = clip(cleaned[-2000:], 800)
    message = f"claude exited {code}: {tail}".strip()
    if _HTTP_AUTH_RE.search(haystack) or any(w in haystack for w in _AUTH_WORDS):
        return "auth", f"auth: {message}"
    return "cli_error", f"cli_error: {message}"


class ClaudeProcess(Process):
    """One ``claude -p`` subprocess. Tests replace `_spawn_claude` with a fake."""

    def events(self) -> Iterator[dict[str, Any]]:
        from ..guard.classify import protect, release

        protected_root = getattr(self, "protected_root", None)
        if protected_root is not None:
            protect(protected_root, temporary=True)
        proc = None
        stderr_thread = None
        try:
            proc = self._start()
            assert proc.stdout is not None
            # Drain stderr on a side thread while iterating stdout. Claude Code
            # writes a per-request model warning there; if we only read stdout the
            # stderr pipe fills up and the child blocks forever (pipe deadlock).
            # The full text is kept for classify_exit().
            stderr_buf: list[str] = []
            stderr_thread = self._drain_thread(proc, stderr_buf)
            last_result: dict[str, Any] | None = None
            for event in self._json_lines(proc.stdout):
                if event.get("type") == "result":
                    last_result = event
                yield event
            code = proc.wait()
            stderr_thread.join(timeout=5)
            if code not in (0, None):
                yield exit_event(code, "".join(stderr_buf), last_result)
        finally:
            if proc is not None and proc.poll() is None:
                self.kill()
            if stderr_thread is not None:
                stderr_thread.join(timeout=5)
            if protected_root is not None:
                release(protected_root)


def _spawn_claude(argv: list[str], env: dict[str, str], cwd: str) -> ClaudeProcess:
    return ClaudeProcess(argv, env, cwd)


class ClaudeProvider:
    """Runs a turn as ``claude -p`` against the provider's endpoint."""

    name = DRIVER_CLAUDE
    # Claude's stream-json pass-through event types accepted by this driver.
    # Pinning the set lets harness verification flag fixture drift without
    # mistaking passthrough for support of every future event name.
    known_event_types = frozenset({
        "system", "assistant", "user", "result", "stream_event",
        "rate_limit_event", "tool_progress", "auth_status", "prompt_suggestion",
        "task_started", "task_progress", "task_notification", "files_persisted",
    })
    # Whether a run needs the provider's API key in this server's environment.
    needs_api_key = True
    prompt_on_stdin = False

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """What stands between the child and the machine, for the trace."""
        return GUARD

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        """Write the agent's settings file; the reason claude cannot run, or None."""
        session = Session(provider=cfg.name, home=settings.agent_home(agent_id))
        binary = self.binary(cfg)
        try:
            settings.hooks_config(agent_id, cfg, cfg.model)
            probe = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10
            )
            if probe.returncode != 0:
                return (
                    probe.stderr.strip() or probe.stdout.strip()
                    or f"{binary} --version exited {probe.returncode}"
                ), session
        except FileNotFoundError:
            return f"{binary!r} is not on PATH; install Claude Code", session
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}", session
        return None, session

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
        argv = [
            self.binary(cfg),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            # Per-token stream events: the only source of a message's real
            # first-token time, which decode tok/s and TTFT are measured from.
            "--include-partial-messages",
            # No --bare: it skips hooks, and the PreToolUse hook is the guard.
            # The flags below restore the isolation --bare gave: built-in tools
            # only, no MCP servers, and no settings but the file passed with
            # --settings (a workspace .claude/settings.json could otherwise
            # carry the child's own hooks). CLAUDE_CONFIG_DIR is per-agent.
            "--tools",
            CHILD_TOOLS,
            "--strict-mcp-config",
            "--setting-sources",
            "",
            # Permission prompts are off; the PreToolUse hook is the gate.
            "--dangerously-skip-permissions",
            "--max-turns",
            str(cfg.max_steps),
            "--model",
            model or "",
            "--settings",
            str(settings.hooks_config(agent_id, cfg, model)),
        ]
        claude_md = cwd / "CLAUDE.md"
        if claude_md.is_file():
            argv.extend(["--append-system-prompt-file", str(claude_md)])
        if session.session_id:
            argv.extend(["--resume", session.session_id])
        return argv

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session,
        model: str | None = None,
    ) -> dict[str, str]:
        return settings.child_env(agent_id, cfg, model)

    def translator(self, session: Session) -> None:
        """Claude Code already prints stream-json; nothing to translate."""
        return None

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> ClaudeProcess:
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        process = _spawn_claude(argv, env, str(cwd))
        if cfg.extra.get("auth") == "login":
            # The dir may be unset: the child must then read the login store the
            # way the CLI does. Protect it from the child's writes either way.
            root = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
            process.protected_root = Path(root) if root else Path.home() / ".claude"
        return process

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """The before-work refusal in a turn's events, or None."""
        from .. import router

        return router.classify_refusal(cfg.vendor, events)


CLAUDE_PROVIDER = ClaudeProvider()

__all__ = [
    "CHILD_TOOLS",
    "CLAUDE_PROVIDER",
    "MODEL_WARNING",
    "ClaudeProcess",
    "ClaudeProvider",
    "classify_exit",
    "exit_event",
    "log",
]
