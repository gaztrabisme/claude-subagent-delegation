"""The dispatcher seam (spec 8.1): request/outcome types, the protocol and a scripted fake."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

RESULT_TEXT_LIMIT = 4000
_SLICE_S = 0.1
_HEARTBEAT_EVERY_S = 1.0


class DispatchError(RuntimeError):
    """An infrastructure fault (not a task failure); `retryable` says whether to try again."""

    def __init__(self, message: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class TaskRequest:
    request_id: str
    attempt: int
    attempt_id: str
    root_run_id: str
    run_path: str
    node_id: str
    role: str | None
    provider: str
    model: str | None
    prompt: str
    instructions: str | None
    workspace: str
    verification: str
    max_steps: int | None
    timeout_s: float

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskRequest:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class TaskOutcome:
    status: str  # completed | unverified | failed | interrupted | lane_refused | cancelled
    run_id: str | None = None
    agent_id: str | None = None
    lane: str | None = None
    verification: dict | None = None
    result_text: str = ""
    result_json: dict | None = None
    verdict: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    cost_kind: str | None = None
    guard_denials: list[dict] = field(default_factory=list)
    test_change_request: str | None = None
    needs_human: bool = False
    error: str | None = None
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskOutcome:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


class Dispatcher(Protocol):
    def run(self, request: TaskRequest, *, heartbeat: Callable[[dict], None],
            cancelled: threading.Event) -> TaskOutcome:
        """Blocking. Heartbeats at least every 10 s; returns `cancelled` promptly once set;
        raises DispatchError only for infrastructure faults, never for a task failure."""
        ...


@dataclass
class FakeStep:
    status: str = "completed"
    cost_usd: float | None = 0.05
    sleep_s: float = 0.0
    result_json: dict | None = None
    verdict: str | None = None
    test_change_request: str | None = None
    guard_denials: list[dict] = field(default_factory=list)
    raise_error: bool = False


class FakeDispatcher:
    """Plays scripted steps. The n-th call for a key takes the n-th step; the last repeats."""

    def __init__(self, script: Mapping[str, Sequence[FakeStep]] | None = None, *,
                 default: FakeStep | None = None, audit_log: Path | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else FakeStep()
        self.audit_log = Path(audit_log) if audit_log is not None else None
        self.calls: list[TaskRequest] = []
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: Path) -> FakeDispatcher:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        script = {key: [FakeStep(**step) for step in steps]
                  for key, steps in data.get("script", {}).items()}
        audit = data.get("audit_log")
        return cls(script, default=FakeStep(**data.get("default", {})),
                   audit_log=Path(audit) if audit else None)

    def _audit(self, event: str, attempt_id: str) -> None:
        if self.audit_log is None:
            return
        line = json.dumps({"event": event, "attempt_id": attempt_id, "pid": os.getpid(),
                           "ts": time.time()}) + "\n"
        with self._lock:
            fd = os.open(self.audit_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode())
                os.fsync(fd)
            finally:
                os.close(fd)

    def _step(self, request: TaskRequest) -> FakeStep:
        with self._lock:
            self.calls.append(request)
            key = request.request_id if request.request_id in self.script else request.node_id
            steps = self.script.get(key)
            if not steps:
                return self.default
            index = self._counts.get(key, 0)
            self._counts[key] = index + 1
            return steps[min(index, len(steps) - 1)]

    def run(self, request: TaskRequest, *, heartbeat: Callable[[dict], None],
            cancelled: threading.Event) -> TaskOutcome:
        step = self._step(request)
        self._audit("start", request.attempt_id)
        try:
            deadline = time.monotonic() + step.sleep_s
            last_beat = 0.0
            heartbeat({"attempt_id": request.attempt_id})
            while True:
                if cancelled.is_set():
                    return TaskOutcome(status="cancelled", lane=request.provider,
                                       error="cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_SLICE_S, remaining))
                if time.monotonic() - last_beat >= _HEARTBEAT_EVERY_S:
                    last_beat = time.monotonic()
                    heartbeat({"attempt_id": request.attempt_id})
            if step.raise_error:
                raise DispatchError("scripted infrastructure fault", retryable=True)
            verdict = step.verdict
            if verdict is None and step.result_json is not None:
                candidate = step.result_json.get("verdict")
                verdict = candidate if isinstance(candidate, str) else None
            return TaskOutcome(
                status=step.status, run_id=f"fake-{request.attempt_id}",
                agent_id=f"fake-agent-{request.node_id}", lane=request.provider,
                result_text=f"fake {step.status}: {request.node_id}"[:RESULT_TEXT_LIMIT],
                result_json=step.result_json, verdict=verdict, cost_usd=step.cost_usd,
                cost_kind="fake" if step.cost_usd is not None else None,
                guard_denials=list(step.guard_denials),
                test_change_request=step.test_change_request,
                error=None if step.status == "completed" else step.status)
        finally:
            self._audit("end", request.attempt_id)
