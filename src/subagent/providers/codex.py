"""The codex driver: one ``codex exec --json`` subprocess per turn.

Parallel to the claude driver. An Agent uses `CODEX_PROVIDER` for any hop on a
provider whose driver is codex, and keeps its own run loop (queue,
verification, distillation, trips, trace); the driver supplies only the boot
check, the spawn (argv, environment, event stream) and the refusal reading.
Codex's JSONL events are translated into the claude stream-json shapes
Agent._ingest already reads, so a codex run fills the same Run fields a claude
run does.

Two guards stand between a codex child and the machine: Codex's own
`workspace-write` sandbox, and the same PreToolUse hook the claude driver
installs, which asks this server's supervisor over SUBAGENT_APPROVAL_SOCKET.

The child runs with CODEX_HOME set to a per-agent directory holding a symlink
to the user's auth.json, a config.toml this module writes (no hooks, no MCP
servers, no notify), and a hooks.json with only the guard hook.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings, log
from ..router import Refusal, codex_reset
from .base import DRIVER_CODEX, Process, ProviderConfig, Session

GUARD = "sandbox+hook"
GUARD_NO_HOOK = "sandbox"

DEFAULT_BINARY = "codex"

REFUSAL_USAGE_LIMIT = "codex_usage_limit"
KIND_CONTEXT_FULL = "context_full"
KIND_USAGE_LIMIT_AFTER_WORK = "usage_limit_after_work"
KIND_CLI_ERROR = "cli_error"

# Item types that are the child acting on the machine: they count as steps,
# and they are the only items that count as work. An agent_message is the
# child talking, which a refusal turn also does.
STEP_ITEMS = frozenset({"command_execution", "file_change"})

_USAGE_LIMIT_RE = re.compile(r"usage limit", re.IGNORECASE)
_CONTEXT_RE = re.compile(r"context window", re.IGNORECASE)


def source_codex_home() -> Path:
    """The user's own CODEX_HOME, where auth.json and config.toml live."""
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


# --- refusal classification ----------------------------------------------------


def parse_reset(message: str, now: datetime | None = None) -> datetime | None:
    """The local time a usage-limit message says to try again at (router.codex_reset)."""
    return codex_reset(message, now)


def _failure_messages(events: Iterable[dict[str, Any]]) -> list[str]:
    found = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            found.append(event["message"])
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                found.append(error["message"])
            elif isinstance(error, str):
                found.append(error)
    return found


def _did_work(events: Iterable[dict[str, Any]]) -> bool:
    """Whether the child acted on the machine or produced output tokens.

    A message item is not work: a child whose first turn answered a usage
    limit emits one, with no tool call and no usage, and that run must still
    be a refusal the router can move to another provider.
    """
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind.startswith("item."):
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") in STEP_ITEMS:
                return True
        elif kind == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if int(usage.get("output_tokens") or 0) > 0:
                return True
    return False


def codex_refusal(
    events: Iterable[dict[str, Any]], now: datetime | None = None
) -> tuple[str, str, datetime | None] | None:
    """`(code, message, reset_at)` when Codex refused the turn, else None.

    A refusal is a usage-limit error before the child did any work: the
    provider said no and nothing was done, so another may take the task.
    A usage limit hit after work, context-window exhaustion and every other
    failure are failures of the run, not refusals.
    """
    events = list(events)
    if _did_work(events):
        return None
    for message in _failure_messages(events):
        if _USAGE_LIMIT_RE.search(message):
            return REFUSAL_USAGE_LIMIT, message, parse_reset(message, now)
    return None


def failure_kind(events: Iterable[dict[str, Any]]) -> str:
    """The error_kind for a failed codex turn."""
    events = list(events)
    if codex_refusal(events) is not None:
        return REFUSAL_USAGE_LIMIT
    messages = _failure_messages(events)
    if any(_USAGE_LIMIT_RE.search(m) for m in messages):
        return KIND_USAGE_LIMIT_AFTER_WORK
    if any(_CONTEXT_RE.search(m) for m in messages):
        return KIND_CONTEXT_FULL
    return KIND_CLI_ERROR


# --- event translation ---------------------------------------------------------


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    """Codex usage in the claude keys Usage.add reads.

    Codex's input_tokens includes the cached part; Claude's does not. Input
    is reported uncached so Usage.total does not count cache reads twice.
    """
    total_in = int(raw.get("input_tokens") or 0)
    cached = int(raw.get("cached_input_tokens") or 0)
    return {
        "input_tokens": max(0, total_in - cached),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": int(raw.get("cache_write_input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "reasoning_output_tokens": int(raw.get("reasoning_output_tokens") or 0),
    }


def _tool_use(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if item.get("type") == "command_execution":
        return "Bash", {"command": item.get("command")}
    return "Edit", {"changes": item.get("changes")}


class Translator:
    """Codex JSONL events in, claude stream-json events out."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.seen: list[dict[str, Any]] = []
        self.last_message = ""
        self.terminal = False

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.seen.append(event)
        kind = event.get("type")
        if kind == "thread.started":
            thread = event.get("thread_id")
            if isinstance(thread, str) and thread:
                self.thread_id = thread
            return [{"type": "system", "subtype": "init", "session_id": self.thread_id}]
        if kind == "item.completed":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            item_type = item.get("type")
            if item_type == "agent_message":
                text = str(item.get("text") or "")
                if text.strip():
                    self.last_message = text
                return [self._assistant([{"type": "text", "text": text}])]
            if item_type in STEP_ITEMS:
                name, args = _tool_use(item)
                return [self._assistant([{"type": "tool_use", "name": name, "input": args}])]
            return [{"type": f"codex/{item_type or 'item'}"}]
        if kind in ("item.started", "item.updated"):
            return []  # counted once, at item.completed
        if kind == "turn.completed":
            self.terminal = True
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            return [{
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": self.last_message,
                "session_id": self.thread_id,
                "usage": _usage(usage),
                "num_turns": 1,
            }]
        if kind == "turn.failed":
            self.terminal = True
            return [self.failure()]
        if kind == "error":
            # Codex also reports retried stream hiccups as `error`; only
            # turn.failed (or the process exit) ends the turn.
            return [{"type": "codex/error"}]
        return [{"type": f"codex/{kind}"}] if kind else []

    def failure(self, fallback: str = "") -> dict[str, Any]:
        messages = _failure_messages(self.seen)
        message = messages[-1] if messages else fallback or "codex reported an error"
        event: dict[str, Any] = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": self.last_message,
            "error": message,
            "error_kind": failure_kind(self.seen),
            "session_id": self.thread_id,
            "num_turns": 1,
        }
        refusal = codex_refusal(self.seen)
        if refusal is not None:
            event["refusal"] = refusal[0]
            event["reset_at"] = refusal[2].isoformat() if refusal[2] else None
        return event

    def _assistant(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "assistant", "session_id": self.thread_id, "message": {"content": content}}


class CodexProcess(Process):
    """One ``codex exec`` subprocess: prompt on stdin, JSONL on stdout."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, prompt: str):
        super().__init__(argv, env, cwd, prompt)
        self.raw: list[dict[str, Any]] = []

    def refusal(self) -> tuple[str, str, datetime | None] | None:
        return codex_refusal(self.raw)

    def events(self) -> Iterator[dict[str, Any]]:
        proc = self._start(stdin=True)
        assert proc.stdin is not None and proc.stdout is not None
        stderr_buf: list[str] = []
        stderr_thread = self._drain_thread(proc, stderr_buf)
        try:
            try:
                proc.stdin.write(self.prompt or "")
                proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                log.debug("codex stdin closed early: %s", exc)
            translator = Translator()
            for event in self._json_lines(proc.stdout):
                self.raw.append(event)
                yield from translator.feed(event)
            code = proc.wait()
            stderr_thread.join(timeout=5)
            if not translator.terminal:
                # No turn.completed / turn.failed: the turn did not finish,
                # whatever the exit code says.
                tail = "".join(stderr_buf).strip()[-800:]
                yield translator.failure(
                    f"codex exited {code} without finishing the turn: {tail}".strip()
                )
        finally:
            if proc.poll() is None:
                self.kill()
            stderr_thread.join(timeout=5)


def _spawn_codex(argv: list[str], env: dict[str, str], cwd: str, prompt: str) -> CodexProcess:
    return CodexProcess(argv, env, cwd, prompt)


# --- CODEX_HOME -----------------------------------------------------------------


def _toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def _user_defaults(home: Path) -> dict[str, str]:
    """model and model_reasoning_effort from the user's config.toml, if set."""
    try:
        import tomllib

        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return {}
    return {
        key: data[key]
        for key in ("model", "model_reasoning_effort")
        if isinstance(data.get(key), str) and data[key].strip()
    }


def prepare_home(
    settings: Settings, agent_id: str, cfg: ProviderConfig, source: Path | None = None
) -> Path:
    """Write the per-agent CODEX_HOME and return it.

    auth.json is a symlink to the user's (never copied, never read here);
    config.toml carries only model and reasoning effort, and only when the
    provider names no model; hooks.json holds the guard hook and nothing else.
    """
    source = source or source_codex_home()
    home = settings.session_root / "agents" / agent_id / "codex-home"
    home.mkdir(parents=True, exist_ok=True)

    auth = home / "auth.json"
    if auth.is_symlink() or auth.exists():
        auth.unlink()
    auth.symlink_to(source / "auth.json")

    lines = ["# Written by subagent for one agent. Hooks live in hooks.json."]
    if cfg.model is None:
        for key, value in _user_defaults(source).items():
            lines.append(f"{key} = {_toml_string(value)}")
    lines.append('approval_policy = "never"')
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if settings.supervisor == "off":
        hooks: dict[str, Any] = {}
    else:
        hooks = {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": settings.guard_hook_command(
                                agent_id, "--dialect", "codex"
                            ),
                            "timeout": int(settings.supervisor_timeout + 30),
                        }
                    ]
                }
            ]
        }
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
    return home


def codex_argv(
    workspace: Path, model: str | None, resume: str | None, binary: str | None = None
) -> list[str]:
    """argv for one turn. The prompt goes on stdin (the trailing `-`)."""
    argv = [
        binary or DEFAULT_BINARY,
        "exec",
        "--json",
        "-C",
        str(workspace),
        "-s",
        "workspace-write",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--dangerously-bypass-hook-trust",
    ]
    if model:
        argv.extend(["-m", model])
    if resume:
        argv.extend(["resume", resume])
    argv.append("-")
    return argv


def codex_env(settings: Settings, agent_id: str, home: Path) -> dict[str, str]:
    """Environment for one codex child, with an isolated per-agent CODEX_HOME."""
    env = settings.base_child_env()
    env.update({
        "CODEX_HOME": str(home),
        "SUBAGENT_APPROVAL_SOCKET": settings.approval_socket,
        "SUBAGENT_HOOK_TIMEOUT": str(settings.supervisor_timeout + 20),
    })
    return env


class CodexProvider:
    """Runs a turn as ``codex exec --json``. The session id is Codex's thread_id."""

    name = DRIVER_CODEX
    needs_api_key = False  # the Codex CLI logs in with its own auth.json
    prompt_on_stdin = True

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        return GUARD_NO_HOOK if settings.supervisor == "off" else GUARD

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        """Write the agent's CODEX_HOME; the reason codex cannot run, or None."""
        session = Session(provider=cfg.name)
        try:
            session.home = prepare_home(settings, agent_id, cfg)
            binary = self.binary(cfg)
            if shutil.which(binary) is None:
                return f"{binary!r} is not on PATH; install the Codex CLI", session
            if not (source_codex_home() / "auth.json").exists():
                return (
                    f"no Codex login at {source_codex_home() / 'auth.json'}; "
                    "run `codex login`"
                ), session
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
        return codex_argv(cwd, model or None, session.session_id, self.binary(cfg))

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session,
        model: str | None = None,
    ) -> dict[str, str]:
        home = session.home or prepare_home(settings, agent_id, cfg)
        session.home = home
        return codex_env(settings, agent_id, home)

    def translator(self, session: Session) -> Translator:
        """Codex prints its own event shapes; CodexProcess feeds this one."""
        return Translator()

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> CodexProcess:
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        return _spawn_codex(argv, env, str(cwd), prompt)

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """The before-work refusal codex_refusal found, from the translated events.

        Translator.failure runs codex_refusal on the raw Codex events and
        stamps its code and reset time on the terminal result event.
        """
        terminal = next(
            (e for e in reversed(events) if isinstance(e, dict) and e.get("type") == "result"),
            None,
        )
        if not isinstance(terminal, dict) or not terminal.get("refusal"):
            return None
        raw = terminal.get("reset_at")
        try:
            reset_at = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            reset_at = None
        return Refusal(str(terminal["refusal"]), str(terminal.get("error") or ""), reset_at)


CODEX_PROVIDER = CodexProvider()
CodexDriver = CodexProvider  # the name the driver had before providers landed


__all__ = [
    "CODEX_PROVIDER",
    "CodexProcess",
    "CodexProvider",
    "GUARD",
    "REFUSAL_USAGE_LIMIT",
    "Translator",
    "codex_argv",
    "codex_env",
    "codex_refusal",
    "failure_kind",
    "parse_reset",
    "prepare_home",
]
