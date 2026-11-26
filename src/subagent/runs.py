"""Agent and run registry.

One delegated subagent == one Claude Code session id == one worker thread
holding a serial run queue. Each run is one ``claude -p`` subprocess. Continue
re-enters the same session via ``--resume``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import importlib
import itertools
import json
import math
import os
import secrets
import stat
import sys
import threading
import time
import uuid
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import adapter, envelope, health, providers, router
from .config import (
    APPROVAL_HOOK,
    Settings,
    child_egress_environment,
    log,
    prepare_private_directory,
)
from .egress import EgressProxy
from .goal import Goal
from .goal_runtime import GoalReviewer, evaluate_goal, pending_uat
from .guard.classify import _git_metadata_roots, protect, provider_guard_context
from .lane_state import LaneState
from .lane_status import LaneStatusStore, codex_usage
from .providers.base import (
    DRIVER_ANTIGRAVITY,
    DRIVER_CODEX,
    DRIVER_DSH,
    Process,
    ProviderConfig,
    RunContext,
    Session,
    capabilities,
)

# classify_exit and exit_event are re-exported: they were part of this
# module's surface before the drivers moved into providers/.
from .providers.claude import (  # noqa: F401
    ClaudeProcess,
    _strip_model_warning,
    classify_exit,
    exit_event,
)
from .providers.omp_config import OMP_HOOK
from .router import FALLBACK_MODES
from .sandbox import (
    agent_profile_kwargs,
    approval_socket_write_deny,
    check_platform,
    egress_port,
    normalize_paths,
    wrap_argv,
)
from .secrets import redact_secrets
from .telemetry.cost import Pricing, price_run
from .telemetry.sampler import Telemetry, open_metrics, summarize
from .telemetry.source_usage import INPUT_SOURCES, make_source_usage
from .telemetry.trace import Trace, open_trace
from .verify import VerificationResult, run_verification

WORKING = "working"
COMPLETED = "completed"
COMPLETED_UNVERIFIED = "completed_unverified"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, COMPLETED_UNVERIFIED, FAILED, CANCELLED})

# Drivers whose child runs the guard hook: each gets an egress proxy (A10) and,
# on macOS, one Seatbelt profile (A9). codex and dsh build that profile in their
# own spawn; the server wraps claude and omp here. antigravity has no hook: the
# same server-built profile and proxy are its only boundary (R1a-agy G1).
_HOOK_DRIVERS = frozenset({"claude", "codex", "omp", "dsh"})
_PROFILE_DRIVERS = _HOOK_DRIVERS | {"antigravity"}
_SERVER_WRAPPED_DRIVERS = frozenset({"claude", "omp", "antigravity"})

PHASE_QUEUED = "queued"
PHASE_RUNNING = "running"
PHASE_VERIFYING = "verifying"
PHASE_DISTILLING = "distilling"
PHASE_DONE = "done"
PHASE_AWAITING_APPROVAL = "awaiting_approval"
PHASE_AWAITING_HUMAN = "awaiting_human"

KILL_CANCEL = "cancel"
KILL_TIMEOUT = "timeout"
KILL_LOOP = "loop"
KILL_BUDGET = "budget"
KILL_STEPS = "steps"
KILL_NO_PROGRESS = "no_progress"
KILL_SHUTDOWN = "shutdown"
KILL_IDLE = "idle"
KILL_IS_FAILURE = frozenset({
    KILL_TIMEOUT, KILL_LOOP, KILL_BUDGET, KILL_STEPS, KILL_NO_PROGRESS,
})

DISTIL_PROMPT = """\
Stop working on the task. Summarize what you did, for a different engineer who \
has none of your context and will not see this conversation.

Write exactly these seven sections, each as a markdown heading:

## Goal
## Constraints & Preferences
## Progress
State Done, In-Progress and Blocked separately.
## Key Decisions
## Next Steps
## Relevant Files
## Critical Context

Rules:
- Preserve exact file paths, function names, error messages and command output. \
Do not paraphrase an identifier or a path.
- State what you changed in the repository, file by file.
- Preserve any question you could not answer, verbatim.
- Do not continue the task, propose new work, or take any tool call. Summarize only.
- Stay under {limit} characters."""



class RegistryError(RuntimeError):
    """A caller-visible problem: unknown id, capacity, or a closed agent."""


def _approval_key_file(settings: Settings, agent_id: str) -> Path:
    """The private file carrying one agent's approval socket key."""
    return settings.session_root / "agents" / agent_id / "approval.key"


def _load_or_create_approval_key(
    settings: Settings, agent_id: str, *, fresh: bool
) -> bytes:
    """Load an adopted key or create a new key atomically with mode 0600."""
    path = _approval_key_file(settings, agent_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def read_existing() -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != getattr(os, "getuid", lambda: info.st_uid)()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise RegistryError(f"approval key file for agent {agent_id!r} is not private")
            raw = os.read(fd, 256).decode("ascii").strip()
        finally:
            os.close(fd)
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise RegistryError(f"approval key file for agent {agent_id!r} is malformed") from exc
        if len(key) != 32:
            raise RegistryError(f"approval key file for agent {agent_id!r} is malformed")
        return key

    if not fresh:
        try:
            return read_existing()
        except FileNotFoundError:
            pass

    key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        if fresh:
            raise RegistryError(
                f"approval key already exists for agent {agent_id!r}"
            ) from None
        return read_existing()
    try:
        view = memoryview((key.hex() + "\n").encode("ascii"))
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    else:
        os.close(fd)
    return key


def _now() -> float:
    return time.time()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _truncate(text: str, cap: int) -> str:
    marker = "\n\n[truncated; call transcript(run_id, raw=True) for the full response]"
    keep = max(0, cap - len(marker))
    return text[:keep] + marker


def _sleep(seconds: float) -> None:
    """One sleep slice. Module-level indirection so tests can patch the clock."""
    time.sleep(seconds)


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    steps: int = 0  # tool calls
    turns: int = 0  # model turns, as --max-turns counts them
    credits: float | None = None  # Copilot AI credits (nano-AIU / 1e9)
    # A service's whole-run snapshot. Its wire fields and total are authoritative.
    native: dict[str, Any] | None = None

    @property
    def total(self) -> int:
        if self.native is not None:
            return self.native.get("total", 0)
        return self.input + self.cache_read + self.cache_write + self.output

    def add(self, usage: dict[str, Any]) -> None:
        self.input += int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
        self.output += int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
        self.cache_read += int(
            usage.get("cache_read_input_tokens") or usage.get("cacheReadTokens") or 0
        )
        self.cache_write += int(
            usage.get("cache_creation_input_tokens") or usage.get("cacheWriteTokens") or 0
        )
        self.reasoning += int(usage.get("reasoning_output_tokens") or 0)
        credits = usage.get("credits")
        if isinstance(credits, (int, float)):
            self.credits = (self.credits or 0.0) + float(credits)

    def as_dict(self) -> dict[str, Any]:
        if self.native is not None:
            return copy.deepcopy(self.native)
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
            "total": self.total,
            "steps": self.steps,
            "turns": self.turns,
            "credits": self.credits,
        }

    def set_native(self, usage: dict[str, Any], credits: float | None = None) -> None:
        self.native = copy.deepcopy(usage)
        for key in (*_USAGE_KEYS, "steps", "turns"):
            setattr(self, key, usage.get(key, 0))
        self.credits = credits

    def merge(self, other: Usage) -> None:
        native = None
        if self.native is not None or other.native is not None:
            before = self.as_dict()
            incoming = other.as_dict()
            if self.native is None and self.total == 0 and not self.steps and not self.turns:
                native = copy.deepcopy(incoming)
            else:
                native = copy.deepcopy(before)
                for key in (*_USAGE_KEYS, "total", "steps", "turns"):
                    native[key] = before.get(key, 0) + incoming.get(key, 0)
                if "usage_unreported" in before or "usage_unreported" in incoming:
                    native["usage_unreported"] = list(dict.fromkeys(
                        [*before.get("usage_unreported", []), *incoming.get("usage_unreported", [])]
                    ))
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.steps += other.steps
        self.turns += other.turns
        if other.credits is not None:
            self.credits = (self.credits or 0.0) + other.credits
        if native is not None:
            if "credits" in native:
                native["credits"] = self.credits
            self.native = native


def _assistant_text(event: dict[str, Any]) -> str:
    message = event.get("message") if isinstance(event.get("message"), dict) else event
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts).strip()


def _tool_uses(event: dict[str, Any]) -> list[tuple[str, Any]]:
    message = event.get("message") if isinstance(event.get("message"), dict) else event
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    found = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            found.append((str(block.get("name") or "?"), block.get("input")))
    return found


def _tool_signature(name: str, args: Any) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return f"{name}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"


def _hit_max_turns(event: dict[str, Any]) -> bool:
    """Claude Code's own report that it stopped at --max-turns."""
    return event.get("type") == "result" and (
        event.get("subtype") == "error_max_turns" or event.get("terminal_reason") == "max_turns"
    )


class _Watch:
    """Loop and token-budget trips for one attempt, decided as events arrive.

    Starts from what the run has already ingested, so a trip is judged on the
    whole run, not one attempt. Nothing here is written back to the run: a
    rate-limited attempt that is discarded must not count twice.

    It also carries the no-progress watchdog (R7a): a run with no write or edit
    tool call is stopped after `no_progress_steps` tool calls or
    `no_progress_seconds` of run time. That trip is written back to the run,
    because the result and the trace report what the worker read.
    """

    # Tool calls that count as progress: the worker changed something.
    WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"})

    def __init__(
        self,
        run: Run,
        settings: Settings,
        seen: Counter[str],
        max_steps: int | None = None,
        *,
        no_progress_steps: int = 0,
        no_progress_seconds: float = 0.0,
    ):
        self.settings = settings
        self.max_steps = settings.max_steps if max_steps is None else max_steps
        # Counted across the agent's runs: a loop split over continues is
        # still a loop.
        self.signatures: Counter[str] = Counter(seen)
        self.base_tokens = run.usage.total
        self.messages: dict[str, int] = {}
        self.run = run
        self.started_at = run.started_at if run.started_at is not None else _now()
        self.no_progress_steps = no_progress_steps
        self.no_progress_seconds = no_progress_seconds
        self.steps_seen = 0
        self.reads: list[str] = []
        self.wrote = False

    def _no_progress(self, reason: str, limit: int | float) -> tuple[str, str] | None:
        self.run.no_progress = {"reads": list(self.reads), "steps": self.steps_seen}
        self.run.overrun = {"kind": KILL_NO_PROGRESS, "limit": limit}
        return (KILL_NO_PROGRESS, reason)

    def _watch_progress(self, event: dict[str, Any]) -> tuple[str, str] | None:
        """Count this event's tool calls and trip if the run only reads."""
        for name, args in _tool_uses(event):
            self.steps_seen += 1
            if name in self.WRITE_TOOLS:
                self.wrote = True
            elif name == "Read" and isinstance(args, dict):
                path = args.get("file_path")
                if isinstance(path, str) and path not in self.reads:
                    self.reads.append(path)
        if self.wrote:
            return None
        if self.no_progress_steps > 0 and self.steps_seen >= self.no_progress_steps:
            return self._no_progress(
                f"no write or edit in {self.steps_seen} tool calls "
                f"(no_progress_steps = {self.no_progress_steps})",
                self.no_progress_steps,
            )
        if (self.no_progress_seconds > 0
                and _now() - self.started_at >= self.no_progress_seconds):
            return self._no_progress(
                f"no write or edit in {_now() - self.started_at:g}s "
                f"(no_progress_seconds = {self.no_progress_seconds:g})",
                self.no_progress_seconds,
            )
        return None

    def see(self, event: dict[str, Any]) -> tuple[str, str] | None:
        if _hit_max_turns(event):
            # Set during the attempt, so an exit that also reads as a rate
            # limit is not retried.
            return (
                KILL_STEPS,
                f"reached --max-turns of {self.max_steps} ([core].max_steps) "
                "before finishing",
            )
        if event.get("type") != "assistant":
            return None
        no_progress = self._watch_progress(event)
        if no_progress is not None:
            return no_progress
        for name, args in _tool_uses(event):
            sig = _tool_signature(name, args)
            self.signatures[sig] += 1
            if self.signatures[sig] >= self.settings.loop_strikes:
                return (KILL_LOOP, f"identical tool call repeated {self.signatures[sig]} times")
        budget = self.settings.turn_token_budget
        message = event.get("message")
        if budget is None or not isinstance(message, dict):
            return None
        usage = message.get("usage")
        if isinstance(usage, dict):
            # One API response streams as several assistant events carrying the
            # same message id and usage; count each response once.
            spent = Usage()
            spent.add(usage)
            self.messages[str(message.get("id") or len(self.messages))] = spent.total
        used = self.base_tokens + sum(self.messages.values())
        if used > budget:
            return (
                KILL_BUDGET,
                f"run used about {used} tokens, over [core].turn_token_budget of {budget}",
            )
        return None


_USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")


def _usage_fields(raw: Any) -> dict[str, int]:
    """A usage dict in the Usage field names, from claude or codex-translated keys."""
    parsed = Usage()
    if isinstance(raw, dict):
        parsed.add(raw)
    return {key: getattr(parsed, key) for key in _USAGE_KEYS}


def _max_fields(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {key: max(a.get(key, 0), b.get(key, 0)) for key in _USAGE_KEYS}


def _tool_result_text_chars(event: dict[str, Any]) -> tuple[int, bool]:
    """Count text in normalized tool-result events, or flag an unknown shape."""
    try:
        return _tool_result_text_chars_unchecked(event)
    except Exception:  # noqa: BLE001 - malformed source events are unmeasured
        return 0, False


def _tool_result_text_chars_unchecked(event: dict[str, Any]) -> tuple[int, bool]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return 0, content in (None, "")
    chars = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        value = block.get("content")
        if isinstance(value, str):
            chars += len(value)
        elif isinstance(value, list):
            for part in value:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        chars += len(text)
                    else:
                        return chars, False
        elif value is not None:
            return chars, False
    return chars, True


class _Meter:
    """Token and timing accounting for one attempt, fed every event as it arrives.

    Usage is read per assistant message, not only from the final `result`
    event, so an attempt that dies, times out or is cancelled still accounts
    for what it spent. One API response streams as several events carrying the
    same message id (one per content block, plus the partial-message stream
    events); each id is counted once, taking the largest value seen per field,
    since a later event of the same message carries the more complete count.

    The attempt's usage is, per field, the larger of the per-message sum and
    the result event's own usage: the result is the CLI's aggregate when it
    exists, the sum is what survives when it does not.

    Timing per message: ts_start is when the request went out (the attempt's
    spawn, the previous tool result, or the previous message's end);
    ts_first_token the first content event of that message (the first
    content_block_delta when partial messages are streamed, else the first
    assistant event); ts_end its last event.
    """

    def __init__(self, attempt: int, spawned_at: float):
        self.attempt = attempt
        self.spawned_at = spawned_at
        self.turns: dict[str, dict[str, Any]] = {}
        self.result_usage: dict[str, int] | None = None
        self.first_assistant: float | None = None
        self._mark = spawned_at
        self._current: str | None = None
        # The message the partial-message stream is on. Kept apart from
        # _current: a denied tool's result can arrive before the message's
        # own message_delta (which carries its usage and stop_reason), and
        # that delta still belongs to the message that started the stream.
        self._stream: str | None = None
        self._pending_tool_result_chars = 0
        self._pending_source_unmeasured: set[str] = set()

    def _turn(self, message_id: Any, now: float, model: Any = None) -> dict[str, Any]:
        key = str(message_id) if message_id else (self._current or f"attempt-{self.attempt}")
        turn = self.turns.get(key)
        if turn is None:
            turn = {
                "message_id": str(message_id) if message_id else None,
                "model": None,
                "ts_start": self._mark,
                "ts_first_token": None,
                "ts_end": None,
                "usage": dict.fromkeys(_USAGE_KEYS, 0),
                "tool_calls": [],
                "tool_ids": set(),
                "stop_reason": None,
                "source_tool_result_chars": self._pending_tool_result_chars,
                "source_unmeasured": set(self._pending_source_unmeasured),
            }
            self._pending_tool_result_chars = 0
            self._pending_source_unmeasured.clear()
            self.turns[key] = turn
        if isinstance(model, str) and model:
            turn["model"] = model
        self._current = key
        return turn

    def see(self, event: dict[str, Any], now: float) -> None:
        kind = event.get("type")
        if kind == "stream_event":
            inner = event.get("event") if isinstance(event.get("event"), dict) else {}
            sub = inner.get("type")
            if sub == "message_start":
                message = inner.get("message") if isinstance(inner.get("message"), dict) else {}
                turn = self._turn(message.get("id"), now, message.get("model"))
                turn["usage"] = _max_fields(turn["usage"], _usage_fields(message.get("usage")))
                self._stream = self._current
                return
            key = self._stream or self._current
            if key is None or key not in self.turns:
                return
            turn = self.turns[key]
            if sub in ("content_block_start", "content_block_delta"):
                if turn["ts_first_token"] is None:
                    turn["ts_first_token"] = now
                if self.first_assistant is None:
                    self.first_assistant = now
            elif sub == "message_delta":
                turn["usage"] = _max_fields(turn["usage"], _usage_fields(inner.get("usage")))
                delta = inner.get("delta") if isinstance(inner.get("delta"), dict) else {}
                if delta.get("stop_reason"):
                    turn["stop_reason"] = delta["stop_reason"]
                turn["ts_end"] = max(turn["ts_end"] or now, now)
            elif sub == "message_stop":
                turn["ts_end"] = max(turn["ts_end"] or now, now)
                if self._current == key:
                    # No tool result since: the next request goes out now.
                    self._mark = now
                self._stream = None
            return
        if kind == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if message.get("model") == "<synthetic>":
                # Claude Code's stand-in for an API error: no model answered.
                return
            turn = self._turn(message.get("id"), now, message.get("model"))
            if self.first_assistant is None:
                self.first_assistant = now
            if turn["ts_first_token"] is None:
                turn["ts_first_token"] = now
            turn["ts_end"] = max(turn["ts_end"] or now, now)
            turn["usage"] = _max_fields(turn["usage"], _usage_fields(message.get("usage")))
            if message.get("stop_reason"):
                turn["stop_reason"] = message["stop_reason"]
            content = message.get("content")
            for index, block in enumerate(content if isinstance(content, list) else []):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                block_id = block.get("id") or f"{len(turn['tool_calls'])}:{index}"
                if block.get("id") and block_id in turn["tool_ids"]:
                    continue
                turn["tool_ids"].add(block_id)
                turn["tool_calls"].append(str(block.get("name") or "?"))
            self._mark = now
            return
        if kind == "user":
            # A tool result: the next message's request goes out now.
            chars, observed = _tool_result_text_chars(event)
            target = self.turns.get(self._current) if self._current is not None else None
            if target is None:
                self._pending_tool_result_chars += chars
                if not observed:
                    self._pending_source_unmeasured.add("tool_results")
            else:
                target["source_tool_result_chars"] += chars
                if not observed:
                    target["source_unmeasured"].add("tool_results")
            self._mark = now
            self._current = None
            return
        if kind == "result":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.result_usage = _usage_fields(usage)

    def usage(self) -> dict[str, int]:
        """The attempt's tokens: per field, max(sum over messages, result usage)."""
        summed = dict.fromkeys(_USAGE_KEYS, 0)
        for turn in self.turns.values():
            for key in _USAGE_KEYS:
                summed[key] += turn["usage"][key]
        if self.result_usage is None:
            return summed
        return _max_fields(summed, self.result_usage)

    def records(self) -> list[dict[str, Any]]:
        """Turn records, in order. A driver that reports usage only per attempt
        (codex's turn.completed) has it put on the attempt's last turn."""
        turns = list(self.turns.values())
        if (
            turns
            and self.result_usage is not None
            and not any(any(t["usage"].values()) for t in turns)
        ):
            turns[-1]["usage"] = dict(self.result_usage)
        if turns and self._pending_tool_result_chars:
            turns[-1]["source_tool_result_chars"] += self._pending_tool_result_chars
            turns[-1]["source_unmeasured"].update(self._pending_source_unmeasured)
        out = []
        for turn in turns:
            out.append({
                "message_id": turn["message_id"],
                "model": turn["model"],
                "attempt": self.attempt,
                "ts_start": round(turn["ts_start"], 3),
                "ts_first_token": _round(turn["ts_first_token"]),
                "ts_end": _round(turn["ts_end"]),
                **turn["usage"],
                "tool_calls": list(turn["tool_calls"]),
                "stop_reason": turn["stop_reason"],
                "_source_tool_result_chars": turn["source_tool_result_chars"],
                "_source_unmeasured": list(turn["source_unmeasured"]),
            })
        return out


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


class _DoneEvent(threading.Event):
    """Signal terminal state immediately, with a tiny bounded status flush grace."""

    def __init__(self, status_ready: threading.Event):
        super().__init__()
        self._status_ready = status_ready

    def wait(self, timeout: float | None = None) -> bool:
        started = time.monotonic()
        if not super().wait(timeout):
            return False
        remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
        grace = 0.02 if remaining is None else min(0.02, remaining)
        if grace > 0:
            self._status_ready.wait(grace)
        return True


@dataclass
class Run:
    run_id: str
    agent_id: str
    prompt: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex, kw_only=True)
    verification: str | None = None
    # Where `verification` came from, when the caller did not pass it.
    verification_note: str | None = None
    goal: Goal | None = None
    uat: list[dict[str, Any]] | None = None
    state: str = WORKING
    phase: str = PHASE_QUEUED
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    final_response: str = ""
    result_text: str = ""
    distilled: bool = False
    truncated: bool = False
    # Loop runs read `.subagent/result.json`, not the distilled text, so they
    # skip the distillation turn (`distill=False`).
    distill: bool = True
    finish_reason: str | None = None
    error: str | None = None
    error_detail: str | None = None
    warning: str | None = None
    event_count: int = 0
    transcript: deque[str] = field(default_factory=lambda: deque(maxlen=400))
    status_ready: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event)
    # MCP waits end at a human gate as well as at a terminal result. `done`
    # continues to mean terminal, for callers that require completion.
    await_ready: threading.Event = field(default_factory=threading.Event)
    admission_ready: threading.Event = field(default_factory=threading.Event)
    admission_error: str | None = None
    cancel_pending: bool = False
    native_outcome: dict[str, Any] | None = None
    native_usage: bool = False
    usage_unreported: list[str] = field(default_factory=list)
    native_verification: bool = False
    signatures: Counter[str] = field(default_factory=Counter)
    usage: Usage = field(default_factory=Usage)
    cost: dict[str, Any] | None = None
    verification_result: VerificationResult | None = None
    trip: tuple[str, str] | None = None
    deadline: float | None = None
    # Per-run caps (R7a): the caller's request and the effective limit, plus
    # the public `caps` view. Both stay None when the caller asked for none.
    time_limit: float | None = None
    step_limit: int | None = None
    caps: dict[str, Any] | None = None
    # The limit this run overran, written to the trace as extra.overrun.
    overrun: dict[str, Any] | None = None
    # The reaper ended this run on its deadline. Its trip must not make a
    # later pass close the agent, and its deadline must not be re-end_run.
    ended_by_end_run: bool = False
    # The run record has been written (or could not be: no trace, or a failed
    # write). A later finish pass must not write a second one.
    trace_written: bool = False
    # A no-progress stop: {"reads": [...], "steps": n}, visible in detail().
    no_progress: dict[str, Any] | None = None
    # Agent ids the registry released to make room for this run (S65).
    released_agents: list[str] = field(default_factory=list)
    _human_wait_started_at: float | None = field(default=None, repr=False)
    _timeout_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    session_id: str | None = None
    lane: str | None = None
    # The lane whose _collect() was entered for the current run, even if it
    # crashes or is cancelled before the router can append a HOP_RAN record.
    attempted_lane: str | None = field(default=None, repr=False)
    fallback: str | None = None
    provider: str | None = None
    # The driver ("claude" / "codex") and guard ("hook" / "sandbox+hook") of
    # the lane the run is on; a routed run takes the values of the lane that ran.
    driver: str | None = None
    guard: str | None = None
    # Set on a delegate's run: it walks the lane chain. Continues never do.
    route: bool = False
    # Lanes the router tried, as router.Hop dicts, in order.
    hops: list[dict[str, Any]] = field(default_factory=list)
    # Whether the child did any work in this run: a tool call or output tokens.
    worked: bool = False
    cold_load: bool = False
    # Turn records written so far (runs._Meter.records, plus run-level keys).
    turn_log: list[dict[str, Any]] = field(default_factory=list)
    # Dispatch of the attempt that answered first -> its first assistant event.
    ttft_seconds: float | None = None
    # Runs this agent had before this one: 0 for the delegate.
    continues: int = 0
    # Caller-supplied context (the parent's request metadata), when any.
    parent: dict[str, Any] | None = None
    # The local lane this run was sampled on, and the samples (ts, snapshot).
    sampling_lane: str | None = None
    samples: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    # Caller hooks: `pre_verify()` runs in a finally before verification (the
    # loop's test-guard release) and also on cancel/timeout; its return
    # (`violations`, `changed_files`) lands in `pre_verify_result`. `on_event`
    # overrides the agent-level watcher for this run only. `pre_run()` runs
    # immediately before routing or continuing the child turn.
    pre_run: Callable[[], None] | None = None
    pre_verify: Callable[[], dict] | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None
    pre_verify_result: dict | None = None
    # Guard denials the supervisor recorded for this run (the loop drains them).
    guard_verdicts: list[dict[str, Any]] = field(default_factory=list)
    # Known lengths for source-tagged prompt text; absent metadata means the
    # entire submitted prompt is treated as harness text.
    source_chars: dict[str, int] | None = None

    def __post_init__(self) -> None:
        self.done = _DoneEvent(self.status_ready)

    def observe_native_wait(self, outcome: dict[str, Any], observed_at: float) -> None:
        """Charge active work only; repeated human-wait snapshots add no time."""
        waiting = outcome.get("state") == WORKING and bool(outcome.get("needs_human"))
        with self._timeout_lock:
            if waiting and self._human_wait_started_at is None:
                self._human_wait_started_at = observed_at
            elif not waiting and self._human_wait_started_at is not None:
                if self.deadline is not None:
                    self.deadline += max(0, observed_at - self._human_wait_started_at)
                self._human_wait_started_at = None

    def timeout_expired(self, now: float) -> bool:
        with self._timeout_lock:
            return (self._human_wait_started_at is None and self.deadline is not None
                    and now >= self.deadline)

    def summary(self) -> dict[str, Any]:
        out = {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "state": self.state,
            "phase": self.phase,
            "lane": self.lane,
            "provider": self.provider,
            "driver": self.driver,
            "fallback": self.fallback,
            "task": _clip(self.prompt, 120),
            "finish_reason": self.finish_reason,
            "elapsed_seconds": round(
                (self.finished_at or _now()) - (self.started_at or self.created_at), 1
            ),
            "activity_count": self.event_count,
            "usage": self.usage.as_dict(),
            "last_activity": self.transcript[-1] if self.transcript else None,
        }
        if self.warning:
            out["warning"] = self.warning
        if self.caps:
            out["caps"] = copy.deepcopy(self.caps)
        if self.no_progress is not None:
            out["no_progress"] = copy.deepcopy(self.no_progress)
        if self.native_outcome is not None:
            for key in ("needs_human", "human_action", "plan_id", "usage_unreported",
                        "credits", "credits_unit", "cash_usd", "extra"):
                if key in self.native_outcome:
                    out[key] = copy.deepcopy(self.native_outcome[key])
        if self.usage_unreported and "usage_unreported" not in out:
            out["usage_unreported"] = list(self.usage_unreported)
        return out

    def detail(self) -> dict[str, Any]:
        out = self.summary()
        if self.native_outcome is not None:
            summary = out.copy()
            # Preserve extensions, but outer identity and cancellation state
            # belong to this registry, not to a service snapshot.
            out.update(copy.deepcopy(self.native_outcome))
            for key in ("run_id", "agent_id", "state", "phase", "lane", "provider",
                        "driver", "fallback", "finish_reason", "usage"):
                out[key] = summary[key]
            if self.hops:
                out["hops"] = list(self.hops)
            if self.cold_load and "cold_load" not in out:
                out["cold_load"] = True
            if self.error is not None:
                out["error"] = self.error
            if self.error_detail is not None:
                out["error_detail"] = self.error_detail
            if not self.native_verification:
                if self.verification_result is not None:
                    out["verification"] = self.verification_result.as_dict()
                if self.goal is not None:
                    out["uat"] = self.uat or []
            return out
        out["result"] = self.result_text
        if self.distilled:
            out["distilled"] = True
            out["raw_response_chars"] = len(self.final_response)
        if self.truncated:
            out["truncated"] = True
        if self.verification_result is not None:
            out["verification"] = self.verification_result.as_dict()
        if self.verification_note:
            out["verification_note"] = self.verification_note
        if self.goal is not None:
            out["uat"] = self.uat or []
        if self.hops:
            out["hops"] = list(self.hops)
        if self.cold_load:
            out["cold_load"] = True
        if self.error:
            out["error"] = self.error
        if self.error_detail:
            out["error_detail"] = self.error_detail
        return out

    @property
    def needs_human(self) -> bool:
        return bool((self.native_outcome or {}).get("needs_human"))

    def note(self, line: str) -> None:
        self.event_count += 1
        self.transcript.append(line)


class Agent:
    """One child session, driven by a serial task queue.

    The session runs on one provider and that provider's driver. A delegate
    may move along the chain before any work is done; from the first hop that
    runs, `cfg`, `driver` and `session_id` (a claude session_id or a codex
    thread_id) belong together for every continue.
    """

    def __init__(
        self,
        agent_id: str,
        approval_key: bytes,
        name: str,
        workspace: Path,
        model: str,
        settings: Settings,
        trace: Trace | None = None,
        cfg: ProviderConfig | None = None,
        fallback: str = "full",
        chain: list[ProviderConfig] | None = None,
        lane_state: LaneState | None = None,
        lane_status: LaneStatusStore | None = None,
        provider_load: Callable[[str, Agent], int] | None = None,
        telemetry: Telemetry | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        goal: Goal | None = None,
        goal_reviewer: GoalReviewer | None = None,
        delegate_options: dict[str, Any] | None = None,
        deny_read: list[str] | None = None,
        entry: str = "mcp",
        released_agents: list[str] | None = None,
        on_close: Callable[[str], None] | None = None,
    ):
        self.trace = trace
        # The route that started this agent's runs (MCP delegate, CLI loop,
        # exec wrapper); written on every run record.
        self.entry = entry
        self.telemetry = telemetry
        # The provider this agent runs on. The delegate's run may move it
        # along `chain` (Settings.chain) when a provider refuses before work;
        # after that every continue stays on the provider that ran.
        self.cfg = cfg or settings.provider()
        # Keep the initial policy across continuations and provider fallback.
        self.deny_read = normalize_paths([*self.cfg.deny_read, *(deny_read or [])])
        check_platform(self.deny_read)
        self.fallback = fallback
        self.driver = providers.for_driver(self.cfg.driver)
        # Provider name -> its boot result (None when it can run) and session.
        # Two provider configs may share a driver while carrying different
        # gates or driver-owned session state.
        self._booted: dict[str, str | None] = {}
        self._sessions: dict[str, Session] = {}
        self.chain = list(chain) if chain else [self.cfg]
        self.lane_state = lane_state
        self.lane_status = lane_status
        self._active_lane_context = None
        self._active_lane: str | None = None
        self._provider_load = provider_load
        # Called for every parsed event of every turn, as it arrives.
        self.on_event = on_event
        self.goal = goal
        self.goal_reviewer = goal_reviewer
        self.delegate_options = delegate_options or {}
        # Agents released to make room for this one's first run (S65); written
        # once, on that run's trace record.
        self._released_agents = list(released_agents or [])
        # Guard context carries configured child-secret names even before the
        # loop adds its protected tests and state allowlist.
        self.guard_context: dict[str, Any] = provider_guard_context(settings)
        self.agent_id = agent_id
        self.approval_key = approval_key
        self.name = name
        self.workspace = workspace
        self.model = model
        self.settings = settings
        self._on_close = on_close
        self._egress_proxy: EgressProxy | None = None
        self._egress_port: int | None = None
        self._egress_run_id: str | None = None
        self.session_id: str | None = None
        # The acceptance command the delegate was given; a continue that
        # names none is judged against it.
        self.delegate_verification: str | None = None
        # Tool-call signatures across every run of this agent (loop detector).
        self.signatures: Counter[str] = Counter()
        self.created_at = _now()
        self._runs: dict[str, Run] = {}
        self._order: list[str] = []
        self._queue: deque[Run] = deque()
        self._wake = threading.Condition()
        self._closing = False
        self._closed = False
        self._kill_kind: str | None = None
        self.last_activity = _now()
        self._ready = threading.Event()
        self._start_error: str | None = None
        self._current: Process | None = None
        # Publish a spawn's session and process before cancellation can inspect
        # them. Do not hold this lock while consuming the event stream.
        self._dispatch_lock = threading.RLock()
        self._cancel_lock = threading.Lock()
        self._cancelled_requests: set[str | None] = set()
        self._cancel_complete = threading.Event()
        self._cancel_complete.set()
        self._thread = threading.Thread(
            target=self._worker, name=f"sam-agent-{agent_id}", daemon=True
        )
        self._thread.start()

    def wait_admission(self, run: Run, timeout: float = 5.0) -> None:
        """Opt-in boundary for clients that require synchronous input errors."""
        if not capabilities(self.driver).admission_barrier:
            return
        if not run.admission_ready.wait(timeout):
            # A serial continuation may still be queued behind a human wait.
            # Cancel only that unsubmitted run, never the active service job.
            queued = False
            finished = False
            with self._dispatch_lock, self._wake:
                queued = run.started_at is None
                if queued:
                    if run in self._queue:
                        self._queue.remove(run)
                    run.trip = (KILL_TIMEOUT, "provider admission timed out before submission")
                    finished = self._finish_locked(run)
            if queued:
                # This run never owned the agent's active lane lease; an
                # earlier human-waiting run may still be using it. A run that
                # already had its terminal state was recorded then.
                if finished:
                    self._record_lane_status(run, release_active_lane=False)
                raise RegistryError("provider admission timed out before submission")
            self.close("provider admission timed out", kind=KILL_TIMEOUT)
            raise RegistryError("provider admission timed out; cancellation requested")
        if run.admission_error:
            raise RegistryError(run.admission_error)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def busy(self) -> bool:
        return any(r.state == WORKING for r in self._runs.values())

    def wait_ready(self, timeout: float = 60.0) -> str | None:
        if not self._ready.wait(timeout):
            return f"runtime did not start within {timeout:g}s"
        return self._start_error

    def submit(
        self,
        prompt: str,
        verification: str | None = None,
        *,
        note: str | None = None,
        route: bool = False,
        parent: dict[str, Any] | None = None,
        pre_run: Callable[[], None] | None = None,
        pre_verify: Callable[[], dict] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        distill: bool = True,
        source_chars: dict[str, int] | None = None,
        request_id: str | None = None,
        time_limit: float | None = None,
        step_limit: int | None = None,
        caps: dict[str, Any] | None = None,
    ) -> Run:
        if self._closed or self._closing:
            raise RegistryError(f"agent {self.agent_id} is closed")
        run = Run(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            agent_id=self.agent_id,
            prompt=prompt,
            source_chars=(
                dict(source_chars)
                if source_chars is not None
                else {"harness_prompt": len(prompt), "plan": 0, "test_output": 0}
            ),
            verification=verification,
            verification_note=note,
            goal=self.goal,
            uat=pending_uat(self.goal) if self.goal is not None else None,
            lane=self.cfg.name,
            fallback=self.fallback,
            provider=self.cfg.vendor,
            driver=self.driver.name,
            native_usage=capabilities(self.driver).native_usage,
            guard=self.driver.guard(self.settings, self.cfg),
            route=route,
            parent=parent or None,
            pre_run=pre_run,
            pre_verify=pre_verify,
            on_event=on_event,
            distill=distill,
            time_limit=time_limit,
            step_limit=step_limit,
            caps=copy.deepcopy(caps),
            released_agents=list(self._released_agents),
            **({"request_id": request_id} if request_id and capabilities(self.driver).run_context
               else {}),
        )
        run.transcript = deque(maxlen=self.settings.transcript_limit)
        with self._wake:
            # A release claim may land between the check above and this queue
            # append. Re-check under the same lock the claim takes.
            if self._closed or self._closing:
                raise RegistryError(f"agent {self.agent_id} is closed")
            # Only a successful submit consumes the ids released for it: a
            # refused submit must keep them for the caller's retry.
            self._released_agents = []
            run.continues = len(self._order)
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            self._queue.append(run)
            self._wake.notify()
        return run

    def delegate(
        self,
        prompt: str,
        verification: str,
        parent: dict[str, Any] | None = None,
        pre_run: Callable[[], None] | None = None,
        pre_verify: Callable[[], dict] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        distill: bool = True,
        goal: Goal | None = None,
        reviewer: GoalReviewer | None = None,
        source_chars: dict[str, int] | None = None,
        request_id: str | None = None,
        timeout: float | None = None,
        max_steps: int | None = None,
    ) -> Run:
        """The agent's first run. Its verification is kept for continues.

        This run walks the lane chain; continues stay on the lane it ran on.

        `timeout` (seconds) and `max_steps` cap this run below the provider's
        own `run_timeout` and `max_steps`: the effective value is the smaller
        of the request and the provider limit, and `detail()['caps']` reports
        both. Zero or negative requests are refused.
        """
        time_limit, step_limit, caps = self._run_caps(timeout, max_steps)
        self.delegate_verification = verification
        if goal is not None:
            self.goal = goal
        if reviewer is not None:
            self.goal_reviewer = reviewer
        return self.submit(
            prompt,
            verification=verification,
            route=True,
            parent=parent,
            pre_run=pre_run,
            pre_verify=pre_verify,
            on_event=on_event,
            distill=distill,
            source_chars=source_chars,
            request_id=request_id,
            time_limit=time_limit,
            step_limit=step_limit,
            caps=caps,
        )

    @staticmethod
    def _step_limit_for(run: Run, cfg: ProviderConfig) -> int:
        """This hop's --max-turns: the run's cap clamped to the hop's lane."""
        if run.step_limit is None:
            return cfg.max_steps
        return min(run.step_limit, cfg.max_steps)

    @staticmethod
    def validate_caps(timeout: float | None, max_steps: int | None) -> None:
        """Refuse non-positive and NaN caps before an agent is created.

        A NaN cap compares false with every bound, so it would slip through
        `min()` and reach the provider as a limit that never triggers. `inf`
        is allowed: it clamps to the provider's own limit.
        """
        for name, value in (("timeout", timeout), ("max_steps", max_steps)):
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise RegistryError(f"{name} must be a number, got {value!r}") from None
            if math.isnan(number):
                raise RegistryError(f"{name} must be a finite number, got {value!r}")
            too_small = number < 1 if name == "max_steps" else number <= 0
            if too_small:
                raise RegistryError(f"{name} must be positive, got {value!r}")

    def _run_caps(
        self, timeout: float | None, max_steps: int | None
    ) -> tuple[float | None, int | None, dict[str, Any] | None]:
        """Validate per-run caps and clamp them to this agent's provider limits."""
        self.validate_caps(timeout, max_steps)
        caps: dict[str, Any] = {}
        time_limit: float | None = None
        step_limit: int | None = None
        if timeout is not None:
            time_limit = min(float(timeout), self.cfg.run_timeout)
            caps["timeout"] = {"requested": timeout, "effective": time_limit}
        if max_steps is not None:
            step_limit = int(min(float(max_steps), float(self.cfg.max_steps)))
            caps["max_steps"] = {"requested": max_steps, "effective": step_limit}
        return time_limit, step_limit, caps or None

    def follow_up(
        self,
        message: str,
        verification: str | None = None,
        parent: dict[str, Any] | None = None,
        pre_run: Callable[[], None] | None = None,
        pre_verify: Callable[[], dict] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        distill: bool = True,
        source_chars: dict[str, int] | None = None,
        request_id: str | None = None,
    ) -> Run:
        """A continue. Omitting `verification` reuses the delegate's; "" skips it.

        Wrap-up continues ("stop and report") were sent without one, so they
        ended completed_unverified by design and said nothing about whether
        the work was done. Judged against the original acceptance check, a
        completed continue means the task passes.
        """
        if (capabilities(self.driver).native_verification and self.runs()
                and not self.busy and self.session_id is None):
            raise RegistryError("native agent was never admitted; delegate a new task")
        if verification is None and self.delegate_verification:
            return self.submit(
                message,
                verification=self.delegate_verification,
                parent=parent,
                note=(
                    "verification omitted: reused the delegate command "
                    f"`{_clip(self.delegate_verification, 200)}`. Pass "
                    'verification="" to skip it.'
                ),
                pre_run=pre_run,
                pre_verify=pre_verify,
                on_event=on_event,
                distill=distill,
                source_chars=source_chars,
                request_id=request_id,
            )
        if verification is not None and not verification.strip():
            return self.submit(
                message,
                verification=None,
                parent=parent,
                note='verification="" given: skipped, so this run reports completed_unverified',
                pre_run=pre_run,
                pre_verify=pre_verify,
                on_event=on_event,
                distill=distill,
                source_chars=source_chars,
                request_id=request_id,
            )
        return self.submit(
            message,
            verification=verification,
            parent=parent,
            pre_run=pre_run,
            pre_verify=pre_verify,
            on_event=on_event,
            distill=distill,
            source_chars=source_chars,
            request_id=request_id,
        )

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def runs(self) -> list[Run]:
        return [self._runs[rid] for rid in self._order if rid in self._runs]

    def usage(self) -> Usage:
        total = Usage()
        for run in self.runs():
            total.merge(run.usage)
        return total

    def info(self) -> dict[str, Any]:
        state = "closed" if self._closed else ("busy" if self.busy else "idle")
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "model": self.model,
            "lane": self.cfg.name,
            "fallback": self.fallback,
            "workspace": str(self.workspace),
            "session_id": self.session_id,
            "state": state,
            "age_seconds": round(_now() - self.created_at, 1),
            "usage": self.usage().as_dict(),
            "runs": [r.summary() for r in self.runs()],
        }

    def try_release(self) -> bool:
        """Claim this agent for slot release, atomically against a new `submit`.

        The caller must hold the registry lock while it decides; this re-check
        under `_wake` is what makes that decision final: once `_closing` is set,
        a `submit` that raced into the gap is refused instead of enqueued on an
        agent that is about to be closed.
        """
        with self._wake:
            if self._closed or self._closing:
                return False
            if self._queue or any(
                run.state not in TERMINAL_STATES for run in self._runs.values()
            ):
                return False
            self._closing = True
            self._wake.notify_all()
            return True

    def close(self, reason: str = "cancelled by caller", kind: str = KILL_CANCEL) -> None:
        with self._wake:
            if self._closed:
                return
            self._cancel_complete.clear()
            self._closing = True
            self._kill_kind = kind
            self._wake.notify_all()
        with self._dispatch_lock:
            try:
                current = self._current
                self._cancel_provider(reason, kind)
                if current is not None:
                    current.kill()
            finally:
                self._cancel_complete.set()
        for run in self.runs():
            if run.state == WORKING:
                if run.cancel_pending:
                    continue
                run.state = FAILED if kind in KILL_IS_FAILURE else CANCELLED
                run.error = reason
                run.phase = PHASE_DONE
                run.finished_at = _now()
                run.done.set()
                run.await_ready.set()
                self._record_lane_status(run)
        self._thread.join(timeout=8)
        self._closed = not any(r.cancel_pending for r in self.runs())
        if self._closed:
            self._stop_egress_proxy()
            key_file = self.settings.session_root / "agents" / self.agent_id / "approval.key"
            try:
                key_file.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove approval key for %s", self.agent_id, exc_info=True)
            if self._on_close is not None:
                callback, self._on_close = self._on_close, None
                callback(self.agent_id)
        # A second close may resolve a previously uncertain native cancel after
        # the serial worker has stopped. Its retained guard still belongs here.
        for run in self.runs():
            if run.pre_verify_result is None and not run.cancel_pending:
                self._run_pre_verify(run)
                if not run.done.is_set():
                    self._finish(run)

    # How long end_run waits for a service-job worker to finish after the kill
    # before it finishes the run itself.
    END_RUN_WAIT = 2.0

    def end_run(
        self, run: Run, reason: str, kind: str, *, finish_stuck: bool = True
    ) -> None:
        """End one run on its deadline without closing the agent (R7a).

        The agent stays resumable: a continue starts a new run in the same
        provider session. Unlike `close`, this never sets `_closing`.

        `finish_stuck=False` is for the reaper, whose caller is itself bounded:
        it leaves a run that its worker may still finish WORKING for that pass,
        and a later pass finishes it if the worker remains stuck.
        """
        if run.done.is_set():
            return
        # A later reaper pass must not read this run's trip as a reason to
        # close the agent (the run is still WORKING while its worker drains).
        run.ended_by_end_run = True
        if run.trip is None:
            run.trip = (kind, reason)
        with self._dispatch_lock:
            self._cancel_provider(reason, kind, run)
            current = self._current
            if current is not None:
                current.kill()
        with self._wake:
            self._wake.notify_all()
        service = (
            capabilities(self.driver).cancel
            or capabilities(self.driver).native_outcome
        )
        # A service job can outlive the local kill (the cancel hook is remote);
        # a plain child should die with it, but a stuck stream may ignore the
        # kill too. Both get the same bounded window, then this side finishes
        # the run as `close` does. The reaper defers the plain case by one pass.
        # A plain-driver worker thread blocked in the child's stream iterator
        # is left to exit when the child's pipe closes; it is not replaced.
        if not run.done.wait(self.END_RUN_WAIT) and (service or finish_stuck):
            self._finish(run)

    def _boot(self) -> None:
        try:
            self._start_error = self._boot_driver(self.driver, self.cfg)
        finally:
            self._ready.set()

    def _boot_driver(self, driver: Any, cfg: ProviderConfig) -> str | None:
        """Boot `driver` for this agent once; the reason it cannot run, or None."""
        if cfg.name not in self._booted:
            try:
                error, session = driver.boot(self.settings, self.agent_id, cfg)
            except NotImplementedError as exc:
                error, session = str(exc), Session(provider=cfg.name)
            self._booted[cfg.name] = (
                redact_secrets(error, self.settings.leaked_keys) if error else None
            )
            self._sessions[cfg.name] = session
        return self._booted[cfg.name]

    def session(self) -> Session:
        """The current provider's per-agent session state."""
        found = self._sessions.get(self.cfg.name)
        if found is None:
            found = Session(provider=self.cfg.name)
            self._sessions[self.cfg.name] = found
        return found

    def _set_active_lane(self, lane: str | None) -> None:
        if lane == self._active_lane:
            return
        self._release_active_lane()
        if lane is None or self.lane_status is None:
            return
        lease = self.lane_status.active_run(lane)
        try:
            lease.__enter__()
        except Exception:  # noqa: BLE001 - a lease must never fail a run
            log.warning("could not hold active lane lease for %s", lane, exc_info=True)
            return
        self._active_lane_context = lease
        self._active_lane = lane

    def _release_active_lane(self) -> None:
        lease, self._active_lane_context = self._active_lane_context, None
        self._active_lane = None
        if lease is not None:
            with contextlib.suppress(Exception):
                lease.__exit__(None, None, None)

    def _record_lane_status(
        self, run: Run, *, release_active_lane: bool = True
    ) -> None:
        """Persist completed run observations after its public done signals."""
        store = self.lane_status
        if store is None:
            if release_active_lane:
                self._release_active_lane()
            run.status_ready.set()
            return
        try:
            observed = datetime.fromtimestamp(run.finished_at or _now(), UTC)
            observations: list[dict[str, Any]] = []
            for hop in run.hops:
                lane = hop.get("lane")
                if not isinstance(lane, str) or not lane:
                    continue
                outcome = hop.get("outcome")
                if (
                    outcome == router.HOP_REFUSED
                    and hop.get("code") not in (router.ADMISSION, router.ENVELOPE)
                ):
                    observations.append({
                        "lane": lane, "kind": "run", "finish_reason": "refused",
                        "code": hop.get("code"), "detail": hop.get("message"),
                        "at": observed,
                    })
                elif outcome == router.HOP_HEALTH_FAILED:
                    observations.append({
                        "lane": lane, "kind": "probe", "ok": False,
                        "detail": hop.get("message") or hop.get("code") or "health failed",
                        "at": observed,
                    })
            ran_lanes = {
                hop.get("lane") for hop in run.hops
                if hop.get("outcome") == router.HOP_RAN
            }
            hopped_lanes = {hop.get("lane") for hop in run.hops}
            if run.lane and (
                not run.hops
                or run.lane in ran_lanes
                or (run.attempted_lane == run.lane and run.lane not in hopped_lanes)
            ):
                observations.append({
                    "lane": run.lane, "kind": "run",
                    "finish_reason": run.finish_reason or run.state or "error",
                    "detail": run.error_detail or run.error, "at": observed,
                })
                if run.driver == DRIVER_CODEX:
                    from .providers.codex import source_codex_home

                    session = self._sessions.get(run.lane)
                    home = session.home if session is not None else None
                    usage = codex_usage(home) if home is not None else None
                    if usage is None:
                        usage = codex_usage(source_codex_home())
                    if usage is not None:
                        observations.append({
                            "lane": run.lane, "kind": "usage", "usage": usage,
                            "at": observed,
                        })
            store.record_observations(observations)
        except Exception:  # noqa: BLE001 - status memory must never fail a run
            log.warning("lane status update failed for %s", run.run_id, exc_info=True)
        finally:
            if release_active_lane:
                self._release_active_lane()
            run.status_ready.set()

    def _worker(self) -> None:
        self._boot()
        while True:
            with self._wake:
                while (not self._queue or any(r.cancel_pending for r in self._runs.values())) \
                        and not self._closing:
                    self._wake.wait(timeout=0.5)
                if self._closing and not self._queue:
                    break
                run = self._queue.popleft() if self._queue else None
            if run is None:
                continue
            try:
                self._execute(run)
            except Exception as exc:  # noqa: BLE001
                if capabilities(self.driver).native_outcome or capabilities(self.driver).cancel:
                    message = redact_secrets(
                        f"{type(exc).__name__}: {exc}", self.settings.leaked_keys
                    )
                    log.warning("run %s crashed: %s", run.run_id, message)
                    self._cancel_provider("provider run crashed", "error", run)
                    self._run_pre_verify(run)
                    if not self._closing:
                        run.state = FAILED
                        run.error = message
                        run.finish_reason = "error"
                    self._finish(run)
                    continue
                self._run_pre_verify(run)
                log.warning("run %s crashed", run.run_id, exc_info=True)
                run.state = FAILED
                run.error = redact_secrets(
                    f"{type(exc).__name__}: {exc}", self.settings.leaked_keys
                )
                run.phase = PHASE_DONE
                run.finished_at = _now()
                run.finish_reason = "error"
                self._finish(run)

    def _spawn(self, run: Run, prompt: str, resume: str | None) -> Process:
        """One turn's child process, on the current provider's driver."""
        paths = normalize_paths([*self.deny_read, *self.cfg.deny_read])
        check_platform(paths)
        if self.cfg.driver == DRIVER_ANTIGRAVITY and sys.platform != "darwin":
            # agy's own --sandbox is not a boundary; without the outer profile
            # there is nothing to run it under (R1a-agy G1).
            raise RuntimeError(
                "antigravity driver runs only under the server's macOS sandbox-exec profile"
            )
        self._egress_run_id = run.run_id
        if self.cfg.driver in _PROFILE_DRIVERS:
            self._start_egress_proxy()
        session = self.session()
        session.session_id = resume
        kwargs = {}
        if capabilities(self.driver).run_context:
            session.data["request_id"] = run.request_id
            kwargs["context"] = RunContext(
                verification=run.verification, goal=run.goal,
                request_id=run.request_id, deadline=run.deadline,
                **({key: self.delegate_options.get(key) for key in (
                    "task", "instructions", "name", "model", "fallback"
                )} if resume is None else {}),
            )
        # Codex must coordinate its inner sandbox mode with its outer profile.
        # Give it the frozen agent policy plus the current provider's exclusions.
        cfg = self.cfg
        step_limit = self._step_limit_for(run, cfg)
        if step_limit != cfg.max_steps:
            # A per-run step cap is Claude Code's --max-turns for this run only.
            cfg = dataclasses.replace(cfg, max_steps=step_limit)
        spawn_cfg = (dataclasses.replace(cfg, deny_read=paths)
                     if cfg.driver in (DRIVER_CODEX, DRIVER_DSH) else cfg)
        with child_egress_environment(self._egress_environment()), \
                egress_port(self._egress_port):
            process = self.driver.spawn(
                spawn_cfg, self.settings, self.agent_id, prompt, self.workspace,
                session, self.model, **kwargs,
            )
        child_env = getattr(process, "env", None)
        if isinstance(child_env, dict):
            child_env.update(self._egress_environment())
        cordis_path = getattr(process, "cordis_path", None)
        if cordis_path is not None:
            process.guard_ready_callback = self._dsh_guard_ready
            self.guard_context.update({
                "dsh_cordis_path": str(cordis_path),
                "dsh_cordis_sha256": None,
                "dsh_cordis_run_id": run.run_id,
                # A12: once a bootstrap change was seen, every later request
                # of this agent stays denied, across later launches too.
                "dsh_guard_tampered": bool(self.guard_context.get("dsh_guard_tampered")),
                "dsh_guard_tamper_traced": bool(
                    self.guard_context.get("dsh_guard_tamper_traced")
                ),
            })
        # Driver processes start lazily in events(), through Process._start().
        # Wrap before publishing or consuming one so all descendants inherit it.
        # codex and dsh build their one profile themselves (they coordinate
        # their inner sandbox mode with it); claude, omp and antigravity are
        # wrapped here.
        if (sys.platform == "darwin" and self.cfg.driver in _SERVER_WRAPPED_DRIVERS
                and isinstance(getattr(process, "argv", None), list)):
            driver_module = importlib.import_module(f".providers.{self.cfg.driver}", __package__)
            agent_tmp = prepare_private_directory(
                self.settings.session_root / "agents" / self.agent_id / "tmp"
            )
            if isinstance(child_env, dict):
                child_env["TMPDIR"] = str(agent_tmp)
            write_deny = [
                str(APPROVAL_HOOK), str(self.workspace / ".git"),
                *(str(root) for root in sorted(_git_metadata_roots(self.workspace))),
                # The driver's own guard configuration and auto-loaded files,
                # as codex denies its hooks.json, config.toml and auth.json.
                *driver_module.sandbox_write_deny(
                    self.settings, self.agent_id, self.cfg, self.workspace
                ),
            ]
            with egress_port(self._egress_port):
                profile = agent_profile_kwargs(
                    session_root=self.settings.session_root,
                    agent_id=self.agent_id,
                    approval_socket=self.settings.approval_socket,
                    # The loopback endpoint the child actually dials: for a
                    # claude lane with an adapter that is the adapter proxy
                    # (child_env points ANTHROPIC_BASE_URL at it), not the
                    # upstream, which only the server-side adapter reaches.
                    base_url=(adapter.child_base_url(self.cfg)
                              if self.cfg.driver == "claude" else self.cfg.base_url),
                    write_roots=driver_module.sandbox_write_roots(
                        self.settings, self.agent_id, self.cfg, self.workspace
                    ),
                    write_deny=write_deny,
                    workspace=self.workspace,
                    driver=self.cfg.driver,
                )
            if self.cfg.driver == DRIVER_ANTIGRAVITY:
                # Owner credential homes agy has no use for (G1.3), read-denied.
                profile["read_deny_subpaths"].extend(
                    getattr(process, "sandbox_read_deny", ())
                )
                # agy has no approval hook, so it has no reason to reach the socket.
                profile["network_unix_sockets"] = []
            process.argv = wrap_argv(process.argv, paths, **profile)
        elif paths and self.cfg.driver not in (DRIVER_CODEX, DRIVER_DSH):
            process.argv = wrap_argv(
                process.argv,
                paths,
                write_deny=[approval_socket_write_deny(
                    self.settings.approval_socket,
                    [self.workspace, self.settings.session_root],
                )],
            )
        if capabilities(self.driver).cancel and isinstance(process, Process):
            process.enable_cancel_fence()
        return process

    def _start_egress_proxy(self) -> None:
        if self._egress_proxy is not None:
            return
        allow = list(self.cfg.network_allow)
        parsed = urlsplit(self.cfg.base_url or "")
        if parsed.hostname:
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            allow.append(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")

        def refused(host: str, port: int) -> None:
            trace = self.trace
            if trace is not None and self._egress_run_id:
                trace.egress_refused(
                    agent_id=self.agent_id, run_id=self._egress_run_id,
                    provider=self.cfg.name, host=host, port=port,
                )

        proxy = EgressProxy(allow, on_refusal=refused)
        self._egress_port = proxy.start()
        self._egress_proxy = proxy

    def _egress_environment(self) -> dict[str, str]:
        if self._egress_port is None:
            return {}
        proxy = f"http://127.0.0.1:{self._egress_port}"
        return {
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "ALL_PROXY": proxy,
            "NO_PROXY": "127.0.0.1,localhost",
        }

    def _stop_egress_proxy(self) -> None:
        proxy, self._egress_proxy = self._egress_proxy, None
        self._egress_port = None
        if proxy is not None:
            proxy.stop()

    def _dsh_guard_ready(self, path: str, digest: str) -> None:
        self.guard_context.update({
            "dsh_cordis_path": path,
            "dsh_cordis_sha256": digest,
            "dsh_cordis_run_id": self._egress_run_id,
        })

    def _execute(self, run: Run) -> None:
        with self._dispatch_lock:
            if run.done.is_set():
                return
            closing = self._closing and run.started_at is None
            if not closing:
                run.started_at = _now()
        if closing:
            self._finish(run)
            return
        run.native_usage = capabilities(self.driver).native_usage
        limit = run.time_limit if run.time_limit is not None else self.cfg.run_timeout
        run.deadline = run.started_at + limit
        run.phase = PHASE_RUNNING
        self.last_activity = _now()
        if (not run.route and run.continues and capabilities(self.driver).native_verification
                and self.session_id is None):
            # A queued predecessor may have refused after follow_up was called.
            # A continuation can never turn into a fresh native delegate.
            run.admission_error = "native agent was never admitted; delegate a new task"
            run.error = run.admission_error
            run.state = FAILED
            run.finish_reason = "error"
            self._finish(run)
            return
        if run.pre_run is not None:
            run.pre_run()
        if run.route:
            if not self._route(run):
                self._run_pre_verify(run)
                self._finish(run)
                return
        else:
            if (
                self.driver.needs_api_key
                and self.cfg.extra.get("auth") != "login"
                and not self.cfg.api_key()
            ):
                run.state = FAILED
                run.error = self._missing_key(self.cfg)
                run.finish_reason = "auth"
                self._run_pre_verify(run)
                self._finish(run)
                return
            self._set_active_lane(self.cfg.name)
            self._turn(run, run.prompt, resume=self.session_id)
        if (capabilities(self.driver).confirmed_cancel
                and self.session().data.pop("cancellation_required", False)):
            self._cancel_provider("native admission could not be determined", "error", run)
        if run.native_outcome is not None and run.state == WORKING and not run.cancel_pending:
            self._follow_native(run)
        # Release the caller's guard only when all provider work has ended,
        # including service work that outlived the CLI. Exceptions reach the
        # worker's cleanup, which cancels native work before releasing it.
        self._run_pre_verify(run)
        if capabilities(self.driver).native_verification:
            if (run.native_outcome is None and run.state == WORKING
                    and not self._closing and not run.trip):
                run.state = FAILED
                run.finish_reason = "error"
                run.error = "provider with native verification returned no outcome"
            self._finish(run)
            return
        if run.native_outcome is not None and run.state in (COMPLETED, COMPLETED_UNVERIFIED):
            # Outcome transport alone does not opt out of the outer checks.
            run.state = WORKING
        if self._closing or run.trip or run.state in TERMINAL_STATES:
            self._finish(run)
            return

        run.phase = PHASE_VERIFYING
        remaining = (run.deadline - _now()) if run.deadline else None
        if remaining is not None and remaining <= 0:
            # The clock is already past the deadline. A live reaper would have
            # ended the run; without one, verify under [core].verify_timeout
            # rather than handing the command a zero-second budget.
            remaining = None
        run.verification_result = run_verification(
            run.verification or "",
            self.workspace,
            self.settings,
            budget=remaining,
        )

        if run.goal is not None:
            remaining = (run.deadline - _now()) if run.deadline else None
            run.uat = evaluate_goal(
                run.goal,
                self.workspace,
                self.settings,
                reviewer=self.goal_reviewer,
                budget=remaining,
            )

        if run.distill and len(run.final_response) > self.settings.result_cap_chars:
            run.phase = PHASE_DISTILLING
            distilled = self._distill(run)
            if distilled:
                run.result_text = distilled
                run.distilled = True
            else:
                run.result_text = _truncate(run.final_response, self.settings.result_cap_chars)
                run.truncated = True
        else:
            run.result_text = run.final_response

        if run.trip:
            self._finish(run)
            return
        goal_failed = any(
            row["kind"] == "command" and row["passed"] is not True
            for row in (run.uat or [])
        )
        goal_changed = bool(
            run.goal is not None
            and run.goal.path
            and any(
                row.get("file") == run.goal.path
                for row in (run.pre_verify_result or {}).get("violations", [])
            )
        )
        provider_completed = run.native_outcome is None or run.native_outcome["state"] == COMPLETED
        if (run.verification_result.passed and not goal_failed and not goal_changed
                and provider_completed):
            run.state = COMPLETED
        else:
            run.state = COMPLETED_UNVERIFIED
        self._finish(run)

    def _run_pre_verify(self, run: Run) -> None:
        """Call the run's pre_verify hook once and store its return.

        Never raises: a broken release must not mask the run's own outcome.
        """
        while True:
            self._cancel_complete.wait()
            with self._dispatch_lock:
                if self._closing and not self._cancel_complete.is_set():
                    continue
                if run.cancel_pending:
                    return
                self._release_pre_verify(run)
                return

    def _release_pre_verify(self, run: Run) -> None:
        if run.pre_verify is None or run.pre_verify_result is not None:
            return
        try:
            run.pre_verify_result = run.pre_verify() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("pre_verify failed for %s", run.run_id, exc_info=True)
            run.pre_verify_result = {"error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _missing_key(cfg: ProviderConfig) -> str:
        names = ", ".join(cfg.api_key_envs) or "(none configured)"
        return f"missing API key for provider {cfg.name!r}: set one of {names}"

    def _envelope_plan(self, run: Run) -> list[envelope.EnvelopeRow] | None:
        """Rows from the configured lane plan; None when the check is off or unusable."""
        plan = self.settings.envelope_lane_plan
        if plan is None:
            return None
        rows = envelope.load_plan(plan)
        if rows:
            return rows
        message = f"envelope check skipped: no usage envelopes table in {plan}"
        if self.trace is not None:
            self.trace.write("envelope_config_warning", run_id=run.run_id, message=message)
        return None

    def _envelope_record(
        self, kind: str, run: Run, decision: envelope.Decision, **extra: Any
    ) -> None:
        if self.trace is None:
            return
        self.trace.write(
            kind,
            run_id=run.run_id,
            lane=decision.lane,
            rule=decision.kind,
            level=decision.level,
            used=decision.used,
            limit=decision.limit,
            unit=decision.unit,
            reason=decision.reason,
            **extra,
        )

    def _check_envelope(
        self,
        run: Run,
        cfg: ProviderConfig,
        rows: list[envelope.EnvelopeRow],
        snapshot: dict[str, dict[str, Any]],
        override: str | None,
    ) -> tuple[envelope.Decision | None, str | None]:
        """Check one candidate lane before admission; record what the decision implies.

        Returns the refusing decision, or None and the warning text to attach
        only once the hop is actually used.
        """
        row = envelope.row_for(rows, cfg.name)
        if row is None:
            return None, None
        reading = envelope.reading_for(row, snapshot)
        decision = envelope.evaluate(row.rules, reading, lane=cfg.name)
        warning = None
        if decision.level == "refuse":
            if override:
                self._envelope_record(
                    "envelope_override", run, decision, entry=self.entry,
                    override_reason=override,
                )
            else:
                return decision, None
        elif decision.level == "warn":
            warning = decision.reason
            self._envelope_record("envelope_warning", run, decision)
        elif decision.level == "unknown":
            self._envelope_record(
                "envelope_no_reading", run, decision,
                missing=envelope.missing_keys(row.rules, reading),
            )
        return None, warning

    def _hop(self, run: Run, hop: router.Hop) -> None:
        if hop.message:
            hop.message = redact_secrets(hop.message, self.settings.leaked_keys)
        run.hops.append(hop.as_dict())
        run.note(f"hop {hop.index}: {hop.lane} {hop.outcome}"
                 + (f" ({hop.code})" if hop.code else ""))
        log.info("run %s hop %d: lane %s %s %s", run.run_id, hop.index, hop.lane,
                 hop.outcome, hop.code or "")
        if self.trace is not None:
            self.trace.hop(run_id=run.run_id, agent_id=self.agent_id, hop=hop)

    def _route(self, run: Run) -> bool:
        """Run a delegate on the first provider in the chain that takes it.

        Per provider: skip it when it is unavailable in this build, has no
        key, is full, is closed in provider memory, or its driver cannot
        start; run its health gate; spawn on its driver. A refusal before any
        work (the driver reads it: router.classify_refusal for claude,
        codex_refusal for codex) moves to the next provider and may close this
        one on disk. Anything else, including a failure after work started, is
        the run's result and ends the walk. Returns False when none ran it.
        """
        primary_model = self.model
        last_refusal: str | None = None
        harness_status = (
            self.lane_status.snapshot(
                self.settings,
                count_runs=self.settings.envelope_lane_plan is not None,
            ) if self.lane_status is not None else {}
        )
        envelope_rows = self._envelope_plan(run)
        override = self.delegate_options.get("override_envelope")
        override = override.strip() if isinstance(override, str) and override.strip() else None
        explicitly_named = bool(self.delegate_options.get("provider_explicit"))
        for index, cfg in enumerate(self.chain):
            if self._closing:
                return False
            model = primary_model if index == 0 else (cfg.model or "")
            driver = providers.for_driver(cfg.driver)
            hop = router.Hop(index, cfg.name, cfg.vendor, model, router.HOP_UNAVAILABLE,
                             driver=driver.name, guard=driver.guard(self.settings, cfg))
            harness_state = harness_status.get(cfg.name, {}).get("harness_state")
            explicit_primary = index == 0 and explicitly_named
            if router.skip_unverified_harness(harness_state, explicit=explicit_primary):
                hop.code = "harness_unverified"
                hop.message = f"provider {cfg.name!r} has an unverified harness"
                self._hop(run, hop)
                continue
            reason = cfg.unavailable()
            if (
                reason is None
                and driver.needs_api_key
                and cfg.extra.get("auth") != "login"
                and not cfg.api_key()
            ):
                reason = self._missing_key(cfg)
            if (
                reason is None
                and index > 0
                and self._provider_load is not None
                and self._provider_load(cfg.name, self) >= cfg.max_agents
            ):
                reason = f"provider {cfg.name!r} is at its agent limit ({cfg.max_agents})"
            if reason is not None:
                hop.message = reason
                self._hop(run, hop)
                continue
            entry = self.lane_state.closed(cfg.name) if self.lane_state else None
            if entry is not None:
                hop.outcome = router.HOP_SKIPPED_CLOSED
                hop.code = entry.get("code")
                hop.message = entry.get("message")
                hop.closed_until = entry["closed_until"]
                self._hop(run, hop)
                continue
            reason = self._boot_driver(driver, cfg)
            if reason is not None:
                hop.message = reason
                self._hop(run, hop)
                continue
            gate = health.check(cfg)
            if not gate.ok:
                hop.outcome = router.HOP_HEALTH_FAILED
                hop.code = router.HEALTH_FAILED
                hop.message = gate.message
                self._hop(run, hop)
                continue
            if gate.base_url and gate.base_url != cfg.base_url:
                cfg = dataclasses.replace(cfg, base_url=gate.base_url)
            if gate.cold_load and cfg.health.warm and gate.base_url:
                # Claude Code gives up on a request that waits out a container
                # start; wake the backend first, bounded by the cold-load time.
                warm = health.warm(cfg, gate.base_url,
                                   self.settings.cold_load_seconds(cfg.name))
                if not warm.ok:
                    hop.outcome = router.HOP_HEALTH_FAILED
                    hop.code = router.HEALTH_FAILED
                    hop.message = warm.message
                    self._hop(run, hop)
                    continue
            warning: str | None = None
            if envelope_rows is not None:
                decision, warning = self._check_envelope(
                    run, cfg, envelope_rows, harness_status, override
                )
                if decision is not None:
                    hop.outcome = router.HOP_REFUSED
                    hop.code = router.ENVELOPE
                    hop.message = decision.reason
                    self._hop(run, hop)
                    last_refusal = hop.message
                    continue
            if self.telemetry is not None and self.telemetry.covers(cfg):
                admission = self.telemetry.admission(cfg)
                hop.admission = admission.snapshot
                hop.would_refuse = admission.would_refuse
                hop.admit_reason = admission.reason
                if admission.would_refuse:
                    log.warning("run %s: provider %s over its admission thresholds (%s)%s",
                                run.run_id, cfg.name, admission.reason,
                                "; skipped" if admission.refuse else "; log-only")
                if admission.refuse:
                    # A skip for this dispatch, not a provider closure.
                    hop.outcome = router.HOP_REFUSED
                    hop.code = router.ADMISSION
                    hop.message = f"admission: {admission.reason}"
                    self._hop(run, hop)
                    last_refusal = hop.message
                    continue

            # Attach a warn only now that this hop is known to be used. A
            # warning from a lane that admission then skipped must not linger
            # on the run.
            if warning:
                run.warning = warning
            self.cfg = cfg
            self.driver = driver
            self.model = model
            self.session_id = None
            self._set_active_lane(cfg.name)
            run.lane = cfg.name
            run.provider = cfg.vendor
            run.driver = hop.driver
            run.guard = hop.guard
            run.native_usage = capabilities(driver).native_usage
            run.cold_load = gate.cold_load
            hop.cold_load = gate.cold_load
            extra = self.settings.cold_load_seconds(cfg.name) if gate.cold_load else 0.0
            if run.time_limit is None:
                timeout = cfg.run_timeout
            else:
                timeout = min(cfg.run_timeout, run.time_limit)
            run.deadline = _now() + timeout + extra
            run.worked = False
            run.attempted_lane = cfg.name
            if explicit_primary and harness_state == "unverified":
                unverified = (
                    f"provider {cfg.name!r} is running with an unverified harness version"
                )
                run.warning = (
                    f"{run.warning}; {unverified}" if run.warning else unverified
                )
            events = self._collect(run, run.prompt, resume=None)
            refusal = None
            if not run.worked and run.trip is None and not self._closing:
                refusal = driver.refusal(cfg, events)
            if refusal is None:
                hop.outcome = router.HOP_RAN
                self._hop(run, hop)
                for event in events:
                    self._ingest(run, event)
                return True
            hop.outcome = router.HOP_REFUSED
            hop.code = refusal.code
            message = redact_secrets(refusal.message, self.settings.leaked_keys)
            hop.message = message
            hop.reset_at = refusal.reset_at
            until = router.close_until(
                refusal,
                balance_close_hours=self.settings.balance_close_hours,
                throttle_close_minutes=self.settings.throttle_close_minutes,
            )
            if until is not None and self.lane_state is not None:
                self.lane_state.close(cfg.name, until, refusal.code, message)
                hop.closed_until = until
            self._hop(run, hop)
            last_refusal = message
            self._set_active_lane(None)
        if self._closing:
            return False
        tried = "; ".join(
            f"{h['lane']} {h['outcome']}" + (f" ({h['code']})" if h.get("code") else "")
            for h in run.hops
        )
        run.state = FAILED
        run.error = f"no provider took the run: {tried}"
        run.error_detail = last_refusal
        # "refused" only when every provider said no itself. A hop skipped by
        # an admission threshold or a usage envelope is this machine's
        # decision, not a refusal by the backend, and leaves the run a plain
        # no_lane.
        refused = [
            h for h in run.hops
            if h["outcome"] == router.HOP_REFUSED
            and h.get("code") not in (router.ADMISSION, router.ENVELOPE)
        ]
        run.finish_reason = "refused" if refused and len(refused) == len(run.hops) else "no_lane"
        return False

    def _distill(self, run: Run) -> str:
        prompt = DISTIL_PROMPT.format(limit=self.settings.result_cap_chars)
        events = self._collect(run, prompt, resume=self.session_id)
        text = ""
        for event in events:
            if event.get("type") == "result":
                text = str(event.get("result") or "")
        return text.strip()

    def _turn(self, run: Run, prompt: str, resume: str | None) -> None:
        for event in self._collect(run, prompt, resume=resume):
            self._ingest(run, event)
        if self.cfg.driver == DRIVER_ANTIGRAVITY:
            self._bypass_audit(run)

    def _bypass_audit(self, run: Run) -> None:
        """R1a-agy addendum R2: record bypassing calls from agy's own conversation record.

        On unless `bypass_audit = false` (S56). Observation only: it never changes
        the run, and any failure becomes one bypass_audit_unavailable record (a
        missing record, as with a fake agy, is expected and not logged).
        """
        from .providers import antigravity

        if not antigravity.bypass_audit_enabled(self.cfg):
            return
        if self.trace is None or self.trace.path is None:
            return  # nowhere to record a finding: read nothing
        try:
            findings, reason = antigravity.audit_conversation(self.cfg, run.session_id)
        except Exception as exc:  # noqa: BLE001 - the audit fails open
            findings, reason = [], f"audit failed: {type(exc).__name__}"
        if reason is not None:
            if reason != "conversation record not found":
                log.warning("bypass audit for run %s unavailable: %s", run.run_id, reason)
            self.trace.bypass_audit_unavailable(
                agent_id=self.agent_id, run_id=run.run_id, provider=self.cfg.name,
                reason=reason,
            )
            return
        for finding in findings:
            self.trace.sandbox_bypass(
                agent_id=self.agent_id, run_id=run.run_id, provider=self.cfg.name,
                tool=finding["tool"], command=finding["command"],
                source="conversation_record",
            )

    def _backoff_sleep(self, run: Run, seconds: float) -> bool:
        """Sleep before a rate-limit retry, in short slices so cancel/close
        stay responsive. Returns False when the run was closed or tripped, or
        when the wait would outlast the run's own deadline."""
        deadline = _now() + seconds
        if run.deadline is not None and deadline >= run.deadline:
            log.warning("run %s rate-limit retry skipped: backoff outlasts the run deadline",
                        run.run_id)
            return False
        while run.trip is None and not self._closing:
            remaining = deadline - _now()
            if remaining <= 0:
                return True
            _sleep(min(remaining, 0.25))
        log.warning(
            "run %s rate-limit retry aborted (closing=%s, trip=%s)",
            run.run_id,
            self._closing,
            run.trip,
        )
        return False

    def _collect(self, run: Run, prompt: str, resume: str | None) -> list[dict[str, Any]]:
        """Run one whole ``claude -p`` turn and return its events.

        A turn whose terminal event is an honest rate limit (z.ai 429/529) is
        retried with exponential backoff, re-issuing the SAME prompt into the
        same session so work already done is not lost. Failed attempts are
        discarded: only the surviving attempt's events are ingested. Loop,
        step and budget kills (run.trip) and every non-rate-limit exit are
        final and never retried.
        """
        attempt = 0
        while True:
            if self._closing:
                return []
            self._sample_begin(run)
            meter = _Meter(attempt, _now())
            with self._dispatch_lock:
                if self._closing:
                    self._sample_end(run)
                    return []
                proc = self._spawn(run, prompt, resume)
                self._current = proc
            collected: list[dict[str, Any]] = []
            watch = _Watch(
                run,
                self.settings,
                self.signatures,
                self._step_limit_for(run, self.cfg),
                no_progress_steps=self.cfg.no_progress_steps,
                no_progress_seconds=self.cfg.no_progress_seconds,
            )
            try:
                if self._closing:
                    self._cancel_provider("agent closing", self._kill_kind or KILL_CANCEL, run)
                    proc.kill()
                    return []
                for event in proc.events():
                    if capabilities(self.driver).admission_barrier:
                        if event.get("admission_error"):
                            run.admission_error = redact_secrets(
                                str(event["admission_error"]), self.settings.leaked_keys,
                            )
                            run.admission_ready.set()
                        elif event.get("admitted"):
                            run.admission_ready.set()
                    self.last_activity = _now()
                    if capabilities(self.driver).native_outcome and "outcome" in event:
                        # _collect buffers events until the CLI exits. Pause as
                        # soon as a service snapshot arrives, even if its client
                        # keeps the stream open, then avoid replaying that timing.
                        run.observe_native_wait(event["outcome"], self.last_activity)
                        event["_native_wait_observed"] = True
                    meter.see(event, self.last_activity)
                    watcher = run.on_event if run.on_event is not None else self.on_event
                    if watcher is not None:
                        try:
                            watcher(event)
                        except Exception:  # noqa: BLE001 - a watcher never fails a run
                            log.warning("on_event watcher failed", exc_info=True)
                    if event.get("type") == "stream_event":
                        # Timing only; nothing downstream reads partial messages,
                        # and one per token would bloat the attempt's event list.
                        continue
                    collected.append(event)
                    # Trips are decided here, while the child still runs, so
                    # the kill below lands; deciding them after the stream
                    # ended could only relabel a run that had already finished.
                    trip = watch.see(event)
                    if trip is not None and run.trip is None:
                        run.trip = trip
                    if run.trip or self._closing:
                        kind, reason = run.trip or (
                            self._kill_kind or KILL_CANCEL, "agent closing"
                        )
                        self._cancel_provider(reason, kind, run)
                        proc.kill()
                        break
            except Exception:  # noqa: BLE001 - a broken reader must not orphan a native client
                if capabilities(self.driver).native_outcome or capabilities(self.driver).cancel:
                    self._cancel_provider("provider event reader failed", "error", run)
                    proc.kill()
                raise
            finally:
                if isinstance(proc, Process):
                    proc.kill()
                self._current = None
                self._sample_end(run)
                # Every attempt's spend counts, including one that is retried,
                # killed or cancelled: the provider billed it either way.
                self._account(run, meter)

            terminal = next(
                (e for e in reversed(collected) if e.get("type") == "result"), None
            )
            succeeded = any(
                e.get("type") == "result" and not e.get("is_error") for e in collected
            )
            # Across attempts: work done before a retried 429 still counts.
            run.worked = run.worked or router.did_work(collected)
            code = terminal.get("zai_code") if isinstance(terminal, dict) else None
            retryable = (
                isinstance(terminal, dict)
                and terminal.get("error_kind") == "rate_limited"
                # A spent plan quota resets hours later; retrying burns wall clock.
                and code not in router.ZAI_QUOTA_CODES
                # Nor does an empty balance or a usage limit.
                and not router.no_retry(str(terminal.get("error") or ""))
                and not succeeded
                and run.trip is None
                and not self._closing
                and attempt < self.settings.rate_limit_retries
            )
            if not retryable:
                # Exhausted retries (or a non-retryable exit): hand the
                # honest terminal event to _ingest, which fails the run.
                return collected

            # Retry resuming whatever session the failed attempt established.
            session = next(
                (
                    e.get("session_id")
                    for e in reversed(collected)
                    if isinstance(e.get("session_id"), str) and e.get("session_id")
                ),
                None,
            )
            if code == router.ZAI_THROTTLE_CODE:
                # Fair-use throttling does not clear in seconds.
                delay = min(self.settings.throttle_backoff * (2 ** attempt), 900.0)
            else:
                delay = min(self.settings.rate_limit_backoff * (2 ** attempt), 300.0)
            attempt += 1
            log.warning(
                "run %s rate-limited, retry %d/%d in %.1fs (%s)",
                run.run_id,
                attempt,
                self.settings.rate_limit_retries,
                delay,
                terminal.get("error"),
            )
            if not self._backoff_sleep(run, delay):
                return collected
            resume = session or resume

    def _account(self, run: Run, meter: _Meter) -> None:
        """Add an attempt's tokens to the run and write its turn records."""
        if capabilities(self.driver).native_usage:
            run.native_usage = True
            return
        spent = meter.usage()
        for key in _USAGE_KEYS:
            setattr(run.usage, key, getattr(run.usage, key) + spent[key])
        if run.ttft_seconds is None and meter.first_assistant is not None:
            run.ttft_seconds = round(meter.first_assistant - meter.spawned_at, 3)
        for turn in meter.records():
            try:
                tool_result_chars = turn.pop("_source_tool_result_chars", 0)
                source_unknown = set(turn.pop("_source_unmeasured", ()))
                if self.driver.name == "codex":
                    source_unknown.add("tool_results")
                source_chars = dict(run.source_chars or {})
                source_chars["tool_results"] = tool_result_chars
                source_usage, unmeasured = make_source_usage(
                    source_chars,
                    output_tokens=turn["output"],
                    reasoning_tokens=turn["reasoning"],
                    chars_per_token=self.settings.chars_per_token,
                    unmeasured=tuple(source_unknown),
                )
            except Exception:  # noqa: BLE001 - source accounting cannot fail the worker
                turn.pop("_source_tool_result_chars", None)
                turn.pop("_source_unmeasured", None)
                source_usage, unmeasured = make_source_usage(
                    None,
                    output_tokens=turn.get("output", 0),
                    reasoning_tokens=turn.get("reasoning", 0),
                    unmeasured=INPUT_SOURCES,
                )
            record = {
                "run_id": run.run_id,
                "agent_id": self.agent_id,
                "lane": self.cfg.name,
                "provider": self.cfg.vendor,
                "turn": len(run.turn_log),
                **turn,
                "model": turn["model"] or self.model,
                "source_usage": source_usage,
                "unmeasured": unmeasured,
            }
            run.turn_log.append(record)
            if self.trace is not None:
                self.trace.turn(**record)

    def _sample_begin(self, run: Run) -> None:
        """Register a child about to start on a local lane with that lane's sampler."""
        if self.telemetry is None or not self.telemetry.covers(self.cfg):
            return
        if run.sampling_lane != self.cfg.name:
            # A routed run that moved providers: the summary covers the one it
            # ended on.
            run.samples.clear()
        run.sampling_lane = self.cfg.name
        self.telemetry.begin(self.cfg, run.run_id)

    def _sample_end(self, run: Run) -> None:
        if self.telemetry is None or run.sampling_lane is None:
            return
        run.samples.extend(self.telemetry.end(run.sampling_lane, run.run_id))

    def _summarize(self, run: Run) -> None:
        """The run_summary record for a run that ran on a local lane."""
        if self.trace is None or run.sampling_lane is None:
            return
        turns = [t for t in run.turn_log if t.get("lane") == run.sampling_lane]
        self.trace.run_summary(
            run_id=run.run_id,
            agent_id=self.agent_id,
            lane=run.sampling_lane,
            **summarize(run.samples, turns),
        )

    def _ingest(self, run: Run, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "stream_event":
            return
        session = event.get("session_id")
        if isinstance(session, str) and session:
            run.session_id = session
            self.session_id = session
            self.session().session_id = session
        if kind == "system" and event.get("subtype") == "init":
            run.note("system/init")
            return
        if kind == "assistant":
            # Counting only. The loop and budget trips are decided by _Watch
            # while the stream is read; tool calls are not a step limit, since
            # [core].max_steps is Claude Code's --max-turns.
            for name, args in _tool_uses(event):
                sig = _tool_signature(name, args)
                run.signatures[sig] += 1
                self.signatures[sig] += 1
                run.usage.steps += 1
                run.note(f"tool_use: {name}")
            text = _assistant_text(event)
            if text:
                run.note(f"assistant: {_clip(text, 240)}")
            return
        if kind == "result":
            if capabilities(self.driver).native_outcome and "outcome" in event:
                self._ingest_native(run, event["outcome"],
                                    observe_wait=not event.get("_native_wait_observed", False))
                return
            # Tokens were counted as the events arrived (_Meter, via _account).
            # Credits are whole-run (Copilot's session.usage_checkpoint), not
            # per message, so the result event is the only place they arrive.
            usage = event.get("usage")
            if isinstance(usage, dict):
                run.usage.add({"credits": usage.get("credits")})
            unreported = event.get("usage_unreported")
            if isinstance(unreported, list):
                run.usage_unreported = list(dict.fromkeys(
                    [*run.usage_unreported, *(key for key in unreported if isinstance(key, str))]
                ))
            if isinstance(event.get("num_turns"), int):
                run.usage.turns += event["num_turns"]
            if _hit_max_turns(event) and run.trip is None:
                limit = self._step_limit_for(run, self.cfg)
                run.trip = (
                    KILL_STEPS,
                    f"reached --max-turns of {limit} ([core].max_steps) "
                    "before finishing",
                )
            budget = self.settings.turn_token_budget
            if budget is not None and run.usage.total > budget:
                run.trip = (
                    KILL_BUDGET,
                    f"run used {run.usage.total} tokens, over "
                    f"[core].turn_token_budget of {budget}",
                )
            text = str(event.get("result") or "")
            run.final_response = text
            bypass = event.get("sandbox_bypass")
            if (event.get("error_kind") == "sandbox_bypass" and isinstance(bypass, dict)
                    and self.trace is not None):
                self.trace.sandbox_bypass(
                    agent_id=self.agent_id, run_id=run.run_id, provider=self.cfg.name,
                    tool=str(bypass.get("tool") or ""), command=str(bypass.get("command") or ""),
                )
            if event.get("is_error"):
                run.state = FAILED
                reported = str(event.get("error") or text or "claude reported an error")
                # Never surface the cosmetic model warning as the cause.
                run.error = _strip_model_warning(reported) or reported
                # Synthetic exit events carry an honest error_kind
                # (rate_limited / auth / cli_error); CLI-reported errors keep
                # the generic "error".
                run.finish_reason = str(event.get("error_kind") or "error")
            else:
                run.finish_reason = "completed"
            run.note("result")
            return
        if kind:
            run.note(str(kind))

    def _ingest_native(
        self, run: Run, outcome: dict[str, Any], *, cancelling: bool = False,
        observe_wait: bool = True,
    ) -> None:
        """One service snapshot. Do not reinterpret checks or fill absent fields."""
        if (self._closing and not cancelling) or run.done.is_set():
            return
        state = outcome.get("state")
        if state not in TERMINAL_STATES and state != WORKING:
            raise ValueError(f"invalid native outcome state: {state!r}")
        if observe_wait:
            run.observe_native_wait(outcome, _now())
        run.native_outcome = copy.deepcopy(outcome)
        run.native_verification = capabilities(self.driver).native_verification
        run.state = state
        run.phase = outcome.get("phase", PHASE_DONE if state in TERMINAL_STATES else PHASE_RUNNING)
        run.finish_reason = outcome.get("finish_reason")
        run.final_response = outcome.get("result", "")
        run.result_text = run.final_response
        run.error = outcome.get("error")
        run.error_detail = outcome.get("error_detail")
        run.uat = outcome.get("uat")
        if capabilities(self.driver).native_usage:
            run.native_usage = True
            if "usage" in outcome:
                run.usage.set_native(outcome["usage"], outcome.get("credits"))
            run.cost = copy.deepcopy(outcome.get("cost"))
        run.note(f"provider: {run.phase}")
        if run.needs_human:
            run.await_ready.set()
        else:
            run.await_ready.clear()

    def _follow_native(self, run: Run) -> None:
        """Keep the serial run alive after its client exits, including human waits."""
        if not capabilities(self.driver).poll:
            raise RuntimeError("working native outcome requires provider poll capability")
        while run.state == WORKING and not self._closing and not run.trip:
            if run.timeout_expired(_now()):
                run.trip = (KILL_TIMEOUT, "run deadline exceeded")
                self._cancel_provider(run.trip[1], KILL_TIMEOUT, run)
                break
            snapshot = self.driver.poll(self.session())
            if snapshot is not None:
                self.last_activity = _now()
                self._ingest_native(run, snapshot)
            if run.state == WORKING:
                with self._wake:
                    if not self._closing:
                        self._wake.wait(timeout=0.25)

    def _cancel_provider(self, reason: str, kind: str, run: Run | None = None) -> None:
        """Cancel a service job at most once per outer run, always before kill."""
        if not capabilities(self.driver).cancel:
            return
        if run is None:
            owner = self.session().data.get("request_id")
            run = next((r for r in self.runs() if owner and r.request_id == owner), None)
            if run is None:
                run = next((r for r in self.runs() if r.state == WORKING and r.started_at), None)
            if run is None and self._order:
                run = self._runs[self._order[-1]]
        key = run.request_id if run is not None else None
        with self._dispatch_lock, self._cancel_lock:
            if key in self._cancelled_requests:
                return
            try:
                snapshot = self.driver.cancel(self.session(), reason=reason, kind=kind)
                self._cancelled_requests.add(key)
                if run is not None:
                    run.cancel_pending = False
                if (isinstance(snapshot, dict) and run is not None
                        and (not capabilities(self.driver).run_context
                             or self.session().data.get("request_id") == run.request_id)):
                    # Final service accounting may arrive only in cancel. The
                    # caller still owns the outer cancellation/timeout state.
                    native_agent = self.session().session_id
                    if native_agent is not None:
                        self.session_id = run.session_id = native_agent
                    previous = (run.state, run.phase, run.finish_reason, run.error)
                    self._ingest_native(run, snapshot, cancelling=True)
                    run.state, run.phase, run.finish_reason, run.error = previous
            except Exception as exc:  # noqa: BLE001 - remote failure must not block local kill
                message = redact_secrets(str(exc), self.settings.leaked_keys)
                if run is not None and capabilities(self.driver).native_outcome:
                    snapshot = copy.deepcopy(run.native_outcome or {})
                    snapshot.update(needs_human=False)
                    run.observe_native_wait(snapshot, _now())
                    snapshot.pop("human_action", None)
                    snapshot.setdefault("extra", {}).update(
                        cancellation_confirmed=False, cancellation_error=message,
                    )
                    run.native_outcome = snapshot
                    if capabilities(self.driver).confirmed_cancel and not run.done.is_set():
                        run.cancel_pending = True
                        run.state = WORKING
                        run.phase = "cancellation_pending"
                        run.await_ready.set()
                log.warning("provider cancel failed for %s: %s: %s",
                            self.agent_id, type(exc).__name__, message)

    def _finish(self, run: Run) -> None:
        # Cancellation can return the last accounting snapshot. Finish and
        # write the trace only after its bounded hook has returned.
        finished = False
        while True:
            self._cancel_complete.wait()
            with self._dispatch_lock:
                if self._closing and not self._cancel_complete.is_set():
                    continue
                wrote = self._finish_locked(run)
                finished = wrote and run.done.is_set() and not run.cancel_pending
                break
        if finished:
            self._record_lane_status(run)

    def _finish_locked(self, run: Run) -> bool:
        """Give the run its terminal state; False when it already had one.

        Exactly one finish writes the run's trace record and reports its lane
        observation. A run `end_run` already finished keeps that outcome, even
        if a later cancel sets `_kill_kind`.
        """
        run.admission_ready.set()
        if run.trace_written:
            # One `run` record per run. `close()` gives a run its terminal
            # state without writing a trace, so `trace_written`, not
            # `ended_by_end_run and done`, decides whether this pass may write.
            return False
        if run.done.is_set() and self._kill_kind is None:
            # Something else already gave this run its terminal state.
            return False
        if run.cancel_pending:
            run.state = WORKING
            run.phase = "cancellation_pending"
            run.await_ready.set()
            return False
        if self._kill_kind:
            kind = self._kill_kind
            run.state = FAILED if kind in KILL_IS_FAILURE else CANCELLED
            run.error = run.error or kind
            run.finish_reason = kind
        elif run.trip:
            kind, reason = run.trip
            if kind == KILL_LOOP:
                # The caller has been told; a continue starts the count again.
                self.signatures.clear()
            run.state = FAILED if kind in KILL_IS_FAILURE else CANCELLED
            run.error = reason
            run.finish_reason = kind
        if run.overrun is None:
            # Only a cap the caller asked for is an overrun worth tracing; a
            # provider-default deadline keeps its old record shape.
            if run.finish_reason == KILL_TIMEOUT and run.time_limit is not None:
                run.overrun = {"kind": KILL_TIMEOUT, "limit": run.time_limit}
            elif run.finish_reason == KILL_STEPS and run.step_limit is not None:
                run.overrun = {"kind": KILL_STEPS, "limit": run.step_limit}
        if run.error:
            run.error = redact_secrets(run.error, self.settings.leaked_keys)
        if run.error_detail:
            run.error_detail = redact_secrets(run.error_detail, self.settings.leaked_keys)
        run.phase = PHASE_DONE
        run.finished_at = _now()
        try:
            if not run.native_usage:
                run.cost = self._price(run)
        except Exception:  # noqa: BLE001 - costing must never fail a run
            log.warning("cost pricing failed for %s", run.run_id, exc_info=True)
            run.cost = None
        if self.trace is not None:
            try:
                self._summarize(run)
                self.trace.run(run, str(self.workspace), self.model, entry=self.entry)
            except Exception:  # noqa: BLE001
                log.warning("trace.write failed for %s", run.run_id, exc_info=True)
        # Set even when there is no trace or the write failed: this run had its
        # one chance at a record, and a later finish must not retry or duplicate.
        run.trace_written = True
        run.done.set()
        run.await_ready.set()
        return True

    def _price(self, run: Run) -> dict[str, Any] | None:
        """The run's cost at run end (telemetry.cost.price_run), priced once per agent."""
        pricing = getattr(self, "_pricing", None)
        if pricing is None:
            pricing = Pricing.from_settings(self.settings)
            self._pricing = pricing
        return price_run(
            run.usage, run.usage.credits, self.cfg, pricing, run.finished_at or _now()
        )


class _ReapStuck(dict[str, threading.Thread]):
    """Agent id -> the still-running close/end_run thread that is stuck on it.

    It is a mapping so a later pass can see whether that thread returned (and
    what it raised), while keeping the old set interface: `in`, `len()`, and
    equality with an empty set or a set of the keys all still work.
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (set, frozenset)):
            return set(self) == set(other)
        return dict.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    __hash__ = None  # type: ignore[assignment]


class Registry:
    """Owns every live agent for the lifetime of the MCP server process."""

    def __init__(
        self,
        settings: Settings,
        start_reaper: bool = True,
        trace: Trace | None = None,
        telemetry: Telemetry | None = None,
        entry: str = "mcp",
    ):
        self.settings = settings
        self.entry = entry
        self.trace = trace if trace is not None else open_trace(
            settings.trace, settings.session_root
        )
        self.telemetry = telemetry if telemetry is not None else Telemetry(
            open_metrics(self.trace, settings.session_root),
            interval=settings.sample_seconds,
        )
        protect(settings.session_root)
        protect(APPROVAL_HOOK)
        protect(OMP_HOOK)
        self.lane_state = LaneState(settings.session_root)
        self.lane_status = LaneStatusStore(
            settings.session_root, redact_keys=settings.leaked_keys
        )
        self._agents: dict[str, Agent] = {}
        self._archive: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()
        # A close or end_run that has not returned after this many seconds is
        # abandoned for the pass and remembered in `reap_stuck`; a later pass
        # must not start another thread for that agent.
        self.reap_close_timeout = 20
        self.reap_stuck: _ReapStuck = _ReapStuck()
        # Agent id -> the exceptions its abandoned close thread raised after
        # the pass moved on; the next pass logs them.
        self._reap_errors: dict[str, list[BaseException]] = {}
        self._requests_lock = threading.RLock()
        self._requests: dict[tuple[str, ...], tuple[dict[str, Any], Run]] = {}
        self._agent_cleanup_callbacks: list[Callable[[str], None]] = []
        self._counter = itertools.count(1)
        # Agent ids name directories under the shared session root
        # (agents/<id>/claude-home, codex-home), so they must not repeat
        # across server processes: a per-registry tag, and the directory is
        # claimed with an exclusive mkdir before the id is used.
        self._tag = secrets.token_hex(3)
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        if start_reaper:
            idle = min(
                [p.idle_timeout for p in settings.providers.values()]
                or [settings.idle_timeout]
            )
            interval = max(1.0, min(30.0, idle / 4))
            self._reaper = threading.Thread(
                target=self._reap_loop, args=(interval,), name="sam-reaper", daemon=True
            )
            self._reaper.start()

    def request_once(self, scope: tuple[str, ...], request_id: str | None,
                     arguments: dict[str, Any], submit: Callable[[], Run]) -> Run:
        """Process-local outer identity for a service's caller idempotency key."""
        if request_id is None:
            return submit()
        if not request_id:
            raise RegistryError("request_id must be a non-empty string")
        key = (*scope, request_id)
        with self._requests_lock:
            previous = self._requests.get(key)
            if previous is not None:
                original, run = previous
                if original != arguments:
                    raise RegistryError("request_id reused with different arguments")
                return run
            run = submit()
            self._requests[key] = (copy.deepcopy(arguments), run)
            return run

    def register_agent_cleanup(self, callback: Callable[[str], None]) -> None:
        """Register per-agent security state to discard at close/eviction."""
        self._agent_cleanup_callbacks.append(callback)

    def _agent_closed(self, agent_id: str) -> None:
        for callback in tuple(self._agent_cleanup_callbacks):
            try:
                callback(agent_id)
            except Exception:  # noqa: BLE001 - cleanup cannot strand other cleanup
                log.warning("agent cleanup callback failed for %s", agent_id, exc_info=True)
        key_file = self.settings.session_root / "agents" / agent_id / "approval.key"
        try:
            key_file.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove approval key for %s", agent_id, exc_info=True)

    def replay_continue(self, agent_id: str, request_id: str | None,
                        arguments: dict[str, Any]) -> Run | None:
        # Archived/closed agents still support a replay of an existing request.
        if request_id is not None:
            with self._requests_lock:
                for key, (original, run) in self._requests.items():
                    if key[-1] == request_id and run.agent_id == agent_id:
                        if original != arguments:
                            raise RegistryError("request_id reused with different arguments")
                        return run
        return None

    def reap_once(self) -> list[tuple[str, str]]:
        now = _now()
        acted: list[tuple[str, str]] = []
        # An abandoned close thread may have returned since the last pass.
        # Forget it so this pass can reap its agent normally, and surface any
        # exception it raised after the earlier pass had already moved on.
        for agent_id, thread in list(self.reap_stuck.items()):
            if thread.is_alive():
                continue
            del self.reap_stuck[agent_id]
            for exc in self._reap_errors.pop(agent_id, ()):
                log.warning("reap: agent %s failed: %s", agent_id, exc, exc_info=exc)
        try:
            for agent in self.agents():
                if agent.closed:
                    self.reap_stuck.pop(agent.agent_id, None)
                    self._reap_errors.pop(agent.agent_id, None)
                    continue
                if agent.agent_id in self.reap_stuck:
                    # An earlier close attempt has not returned; starting
                    # another thread for it would only pile up more work.
                    continue
                try:
                    action = self._reap_action(agent, now)
                except Exception:  # noqa: BLE001 - one agent must not stop the pass
                    log.warning("reap: inspecting agent %s failed", agent.agent_id,
                                exc_info=True)
                    continue
                if action is None:
                    continue
                kind, work = action
                if self._reap_bounded(agent, work):
                    acted.append((agent.agent_id, kind))
        finally:
            self._evict_closed()
        return acted

    @staticmethod
    def _reap_action(
        agent: Agent, now: float
    ) -> tuple[str, Callable[[], None]] | None:
        """The one action this pass takes for `agent`, if any."""
        active = [run for run in agent.runs() if run.state == WORKING and run.started_at]
        # A run `end_run` already ended on its deadline may still be WORKING
        # while its worker drains. Its trip is not a reason to close the agent.
        pending = [run for run in active if not run.ended_by_end_run]
        tripped = next((run for run in pending if run.trip), None)
        if tripped is not None and tripped.trip is not None:
            kind, reason = tripped.trip
            return kind, lambda: agent.close(reason, kind=kind)
        overdue = next((run for run in pending if run.timeout_expired(now)), None)
        if overdue is not None:
            # A deadline is the run's, not the session's: an agent whose run
            # timed out can still be resumed with `continue` (R7a). The reaper
            # does not finish a plain child in this call: its own bounded
            # thread does not block a caller, and the next pass below finishes
            # the run if that child is still stuck.
            return KILL_TIMEOUT, lambda: agent.end_run(
                overdue, "run deadline exceeded", KILL_TIMEOUT, finish_stuck=False
            )
        # A plain child that ignored `end_run`'s kill in an earlier pass still
        # has a WORKING run: its stream never drained. Finish it here so the
        # agent cannot hold its slot forever. Its worker thread stays blocked
        # in the child's stream iterator and is left to exit when the pipe
        # closes; the reaper does not replace it.
        abandoned = next((run for run in active if run.ended_by_end_run), None)
        if abandoned is not None:
            return KILL_TIMEOUT, lambda: agent._finish(abandoned)
        if (not active and not agent.busy
                and now - agent.last_activity > agent.cfg.idle_timeout):
            return KILL_IDLE, lambda: agent.close("idle", kind=KILL_IDLE)
        return None

    def _reap_bounded(self, agent: Agent, work: Callable[[], None]) -> bool:
        """Run one close/end_run in its own thread, bounded by the close timeout."""
        errors: list[BaseException] = []

        def target() -> None:
            try:
                work()
            except BaseException as exc:  # noqa: BLE001 - never stop the pass
                errors.append(exc)

        worker = threading.Thread(
            target=target, name=f"sam-reap-{agent.agent_id}", daemon=True
        )
        worker.start()
        worker.join(self.reap_close_timeout)
        if worker.is_alive():
            self.reap_stuck[agent.agent_id] = worker
            # The thread still owns this list; it appends to it if it raises
            # after the pass has moved on, and the next pass logs that.
            self._reap_errors[agent.agent_id] = errors
            log.warning("reap: agent %s did not close within %ss; moving on",
                        agent.agent_id, self.reap_close_timeout)
            return False
        self.reap_stuck.pop(agent.agent_id, None)
        self._reap_errors.pop(agent.agent_id, None)
        if errors:
            log.warning("reap: agent %s failed: %s", agent.agent_id, errors[0],
                        exc_info=errors[0])
            return False
        return True

    def _evict_closed(self) -> None:
        with self._lock:
            closed = [
                a for a in self._agents.values()
                if a.closed and all(r.state in TERMINAL_STATES for r in a.runs())
            ]
            for agent in closed:
                self.reap_stuck.pop(agent.agent_id, None)
                self._reap_errors.pop(agent.agent_id, None)
                self._agent_closed(agent.agent_id)
                for run in agent.runs():
                    self._archive[run.run_id] = run
                self._agents.pop(agent.agent_id, None)
            while len(self._archive) > self.settings.run_archive:
                self._archive.popitem(last=False)

    def _reap_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001
                log.warning("reaper pass failed", exc_info=True)

    def select_provider(
        self, provider: str | None, fallback: str | None = "full"
    ) -> ProviderConfig:
        """The provider a new agent runs on, or RegistryError saying why not.

        Checked before anything is spawned: an unknown provider, an unknown
        fallback mode, or a provider this build cannot drive.
        """
        mode = fallback if fallback is not None else "full"
        if mode not in FALLBACK_MODES:
            raise RegistryError(
                f"unknown fallback {fallback!r}; expected one of {', '.join(FALLBACK_MODES)}"
            )
        name = provider or self.settings.default_provider
        chosen = self.settings.providers.get(name)
        if chosen is None:
            known = ", ".join(self.settings.providers) or "(none)"
            raise RegistryError(f"unknown provider {name!r}; known providers: {known}")
        reason = chosen.unavailable()
        if reason is not None:
            raise RegistryError(reason)
        return chosen

    def create_agent(
        self,
        name: str | None,
        workspace: Path,
        model: str | None = None,
        provider: str | None = None,
        fallback: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        agent_id: str | None = None,
        goal: Goal | None = None,
        goal_reviewer: GoalReviewer | None = None,
        task: str | None = None,
        instructions: str | None = None,
        deny_read: list[str] | None = None,
        provider_explicit: bool | None = None,
        override_envelope: str | None = None,
    ) -> Agent:
        chosen = self.select_provider(provider, fallback)
        refusal = self.settings.workspace_refusal(workspace)
        if refusal is not None:
            raise RegistryError(refusal)
        released: list[str] = []
        while True:
            with self._lock:
                live = [a for a in self._agents.values() if not a.closed]
                if len(live) >= self.settings.max_agents:
                    raise RegistryError(
                        f"agent limit reached ({self.settings.max_agents} live). "
                        "Cancel one with cancel, or raise [core].max_agents."
                    )
                here = [a for a in live if a.cfg.name == chosen.name]
                if len(here) < chosen.max_agents:
                    claimed = agent_id or self._claim_agent_id()
                    approval_key = _load_or_create_approval_key(
                        self.settings, claimed, fresh=True
                    )
                    agent = Agent(
                        agent_id=claimed,
                        approval_key=approval_key,
                        name=name or f"subagent-{claimed}",
                        workspace=workspace,
                        model=model or chosen.model or "",
                        settings=self.settings,
                        trace=self.trace,
                        cfg=chosen,
                        fallback=fallback or "full",
                        chain=self.settings.chain(chosen.name, fallback or "full"),
                        lane_state=self.lane_state,
                        lane_status=self.lane_status,
                        provider_load=self._provider_load,
                        telemetry=self.telemetry,
                        on_event=on_event,
                        goal=goal,
                        goal_reviewer=goal_reviewer,
                        delegate_options={
                            "task": task, "instructions": instructions,
                            "name": name, "model": model, "fallback": fallback,
                            "provider_explicit": (
                                provider is not None
                                if provider_explicit is None else provider_explicit
                            ),
                            "override_envelope": override_envelope},
                        deny_read=deny_read,
                        entry=self.entry,
                        released_agents=released,
                        on_close=self._agent_closed,
                    )
                    self._agents[claimed] = agent
                    return agent
                candidate = self._releasable(
                    here, chosen.slot_release_grace_seconds
                )
                if candidate is None or not candidate.try_release():
                    # Somebody queued work on the candidate after it looked
                    # idle; never cancel that work to free this slot.
                    raise RegistryError(
                        f"agent limit reached on provider {chosen.name!r} "
                        f"({chosen.max_agents} live). Cancel one with cancel, or raise "
                        f"[providers.{chosen.name}].max_agents."
                    )
            # Close outside the registry lock: the agent's own worker may be
            # exiting and this join must not wait on a lock this thread holds.
            candidate.close("released for a new run", kind=KILL_IDLE)
            self._evict_closed()
            released.append(candidate.agent_id)

    @staticmethod
    def _releasable(agents: list[Agent], grace: float) -> Agent | None:
        """The oldest finished, non-busy agent idle at least `grace` seconds."""
        now = _now()
        finished = [
            agent for agent in agents
            if agent.runs() and not agent.busy
            and all(run.state in TERMINAL_STATES for run in agent.runs())
            and now - agent.last_activity >= grace
        ]
        return min(
            finished, key=lambda agent: (agent.last_activity, agent.created_at), default=None
        )

    def adopt(
        self,
        record: dict[str, Any],
        workspace: Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Agent:
        """Reopen an Agent from a session record (the loop's `--continue`).

        The recorded id and driver home are checked against the loop's opaque
        UUID namespace before the recorded session is reopened.
        """
        provider_name = record.get("provider")
        chosen = self.settings.providers.get(provider_name)
        if chosen is None:
            raise RegistryError(
                f"session record names provider {provider_name!r}, which is not declared"
            )
        agent_id = record.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.startswith("loop-"):
            raise RegistryError("session record agent_id must match loop-<uuid4>")
        try:
            parsed_id = uuid.UUID(agent_id.removeprefix("loop-"))
        except (ValueError, AttributeError):
            parsed_id = None
        if parsed_id is None or parsed_id.version != 4 or str(parsed_id) != agent_id[5:]:
            raise RegistryError(f"session record agent_id {agent_id!r} must match loop-<uuid4>")

        if chosen.driver == "claude":
            expected_home = self.settings.agent_home(agent_id)
        elif chosen.driver == "codex":
            expected_home = self.settings.session_root / "agents" / agent_id / "codex-home"
        else:
            expected_home = None
        recorded_home = record.get("agent_home")
        if expected_home is None:
            if recorded_home not in (None, ""):
                raise RegistryError(
                    f"session record agent_home {recorded_home!r} is not expected "
                    f"for {chosen.driver}"
                )
        else:
            try:
                home_matches = (
                    isinstance(recorded_home, str)
                    and Path(recorded_home).expanduser().resolve() == expected_home.resolve()
                )
            except (OSError, RuntimeError, ValueError):
                home_matches = False
            if not home_matches:
                raise RegistryError(
                    f"session record agent_home {recorded_home!r} does not match "
                    f"expected {expected_home}"
                )
        agents_root = (self.settings.session_root / "agents").resolve()
        if expected_home is not None and not expected_home.resolve().is_relative_to(agents_root):
            raise RegistryError(f"expected agent_home {expected_home} escapes the agents directory")
        fallback = record.get("fallback") or "none"
        with self._lock:
            existing = self._agents.get(agent_id)
            if existing is not None:
                # A closed agent whose runs have all ended is the same process's
                # earlier round waiting for the reaper; evict it so its session
                # can be reopened. A live agent is never replaced.
                if not (existing.closed
                        and all(r.state in TERMINAL_STATES for r in existing.runs())):
                    raise RegistryError(
                        f"session record agent_id {agent_id!r} is already registered"
                    )
                for run in existing.runs():
                    self._archive[run.run_id] = run
                del self._agents[agent_id]
            agent = Agent(
                agent_id=agent_id,
                approval_key=_load_or_create_approval_key(
                    self.settings, agent_id, fresh=False
                ),
                name=f"subagent-{agent_id}",
                workspace=workspace or self.settings.workspace,
                model=record.get("model") or chosen.model or "",
                settings=self.settings,
                trace=self.trace,
                cfg=chosen,
                fallback=fallback,
                chain=self.settings.chain(chosen.name, fallback),
                lane_state=self.lane_state,
                lane_status=self.lane_status,
                provider_load=self._provider_load,
                telemetry=self.telemetry,
                on_event=on_event,
                entry=self.entry,
                on_close=self._agent_closed,
            )
            session_id = record.get("session_id")
            if session_id:
                # The authoritative resume id: `_spawn` re-applies it to the
                # driver's session on the first continue, after boot has run.
                agent.session_id = session_id
            self._agents[agent_id] = agent
        return agent

    def _claim_agent_id(self) -> str:
        """A fresh agent id whose session directory this process created."""
        agents = self.settings.session_root / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        while True:
            agent_id = f"a{next(self._counter)}-{self._tag}"
            try:
                (agents / agent_id).mkdir()
            except FileExistsError:
                continue
            return agent_id

    def _provider_load(self, provider: str, asking: Agent) -> int:
        """Live agents other than `asking` currently on `provider`."""
        with self._lock:
            return sum(
                1 for a in self._agents.values()
                if a is not asking and not a.closed and a.cfg.name == provider
            )

    def agent(self, agent_id: str) -> Agent:
        with self._lock:
            agent = self._agents.get(agent_id)
        if agent is None:
            raise RegistryError(f"unknown agent_id {agent_id!r}")
        return agent

    def cancel(self, agent_id: str, reason: str = "cancelled by caller") -> None:
        """Close an agent and cancel any current or queued run."""
        self.agent(agent_id).close(reason)

    def find_agent(self, agent_id: str) -> Agent | None:
        with self._lock:
            return self._agents.get(agent_id)

    def find_run(self, run_id: str) -> Run:
        with self._lock:
            agents = list(self._agents.values())
            archived = self._archive.get(run_id)
        for agent in agents:
            run = agent.get_run(run_id)
            if run is not None:
                return run
        if archived is not None:
            return archived
        raise RegistryError(f"unknown run_id {run_id!r}")

    def agents(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def archived_runs(self) -> list[Run]:
        with self._lock:
            return list(self._archive.values())

    def shutdown(self) -> None:
        self._stop.set()
        for agent in self.agents():
            if not agent.closed:
                agent.close("server shutting down", kind=KILL_SHUTDOWN)
        self.telemetry.shutdown()
        adapter.shutdown()
