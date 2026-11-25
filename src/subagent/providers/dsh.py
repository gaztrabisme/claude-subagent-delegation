"""Experimental DeepSeek Harness (`dsh`) worker driver.

DSH headless JSON is translated into the Claude event shapes consumed by
``Agent``. Every DSH tool call is gated by the packaged pre-execute bridge,
which asks the shared subagent approval hook and denies on any error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..config import APPROVAL_HOOK, Settings, prepare_private_directory
from ..guard.classify import _git_metadata_roots, _redact_private_paths, protect, release
from ..router import Refusal, clip
from ..sandbox import approval_socket_write_deny
from .base import DRIVER_DSH, Process, ProviderConfig, Session

DEFAULT_BINARY = "dsh"
GUARD = "hook"
PATCH_MARKER = "__SUBAGENT_DSH_GUARD_ENTRY__"
READY_MARKER = "__SUBAGENT_DSH_GUARD_READY__"
NONCE_ENV = "SUBAGENT_DSH_GUARD_NONCE"
_GUARD_INACTIVE = re.compile(
    r"(?=.*subagent-dsh-guard)(?=.*(?:did not activate|failed to activate|failed to load|"
    r"failed to import|could not load|could not import|unable to load))",
    re.IGNORECASE | re.DOTALL,
)


def sandbox_write_roots(settings: Settings, agent_id: str, cfg: ProviderConfig,
                        cwd: Path) -> list[str]:
    """Workspace, this agent's DSH runtime and the agent-specific temp directory."""
    agent_root = settings.session_root / "agents" / agent_id
    roots = [
        str(cwd),  # Project files are the worker's deliverable.
        str(agent_root),  # Own hook, bootstrap, and session state.
        str(_dsh_home(settings, agent_id)),  # Isolated DSH runtime data.
        str(agent_root / "tmp"),  # DSH's private temporary directory.
    ]
    return list(dict.fromkeys(roots))
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
_HEADLESS_PROFILE_PACKAGE = (
    '{\n'
    '  "name": "dsh-profile-headless",\n'
    '  "private": true,\n'
    '  "dependencies": {},\n'
    '  "dsh": {\n'
    '    "profile": {\n'
    '      "bundles": [\n'
    '        "@deepseek-ai/dsh-base",\n'
    '        "@deepseek-ai/dsh-headless"\n'
    '      ]\n'
    '    }\n'
    '  }\n'
    '}\n'
)
_HEADLESS_PROFILE_PNPM_WORKSPACE = (
    "packages:\n"
    "  - .\n"
    "\n"
    "nodeLinker: hoisted\n"
    "autoInstallPeers: false\n"
)
_HEADLESS_PROFILE_ROOT = (
    "# dsh profile root — an empty entry list. The tree is composed as patches:\n"
    "# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any\n"
    "# --patch overlays. Edit cordis.patch.yml, not this file.\n"
    "[]\n"
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
    # DSH's profile bootstrap creates this directory before it loads patches.
    # Prepare it outside Seatbelt so a fresh isolated home can boot read-only.
    headless = path / "profiles" / "headless"
    headless.mkdir(parents=True, exist_ok=True)
    (headless / "package.json").write_text(_HEADLESS_PROFILE_PACKAGE, encoding="utf-8")
    (headless / "pnpm-workspace.yaml").write_text(
        _HEADLESS_PROFILE_PNPM_WORKSPACE, encoding="utf-8",
    )
    cordis_yml = headless / "cordis.yml"
    # DSH rewrites this generated root at boot. Reset it in the parent before
    # every launch so a worker cannot persist entries into the next run. Remove
    # first so a worker-created symlink is replaced rather than followed.
    cordis_yml.unlink(missing_ok=True)
    cordis_yml.write_text(_HEADLESS_PROFILE_ROOT, encoding="utf-8")
    return path


def _owner_dsh_home() -> Path:
    return Path(os.environ.get("DSH_OWNER_HOME") or (Path.home() / ".dsh")).expanduser()


def _guard_patch(
    settings: Settings, agent_id: str, dsh_home: Path, *, login: bool = False,
) -> Path:
    _root, entry, source = _bridge_bundle()
    template = source.read_text(encoding="utf-8")
    if template.count(PATCH_MARKER) != 1:
        raise RuntimeError("DSH guard bridge patch has an invalid entry marker")
    generated = template.replace(PATCH_MARKER, entry.as_uri())
    if login:
        generated += (
            "\n- id: web-search-deepseek\n"
            "  disabled: true\n"
        )
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
        self.step_usage: list[tuple[str, dict[str, int]]] = []
        self.unreported: set[str] = set()
        self.step_ends = 0
        self.turn_starts = 0
        self.text_fragments: list[str] = []
        self._step_sequence = 0
        self._active_step_id: str | None = None
        self._last_step_id: str | None = None

    def _message_id(self) -> str:
        if self._active_step_id is None:
            self._step_sequence += 1
            self._active_step_id = f"dsh-step-{self._step_sequence}"
        self._last_step_id = self._active_step_id
        return self._active_step_id

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
            if phase == "turn_start":
                self.turn_starts += 1
            elif phase == "step_start":
                self._step_sequence += 1
                self._active_step_id = f"dsh-step-{self._step_sequence}"
                self._last_step_id = self._active_step_id
            elif phase == "step_end":
                self.step_ends += 1
                message_id = self._message_id()
                usage, missing = _usage_sample(event.get("usage"))
                self.unreported.update(missing)
                if not isinstance(event.get("usage"), dict):
                    self.unreported.update(USAGE_KEYS)
                elif not missing:
                    self.step_usage.append((message_id, usage))
                self._active_step_id = None
                if isinstance(event.get("usage"), dict) and not missing:
                    return [{"type": "assistant", "message": {
                        "id": message_id,
                        "content": [],
                        "usage": _to_meter_usage(usage),
                    }}]
                return [{"type": "assistant", "message": {
                    "id": message_id,
                    "content": [],
                }}]
            elif phase == "turn_end":
                error = _reason_error(event.get("reason"))
                if error:
                    self.error_message = error
            return []
        if kind == "thinking":
            text = event.get("text")
            if not isinstance(text, str) or not text:
                return []
            return [{"type": "assistant", "message": {"id": self._message_id(), "content": [{
                "type": "thinking", "thinking": text,
            }]}}]
        if kind == "text":
            text = event.get("text")
            if not isinstance(text, str) or not text:
                return []
            self.text_fragments.append(text)
            return [{"type": "assistant", "message": {"id": self._message_id(), "content": [{
                "type": "text", "text": text,
            }]}}]
        if kind == "tool_call":
            name = str(event.get("tool") or "")
            call_id = event.get("callId") or f"dsh-tool-{self.step_ends + 1}"
            return [{"type": "assistant", "message": {"id": self._message_id(), "content": [{
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
        """Usage is emitted with each `step_end`; no buffered events remain."""
        return []

    def final_text_event(self) -> dict[str, Any] | None:
        """Preserve final text if the runtime omitted committed text events."""
        text = self.final.get("text") if isinstance(self.final, dict) else None
        if not isinstance(text, str) or not text or self.text_fragments:
            return None
        self.text_fragments.append(text)
        message_id = self._active_step_id or self._last_step_id or self._message_id()
        return {"type": "assistant", "message": {
            "id": message_id,
            "content": [{"type": "text", "text": text}],
        }}

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
        diagnostic = _redact_private_paths(self.error_message)
        if not completed and not diagnostic and self.final is not None:
            diagnostic = _reason_error(reason)
        stderr = _redact_private_paths((stderr or "").strip())
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
        diagnostic = _redact_private_paths(diagnostic)

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
            for _step_id, sample in self.step_usage:
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
        login_patch: Path | None = None,
        cordis_path: Path | None = None,
    ):
        super().__init__(argv, env, cwd, prompt)
        self.dsh_translator = translator
        self.protected_root = protected_root
        self.login_patch = login_patch
        self.cordis_path = cordis_path
        self.guard_ready_callback: Callable[[str, str], None] | None = None

    def events(self) -> Iterator[dict[str, Any]]:
        if self.protected_root is not None:
            protect(self.protected_root, temporary=True)
        proc = None
        selector: selectors.BaseSelector | None = None
        login_patch = self.login_patch
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
            guard_ready = False
            guard_inactive = False
            expected_ready = f"{READY_MARKER}{self.env.get(NONCE_ENV, '')}"
            selector = selectors.DefaultSelector()
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            streams = {"stdout": proc.stdout, "stderr": proc.stderr}
            for name, stream in streams.items():
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)

            def stderr_line(line: bytes) -> None:
                nonlocal guard_ready, guard_inactive
                text = line.decode("utf-8", errors="replace")
                if text.rstrip("\r\n") == expected_ready and self.env.get(NONCE_ENV):
                    guard_ready = True
                    if self.cordis_path is not None and self.guard_ready_callback is not None:
                        try:
                            digest = hashlib.sha256(self.cordis_path.read_bytes()).hexdigest()
                            self.guard_ready_callback(str(self.cordis_path), digest)
                        except OSError:
                            # Missing/unreadable bootstrap will be caught by the
                            # approval side as a failed hash check.
                            self.guard_ready_callback(str(self.cordis_path), "")
                    return
                stderr_buf.append(text)
                if _GUARD_INACTIVE.search("".join(stderr_buf)):
                    guard_inactive = True
                    self.dsh_translator.error_message = (
                        "DSH reported that subagent-dsh-guard did not activate"
                    )
                    self.kill()

            def stdout_line(line: bytes) -> Iterator[dict[str, Any]]:
                nonlocal guard_inactive
                if guard_inactive:
                    return
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    return
                try:
                    native = json.loads(raw)
                except json.JSONDecodeError:
                    return
                if not isinstance(native, dict):
                    return
                # The selector handles stderr first whenever both pipes are
                # readable. No event is translated before its nonce proof.
                if not guard_ready:
                    self.dsh_translator.error_message = (
                        "DSH guard readiness proof was not received before the first event"
                    )
                    self.kill()
                    guard_inactive = True
                    return
                yield from self.dsh_translator.feed(native)

            while selector.get_map() and not guard_inactive:
                ready = selector.select()
                # Read proof and activation diagnostics before events when both
                # file descriptors became readable in the same poll cycle.
                ready.sort(key=lambda item: item[0].data != "stderr")
                for key, _mask in ready:
                    name = key.data
                    stream = key.fileobj
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        pending = bytes(buffers[name])
                        if pending:
                            if name == "stderr":
                                stderr_line(pending)
                            else:
                                yield from stdout_line(pending)
                        continue
                    buffers[name].extend(chunk)
                    while b"\n" in buffers[name]:
                        line, _, tail = buffers[name].partition(b"\n")
                        buffers[name] = bytearray(tail)
                        if name == "stderr":
                            stderr_line(line + b"\n")
                        else:
                            yield from stdout_line(line)
                        if guard_inactive:
                            break
            selector.close()
            selector = None
            code = proc.wait()
            if guard_inactive:
                self.dsh_translator.error_message = (
                    "DSH reported that subagent-dsh-guard did not activate"
                )
            elif not guard_ready:
                self.dsh_translator.error_message = (
                    "DSH guard readiness proof was not received"
                )
            else:
                final_text = self.dsh_translator.final_text_event()
                if final_text is not None:
                    yield final_text
            yield self.dsh_translator.finish(code, "".join(stderr_buf))
        finally:
            if proc is not None:
                self.kill()
            if selector is not None:
                selector.close()
            if self.protected_root is not None:
                release(self.protected_root)
            if login_patch is not None:
                with suppress(OSError):
                    login_patch.unlink(missing_ok=True)


class DshProvider:
    """Run a configured DeepSeek Harness binary in headless JSON mode."""

    name = DRIVER_DSH
    needs_api_key = True
    prompt_on_stdin = True

    def guard(self, settings: Settings, cfg: ProviderConfig) -> str:
        return GUARD

    def secret_files(self, cfg: ProviderConfig) -> tuple[Path, ...]:
        """Protect every path under the owner's DSH home from guard reads."""
        owner_home = _owner_dsh_home()
        # Keep the prior leaf declarations as explicit entries for consumers
        # that display declared secrets, while the root closes scans of all
        # other DSH state (sessions, config, and future files).
        return (owner_home, owner_home / ".credentials.yaml", owner_home / "storages")

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
        guard_patch = _guard_patch(settings, agent_id, dsh_home, login=login)
        selected_model = model or cfg.model
        argv = [
            self.binary(cfg),
            "--profile", "headless",
            "--patch", str(guard_patch),
        ]
        if login:
            owner_home = _owner_dsh_home().resolve()
            login_model = selected_model or "deepseek-flash"
            login_patch = _login_patch(settings, agent_id, owner_home, login_model)
            argv.extend(["--patch", str(login_patch)])
        elif selected_model:
            model_patch = _model_patch(settings, agent_id, selected_model, login=False)
            argv.extend(["--patch", str(model_patch)])
        argv.extend([
            "--json",
            "--approval-policy", "never",
            # On macOS Seatbelt supplies the boundary. Elsewhere keep DSH's
            # ordinary workspace sandbox until R1b defines a platform refusal.
            "--permission-mode", "danger-full-access" if sys.platform == "darwin"
            else "workspace-write",
        ])
        session_id = _session_text(session)
        if session_id:
            argv.extend(["--session-id", session_id])
        argv.append("-")
        agent_dir = _agent_dir(settings, agent_id)
        bridge_root, bridge_entry, bridge_patch = _bridge_bundle()
        headless_profile = dsh_home / "profiles" / "headless"
        headless_cordis = headless_profile / "cordis.yml"
        agent_tmp = prepare_private_directory(
            settings.session_root / "agents" / agent_id / "tmp"
        )
        # DSH rewrites only this generated bootstrap file on every launch.
        # The profile directory and every patch remain write-denied.
        write_allow = [
            str(cwd),
            str(agent_tmp),
            str(dsh_home / "sessions"),
            str(dsh_home / "storages"),
            str(dsh_home / "spills"),
            str(dsh_home / ".anonymous-user-id"),
            "/dev",
        ]
        write_deny = [
            str(cwd / ".env"),
            str(cwd / ".git"),
            *(str(root) for root in sorted(_git_metadata_roots(cwd))),
            str(APPROVAL_HOOK),
            str(dsh_home / "cordis.patch.yml"),
            str(dsh_home / ".env"),
            str(dsh_home / "profiles"),
            str(dsh_home / "dsh-guard"),
            str(guard_patch),
            str(agent_dir / "dsh-login.patch.yml"),
            str(agent_dir / "dsh-model.patch.yml"),
            str(bridge_root),
            str(bridge_entry),
            str(bridge_patch),
            approval_socket_write_deny(
                settings.approval_socket,
                [
                    cwd,
                    settings.session_root,
                    *sandbox_write_roots(settings, agent_id, cfg, cwd),
                    *write_allow,
                ],
            ),
        ]
        owner_home = _owner_dsh_home().resolve()
        deny_paths = [*cfg.deny_read, str(owner_home), str(owner_home / "storages")]
        read_allow = [str(owner_home / ".credentials.yaml")] if login else []
        read_metadata_allow = [str(owner_home)] if login else []
        session.data.update({
            "sandbox_deny_paths": deny_paths,
            "sandbox_write_deny": write_deny,
            "sandbox_read_allow": read_allow,
            "sandbox_read_metadata_allow": read_metadata_allow,
            "sandbox_write_allow_exact": [str(headless_cordis)],
            "sandbox_write_allow_subpaths": [str(agent_tmp)],
            "cordis_path": str(headless_cordis),
        })
        return argv

    def env(
        self, settings: Settings, agent_id: str, cfg: ProviderConfig, session: Session
    ) -> dict[str, str]:
        env = settings.base_child_env()
        dsh_home = _dsh_home(settings, agent_id)
        agent_tmp = prepare_private_directory(
            settings.session_root / "agents" / agent_id / "tmp"
        )
        env["DSH_HOME"] = str(dsh_home)
        env["TMPDIR"] = str(agent_tmp)
        env["SUBAGENT_GUARD_HOOK"] = settings.guard_hook_command(agent_id)
        env["SUBAGENT_APPROVAL_SOCKET"] = settings.approval_socket
        env["SUBAGENT_HOOK_TIMEOUT"] = str(settings.supervisor_timeout + 20)
        env[NONCE_ENV] = secrets.token_urlsafe(32)
        env.pop("DSH_OWNER_HOME", None)
        if cfg.extra.get("auth") == "login":
            # An empty value shadows DSH's workspace and home dotenv fallbacks.
            env["DEEPSEEK_API_KEY"] = ""
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
        login_patch = None
        if cfg.extra.get("auth") == "login":
            protected_root = _owner_dsh_home()
            login_patch = _agent_dir(settings, agent_id) / "dsh-login.patch.yml"
        process = DshProcess(
            argv, env, str(cwd), prompt, self.translator(session), protected_root,
            login_patch, Path(session.data["cordis_path"]),
        )
        process.sandbox_deny_paths = session.data.get("sandbox_deny_paths", [])
        process.sandbox_write_deny = session.data.get("sandbox_write_deny", [])
        process.sandbox_read_allow = session.data.get("sandbox_read_allow", [])
        process.sandbox_read_metadata_allow = session.data.get(
            "sandbox_read_metadata_allow", []
        )
        process.sandbox_write_allow_exact = session.data.get("sandbox_write_allow_exact", [])
        process.sandbox_write_allow_subpaths = session.data.get(
            "sandbox_write_allow_subpaths", []
        )
        return process

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
        from .. import router

        refusal = router.classify_refusal(cfg.vendor, events)
        if refusal is not None:
            return refusal
        if "ACCOUNT_QUOTA_EXCEEDED" in text.upper():
            return Refusal(router.DEEPSEEK_BALANCE, clip(text, 300))
        return None


DSH_PROVIDER = DshProvider()
DshDriver = DshProvider

__all__ = ["DSH_PROVIDER", "DshProcess", "DshProvider", "DshTranslator"]
