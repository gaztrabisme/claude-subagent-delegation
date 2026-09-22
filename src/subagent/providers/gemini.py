"""The gemini driver: one ``gemini -p --output-format stream-json`` per turn.

Gemini CLI's stream-json emits ``init`` / ``message`` / ``tool_use`` /
``tool_result`` / ``error`` / ``result`` lines. A `Translator` rewrites them
into the claude stream-json shapes the run loop reads, and folds Gemini's
per-model token counters into the result event's usage.

Fixture-tested only: neither the driver nor the fixture was captured from a
live ``gemini`` run. ``boot()`` refuses until ``[providers.<name>].experimental
= true`` says the operator has accepted that.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..router import Refusal
from .base import DRIVER_GEMINI, Process, ProviderConfig, Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

GUARD = "none"

DEFAULT_BINARY = "gemini"


def _usage(event: dict[str, Any]) -> dict[str, int]:
    """Gemini's per-model tokens, in the claude keys Usage.add reads.

    Gemini reports `prompt`, `cached`, `candidates`, `thoughts` and `tool`
    counts per model under `stats.models[*].tokens`. The assumption, documented
    because it is not yet verified against a live run: `prompt - cached` is the
    uncached input, `cached` is the cache read, and `candidates + thoughts` is
    the output (thoughts are reasoning output tokens, `tool` is not counted).
    """
    tokens: dict[str, int] = {}
    models = ((event.get("stats") or {}).get("models") or {})
    for model in models.values() if isinstance(models, dict) else []:
        per = (model.get("tokens") or {}) if isinstance(model, dict) else {}
        for key in ("prompt", "cached", "candidates", "thoughts"):
            if isinstance(per.get(key), (int, float)):
                tokens[key] = int(tokens.get(key, 0)) + int(per[key])
    prompt = tokens.get("prompt", 0)
    cached = tokens.get("cached", 0)
    return {
        "input_tokens": max(0, prompt - cached),
        "cache_read_input_tokens": cached,
        "output_tokens": tokens.get("candidates", 0) + tokens.get("thoughts", 0),
    }


class Translator:
    """Gemini stream-json events in, claude stream-json events out."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.last_message = ""
        self.terminal = False

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.seen.append(event)
        kind = event.get("type")
        if kind == "init":
            out: dict[str, Any] = {"type": "system", "subtype": "init"}
            if isinstance(event.get("session_id"), str) and event["session_id"]:
                out["session_id"] = event["session_id"]
            return [out]
        if kind == "message":
            text = str(event.get("content") or "")
            if text.strip():
                self.last_message = text.strip()
            return [self._assistant([{"type": "text", "text": text}])]
        if kind == "tool_use":
            return [self._assistant([{
                "type": "tool_use",
                "id": event.get("id") or event.get("tool_use_id"),
                "name": event.get("name"),
                "input": event.get("input") if event.get("input") is not None
                else event.get("arguments"),
            }])]
        if kind == "tool_result":
            return [{
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": event.get("tool_use_id") or event.get("id"),
                    "content": str(event.get("content") or event.get("result") or ""),
                    "is_error": bool(event.get("is_error") or event.get("error")),
                }]},
            }]
        if kind == "result":
            self.terminal = True
            is_error = bool(event.get("error") or event.get("is_error"))
            out = {
                "type": "result",
                "subtype": "error" if is_error else "success",
                "is_error": is_error,
                "result": str(event.get("response") or self.last_message),
                "usage": _usage(event),
                "num_turns": 1,
            }
            if event.get("error"):
                out["error"] = str(event["error"])
            return [out]
        if kind == "error":
            # A stream hiccup; the result event (or the process exit) reports
            # whether the turn failed.
            return []
        return [{"type": f"gemini/{kind}"}] if kind else []

    def failure(self, fallback: str = "") -> dict[str, Any]:
        return {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": self.last_message,
            "error": fallback or "gemini reported an error",
            "num_turns": 1,
        }

    def _assistant(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "assistant", "message": {"content": content}}


class GeminiProcess(Process):
    """One ``gemini -p`` subprocess: prompt as an argv flag, stream-json on stdout."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str):
        super().__init__(argv, env, cwd)
        self.raw: list[dict[str, Any]] = []

    def events(self) -> Iterator[dict[str, Any]]:
        proc = self._start()
        assert proc.stdout is not None
        stderr_buf: list[str] = []
        stderr_thread = self._drain_thread(proc, stderr_buf)
        translator = Translator()
        try:
            for event in self._json_lines(proc.stdout):
                self.raw.append(event)
                yield from translator.feed(event)
            code = proc.wait()
            stderr_thread.join(timeout=5)
            if not translator.terminal:
                tail = "".join(stderr_buf).strip()[-800:]
                yield translator.failure(
                    f"gemini exited {code} without finishing the turn: {tail}".strip()
                )
        finally:
            if proc.poll() is None:
                self.kill()
            stderr_thread.join(timeout=5)


def _spawn_gemini(argv: list[str], env: dict[str, str], cwd: str) -> GeminiProcess:
    return GeminiProcess(argv, env, cwd)


class GeminiProvider:
    """Runs a turn as ``gemini -p --output-format stream-json``."""

    name = DRIVER_GEMINI
    needs_api_key = False  # the Gemini CLI logs in with its own credentials
    prompt_on_stdin = False
    experimental = True  # fixture-tested only

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """Gemini runs with --yolo; nothing guards the child."""
        return GUARD

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        """The reason gemini cannot run, or None.

        The driver is fixture-tested only, so it refuses until the config
        opts in with ``experimental = true``.
        """
        session = Session(provider=cfg.name)
        if not cfg.experimental:
            return (
                "gemini driver is fixture-tested only; set experimental = true "
                f"on provider {cfg.name!r} to use it"
            ), session
        binary = self.binary(cfg)
        if shutil.which(binary) is None:
            return f"{binary!r} is not on PATH; install the Gemini CLI", session
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
        return [
            self.binary(cfg),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--yolo",
        ]

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
        """Gemini prints its own event shapes; GeminiProcess feeds this one."""
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
    ) -> GeminiProcess:
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        return _spawn_gemini(argv, env, str(cwd))

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """Gemini has no refusal dialect the router acts on."""
        return None


GEMINI_PROVIDER = GeminiProvider()

__all__ = [
    "GEMINI_PROVIDER",
    "GeminiProcess",
    "GeminiProvider",
    "GUARD",
    "Translator",
]
