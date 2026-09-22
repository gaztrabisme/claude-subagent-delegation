"""The copilot driver: one ``copilot -p`` subprocess per turn.

Copilot's JSON event stream (``assistant.turn_start``, ``assistant.message``,
``tool.execution_*``, ``session.usage_checkpoint``) is not Claude stream-json,
so a `Translator` rewrites it into the shapes `Agent._ingest` already reads,
exactly like the codex driver does. The one native concept that has no claude
equivalent -- AI credits, reported per session in nano-AIU -- rides on the
synthetic `result` event's `usage["credits"]`.

The Copilot CLI has no PreToolUse hook, so this driver installs no guard: the
child runs with ``--allow-all-tools``. That is only acceptable when the server
itself said so. ``boot()`` refuses for a plain MCP delegation unless
``[guard].allow_unguarded`` is true; the delegate loop (which does its own
test-file and checkpoint protection around the child) lifts the gate by setting
the provider's ``extra["loop"]`` flag.

The session id is chosen up front, not learned from the stream: ``boot()``
mints a uuid and every turn, first or resume, passes it as ``--session-id``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ..config import Settings, log
from ..router import COPILOT_MODEL_UNAVAILABLE, Refusal, clip
from .base import DRIVER_COPILOT, Process, ProviderConfig, Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

GUARD = "none"

DEFAULT_BINARY = "copilot"

# What fake_copilot.py (and the real CLI) print when --model names an account
# this build cannot use: Error: Model "gpt-5" from --model flag is not available.
_MODEL_UNAVAILABLE_RE = re.compile(r'model\s+"[^"]*"[^\n]*not available', re.IGNORECASE)

# Token fields a session.usage_checkpoint may carry, in the keys Usage.add
# reads. Credits are separate; the checkpoint reports them in nano-AIU.
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
)


def _extra_list(cfg: ProviderConfig, key: str) -> list[str]:
    """An extra key given as a string or a list of strings, as argv tail."""
    raw = cfg.extra.get(key) or ()
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    return [str(v) for v in raw]


class Translator:
    """Copilot JSON events in, claude stream-json events out.

    One assistant message, tool call or tool result maps to one claude event;
    the `session.usage_checkpoint` is remembered, not emitted, and folded into
    the synthetic `result` `finish()` writes when the process exits.
    """

    def __init__(self, session_id: str | None):
        self.session_id = session_id
        self.seen: list[dict[str, Any]] = []
        self.last_message = ""
        self.credits: float | None = None
        self.usage_tokens: dict[str, int] = {}
        self.saw_error = False
        self.error_texts: list[str] = []
        self.raw_lines: list[str] = []
        self._emitted_init = False

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.seen.append(event)
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        out: list[dict[str, Any]] = []
        if not self._emitted_init:
            out.append(self._init())
            self._emitted_init = True
        if kind == "assistant.turn_start":
            return out
        if kind == "assistant.message":
            text = str(data.get("content") or "")
            if text.strip():
                self.last_message = text.strip()
            out.append(self._assistant([{"type": "text", "text": text}]))
            return out
        if kind == "tool.execution_start":
            out.append(self._assistant([{
                "type": "tool_use",
                "id": data.get("toolCallId"),
                "name": data.get("toolName"),
                "input": data.get("arguments"),
            }]))
            return out
        if kind == "tool.execution_complete":
            out.append(self._tool_result(data))
            return out
        if kind == "session.usage_checkpoint":
            if isinstance(data.get("totalNanoAiu"), (int, float)):
                self.credits = float(data["totalNanoAiu"]) / 1e9
            for key in _TOKEN_KEYS:
                if isinstance(data.get(key), (int, float)):
                    self.usage_tokens[key] = int(data[key])
            return out
        if "error" in kind:
            self.saw_error = True
            text = data.get("message") or data.get("error") or json.dumps(data)
            if text:
                self.error_texts.append(str(text))
            return out
        return out

    def finish(self, exit_code: int | None, extra_text: str = "") -> dict[str, Any]:
        """The synthetic result for the turn, emitted when the process exits.

        `extra_text` is the child's stderr and non-JSON stdout lines -- where
        the CLI reports a model it cannot use -- so a refusal can be read from
        this event without the driver seeing the raw process.
        """
        usage: dict[str, Any] = dict(self.usage_tokens)
        usage.setdefault("input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        if self.credits is not None:
            usage["credits"] = self.credits
        is_error = exit_code not in (0, None) or self.saw_error
        event: dict[str, Any] = {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "result": self.last_message,
            "session_id": self.session_id,
            "usage": usage,
            "num_turns": 1,
        }
        error = "\n".join([*self.error_texts, extra_text]).strip()
        if is_error:
            event["error"] = error or "copilot reported an error"
        return event

    def _init(self) -> dict[str, Any]:
        return {"type": "system", "subtype": "init", "session_id": self.session_id}

    def _assistant(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "assistant",
            "session_id": self.session_id,
            "message": {"content": content},
        }

    def _tool_result(self, data: dict[str, Any]) -> dict[str, Any]:
        success = bool(data.get("success"))
        content = data.get("result") if success else (data.get("error") or data.get("result"))
        if not isinstance(content, str):
            content = json.dumps(content) if content else ""
        return {
            "type": "user",
            "session_id": self.session_id,
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": data.get("toolCallId"),
                "content": content,
                "is_error": not success,
            }]},
        }


class CopilotProcess(Process):
    """One ``copilot -p`` subprocess: prompt as an argv flag, JSON on stdout."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, session_id: str):
        super().__init__(argv, env, cwd)
        self.session_id = session_id
        self.raw: list[dict[str, Any]] = []

    def events(self) -> Iterator[dict[str, Any]]:
        proc = self._start()
        assert proc.stdout is not None
        stderr_buf: list[str] = []
        stderr_thread = self._drain_thread(proc, stderr_buf)
        translator = Translator(self.session_id)
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Raw text (e.g. "Model ... is not available") is where the
                    # CLI reports the only refusal this driver reads.
                    translator.raw_lines.append(line)
                    continue
                if not isinstance(event, dict):
                    translator.raw_lines.append(line)
                    continue
                self.raw.append(event)
                yield from translator.feed(event)
            code = proc.wait()
            stderr_thread.join(timeout=5)
            yield translator.finish(
                code, "\n".join([*translator.raw_lines, "".join(stderr_buf)])
            )
        finally:
            if proc.poll() is None:
                self.kill()
            stderr_thread.join(timeout=5)


def _spawn_copilot(
    argv: list[str], env: dict[str, str], cwd: str, session_id: str
) -> CopilotProcess:
    return CopilotProcess(argv, env, cwd, session_id)


class CopilotProvider:
    """Runs a turn as ``copilot -p``. The session id is a minted uuid."""

    name = DRIVER_COPILOT
    needs_api_key = False  # the Copilot CLI logs in with its own credentials
    prompt_on_stdin = False

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """The Copilot CLI offers no PreToolUse hook; nothing guards the child."""
        return GUARD

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        """Mint the session id; the reason copilot cannot run, or None.

        A plain MCP delegation may not run an unguarded child unless
        ``[guard].allow_unguarded`` is true. The delegate loop, which does its
        own protection around the child, lifts this by setting the provider's
        ``extra["loop"]`` flag.
        """
        session = Session(provider=cfg.name)
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        if not settings.allow_unguarded and not cfg.extra.get("loop"):
            return (
                "copilot runs without a guard hook (--allow-all-tools); set "
                "[guard].allow_unguarded = true to use it from the MCP tools "
                "(the delegate loop lifts this itself)"
            ), session
        binary = self.binary(cfg)
        if shutil.which(binary) is None:
            return f"{binary!r} is not on PATH; install the Copilot CLI", session
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
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        argv = [
            self.binary(cfg),
            "-p",
            prompt,
            "--allow-all-tools",
            "--output-format",
            "json",
            "-C",
            str(cwd),
            "--session-id",
            session.session_id,
        ]
        if not cfg.extra.get("builtin_mcps"):
            argv.append("--disable-builtin-mcps")
        if model:
            argv.extend(["--model", model])
        argv.extend(_extra_list(cfg, "extra_args"))
        return argv

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session,
        model: str | None = None,
    ) -> dict[str, str]:
        """The parent's environment, with this server's own keys removed."""
        env = {k: v for k, v in os.environ.items() if v is not None}
        for leaked in settings.leaked_keys:
            env.pop(leaked, None)
        return env

    def translator(self, session: Session) -> Translator:
        """Copilot prints its own event shapes; CopilotProcess feeds this one."""
        return Translator(session.session_id)

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> CopilotProcess:
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        return _spawn_copilot(argv, env, str(cwd), session.session_id)

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """A model the account cannot use, before any work was done.

        The CLI reports it as a raw line the process folded into the synthetic
        result's `error`. Never closes the lane: the same provider with another
        model may still run.
        """
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            text = str(event.get("error") or "")
            if text and _MODEL_UNAVAILABLE_RE.search(text):
                return Refusal(COPILOT_MODEL_UNAVAILABLE, clip(text, 300), None)
        return None


COPILOT_PROVIDER = CopilotProvider()
CopilotDriver = CopilotProvider  # the name the driver had before providers landed

__all__ = [
    "COPILOT_PROVIDER",
    "CopilotProcess",
    "CopilotProvider",
    "GUARD",
    "Translator",
]
