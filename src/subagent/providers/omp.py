"""The omp driver: one ``omp -p --mode json`` subprocess per turn.

Oh My Pi prints one JSON object per line: a session header (``type:
"session"``, ignored), then ``agent_start`` / ``turn_start`` /
``message_start`` / ``message_update`` / ``message_end`` /
``tool_execution_start`` / ``tool_execution_update`` / ``tool_execution_end``
/ ``turn_end`` / ``agent_end`` events (the shapes in
``@oh-my-pi/pi-agent-core/src/types.ts``, ``AgentEvent``, as
``print-mode.ts`` trims them: a ``message_update`` carries only its delta).
That stream is not Claude stream-json, so a `Translator` rewrites it into the
shapes `Agent._ingest` already reads, exactly like the copilot driver does.
The recorded streams in ``tests/fixtures/omp`` are the reference.

omp reads a piped stdin to EOF before it starts, so the child gets /dev/null
as stdin: inheriting the server's would hand it the MCP transport.

omp's ``--hook`` loads a TypeScript hook. This driver installs the packaged
``guard/omp_hook.ts`` on every run; it translates omp's tool events to the
Claude-shaped JSON consumed by ``guard/approval_hook.py``. The per-agent
``models.yml`` and settings overlay live in the session root, outside the
workspace; the configured API key is passed only through the child environment.
When ``send_sampling`` is false, the generated model omits the output-token cap and
sets omp's sampling defaults to provider-default values with thinking disabled. In
18.0.11, Qwen requests still include ``preserve_thinking: true`` in the body; the
installed config schema exposes no switch for that compatibility field.

Sessions: omp runs with ``--no-session``, so nothing is persisted between
turns and ``--resume`` is off the table. A resume turn instead re-issues the
prompt with a short "Continue the previous task in this directory." line
prepended, plus the previous turn's final assistant text as context when the
`Session` kept it (the process writes it back into ``session.data`` when the
turn ends). The session id on the events is a minted uuid that only tags the
records; omp itself never sees it.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..router import COPILOT_MODEL_UNAVAILABLE, Refusal, clip
from .base import DRIVER_OMP, Process, ProviderConfig, Session
from .omp_config import OMP_HOOK, write_agent_config
from .omp_config import child_env as omp_child_env

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

GUARD = "hook"

DEFAULT_BINARY = "omp"

_NO_SAMPLING_ARGS = frozenset({
    "--thinking", "--external-thinking", "--temperature", "--top-p", "--top-k",
    "--min-p", "--typical-p", "--presence-penalty", "--frequency-penalty",
    "--repetition-penalty", "--repeat-penalty", "--seed", "--max-tokens",
    "--max-output-tokens",
})

# The line that marks a resume turn for a backend with no saved session to
# reopen (omp runs --no-session, so its own --resume is disabled).
RESUME_PREFIX = "Continue the previous task in this directory."

# What omp prints when the model cannot be reached, as recorded in
# tests/fixtures/omp: a server that is down stops the assistant message with
# errorMessage "Unable to connect. Is the computer able to access the url?"
# (no ECONNREFUSED in sight, though a raw one is matched too); an unknown
# --model exits 1 with `Model "x" not found` on stderr. Plus an unconfigured
# provider and the auth/throttle statuses. The closest code the router already
# defines is COPILOT_MODEL_UNAVAILABLE ("a model this account cannot use"); it
# never closes the lane.
_REFUSAL_RE = re.compile(
    r"unable to connect|connection refused|econnrefused"
    r"|no model|model\b[^\n]*\bnot found"
    r"|provider\s+\S+\s+not configured|\b401\b|\b429\b",
    re.IGNORECASE,
)

# omp's usage keys (on each assistant message in agent_end) onto the keys
# Usage.add reads.
_USAGE_KEY_MAP = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheRead": "cache_read_input_tokens",
    "cacheWrite": "cache_creation_input_tokens",
}


def _extra_list(cfg: ProviderConfig, key: str) -> list[str]:
    """An extra key given as a string or a list of strings, as argv tail."""
    raw = cfg.extra.get(key) or ()
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    return [str(v) for v in raw]


def _extra_arg_conflicts(cfg: ProviderConfig) -> list[str]:
    """CLI overrides that could replace the endpoint, hook, or sampling policy."""
    args = _extra_list(cfg, "extra_args")
    forbidden = {
        "--model", "--provider", "--api-key", "--config", "--profile",
        "--hook", "--extension", "-e", "--no-extensions", "--auto-approve",
        "--approval-mode",
    }
    if not cfg.send_sampling:
        forbidden.update(_NO_SAMPLING_ARGS)
    return [arg for arg in args if arg.split("=", 1)[0] in forbidden]


class Translator:
    """omp JSON events in, claude stream-json events out.

    Text/thinking deltas and tool starts map to claude `assistant` events,
    tool ends to `user`/`tool_result` events; each tool execution counts as
    one step. Usage is read from one place -- the assistant messages
    `agent_end` carries (each is one model call; `message_end` repeats the
    same numbers) -- remembered, not emitted, and folded into the synthetic
    `result` `finish()` writes when the process exits. The session header
    (`type: "session"`) and every unlisted type are ignored.
    """

    def __init__(self, session_id: str | None):
        self.session_id = session_id
        self.seen: list[dict[str, Any]] = []
        self.last_message = ""
        self.usage_tokens: dict[str, int] = {}
        self.turns = 0
        self.saw_error = False
        self.error_texts: list[str] = []
        self.raw_lines: list[str] = []
        self._emitted_init = False

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.seen.append(event)
        kind = event.get("type")
        out: list[dict[str, Any]] = []
        if not self._emitted_init:
            out.append(self._init())
            self._emitted_init = True
        if kind == "turn_start":
            # One turn = one assistant message (+ its tool calls). The text
            # restarts so `last_message` ends as the final turn's, the way a
            # claude `result` carries the last assistant message.
            self.turns += 1
            self.last_message = ""
            return out
        if kind == "message_end":
            self._note_message_error(event.get("message"))
            return out
        if kind in ("agent_start", "message_start", "tool_execution_update", "turn_end"):
            # turn_end repeats the message and the tool results that
            # message_update / tool_execution_end already streamed: ignored.
            return out
        if kind == "message_update":
            ame = event.get("assistantMessageEvent")
            ame = ame if isinstance(ame, dict) else {}
            sub = ame.get("type")
            if sub == "text_delta":
                delta = str(ame.get("delta") or "")
                if delta:
                    self.last_message += delta
                    out.append(self._assistant([{"type": "text", "text": delta}]))
            elif sub == "thinking_delta":
                delta = str(ame.get("delta") or "")
                if delta:
                    out.append(self._assistant([{"type": "thinking", "thinking": delta}]))
            elif sub == "error":
                self.saw_error = True
                text = ame.get("reason") or ame.get("error") or json.dumps(ame, default=str)
                self.error_texts.append(str(text))
            # "done" and any other sub-event: nothing to emit.
            return out
        if kind == "tool_execution_start":
            out.append(self._assistant([{
                "type": "tool_use",
                "id": event.get("toolCallId"),
                "name": event.get("toolName"),
                "input": event.get("args"),
            }]))
            return out
        if kind == "tool_execution_end":
            out.append(self._tool_result(event))
            return out
        if kind == "agent_end":
            self._collect_usage(event)
            return out
        # Session header (`type: "session"`), auto_retry_start (its
        # errorMessage already arrived on message_end) and unknown types:
        # ignored.
        return out

    def finish(self, exit_code: int | None, extra_text: str = "") -> dict[str, Any]:
        """The synthetic result for the turn, emitted when the process exits.

        `extra_text` is the child's stderr and non-JSON stdout lines -- where
        a refused connection or a missing model is reported -- so a refusal
        can be read from this event without the driver seeing the raw process.
        """
        usage: dict[str, Any] = dict(self.usage_tokens)
        usage.setdefault("input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        is_error = exit_code not in (0, None) or self.saw_error
        event: dict[str, Any] = {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "result": self.last_message.strip(),
            "session_id": self.session_id,
            "usage": usage,
            "num_turns": max(self.turns, 1),
        }
        error = "\n".join([*self.error_texts, extra_text]).strip()
        if is_error:
            event["error"] = error or "omp reported an error"
        return event

    def _collect_usage(self, event: dict[str, Any]) -> None:
        """Sum the `usage` of every assistant message `agent_end` carries.

        That is the one place usage is read from: omp puts it on each
        assistant message (one per model call), and `message_end` / `turn_end`
        repeat the same object, so reading those too would double-count.
        """
        for msg in event.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            for src, dst in _USAGE_KEY_MAP.items():
                value = usage.get(src)
                if isinstance(value, (int, float)):
                    self.usage_tokens[dst] = self.usage_tokens.get(dst, 0) + int(value)

    def _note_message_error(self, message: Any) -> None:
        """An assistant message that stopped on `error` carries the reason in
        `errorMessage` (a refused connection, a missing model): fold it in."""
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        if message.get("stopReason") != "error":
            return
        self.saw_error = True
        text = message.get("errorMessage") or "omp: request error"
        if str(text) not in self.error_texts:
            self.error_texts.append(str(text))

    def _init(self) -> dict[str, Any]:
        return {"type": "system", "subtype": "init", "session_id": self.session_id}

    def _assistant(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "assistant",
            "session_id": self.session_id,
            "message": {"content": content},
        }

    def _tool_result(self, data: dict[str, Any]) -> dict[str, Any]:
        """`result` is a tool's `{content: [{type: "text", text}...], details}`;
        the text blocks become the tool_result's content, anything else is
        JSON-dumped."""
        is_error = bool(data.get("isError"))
        result = data.get("result")
        blocks = result.get("content") if isinstance(result, dict) else None
        if isinstance(result, str):
            content = result
        elif isinstance(blocks, list):
            content = "\n".join(
                str(b.get("text") or "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            content = json.dumps(result, default=str) if result else ""
        return {
            "type": "user",
            "session_id": self.session_id,
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": data.get("toolCallId"),
                "content": content,
                "is_error": is_error,
            }]},
        }


class OmpProcess(Process):
    """One ``omp -p --mode json`` subprocess: prompt as an argv tail.

    Holds the `Session` (not just its id) so the turn's final assistant text
    can be written back into ``session.data["last_message"]`` for the next
    turn's argv to pick up -- omp persists nothing itself.
    """

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, session: Session):
        super().__init__(argv, env, cwd)
        self.session = session

    def events(self) -> Iterator[dict[str, Any]]:
        # omp reads a piped stdin to EOF before starting; never hand it ours.
        proc = self._start(closed_stdin=True)
        assert proc.stdout is not None
        stderr_buf: list[str] = []
        stderr_thread = self._drain_thread(proc, stderr_buf)
        translator = Translator(self.session.session_id)
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Raw text (e.g. "connection refused") is where the CLI
                    # reports the only refusals this driver reads.
                    translator.raw_lines.append(line)
                    continue
                if not isinstance(event, dict):
                    translator.raw_lines.append(line)
                    continue
                yield from translator.feed(event)
            code = proc.wait()
            stderr_thread.join(timeout=5)
            if translator.last_message:
                self.session.data["last_message"] = translator.last_message
            yield translator.finish(
                code, "\n".join([*translator.raw_lines, "".join(stderr_buf)])
            )
        finally:
            if proc.poll() is None:
                self.kill()
            stderr_thread.join(timeout=5)


def _spawn_omp(argv: list[str], env: dict[str, str], cwd: str, session: Session) -> OmpProcess:
    return OmpProcess(argv, env, cwd, session)


class OmpProvider:
    """Runs ``omp -p --mode json`` with isolated config and the approval hook."""

    name = DRIVER_OMP
    # Local OpenAI-compatible endpoints may be keyless. When a key is named,
    # the generated models.yml points at that environment variable.
    needs_api_key = False
    prompt_on_stdin = False

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """The packaged omp tool_call hook reaches the server-side classifier."""
        return GUARD

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        """Write this agent's omp configuration and verify the binary."""
        session = Session(provider=cfg.name)
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        session.home = settings.session_root / "agents" / agent_id / "omp-agent"
        try:
            write_agent_config(settings, agent_id, cfg, cfg.model)
        except (OSError, ValueError) as exc:
            return f"omp configuration failed: {type(exc).__name__}: {exc}", session
        if not OMP_HOOK.is_file():
            return f"omp approval hook is missing: {OMP_HOOK}", session
        conflicts = _extra_arg_conflicts(cfg)
        if conflicts:
            message = "omp extra_args conflict with agent configuration or hook"
            if not cfg.send_sampling and any(
                arg.split("=", 1)[0] in _NO_SAMPLING_ARGS for arg in conflicts
            ):
                message = "omp send_sampling = false conflicts with extra_args"
            return f"{message}: {conflicts}", session
        binary = self.binary(cfg)
        if shutil.which(binary) is None:
            return f"{binary!r} is not on PATH; install Oh My Pi (omp)", session
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
        """argv for one turn. First turn: the prompt verbatim. Resume turn
        (the Session kept a previous turn's text -- omp runs --no-session, so
        there is nothing to --resume): prepend the continue line and, when
        the Session holds it, the previous turn's final assistant text.
        """
        resume = bool(session.data.get("last_message"))
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        if resume:
            previous = str(session.data.get("last_message") or "").strip()
            if previous:
                prompt = f"{RESUME_PREFIX}\n\n{previous}\n\n{prompt}"
            else:
                prompt = f"{RESUME_PREFIX}\n\n{prompt}"
        argv = [
            self.binary(cfg),
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--cwd",
            str(cwd),
            "--hook",
            str(OMP_HOOK),
            "--config",
            str(
                (session.home or settings.session_root / "agents" / agent_id / "omp-agent")
                / "config.yml"
            ),
        ]
        if model:
            reference = model if model.startswith(f"{cfg.name}/") else f"{cfg.name}/{model}"
            argv.extend(["--model", reference])
        if not cfg.send_sampling:
            argv.extend(["--thinking", "off"])
        argv.extend(_extra_list(cfg, "extra_args"))
        argv.append(prompt)
        return argv

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session,
        model: str | None = None,
    ) -> dict[str, str]:
        """An allowlisted environment, including only the selected provider key."""
        env = omp_child_env(settings, agent_id, cfg)
        env["PI_CODING_AGENT_DIR"] = str(
            session.home or settings.session_root / "agents" / agent_id / "omp-agent"
        )
        return env

    def translator(self, session: Session) -> Translator:
        """omp prints its own event shapes; OmpProcess feeds this one."""
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
    ) -> OmpProcess:
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        session.home = settings.session_root / "agents" / agent_id / "omp-agent"
        write_agent_config(settings, agent_id, cfg, model or cfg.model)
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        return _spawn_omp(argv, env, str(cwd), session)

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """A model or server that cannot be reached, before any work was done.

        The CLI reports it on stderr or in a message_update error reason; the
        process folded both into the synthetic result's `error`. Never closes
        the lane: the same provider with another model may still run.
        """
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            text = str(event.get("error") or "")
            if text and _REFUSAL_RE.search(text):
                return Refusal(COPILOT_MODEL_UNAVAILABLE, clip(text, 300), None)
        return None


OMP_PROVIDER = OmpProvider()
OmpDriver = OmpProvider  # the name the driver had before providers landed

__all__ = [
    "GUARD",
    "OMP_PROVIDER",
    "OmpProcess",
    "OmpProvider",
    "Translator",
]
