"""Google Antigravity CLI (`agy`) as a Gemini provider.

The CLI emits its own stream-json events, which this module translates into
Claude stream-json shapes. Captured `result.usage` counters are cumulative
across a resumed conversation, so the translator stores the last totals on the
session and reports only each turn's token delta.

The CLI's pre-tool hook configuration could not be established from its
settings, plugin help, or installed binary. Turns therefore stay sandboxed and
the driver remains behind the `experimental = true` opt-in.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..router import Refusal, clip
from .base import DRIVER_ANTIGRAVITY, Process, ProviderConfig, Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

GUARD = "none"
DEFAULT_BINARY = "agy"


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

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
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

    def _step(self, step: dict[str, Any]) -> list[dict[str, Any]]:
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
    needs_api_key = False  # agy owns its login
    prompt_on_stdin = False
    experimental = True

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """The hook protocol is not known; this driver relies on agy's sandbox."""
        return GUARD

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        session = Session(provider=cfg.name)
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
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        return _spawn_antigravity(argv, env, str(cwd), session)

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """Recognize only CLI-reported quota, authentication and model failures."""
        errors: list[str] = []
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
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
    "GUARD",
    "Translator",
]
