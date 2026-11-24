"""Experimental DeepSeek Harness (`dsh`) worker driver.

DSH headless JSON is translated into the Claude event shapes consumed by
``Agent``. Every DSH tool call is gated by the packaged pre-execute bridge,
which asks the shared subagent approval hook and denies on any error.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..config import APPROVAL_HOOK, Settings
from ..guard.classify import _git_metadata_roots, protect, release
from ..router import Refusal, clip
from ..sandbox import wrap_argv
from .base import DRIVER_DSH, Process, ProviderConfig, Session

DEFAULT_BINARY = "dsh"
GUARD = "hook"
PATCH_MARKER = "__SUBAGENT_DSH_GUARD_ENTRY__"
USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
AUTH_CODES = (
    "MISSING_CREDENTIAL",
    "ACCOUNT_SIGN_IN_REQUIRED",
    "ACCOUNT_TOKEN_INVALID",
    "INVALID_CREDENTIAL",
)
AUTH_WORDS = (
    "401 unauthorized",
    "401 unauthorized authentication failed",
    "invalid api key",
    "authentication failed",
    "unauthorized",
)


def _bridge_bundle() -> tuple[Path, Path, Path]:
    """Find the packaged bridge, or the repository copy in a source checkout."""
    provider_file = Path(__file__).resolve()
    candidates = (
        provider_file.parents[1] / "integrations" / "dsh-guard",
        provider_file.parents[3] / "integrations" / "dsh-guard",
    )
    for root in candidates:
        package = root / "package.json"
        patch = root / "cordis.patch.yml"
        if not root.is_dir() or not package.is_file() or not patch.is_file():
            continue
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        main = data.get("main") if isinstance(data, dict) else None
        if not isinstance(main, str) or not main.strip():
            continue
        entry = (root / main).resolve()
        if entry.is_file() and entry.is_relative_to(root.resolve()):
            return root, entry, patch
    raise RuntimeError(
        "DSH guard bridge is missing or incomplete; refusing to start an unguarded DSH worker"
    )


def _agent_dir(settings: Settings, agent_id: str) -> Path:
    agents = (settings.session_root / "agents").resolve()
    root = (agents / agent_id).resolve()
    if not root.is_relative_to(agents):
        raise ValueError("invalid DSH agent id")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _dsh_home(settings: Settings, agent_id: str) -> Path:
    path = _agent_dir(settings, agent_id) / "dsh-home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _guard_patch(settings: Settings, agent_id: str, dsh_home: Path) -> Path:
    _root, entry, source = _bridge_bundle()
    template = source.read_text(encoding="utf-8")
    if template.count(PATCH_MARKER) != 1:
        raise RuntimeError("DSH guard bridge patch has an invalid entry marker")
    generated = template.replace(PATCH_MARKER, entry.as_uri())
    path = dsh_home / "dsh-guard" / "cordis.patch.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated, encoding="utf-8")
    return path


def _yaml_string(value: str) -> str:
    # JSON strings are valid YAML scalars and safely quote paths/model names.
    return json.dumps(value, ensure_ascii=False)


def _model_patch(
    settings: Settings,
    agent_id: str,
    model: str,
    *,
    login: bool,
) -> Path:
    provider = "deepseek-account" if login else "deepseek-official"
    content = (
        "- id: agent-default-model\n"
        "  config:\n"
        f"    provider: {provider}\n"
        f"    model: {_yaml_string(model)}\n"
    )
    path = _agent_dir(settings, agent_id) / "dsh-model.patch.yml"
    path.write_text(content, encoding="utf-8")
    return path


def _login_patch(settings: Settings, agent_id: str, owner_home: Path, model: str) -> Path:
    credential_file = owner_home / ".credentials.yaml"
    content = (
        "- id: credentials\n"
        "  disabled: false\n"
        "  config:\n"
        f"    path: {_yaml_string(str(credential_file))}\n"
        "- id: authorization\n"
        "  disabled: false\n"
        "- id: deepseek-account\n"
        "  disabled: false\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: deepseek-account\n"
        f"    model: {_yaml_string(model)}\n"
    )
    path = _agent_dir(settings, agent_id) / "dsh-login.patch.yml"
    path.write_text(content, encoding="utf-8")
    return path


def _session_text(session: Session) -> str | None:
    value = session.session_id or session.data.get("session_id")
    return value if isinstance(value, str) and value else None


def _usage_number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _usage_sample(value: Any) -> tuple[dict[str, int], list[str]]:
    if not isinstance(value, dict):
        return {}, list(USAGE_KEYS)
    mapping = {
        "input": ("inputTokens", "input_tokens"),
        "output": ("outputTokens", "output_tokens"),
        "cache_read": ("cacheReadTokens", "cache_read_input_tokens"),
        "cache_write": ("cacheWriteTokens", "cache_creation_input_tokens"),
        "reasoning": ("reasoningTokens", "reasoning_tokens"),
    }
    normalized: dict[str, int] = {}
    missing: list[str] = []
    for key, aliases in mapping.items():
        number = next((_usage_number(value[name]) for name in aliases if name in value), None)
        if number is None:
            if key in ("input", "output"):
                missing.append(key)
            normalized[key] = 0
        else:
            normalized[key] = number
    return normalized, missing


def _to_meter_usage(usage: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "cache_read_input_tokens": usage["cache_read"],
        "cache_creation_input_tokens": usage["cache_write"],
        "reasoning_output_tokens": usage["reasoning"],
    }


def _tool_input(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {"raw": value}
    return value if isinstance(value, dict) else {}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _reason_error(reason: Any) -> str:
    if not isinstance(reason, dict):
        return ""
    error = reason.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or error.get("reason")
        if code and message:
            return f"{code}: {message}"
        if code:
            return str(code)
        if message:
            return str(message)
    kind = reason.get("kind")
    return f"DSH turn ended with reason {kind!r}" if kind and kind != "completed" else ""


class DshTranslator:
    """Convert DSH JSONL events to the shared Claude stream event format."""

    def __init__(self, session: Session):
        self.session = session
        self.session_id = _session_text(session)
        self.final: dict[str, Any] | None = None
        self.error_message = ""
        self.step_usage: list[tuple[int, int, dict[str, int]]] = []
        self.unreported: set[str] = set()
        self.step_ends = 0
        self.turn_starts = 0
        self.text_fragments: list[str] = []

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "session":
            session_id = event.get("sessionId")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
                self.session.session_id = session_id
            return [{
                "type": "system",
                "subtype": "init",
                "session_id": self.session_id,
                "cwd": event.get("cwd"),
            }]
        if kind == "status":
            phase = event.get("phase")
            turn = _usage_number(event.get("turn")) or 0
            step = _usage_number(event.get("step")) or 0
            if phase == "turn_start":
                self.turn_starts += 1
            elif phase == "step_end":
                self.step_ends += 1
                usage, missing = _usage_sample(event.get("usage"))
                self.unreported.update(missing)
                if not isinstance(event.get("usage"), dict):
                    self.unreported.update(USAGE_KEYS)
                else:
                    self.step_usage.append((turn, step, usage))
            elif phase == "turn_end":
                error = _reason_error(event.get("reason"))
                if error:
                    self.error_message = error
            return []
        if kind == "thinking":
            text = event.get("text")
            if not isinstance(text, str) or not text:
                return []
            return [{"type": "assistant", "message": {"content": [{
                "type": "thinking", "thinking": text,
            }]}}]
        if kind == "text":
            text = event.get("text")
            if not isinstance(text, str) or not text:
                return []
            self.text_fragments.append(text)
            return [{"type": "assistant", "message": {"content": [{
                "type": "text", "text": text,
            }]}}]
        if kind == "tool_call":
            name = str(event.get("tool") or "")
            call_id = event.get("callId") or f"dsh-tool-{self.step_ends + 1}"
            return [{"type": "assistant", "message": {"content": [{
                "type": "tool_use",
                "id": str(call_id),
                "name": name[:1].upper() + name[1:],
                "input": _tool_input(event.get("input")),
            }]}}]
        if kind == "tool_result":
            status = event.get("status")
            error = status == "error"
            content = _content_text(event.get("result"))
            if error and not content:
                content = _reason_error(event.get("error")) or "DSH tool call failed"
            return [{"type": "user", "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": str(event.get("callId") or ""),
                "content": content,
                "is_error": error,
            }]}}]
        if kind == "final":
            self.final = event
            session_id = event.get("sessionId")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
                self.session.session_id = session_id
            error = _reason_error(event.get("reason"))
            if error:
                self.error_message = error
            return []
        if kind == "error":
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                self.error_message = message.strip()
            return []
        return []

    def usage_events(self) -> list[dict[str, Any]]:
        """Return complete step samples, suppressing partial totals on any gap."""
        if not self.step_usage or self.unreported or len(self.step_usage) != self.step_ends:
            return []
        events = []
        for turn, step, usage in self.step_usage:
            events.append({"type": "assistant", "message": {
                "id": f"dsh-{turn}-{step}",
                "content": [],
                "usage": _to_meter_usage(usage),
            }})
        return events

    def final_text_event(self) -> dict[str, Any] | None:
        """Preserve final text if the runtime omitted committed text events."""
        text = self.final.get("text") if isinstance(self.final, dict) else None
        if not isinstance(text, str) or not text or self.text_fragments:
            return None
        self.text_fragments.append(text)
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    def finish(self, exit_code: int | None, stderr: str) -> dict[str, Any]:
        """Return the one terminal event after status, final and process status agree."""
        final = self.final or {}
        reason = final.get("reason") if isinstance(final.get("reason"), dict) else {}
        final_text = final.get("text")
        if not isinstance(final_text, str):
            final_text = "".join(self.text_fragments)
        if final_text and not self.text_fragments:
            # Preserve final text when a profile/runtime omitted committed text events.
            self.text_fragments.append(final_text)

        completed = reason.get("kind") == "completed"
        diagnostic = self.error_message
        if not completed and not diagnostic and self.final is not None:
            diagnostic = _reason_error(reason)
        stderr = (stderr or "").strip()
        if diagnostic and stderr and stderr not in diagnostic:
            diagnostic = f"{diagnostic}\n{stderr}"
        if not diagnostic and stderr and (exit_code not in (0, None) or self.final is None):
            diagnostic = stderr
        if not diagnostic and self.final is None:
            diagnostic = f"dsh exited {exit_code} without a final event"
        if not diagnostic and exit_code not in (0, None):
            diagnostic = f"dsh exited {exit_code}"
        failed = bool(diagnostic) or not completed or exit_code not in (0, None)
        if failed and not diagnostic:
            diagnostic = "dsh did not complete the requested turn"

        result: dict[str, Any] = {
            "type": "result",
            "subtype": "error" if failed else "success",
            "is_error": failed,
            "result": final_text,
            "session_id": self.session_id,
            "num_turns": self.turn_starts,
        }
        if failed:
            result["error"] = diagnostic
            result["error_kind"] = "auth" if is_auth_error(diagnostic) else "cli_error"
        elif self.step_usage and not self.unreported:
            total = dict.fromkeys(USAGE_KEYS, 0)
            for _turn, _step, sample in self.step_usage:
                for key in USAGE_KEYS:
                    total[key] += sample[key]
            result["usage"] = _to_meter_usage(total)
        if self.unreported:
            result["usage_unreported"] = sorted(self.unreported)
        return result


def is_auth_error(text: str) -> bool:
    upper = (text or "").upper()
    if any(code in upper for code in AUTH_CODES):
        return True
    low = (text or "").lower()
    return any(word in low for word in AUTH_WORDS)


class DshProcess(Process):
    """One DSH headless process; prompt stdin is always closed before reading."""

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        prompt: str,
        translator: DshTranslator,
        protected_root: Path | None = None,
    ):
        super().__init__(argv, env, cwd, prompt)
        self.dsh_translator = translator
        self.protected_root = protected_root

    def events(self) -> Iterator[dict[str, Any]]:
        if self.protected_root is not None:
            protect(self.protected_root, temporary=True)
        proc = None
        stderr_thread = None
        try:
            proc = self._start(stdin=True)
            if proc.stdin is not None:
                try:
                    proc.stdin.write(self.prompt or "")
                    proc.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    proc.stdin.close()
            assert proc.stdout is not None
            stderr_buf: list[str] = []
            stderr_thread = self._drain_thread(proc, stderr_buf)
            for native in self._json_lines(proc.stdout):
                yield from self.dsh_translator.feed(native)
            code = proc.wait()
            stderr_thread.join(timeout=5)
            final_text = self.dsh_translator.final_text_event()
            if final_text is not None:
                yield final_text
            yield from self.dsh_translator.usage_events()
            yield self.dsh_translator.finish(code, "".join(stderr_buf))
        finally:
            if proc is not None and proc.poll() is None:
                self.kill()
            if stderr_thread is not None:
                stderr_thread.join(timeout=5)
            if self.protected_root is not None:
                release(self.protected_root)


class DshProvider:
    """Run a configured DeepSeek Harness binary in headless JSON mode."""

    name = DRIVER_DSH
    needs_api_key = True
    prompt_on_stdin = True

    def guard(self, settings: Settings, cfg: ProviderConfig) -> str:
        return GUARD

    @staticmethod
    def binary(cfg: ProviderConfig) -> str:
        return cfg.binary or DEFAULT_BINARY

    def boot(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig
    ) -> tuple[str | None, Session]:
        session = Session(provider=cfg.name, home=_dsh_home(settings, agent_id))
        binary = self.binary(cfg)
        if shutil.which(binary) is None:
            return f"{binary!r} is not on PATH; install DeepSeek Harness", session
        try:
            _bridge_bundle()
        except Exception as exc:  # noqa: BLE001 - refuse before dispatch if unguarded
            return str(exc), session
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
        login = cfg.extra.get("auth") == "login"
        dsh_home = _dsh_home(settings, agent_id)
        guard_patch = _guard_patch(settings, agent_id, dsh_home)
        selected_model = model or cfg.model
        argv = [
            self.binary(cfg),
            "--profile", "headless",
            "--patch", str(guard_patch),
        ]
        if login:
            owner_home = Path(os.environ.get("DSH_OWNER_HOME") or (Path.home() / ".dsh"))
            owner_home = owner_home.expanduser().resolve()
            login_model = selected_model or "deepseek-flash"
            login_patch = _login_patch(settings, agent_id, owner_home, login_model)
            argv.extend(["--patch", str(login_patch)])
        elif selected_model:
            model_patch = _model_patch(settings, agent_id, selected_model, login=False)
            argv.extend(["--patch", str(model_patch)])
        argv.extend([
            "--json",
            "--approval-policy", "never",
            "--permission-mode", "danger-full-access" if cfg.deny_read else "workspace-write",
        ])
        session_id = _session_text(session)
        if session_id:
            argv.extend(["--session-id", session_id])
        argv.append("-")
        if cfg.deny_read:
            agent_dir = _agent_dir(settings, agent_id)
            write_allow = [
                str(cwd),
                "/private/tmp",
                os.environ.get("TMPDIR") or "/private/tmp",
                str(dsh_home),
                "/dev",
            ]
            write_deny = [
                str(cwd / ".git"),
                *(str(root) for root in sorted(_git_metadata_roots(cwd))),
                str(APPROVAL_HOOK),
                str(guard_patch),
                str(agent_dir / "dsh-login.patch.yml"),
                str(agent_dir / "dsh-model.patch.yml"),
            ]
            argv = wrap_argv(argv, cfg.deny_read, write_allow=write_allow, write_deny=write_deny)
        return argv

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session
    ) -> dict[str, str]:
        env = settings.base_child_env()
        dsh_home = _dsh_home(settings, agent_id)
        env["DSH_HOME"] = str(dsh_home)
        env["SUBAGENT_GUARD_HOOK"] = settings.guard_hook_command(agent_id)
        env["SUBAGENT_APPROVAL_SOCKET"] = settings.approval_socket
        env["SUBAGENT_HOOK_TIMEOUT"] = str(settings.supervisor_timeout + 20)
        env.pop("DSH_OWNER_HOME", None)
        if cfg.extra.get("auth") == "login":
            env.pop("DEEPSEEK_API_KEY", None)
            env.pop("DEEPSEEK_BASE_URL", None)
        else:
            env["DEEPSEEK_API_KEY"] = cfg.api_key() or ""
            if cfg.base_url:
                env["DEEPSEEK_BASE_URL"] = cfg.base_url
            else:
                env.pop("DEEPSEEK_BASE_URL", None)
        return env

    def translator(self, session: Session) -> DshTranslator:
        return DshTranslator(session)

    def spawn(
        self,
        cfg: ProviderConfig,
        settings: Settings,
        agent_id: str,
        prompt: str,
        cwd: Path,
        session: Session,
        model: str | None,
    ) -> DshProcess:
        # Resolve and verify the bridge before even constructing a process.
        _bridge_bundle()
        argv = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        env = self.env(settings, agent_id, cfg, session)
        protected_root = None
        if cfg.extra.get("auth") == "login":
            owner_home = os.environ.get("DSH_OWNER_HOME") or str(Path.home() / ".dsh")
            protected_root = Path(owner_home).expanduser().resolve()
        return DshProcess(
            argv, env, str(cwd), prompt, self.translator(session), protected_root,
        )

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        parts: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result":
                if isinstance(event.get("error"), str):
                    parts.append(event["error"])
                if event.get("is_error") and isinstance(event.get("result"), str):
                    parts.append(event["result"])
            elif event.get("type") == "error" and isinstance(event.get("message"), str):
                parts.append(event["message"])
        text = "\n".join(parts)
        if text and is_auth_error(text):
            return Refusal("dsh_auth", clip(text, 300))
        return None


DSH_PROVIDER = DshProvider()
DshDriver = DshProvider

__all__ = ["DSH_PROVIDER", "DshProcess", "DshProvider", "DshTranslator"]
