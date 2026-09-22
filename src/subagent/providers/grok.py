"""The grok driver: one ``grok -p`` subprocess per turn.

Grok Build's ``--output-format streaming-messages-json`` is byte-compatible
with Claude stream-json (``system/init`` carrying the session id, then
``result`` with usage and errors), so this driver has no translator: the
child's lines are the events the run loop already reads.

Two guards are possible, chosen from the config: when the provider honours
Grok's own hooks directory (``extra["hooks"]``) the label is "hook"; otherwise
the label is "sandbox", for Grok's built-in sandbox profile. The driver
installs neither -- that wiring is the coordinator's job -- it only reports
which one stands between the child and the machine.

Verified against the installed binary's fixture; a live check is a coordinator
task, so this driver is not gated behind ``experimental``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..router import Refusal
from .base import DRIVER_GROK, Process, ProviderConfig, Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

GUARD_HOOK = "hook"
GUARD_SANDBOX = "sandbox"

DEFAULT_BINARY = "grok"


class GrokProcess(Process):
    """One ``grok -p`` subprocess: prompt as an argv flag, stream-json on stdout."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str):
        super().__init__(argv, env, cwd)
        self.raw: list[dict[str, Any]] = []

    def events(self) -> Iterator[dict[str, Any]]:
        proc = self._start()
        assert proc.stdout is not None
        stderr_buf: list[str] = []
        stderr_thread = self._drain_thread(proc, stderr_buf)
        try:
            for event in self._json_lines(proc.stdout):
                self.raw.append(event)
                yield event
            code = proc.wait()
            stderr_thread.join(timeout=5)
            if code not in (0, None) and not any(
                e.get("type") == "result" for e in self.raw
            ):
                # Grok reports its own result event for a failed turn; this is
                # only the case where the CLI died before it could write one.
                tail = "".join(stderr_buf).strip()[-800:]
                yield {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "",
                    "error": f"grok exited {code}: {tail}".strip(),
                }
        finally:
            if proc.poll() is None:
                self.kill()
            stderr_thread.join(timeout=5)


def _spawn_grok(argv: list[str], env: dict[str, str], cwd: str) -> GrokProcess:
    return GrokProcess(argv, env, cwd)


class GrokProvider:
    """Runs a turn as ``grok -p --output-format streaming-messages-json``."""

    name = DRIVER_GROK
    needs_api_key = False  # XAI_API_KEY is optional; the CLI also uses OAuth
    prompt_on_stdin = False
    experimental = False  # verified by fixture; live check is a coordinator task

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        """"hook" when Grok's own hooks are honoured, else its sandbox profile."""
        if cfg is not None and cfg.extra.get("hooks"):
            return GUARD_HOOK
        return GUARD_SANDBOX

    def binary(self, cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        """The reason grok cannot run, or None. Only the binary is checked."""
        session = Session(provider=cfg.name)
        binary = self.binary(cfg)
        if shutil.which(binary) is None:
            return f"{binary!r} is not on PATH; install the Grok CLI", session
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
            "streaming-messages-json",
            "--always-approve",
            "--no-auto-update",
            "--cwd",
            str(cwd),
        ]
        if model:
            argv.extend(["-m", model])
        if session.session_id:
            argv.extend(["-r", session.session_id])
        return argv

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session,
        model: str | None = None,
    ) -> dict[str, str]:
        """The parent's environment, minus leaked keys, plus XAI_API_KEY."""
        env = {k: v for k, v in os.environ.items() if v is not None}
        for leaked in settings.leaked_keys:
            env.pop(leaked, None)
        key = cfg.api_key()
        if key:
            env["XAI_API_KEY"] = key
        return env

    def translator(self, session: Session) -> None:
        """Grok already prints claude stream-json; nothing to translate."""
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
    ) -> GrokProcess:
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session, model)
        return _spawn_grok(argv, env, str(cwd))

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        """The before-work refusal in a turn's events, or None."""
        from .. import router

        return router.classify_refusal(cfg.vendor, events)


GROK_PROVIDER = GrokProvider()

__all__ = [
    "GROK_PROVIDER",
    "GrokProcess",
    "GrokProvider",
    "GUARD_HOOK",
    "GUARD_SANDBOX",
]
