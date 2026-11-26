"""Google Antigravity CLI (`agy`) as a Gemini provider.

The CLI emits its own stream-json events, which this module translates into
Claude stream-json shapes. Captured `result.usage` counters are cumulative
across a resumed conversation, so the translator stores the last totals on the
session and reports only each turn's token delta.

The CLI's pre-tool hook configuration could not be established from its
settings, plugin help, or installed binary. On macOS the server therefore wraps
every agy turn in its own Seatbelt profile (the same path as the hook drivers,
in `Agent._spawn`); agy's `--sandbox` stays on as an inner layer only, because a
tool call with `BypassSandbox: true` leaves it. Off macOS the driver refuses to
start. A tool step that asks for `BypassSandbox` ends the run (see
`Translator._bypass`). The driver remains behind the `experimental = true` opt-in.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..router import Refusal, clip
from .base import DRIVER_ANTIGRAVITY, Process, ProviderConfig, Session

# The server's outer Seatbelt profile is the boundary; agy's own --sandbox is
# not one on its own (a tool step may ask for BypassSandbox).
GUARD = "sandbox"
GUARD_UNSANDBOXED = "none"
BOUNDARY = "server profile (sandbox-exec)"
DEFAULT_BINARY = "agy"
FINISH_SANDBOX_BYPASS = "sandbox_bypass"
BYPASS_KEY = "bypasssandbox"
COMMAND_CLIP = 500
OFF_MACOS = (
    "antigravity driver runs only under the server's macOS sandbox-exec profile; "
    "agy's own --sandbox is not a boundary on its own, so it refuses to start here"
)
# Owner homes agy has a reason to read: its own login and state.
_AGY_HOMES = (".gemini",)
# Not denied although the classifier protects it: the login Keychain is read
# through the Security framework, and agy may keep its sign-in there. Items stay
# guarded by their own Keychain access control lists.
_READ_DENY_EXEMPT = ("Library/Keychains",)


def runtime_state_dir(cfg: ProviderConfig | None = None) -> Path:
    """agy's own runtime state (conversations, CLI settings, logs).

    `agy_state_dir` on the provider overrides the default location.
    """
    configured = cfg.extra.get("agy_state_dir") if cfg is not None else None
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli"


# --- bypass audit (R1a-agy addendum R2) ----------------------------------------
# agy's stream-json drops BypassSandbox from tool parameters, so the stream
# detector cannot see a real bypass. The audit reads agy's own record of the
# run's conversation after the turn, read-only, and only records what it finds.

_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
# A flat JSON object (no nested braces) that mentions BypassSandbox.
_BYPASS_OBJECT = re.compile(r"\{[^{}]*\"BypassSandbox\"[^{}]*\}")


def bypass_audit_enabled(cfg: ProviderConfig) -> bool:
    """On unless the provider sets `bypass_audit = false` (S56)."""
    return cfg.extra.get("bypass_audit", True) is not False


def conversation_record(cfg: ProviderConfig, conversation_id: str) -> Path:
    return runtime_state_dir(cfg) / "conversations" / f"{conversation_id}.db"


def audit_conversation(cfg: ProviderConfig, conversation_id: str | None
                       ) -> tuple[list[dict[str, str]], str | None]:
    """Distinct BypassSandbox-true calls in the run's own conversation record.

    Returns (findings, None), or ([], reason) when the record cannot be read.
    Opens exactly one file, read-only; never raises.
    """
    if not isinstance(conversation_id, str) or not _CONVERSATION_ID.match(conversation_id) \
            or conversation_id in (".", ".."):
        return [], "no usable conversation id for this run"
    path = conversation_record(cfg, conversation_id)
    # The state directory is a worker write root: never follow a link planted
    # there, so nothing but this run's own record is ever opened.
    try:
        if path.parent.is_symlink() or path.is_symlink():
            return [], "conversation record path is a symbolic link; not read"
        if not path.exists():
            return [], "conversation record not found"
        if not path.is_file():
            return [], "conversation record is not a regular file"
    except OSError as exc:
        return [], f"conversation record not accessible: {type(exc).__name__}"
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute("select step_payload from steps").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return [], f"conversation record unreadable: {type(exc).__name__}: {exc}"
    seen: dict[str, dict[str, str]] = {}
    for (payload,) in rows:
        if isinstance(payload, (bytes, bytearray, memoryview)):
            text = bytes(payload).decode("utf-8", errors="replace")
        elif isinstance(payload, str):
            text = payload
        else:
            continue
        for match in _BYPASS_OBJECT.finditer(text):
            try:
                obj = json.loads(match.group(0))
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("BypassSandbox") is not True:
                continue
            key = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                continue
            command = obj.get("CommandLine")
            seen[key] = {
                "tool": "run_command",
                "command": (command if isinstance(command, str) else "")[:COMMAND_CLIP],
            }
    return list(seen.values()), None


def sandbox_write_roots(settings: Settings, agent_id: str, cfg: ProviderConfig,
                        cwd: Path) -> list[str]:
    """Workspace, this agent's server state and temp directory, and agy's runtime state."""
    agent_root = settings.session_root / "agents" / agent_id
    roots = [
        str(cwd),  # Project files are the worker's deliverable.
        str(agent_root),  # Own server-side state for this agent.
        str(agent_root / "tmp"),  # The child's TMPDIR (set in Agent._spawn).
        # agy writes its login refresh, CLI state and conversation store in its own
        # fixed directory (it does not read agy_state_dir, which only moves where the
        # audit reads); listed in sandbox.OWNER_HOME_EXCEPTIONS["antigravity"].
        str(runtime_state_dir()),
    ]
    return list(dict.fromkeys(roots))


def sandbox_write_deny(settings: Settings, agent_id: str, cfg: ProviderConfig,
                       cwd: Path) -> list[str]:
    """Hook files in agy's state directory the child may not write (R1a round 6 H5).

    agy reads hooks from `~/.gemini/config/hooks.json`, which is outside every write
    root; older agy builds wrote them to `antigravity-cli/hooks.json`, which is inside
    the one owner-home root agy keeps, so that file is denied too. The rest of the
    directory stays writable: the stated residual of OWNER_HOME_EXCEPTIONS["antigravity"].
    """
    return [str(runtime_state_dir() / "hooks.json")]


def sandbox_read_deny(settings: Settings, agent_id: str, cfg: ProviderConfig,
                      cwd: Path) -> list[str]:
    """Owner credential homes the classifier protects, except the ones agy needs.

    The list is the guard classifier's (`SENSITIVE_HOME` plus every provider home
    in `PROVIDER_HOMES`), so a home added there is denied here too. A home that
    contains the workspace, the session root or a write root is left out: denying
    it would deny the run's own files.
    """
    from ..guard.classify import PROVIDER_HOMES, SENSITIVE_HOME

    home = Path.home()
    entries = dict.fromkeys(
        [*SENSITIVE_HOME, *(rel for homes in PROVIDER_HOMES.values() for rel in homes)]
    )
    keep = [
        Path(p).expanduser().resolve()
        for p in (cwd, settings.session_root,
                  *sandbox_write_roots(settings, agent_id, cfg, cwd))
    ]
    denied: list[str] = []
    for rel in entries:
        if rel in _AGY_HOMES or rel in _READ_DENY_EXEMPT:
            continue
        path = (home / rel).resolve()
        if any(own == path or own.is_relative_to(path) for own in keep):
            continue
        denied.append(str(home / rel))
    return denied


def _bypass_requested(params: Any) -> bool:
    """BypassSandbox true: boolean True, or the string "true" in any case."""
    if not isinstance(params, dict):
        return False
    for key, value in params.items():
        if isinstance(key, str) and key.lower() == BYPASS_KEY:
            if value is True:
                return True
            if isinstance(value, str) and value.strip().lower() == "true":
                return True
    return False


def _bypass_command(params: dict[str, Any]) -> str:
    for key in ("CommandLine", "Command", "command"):
        value = params.get(key)
        if isinstance(value, str):
            return value[:COMMAND_CLIP]
    try:
        return json.dumps(params, ensure_ascii=False, default=str)[:COMMAND_CLIP]
    except (TypeError, ValueError):
        return ""


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _counter_delta(current: dict[str, int], previous: dict[str, int] | None) -> dict[str, int]:
    """Subtract prior cumulative counters, or treat current counters as a turn."""
    if previous is None:
        return current
    return {
        key: max(0, current.get(key, 0) - previous.get(key, 0))
        for key in current
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class Translator:
    """Antigravity event records in, Claude stream-json events out."""

    def __init__(self, session: Session):
        self.session = session
        self.conversation_id = session.session_id or session.data.get("conversation_id")
        self.last_message = ""
        self.terminal = False
        self.result_event: dict[str, Any] | None = None
        self.errors: list[str] = []
        self.turn_count = 1
        self.pending_usage_cumulative: dict[str, int] | None = None
        self.pending_num_turns = 0
        # Set by the first step that asks to leave agy's sandbox; from then on
        # nothing more is translated as work.
        self.bypass: dict[str, str] | None = None

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if self.bypass is not None:
            return []
        kind = event.get("event")
        if kind == "init":
            conversation_id = event.get("conversation_id")
            if isinstance(conversation_id, str) and conversation_id:
                self.conversation_id = conversation_id
                self.session.session_id = conversation_id
                self.session.data["conversation_id"] = conversation_id
            init = {
                "type": "system",
                "subtype": "init",
                "session_id": self.conversation_id,
            }
            return [init]
        if kind == "step_update":
            step = event.get("step_update")
            if isinstance(step, dict):
                return self._step(step)
            return []
        if kind == "result":
            result = event.get("result")
            if isinstance(result, dict):
                self.terminal = True
                self.result_event = self._result(result)
                return [self.result_event]
            return []
        if kind == "error":
            message = event.get("error") or event.get("message")
            if message:
                self.errors.append(str(message))
        return []

    def _bypass(self, step: dict[str, Any]) -> bool:
        """Flag a step (any type or state) whose tool parameters ask for BypassSandbox."""
        info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
        params = info.get("parameters")
        if not _bypass_requested(params):
            return False
        name = step.get("tool_name") or info.get("name") or "unknown"
        self.bypass = {"tool": str(name), "command": _bypass_command(params)}
        self.terminal = True
        return True

    def bypass_result(self) -> dict[str, Any]:
        """The terminal event for a run ended because a step asked to bypass the sandbox."""
        assert self.bypass is not None
        message = (
            f"sandbox bypass: agy tool step {self.bypass['tool']!r} asked for "
            "BypassSandbox; the server ended the run and killed the agy process tree"
        )
        return {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": message,
            "error": message,
            "error_kind": FINISH_SANDBOX_BYPASS,
            "sandbox_bypass": dict(self.bypass),
            "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
            "num_turns": 1,
            "session_id": self.conversation_id,
        }

    def _step(self, step: dict[str, Any]) -> list[dict[str, Any]]:
        if self._bypass(step):
            return []
        step_type = step.get("step_type")
        if step_type == "agent_response":
            text = step.get("text_delta")
            if isinstance(text, str) and text:
                self.last_message += text
                return [self._assistant([{"type": "text", "text": text}])]
            return []
        if step_type != "tool":
            return []

        state = str(step.get("state") or "").upper()
        info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
        name = step.get("tool_name") or info.get("name")
        conversation_id = step.get("conversation_id") or self.conversation_id or "agy"
        step_index = step.get("step_index", "unknown")
        tool_id = f"{conversation_id}:{step_index}"
        if state == "ACTIVE":
            return [self._assistant([{
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": info.get("parameters") if info.get("parameters") is not None else {},
            }])]
        if state == "DONE":
            output = info.get("output")
            failed = bool(info.get("error") or info.get("is_error"))
            if info.get("success") is False:
                failed = True
            return [{
                "type": "user",
                "session_id": self.conversation_id,
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": _content_text(output),
                    "is_error": failed,
                }]},
            }]
        return []

    def _result(self, result: dict[str, Any]) -> dict[str, Any]:
        conversation_id = result.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self.conversation_id = conversation_id
            self.session.session_id = conversation_id
            self.session.data["conversation_id"] = conversation_id

        raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        current = {
            key: _int(raw_usage.get(key))
            for key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens")
        }
        previous = self.session.data.get("agy_usage_cumulative")
        previous = previous if isinstance(previous, dict) else None
        raw_num_turns = result.get("num_turns")
        num_turns = _int(raw_num_turns)
        prior_turns = self.session.data.get("agy_num_turns")
        if previous is not None and isinstance(prior_turns, int) and num_turns > prior_turns:
            delta = _counter_delta(current, previous)
            self.turn_count = max(1, num_turns - prior_turns)
        else:
            delta = current
            self.turn_count = max(0, num_turns) if isinstance(raw_num_turns, (int, float)) else 1
        if str(result.get("status") or "").upper() == "SUCCESS":
            self.pending_usage_cumulative = current
            self.pending_num_turns = num_turns

        cache = delta["cache_read_tokens"]
        usage = {
            "input_tokens": max(0, delta["input_tokens"] - cache),
            "cache_read_input_tokens": cache,
            "output_tokens": delta["output_tokens"] + delta["thinking_tokens"],
        }
        status = str(result.get("status") or "").upper()
        failed = status != "SUCCESS"
        out: dict[str, Any] = {
            "type": "result",
            "subtype": "error" if failed else "success",
            "is_error": failed,
            "result": str(result.get("response") or self.last_message),
            "usage": usage,
            "num_turns": self.turn_count,
            "session_id": self.conversation_id,
        }
        if result.get("error"):
            out["error"] = str(result["error"])
        elif failed:
            out["error"] = f"agy reported status {status or 'unknown'}"
        return out

    def finish(self, exit_code: int | None, stderr: str = "") -> dict[str, Any]:
        """Apply the process exit code and stderr to the buffered result."""
        event = self.result_event
        if event is None:
            return self.failure(exit_code, stderr)
        failed = bool(event.get("is_error")) or exit_code not in (0, None)
        event["is_error"] = failed
        event["subtype"] = "error" if failed else "success"
        if not failed and self.pending_usage_cumulative is not None:
            self.session.data["agy_usage_cumulative"] = self.pending_usage_cumulative
            if self.pending_num_turns:
                self.session.data["agy_num_turns"] = self.pending_num_turns
        if failed:
            parts = [str(event.get("error") or "")]
            if exit_code not in (0, None) and not parts[0]:
                parts.append(f"agy exited with code {exit_code}")
            parts.extend(self.errors)
            if stderr.strip():
                parts.append(stderr.strip()[-2000:])
            event["error"] = "\n".join(part for part in parts if part).strip()
            if not event["error"]:
                event["error"] = "agy reported an error"
        return event

    def failure(self, exit_code: int | None, stderr: str = "") -> dict[str, Any]:
        details = [*self.errors]
        if exit_code not in (0, None):
            details.append(f"agy exited with code {exit_code}")
        if stderr.strip():
            details.append(stderr.strip()[-2000:])
        return {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": self.last_message,
            "error": "\n".join(details) or "agy ended without a result event",
            "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
            "num_turns": 1,
            "session_id": self.conversation_id,
        }

    @staticmethod
    def _assistant(content: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "assistant", "message": {"content": content}}


class AntigravityProcess(Process):
    """One `agy -p` child. Its prompt is in argv; stdin is closed immediately."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, session: Session):
        super().__init__(argv, env, cwd)
        self.session = session
        self.raw: list[dict[str, Any]] = []
        self.translator = Translator(session)

    def events(self) -> Iterator[dict[str, Any]]:
        proc = self._start(stdin=True)
        if proc.stdin is not None:
            proc.stdin.close()
        assert proc.stdout is not None
        stderr_buf: list[str] = []
        stderr_thread = self._drain_thread(proc, stderr_buf)
        terminal: dict[str, Any] | None = None
        try:
            for event in self._json_lines(proc.stdout):
                self.raw.append(event)
                for translated in self.translator.feed(event):
                    if translated.get("type") == "result":
                        terminal = translated
                    else:
                        yield translated
                if self.translator.bypass is not None:
                    break
            if self.translator.bypass is not None:
                # Detection is after the fact: end the turn and the whole tree now.
                self.kill()
                yield self.translator.bypass_result()
                return
            code = proc.wait()
            stderr_thread.join(timeout=5)
            if terminal is not None:
                yield self.translator.finish(code, "".join(stderr_buf))
            else:
                yield self.translator.failure(code, "".join(stderr_buf))
        finally:
            if proc.poll() is None:
                self.kill()
            stderr_thread.join(timeout=5)


def _spawn_antigravity(
    argv: list[str], env: dict[str, str], cwd: str, session: Session
) -> AntigravityProcess:
    return AntigravityProcess(argv, env, cwd, session)


class AntigravityProvider:
    """Runs Google Antigravity CLI under the `antigravity` driver name."""

    name = DRIVER_ANTIGRAVITY
    known_event_types = frozenset({"init", "step_update", "result", "error"})
    needs_api_key = False  # agy owns its login
    prompt_on_stdin = False
    experimental = True

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """The hook protocol is not known; the server's Seatbelt profile is the boundary."""
        return GUARD if sys.platform == "darwin" else GUARD_UNSANDBOXED

    def boundary(self) -> str:
        """What confines the child, for `doctor`."""
        return BOUNDARY if sys.platform == "darwin" else "none (refuses to start off macOS)"

    def sandbox_write_roots(self, settings: Settings, agent_id: str, cfg: ProviderConfig,
                            cwd: Path) -> list[str]:
        return sandbox_write_roots(settings, agent_id, cfg, cwd)

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        session = Session(provider=cfg.name)
        if sys.platform != "darwin":
            return OFF_MACOS, session
        if not cfg.experimental:
            return (
                "antigravity driver has no installed approval hook; set "
                f"experimental = true on provider {cfg.name!r} to use it"
            ), session
        binary = self.binary(cfg)
        if shutil.which(binary) is None:
            return f"{binary!r} is not on PATH; install the Antigravity CLI", session
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
        ]
        chosen_model = model or cfg.model
        if chosen_model:
            argv.extend(["--model", chosen_model])
        effort = "low" if cfg.thinking == "low" else cfg.effort
        if effort:
            argv.extend(["--effort", effort])
        if session.session_id:
            argv.extend(["--conversation", session.session_id])
        argv.extend(["--dangerously-skip-permissions", "--sandbox"])
        return argv

    def env(
        self,
        settings: Settings,
        agent_id: str,
        cfg: ProviderConfig,
        session: Session,
        model: str | None = None,
    ) -> dict[str, str]:
        """Use the child allowlist; HOME lets agy find its own user login."""
        return settings.base_child_env()

    def translator(self, session: Session) -> Translator:
        return Translator(session)

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> AntigravityProcess:
        if sys.platform != "darwin":
            raise RuntimeError(OFF_MACOS)
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        process = _spawn_antigravity(argv, env, str(cwd), session)
        # Read by Agent._spawn when it builds the outer profile (G1.3). Its own
        # agent directory and the standard devices (/dev/null) are writable
        # through sandbox.agent_profile_kwargs.
        process.sandbox_read_deny = sandbox_read_deny(settings, agent_id, cfg, cwd)
        return process

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """Recognize only CLI-reported quota, authentication and model failures."""
        errors: list[str] = []
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            if event.get("error_kind") == FINISH_SANDBOX_BYPASS:
                return None  # The server ended it; not a lane failure to route around.
            if isinstance(event.get("error"), str):
                errors.append(event["error"])
            if event.get("is_error") and isinstance(event.get("result"), str):
                errors.append(event["result"])
        text = "\n".join(errors).strip()
        low = text.lower()
        if not low:
            return None
        if (
            "invalid model" in low
            or "unknown model" in low
            or "not recognized as a known model" in low
            or "model is unavailable" in low
            or "model not available" in low
        ):
            return Refusal("antigravity_model_unavailable", clip(text, 300))
        if (
            "resource exhausted" in low
            or "quota" in low
            or "rate limit" in low
            or "too many requests" in low
            or re.search(r"\b429\b", low)
        ):
            return Refusal("antigravity_quota", clip(text, 300))
        if (
            "unauthorized" in low
            or "unauthenticated" in low
            or "authentication" in low
            or "invalid credential" in low
            or "sign in" in low
            or re.search(r"\b(?:401|403)\b", low)
        ):
            return Refusal("antigravity_auth", clip(text, 300))
        return None


ANTIGRAVITY_PROVIDER = AntigravityProvider()

__all__ = [
    "ANTIGRAVITY_PROVIDER",
    "AntigravityProcess",
    "AntigravityProvider",
    "FINISH_SANDBOX_BYPASS",
    "GUARD",
    "Translator",
    "audit_conversation",
    "bypass_audit_enabled",
    "sandbox_read_deny",
    "sandbox_write_roots",
]
