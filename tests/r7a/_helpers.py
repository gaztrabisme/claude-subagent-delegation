"""Shared offline helpers for the R7a acceptance tests.

Everything here is in-process: the Claude child is a scripted object handed to
`subagent.providers.claude._spawn_claude`, so no test starts a real process,
touches the network or sleeps longer than a poll interval.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from subagent.runs import Registry

from ..conftest import default_providers, make_settings


class Clock:
    """A settable replacement for `subagent.runs._now`."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


class ScriptedProcess:
    """Yields scripted events; an optional clock moves on before each one."""

    def __init__(self, events: list[dict[str, Any]], argv: list[str], env: dict[str, str],
                 clock: Clock | None = None, step_seconds: float = 0.0):
        self.argv = argv
        self.env = env
        self._events = events
        self._clock = clock
        self._step = step_seconds
        self.killed = False

    def events(self):
        for event in self._events:
            if self._clock is not None:
                self._clock.tick(self._step)
            yield event
            if self.killed:
                return

    def kill(self) -> None:
        self.killed = True


class BlockedProcess:
    """Yields `head`, then waits until killed; never produces a result."""

    def __init__(self, head: list[dict[str, Any]], argv: list[str], env: dict[str, str]):
        self.argv = argv
        self.env = env
        self._head = head
        self._gate = threading.Event()
        self.killed = False

    def events(self):
        yield from self._head
        self._gate.wait(timeout=5)

    def kill(self) -> None:
        self.killed = True
        self._gate.set()


def init(session_id: str = "sess-1") -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "session_id": session_id}


def tool(name: str, **inputs: Any) -> dict[str, Any]:
    """One assistant event holding one tool call."""
    return {"type": "assistant", "message": {
        "id": f"m-{name}-{len(inputs)}-{id(inputs)}",
        "content": [{"type": "tool_use", "name": name, "input": inputs}],
    }}


def read(path: str) -> dict[str, Any]:
    return tool("Read", file_path=path)


def write(path: str, name: str = "Write") -> dict[str, Any]:
    if name == "apply_patch":
        return tool(name, input=f"*** Update File: {path}")
    return tool(name, file_path=path, content="x")


def success(text: str = "done", session_id: str = "sess-1") -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text,
            "session_id": session_id, "usage": {"input_tokens": 1, "output_tokens": 1},
            "num_turns": 1}


def completed(session_id: str = "sess-1") -> list[dict[str, Any]]:
    return [init(session_id), {"type": "assistant", "message": {
        "content": [{"type": "text", "text": "done"}]}}, success(session_id=session_id)]


def echo_resume(argv: list[str], env: dict[str, str]) -> ScriptedProcess:
    """A child that answers in the session it was resumed into, else a new one."""
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-fresh"
    return ScriptedProcess(completed(sid), argv, env)


def provider(**extra: Any) -> dict[str, dict[str, Any]]:
    """The `[providers]` table with one claude-driver provider, `glm`."""
    return {"glm": {**default_providers()["glm"], **extra}}


def registry(tmp_path: Path, monkeypatch, makers, *, provider_extra: dict[str, Any] | None = None,
             **core: Any) -> Registry:
    """A Registry whose children come from `makers`, one per spawn (the last repeats).

    A maker is `callable(argv, env) -> process`. `reg.spawned` lists the
    processes in spawn order.
    """
    settings = make_settings(tmp_path, providers=provider(**(provider_extra or {})), **core)
    spawned: list[Any] = []

    def spawn(argv, env, cwd):
        maker = makers[len(spawned)] if len(spawned) < len(makers) else makers[-1]
        spawned.append(maker(argv, env))
        return spawned[-1]

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    reg.spawned = spawned  # type: ignore[attr-defined]
    return reg


def script(events: list[dict[str, Any]], **kw: Any):
    """A maker for a scripted child."""
    return lambda argv, env: ScriptedProcess(events, argv, env, **kw)


def blocked(head: list[dict[str, Any]] | None = None):
    """A maker for a child that starts, then hangs until killed."""
    return lambda argv, env: BlockedProcess(head if head is not None else [init()], argv, env)


def wait(run, timeout: float = 5.0):
    assert run.done.wait(timeout), f"run did not finish: {run.state} {run.error}"
    return run


def wait_started(reg: Registry, run, timeout: float = 2.0) -> None:
    """Until the run has a deadline and its child has been spawned."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if run.deadline is not None and reg.spawned:  # type: ignore[attr-defined]
            return
        time.sleep(0.01)
    raise AssertionError("run never started")


def run_records(trace_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in trace_records if r.get("kind") == "run"]
