"""Authenticated CLI client of an independently managed epi serve process.

The service owns project admission, human gates, verification and accounting.
This driver never starts it, chooses a profile, reads a token, or approves a plan.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..config import Settings
from ..router import Refusal
from .base import Process, ProviderCapabilities, ProviderConfig, RunContext, Session

TERMINAL = {"completed", "completed_unverified", "failed", "cancelled"}
ROUTING = {"agent_id", "run_id", "provider", "lane", "driver", "model", "fallback",
           "hops", "profile", "known_providers", "fallback_modes"}
MAX_OUTPUT_CHARS = 1_048_576


def _capture(stream: Any, buffer: list[str], limit: int,
             overflow: threading.Event | None = None, process: Process | None = None) -> None:
    """Drain pipes without retaining unbounded output or blocking the child."""
    size = 0
    try:
        while chunk := stream.read(4096):
            room = max(0, limit - size)
            if room:
                buffer.append(chunk[:room])
            size += len(chunk)
            if size > limit and overflow is not None:
                overflow.set()
                if process is not None:
                    process.kill()
                break
    except (ValueError, OSError):
        pass  # pipe closed by cancellation


def _token_path(cfg: ProviderConfig) -> Path | None:
    value = cfg.extra.get("token_file")
    return Path(value).expanduser().absolute() if isinstance(value, str) and value else None


def _project(cfg: ProviderConfig) -> Path | None:
    value = cfg.extra.get("project_root")
    return Path(value).expanduser().resolve() if isinstance(value, str) and value else None


def _argument(args: list[str], flag: str, value: str) -> None:
    # epi's value parser otherwise treats a leading '--' as the next option.
    args.extend([f"--{flag}={value}"] if value.startswith("--") else [f"--{flag}", value])


def _initialized(root: Path | None) -> bool:
    if root is None:
        return False
    pi = root / ".pi"
    board = pi / "board.db"
    try:
        return (stat.S_ISDIR(pi.lstat().st_mode) and stat.S_ISREG(board.lstat().st_mode)
                and board.resolve() == board)
    except OSError:
        return False


def _provider_extra(value: dict[str, Any]) -> dict[str, Any]:
    for entry in value.get("providers", []):
        if isinstance(entry, dict) and entry.get("name") in {"epi", "harness"}:
            extra = entry.get("extra")
            return extra if isinstance(extra, dict) else {}
    return {}


class Translator:
    """Each CLI line is a native run snapshot, including progress snapshots."""

    def __init__(self, session: Session, *, run_id: str | None = None):
        self.session = session
        self.agent_id = session.session_id
        self.run_id = run_id
        self.admitted = False
        self.seen = False
        self.last: dict[str, Any] | None = None

    def outcome(self, detail: dict[str, Any]) -> dict[str, Any]:
        state = detail.get("state")
        if state not in TERMINAL | {"working", "rejected"}:
            raise ValueError("epi returned an invalid run state")
        if state == "rejected" and self.admitted:
            raise ValueError("epi rejected a run after admission")
        if state != "rejected" and not all(
            isinstance(detail.get(key), str) and detail[key] for key in ("agent_id", "run_id")
        ):
            raise ValueError("epi returned a run without its admission identities")
        if state != "rejected":
            if self.agent_id is not None and detail["agent_id"] != self.agent_id:
                raise ValueError("epi returned a different admitted agent")
            if self.run_id is not None and detail["run_id"] != self.run_id:
                raise ValueError("epi returned a different admitted run")
            self.agent_id = detail["agent_id"]
            self.run_id = detail["run_id"]
            self.admitted = True
            self.session.session_id = self.agent_id
            self.session.data["run_id"] = self.run_id
            receipt = self.session.data.get("receipt")
            if receipt is not None:
                receipt.set()
        if state == "completed":
            verification = detail.get("verification")
            uat = detail.get("uat", [])
            if not isinstance(verification, dict) or verification.get("passed") is not True:
                raise ValueError("epi completed without a passed verification")
            if not isinstance(uat, list) or any(
                not isinstance(row, dict) or row.get("passed") is not True for row in uat
            ):
                raise ValueError("epi completed without passed goal checks")
            context = self.session.data.get("context")
            if context is not None and context.goal is not None:
                expected = {row.row for row in context.goal.rows}
                actual = [row.get("row") for row in uat]
                if len(actual) != len(expected) or set(actual) != expected:
                    raise ValueError("epi completed without all requested goal rows")
        out = copy.deepcopy({k: v for k, v in detail.items() if k not in ROUTING})
        extra = out.setdefault("extra", {})
        extra["epi"] = {k: copy.deepcopy(v) for k, v in detail.items() if k in ROUTING}
        if detail.get("state") == "rejected":
            out.update(state="failed", phase="done", finish_reason="refused")
            self.session.data["prework_refusal"] = True
        self.session.data["last_outcome"] = copy.deepcopy(out)
        return out

    def feed(self, detail: dict[str, Any]) -> list[dict[str, Any]]:
        if detail.get("state") not in TERMINAL | {"working", "rejected"}:
            raise ValueError("epi returned an invalid run snapshot")
        first = not self.seen
        out = self.outcome(detail)
        self.seen = True
        self.last = out
        events = []
        if first and self.session.session_id:
            events.append({"type": "system", "subtype": "init",
                           "session_id": self.session.session_id})
        event = {"type": "result", "outcome": out,
                 "result": out.get("result", ""),
                 "is_error": out["state"] in {"failed", "cancelled"},
                 "epi_admitted": self.admitted}
        if self.session.session_id:
            event["session_id"] = self.session.session_id
        if detail.get("state") == "rejected" and not self.admitted:
            event["epi_prework_refusal"] = True
        events.append(event)
        return events

    def failure(self, *, prework: bool = False) -> dict[str, Any]:
        # Do not surface subprocess stderr or malformed JSON. The driver never
        # reads the token, so cannot safely redact a misbehaving client's echo.
        error = "epi client refused before admission" if prework else "epi client failed"
        if prework and not self.admitted:
            self.session.data["prework_refusal"] = True
        extra = copy.deepcopy((self.last or {}).get("extra", {}))
        snapshot = {**(self.last or {}), "state": "failed", "phase": "done",
                    "finish_reason": "refused" if prework else "error",
                    "error": error, "result": "", "extra": extra, "needs_human": False}
        snapshot.pop("human_action", None)
        return {"type": "result", "is_error": True, "error": error,
                "epi_admitted": self.admitted,
                "epi_prework_refusal": prework and not self.admitted,
                "outcome": snapshot}


class EpiProcess(Process):
    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, session: Session):
        super().__init__(argv, env, cwd)
        self.session = session

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen[str], buf: list[str]) -> None:
        if proc.stderr is not None:
            _capture(proc.stderr, buf, 4096)

    def events(self) -> Iterator[dict[str, Any]]:
        translator = Translator(self.session)
        proc = self._start(closed_stdin=True)
        assert proc.stdout is not None
        stderr: list[str] = []
        reader = self._drain_thread(proc, stderr)
        malformed = False
        size = 0
        try:
            while line := proc.stdout.readline(MAX_OUTPUT_CHARS + 1):
                size += len(line)
                if len(line) > MAX_OUTPUT_CHARS or size > 16 * MAX_OUTPUT_CHARS:
                    malformed = True
                    break
                try:
                    detail = json.loads(line)
                    if not isinstance(detail, dict):
                        raise ValueError
                    events = translator.feed(detail)
                except (ValueError, TypeError):
                    malformed = True
                    break
                yield from events
            if malformed:
                self.kill()
            code = proc.wait()
            reader.join(timeout=5)
            if code or not translator.seen or malformed:
                evidence = "".join(stderr).lower()
                refusal = not translator.admitted and not malformed and (
                    evidence.startswith("agent serve socket is unavailable:")
                    or evidence.startswith("caller token rejected")
                )
                yield translator.failure(prework=refusal)
        finally:
            self.session.data["receipt"].set()
            if proc.poll() is None:
                self.kill()
            reader.join(timeout=5)


class EpiProvider:
    name = "epi"
    needs_api_key = False
    prompt_on_stdin = False
    experimental = True
    capabilities = ProviderCapabilities(
        run_context=True, native_outcome=True, native_verification=True,
        native_usage=True, cancel=True, poll=True, transcript=True, metadata=True,
    )

    def guard(self, settings: Settings, cfg: ProviderConfig | None = None) -> str:
        return "harness"

    def boot(self, settings: Settings, agent_id: str,
             cfg: ProviderConfig) -> tuple[str | None, Session]:
        session = Session(provider=cfg.name)
        if not cfg.experimental:
            return "epi is experimental; set experimental = true", session
        if shutil.which(cfg.binary or "epi") is None:
            return "epi binary is unavailable", session
        token = _token_path(cfg)
        if token is None:
            return "epi requires token_file (a path, never a token value)", session
        try:
            info = token.lstat()
            if not stat.S_ISREG(info.st_mode):
                return "epi token_file must be a regular file", session
            if stat.S_IMODE(info.st_mode) != 0o600:
                return "epi token_file must have mode 0600", session
            if not os.access(token, os.R_OK):
                return "epi token_file is unreadable", session
        except OSError:
            return "epi token_file is unavailable", session
        session.data.update(cfg=cfg, env=self.env(settings, agent_id, cfg, session),
                            cwd=str(_project(cfg) or settings.workspace))
        return None, session

    def _call_argv(self, cfg: ProviderConfig, tool: str) -> list[str]:
        token = _token_path(cfg)
        if token is None:
            raise ValueError("epi requires token_file")
        return [cfg.binary or "epi", "call", tool, "--json", "--token-file", str(token)]

    def argv(self, cfg: ProviderConfig, settings: Settings, agent_id: str, prompt: str,
             cwd: Path | None, session: Session, model: str | None) -> list[str]:
        context: RunContext = session.data["context"]
        continuing = session.session_id is not None
        args = self._call_argv(cfg, "continue" if continuing else "delegate")
        args += ["--follow", "--request-id", context.request_id]
        if continuing:
            args += ["--agent-id", session.session_id]
            _argument(args, "message", prompt)
            _argument(args, "verification", context.verification or "")
            return args
        _argument(args, "task", context.task if context.task is not None else prompt)
        _argument(args, "verification", context.verification or "")
        if cwd is not None:
            args += ["--workspace", str(cwd.resolve())]
        for flag, value in (("instructions", context.instructions), ("name", context.name),
                            ("model", context.model), ("fallback", context.fallback)):
            if value is not None:
                # Inline values also handle text beginning with '--'.
                args.append(f"--{flag}={value}")
        if context.goal is not None:
            goal = context.goal
            block = goal.block or ('Execute plan "subagent" (subagent). Goal rows:\n' +
                                   "\n".join(f"{r.row} {r.label}: {r.check}" for r in goal.rows))
            args.append(f"--goal={block}")
        return args

    def env(self, settings: Settings, agent_id: str, cfg: ProviderConfig,
            session: Session) -> dict[str, str]:
        env = settings.base_child_env()
        # Non-secret paths required by epi's default socket resolver. No
        # credentials or new general-purpose child allowlist entries.
        for key in ("XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def translator(self, session: Session) -> Translator:
        return Translator(session)

    def spawn(self, cfg: ProviderConfig, settings: Settings, agent_id: str, prompt: str,
              cwd: Path, session: Session, model: str | None, *,
              context: RunContext) -> EpiProcess:
        # A continue keeps the native agent, but its run and accounting receipt
        # belong to the new request. Never charge the previous run twice.
        session.data.pop("run_id", None)
        session.data.pop("last_outcome", None)
        session.data.pop("prework_refusal", None)
        session.data.update(context=context, request_id=context.request_id,
                            cwd=str(cwd), receipt=threading.Event())
        args = self.argv(cfg, settings, agent_id, prompt, cwd, session, model)
        session.data.pop("delegate_args", None)
        if session.session_id is None:
            # Retain the exact work arguments, including the idempotency key.
            # A non-following replay returns the original run's current detail.
            prefix = len(self._call_argv(cfg, "delegate"))
            session.data["delegate_args"] = [a for a in args[prefix:] if a != "--follow"]
        process = EpiProcess(args, self.env(settings, agent_id, cfg, session), str(cwd), session)
        session.data["process"] = process
        return process

    def refusal(self, cfg: ProviderConfig, events: list[dict[str, Any]]) -> Refusal | None:
        if any(e.get("epi_admitted") for e in events):
            return None
        if any(e.get("epi_prework_refusal") for e in events):
            return Refusal("epi_refused", "epi refused before admitting the job")
        return None

    def _call(self, session: Session, tool: str, *args: str) -> dict[str, Any]:
        cfg = session.data["cfg"]
        process = Process([*self._call_argv(cfg, tool), *args], session.data["env"],
                          session.data["cwd"])
        workers = []
        try:
            proc = process._start(closed_stdin=True)
            stdout: list[str] = []
            stderr: list[str] = []
            overflow = threading.Event()
            for stream, buffer, limit, fence in (
                (proc.stdout, stdout, MAX_OUTPUT_CHARS, overflow),
                (proc.stderr, stderr, 4096, None),
            ):
                worker = threading.Thread(target=_capture,
                                          args=(stream, buffer, limit, fence, process), daemon=True)
                worker.start()
                workers.append(worker)
            code = proc.wait(timeout=5)
            for worker in workers:
                worker.join(timeout=1)
            if code or overflow.is_set() or any(w.is_alive() for w in workers):
                raise ValueError
            value = json.loads("".join(stdout))
            if not isinstance(value, dict) or (
                value.get("error") and value.get("state") not in TERMINAL | {"working", "rejected"}
            ):
                raise ValueError
            return value
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise RuntimeError(f"epi {tool} client failed") from None
        finally:
            process.kill()
            for worker in workers:
                worker.join(timeout=1)

    def poll(self, session: Session) -> dict[str, Any] | None:
        run_id = session.data.get("run_id")
        if not run_id:
            raise RuntimeError("epi await requires a saved run id")
        detail = self._call(session, "await", "--run-id", run_id, "--wait-seconds", "1")
        return Translator(session, run_id=run_id).outcome(detail)

    def cancel(self, session: Session, *, reason: str, kind: str) -> dict[str, Any] | None:
        process = session.data.get("process")
        if process is not None:
            # Fence publication against Popen before waiting for admission.
            fence = process._cancel_fence
            if fence is not None:
                with fence:
                    process._cancelled = True
                    started = process._proc is not None
            else:
                started = process._proc is not None
            if started and not session.session_id:
                session.data["receipt"].wait(timeout=5)
                if not session.session_id:
                    if session.data.get("prework_refusal"):
                        # A known refusal has no admitted job to recover. A
                        # fresh call could start work if conditions changed.
                        return None
                    arguments = session.data.get("delegate_args")
                    if arguments is None:
                        raise RuntimeError("epi cancel has no original delegate arguments")
                    detail = self._call(session, "delegate", *arguments)
                    Translator(session, run_id=session.data.get("run_id")).outcome(detail)
                    if not session.session_id:
                        raise RuntimeError("epi cancel could not identify an admitted agent")
        if session.session_id:
            result = self._call(session, "cancel", "--agent-id", session.session_id)
            if result.get("closed") is not True or result.get("agent_id") != session.session_id:
                raise RuntimeError("epi cancellation was not confirmed")
            snapshot = copy.deepcopy(session.data.get("last_outcome", {}))
            # Cancel usage at the top level is agent-wide. Only a matching
            # per-run summary can replace this run's accounting.
            saved = session.data.get("run_id")
            for summary in result.get("runs", []):
                if summary.get("run_id") == saved:
                    for key in ("usage", "usage_unreported", "cash_usd", "credits",
                                "credits_unit", "turn_usage"):
                        if key in summary:
                            snapshot[key] = copy.deepcopy(summary[key])
            snapshot.update(state="cancelled", phase="done", needs_human=False)
            snapshot.pop("human_action", None)
            snapshot.setdefault("extra", {}).setdefault("epi", {})["cancel"] = result
            snapshot["extra"]["cancellation_confirmed"] = True
            return snapshot
        return None

    def transcript(self, session: Session, *, run_id: str | None = None,
                   limit: int = 60, raw: bool = False) -> dict[str, Any]:
        saved = run_id or session.data.get("run_id")
        if not saved:
            raise RuntimeError("epi transcript requires a saved run id")
        args = ["--run-id", saved, "--limit", str(max(0, limit))]
        if raw:
            args.append("--raw")
        value = self._call(session, "transcript", *args)
        if value.get("run_id") != saved or (
            session.session_id is not None and value.get("agent_id") != session.session_id
        ):
            raise RuntimeError("epi transcript returned a different native identity")
        return value

    def metadata(self, settings: Settings, cfg: ProviderConfig) -> dict[str, Any]:
        error, session = self.boot(settings, "metadata", cfg)
        if error:
            raise RuntimeError(error)
        value = self._call(session, "list")
        scope = _provider_extra(value)
        metadata = {key: copy.deepcopy(scope[key])
                    for key in ("default_project", "allowed_projects") if key in scope}
        return {"profile": scope.get("profile"), **metadata, "epi": value}

    def doctor(self, settings: Settings, cfg: ProviderConfig) -> dict[str, Any]:
        error, session = self.boot(settings, "doctor", cfg)
        row: dict[str, Any] = {"token_file_ok": error is None, "socket_ok": False,
                               "project_initialized": False, "project_allowed": None,
                               "profile": None, "health_ok": False}
        if error:
            return {**row, "health": error}
        try:
            value = self._call(session, "list")
        except RuntimeError as exc:
            return {**row, "health": str(exc)}
        row["socket_ok"] = True
        row["extra"] = {"epi": value}
        scope = _provider_extra(value)
        row["profile"] = scope.get("profile")
        row["default_project"] = scope.get("default_project")
        root = _project(cfg)
        row["project_initialized"] = _initialized(root)
        # Scope belongs to the authenticated native provider entry. Old jobs,
        # a default project, or local board files cannot establish permission.
        allowed = scope.get("allowed_projects")
        if root is not None and isinstance(allowed, list) and all(
            isinstance(path, str) for path in allowed
        ):
            row["project_allowed"] = str(root) in allowed
        row["health_ok"] = bool(row["project_initialized"] and row["project_allowed"]
                                and row["profile"])
        row["health"] = "ok" if row["health_ok"] else (
            "epi project_root must be initialized and allowlisted, with a profile; "
            "call list must expose allowed_projects to check caller scope"
        )
        return row


EPI_PROVIDER = EpiProvider()
