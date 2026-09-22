"""Agent and run registry.

One delegated subagent == one Claude Code session id == one worker thread
holding a serial run queue. Each run is one ``claude -p`` subprocess. Continue
re-enters the same session via ``--resume``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import secrets
import threading
import time
import uuid
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import adapter, health, providers, router
from .config import Settings, log
from .guard.classify import protect
from .lane_state import LaneState
from .providers.base import ProviderConfig, Session

# classify_exit and exit_event are re-exported: they were part of this
# module's surface before the drivers moved into providers/.
from .providers.claude import (  # noqa: F401
    ClaudeProcess,
    _strip_model_warning,
    classify_exit,
    exit_event,
)
from .router import FALLBACK_MODES
from .telemetry.sampler import Telemetry, open_metrics, summarize
from .telemetry.trace import Trace, open_trace
from .verify import VerificationResult, run_verification

WORKING = "working"
COMPLETED = "completed"
COMPLETED_UNVERIFIED = "completed_unverified"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, COMPLETED_UNVERIFIED, FAILED, CANCELLED})

PHASE_QUEUED = "queued"
PHASE_RUNNING = "running"
PHASE_VERIFYING = "verifying"
PHASE_DISTILLING = "distilling"
PHASE_DONE = "done"

KILL_CANCEL = "cancel"
KILL_TIMEOUT = "timeout"
KILL_LOOP = "loop"
KILL_BUDGET = "budget"
KILL_STEPS = "steps"
KILL_SHUTDOWN = "shutdown"
KILL_IDLE = "idle"
KILL_IS_FAILURE = frozenset({KILL_TIMEOUT, KILL_LOOP, KILL_BUDGET, KILL_STEPS})

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

    @property
    def total(self) -> int:
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

    def merge(self, other: Usage) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.steps += other.steps
        self.turns += other.turns
        if other.credits is not None:
            self.credits = (self.credits or 0.0) + other.credits


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
    """

    def __init__(
        self, run: Run, settings: Settings, seen: Counter[str], max_steps: int | None = None
    ):
        self.settings = settings
        self.max_steps = settings.max_steps if max_steps is None else max_steps
        # Counted across the agent's runs: a loop split over continues is
        # still a loop.
        self.signatures: Counter[str] = Counter(seen)
        self.base_tokens = run.usage.total
        self.messages: dict[str, int] = {}

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
            }
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
            })
        return out


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


@dataclass
class Run:
    run_id: str
    agent_id: str
    prompt: str
    verification: str | None = None
    # Where `verification` came from, when the caller did not pass it.
    verification_note: str | None = None
    state: str = WORKING
    phase: str = PHASE_QUEUED
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    final_response: str = ""
    result_text: str = ""
    distilled: bool = False
    truncated: bool = False
    finish_reason: str | None = None
    error: str | None = None
    error_detail: str | None = None
    event_count: int = 0
    transcript: deque[str] = field(default_factory=lambda: deque(maxlen=400))
    done: threading.Event = field(default_factory=threading.Event)
    signatures: Counter[str] = field(default_factory=Counter)
    usage: Usage = field(default_factory=Usage)
    verification_result: VerificationResult | None = None
    trip: tuple[str, str] | None = None
    deadline: float | None = None
    session_id: str | None = None
    lane: str | None = None
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

    def summary(self) -> dict[str, Any]:
        return {
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

    def detail(self) -> dict[str, Any]:
        out = self.summary()
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
        if self.hops:
            out["hops"] = list(self.hops)
        if self.cold_load:
            out["cold_load"] = True
        if self.error:
            out["error"] = self.error
        if self.error_detail:
            out["error_detail"] = self.error_detail
        return out

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
        name: str,
        workspace: Path,
        model: str,
        settings: Settings,
        trace: Trace | None = None,
        cfg: ProviderConfig | None = None,
        fallback: str = "full",
        chain: list[ProviderConfig] | None = None,
        lane_state: LaneState | None = None,
        provider_load: Callable[[str, Agent], int] | None = None,
        telemetry: Telemetry | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.trace = trace
        self.telemetry = telemetry
        # The provider this agent runs on. The delegate's run may move it
        # along `chain` (Settings.chain) when a provider refuses before work;
        # after that every continue stays on the provider that ran.
        self.cfg = cfg or settings.provider()
        self.fallback = fallback
        self.driver = providers.for_driver(self.cfg.driver)
        # Driver name -> its boot result (None when it can run) and the
        # session it minted. Filled lazily: a later hop's driver is checked
        # only when the walk gets there.
        self._booted: dict[str, str | None] = {}
        self._sessions: dict[str, Session] = {}
        self.chain = list(chain) if chain else [self.cfg]
        self.lane_state = lane_state
        self._provider_load = provider_load
        # Called for every parsed event of every turn, as it arrives.
        self.on_event = on_event
        self.agent_id = agent_id
        self.name = name
        self.workspace = workspace
        self.model = model
        self.settings = settings
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
        self._current: ClaudeProcess | None = None
        self._thread = threading.Thread(
            target=self._worker, name=f"sam-agent-{agent_id}", daemon=True
        )
        self._thread.start()

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
    ) -> Run:
        if self._closed or self._closing:
            raise RegistryError(f"agent {self.agent_id} is closed")
        run = Run(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            agent_id=self.agent_id,
            prompt=prompt,
            verification=verification,
            verification_note=note,
            lane=self.cfg.name,
            fallback=self.fallback,
            provider=self.cfg.vendor,
            driver=self.driver.name,
            guard=self.driver.guard(self.settings, self.cfg),
            route=route,
            parent=parent or None,
        )
        run.transcript = deque(maxlen=self.settings.transcript_limit)
        with self._wake:
            run.continues = len(self._order)
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            self._queue.append(run)
            self._wake.notify()
        return run

    def delegate(
        self, prompt: str, verification: str, parent: dict[str, Any] | None = None
    ) -> Run:
        """The agent's first run. Its verification is kept for continues.

        This run walks the lane chain; continues stay on the lane it ran on.
        """
        self.delegate_verification = verification
        return self.submit(prompt, verification=verification, route=True, parent=parent)

    def follow_up(
        self,
        message: str,
        verification: str | None = None,
        parent: dict[str, Any] | None = None,
    ) -> Run:
        """A continue. Omitting `verification` reuses the delegate's; "" skips it.

        Wrap-up continues ("stop and report") were sent without one, so they
        ended completed_unverified by design and said nothing about whether
        the work was done. Judged against the original acceptance check, a
        completed continue means the task passes.
        """
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
            )
        if verification is not None and not verification.strip():
            return self.submit(
                message,
                verification=None,
                parent=parent,
                note='verification="" given: skipped, so this run reports completed_unverified',
            )
        return self.submit(message, verification=verification, parent=parent)

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

    def close(self, reason: str = "cancelled by caller", kind: str = KILL_CANCEL) -> None:
        with self._wake:
            if self._closed:
                return
            self._closing = True
            self._kill_kind = kind
            self._wake.notify_all()
        current = self._current
        if current is not None:
            current.kill()
        for run in self.runs():
            if run.state == WORKING:
                run.state = FAILED if kind in KILL_IS_FAILURE else CANCELLED
                run.error = reason
                run.phase = PHASE_DONE
                run.finished_at = _now()
                run.done.set()
        self._thread.join(timeout=8)
        self._closed = True

    def _boot(self) -> None:
        try:
            self._start_error = self._boot_driver(self.driver, self.cfg)
        finally:
            self._ready.set()

    def _boot_driver(self, driver: Any, cfg: ProviderConfig) -> str | None:
        """Boot `driver` for this agent once; the reason it cannot run, or None."""
        if driver.name not in self._booted:
            try:
                error, session = driver.boot(self.settings, self.agent_id, cfg)
            except NotImplementedError as exc:
                error, session = str(exc), Session(provider=cfg.name)
            self._booted[driver.name] = error
            self._sessions[driver.name] = session
        return self._booted[driver.name]

    def session(self) -> Session:
        """The current driver's per-agent session state."""
        found = self._sessions.get(self.driver.name)
        if found is None:
            found = Session(provider=self.cfg.name)
            self._sessions[self.driver.name] = found
        return found

    def _worker(self) -> None:
        self._boot()
        while True:
            with self._wake:
                while not self._queue and not self._closing:
                    self._wake.wait(timeout=0.5)
                if self._closing and not self._queue:
                    break
                run = self._queue.popleft() if self._queue else None
            if run is None:
                continue
            try:
                self._execute(run)
            except Exception as exc:  # noqa: BLE001
                log.warning("run %s crashed", run.run_id, exc_info=True)
                run.state = FAILED
                run.error = f"{type(exc).__name__}: {exc}"
                run.phase = PHASE_DONE
                run.finished_at = _now()
                run.done.set()

    def _spawn(self, prompt: str, resume: str | None) -> ClaudeProcess:
        """One turn's child process, on the current provider's driver."""
        session = self.session()
        session.session_id = resume
        return self.driver.spawn(
            self.cfg, self.settings, self.agent_id, prompt, self.workspace,
            session, self.model,
        )

    def _execute(self, run: Run) -> None:
        run.started_at = _now()
        run.deadline = run.started_at + self.cfg.run_timeout
        run.phase = PHASE_RUNNING
        self.last_activity = _now()
        if run.route:
            if not self._route(run):
                self._finish(run)
                return
        else:
            if self.driver.needs_api_key and not self.cfg.api_key():
                run.state = FAILED
                run.error = self._missing_key(self.cfg)
                run.phase = PHASE_DONE
                run.finished_at = _now()
                run.done.set()
                return
            self._turn(run, run.prompt, resume=self.session_id)
        if self._closing or run.trip or run.state in TERMINAL_STATES:
            self._finish(run)
            return

        run.phase = PHASE_VERIFYING
        remaining = (run.deadline - _now()) if run.deadline else None
        run.verification_result = run_verification(
            run.verification or "",
            self.workspace,
            self.settings,
            budget=remaining,
        )

        if len(run.final_response) > self.settings.result_cap_chars:
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
        if run.verification_result.passed:
            run.state = COMPLETED
        else:
            run.state = COMPLETED_UNVERIFIED
        self._finish(run)

    @staticmethod
    def _missing_key(cfg: ProviderConfig) -> str:
        names = ", ".join(cfg.api_key_envs) or "(none configured)"
        return f"missing API key for provider {cfg.name!r}: set one of {names}"

    def _hop(self, run: Run, hop: router.Hop) -> None:
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
        for index, cfg in enumerate(self.chain):
            if self._closing:
                return False
            model = primary_model if index == 0 else (cfg.model or "")
            driver = providers.for_driver(cfg.driver)
            hop = router.Hop(index, cfg.name, cfg.vendor, model, router.HOP_UNAVAILABLE,
                             driver=driver.name, guard=driver.guard(self.settings, cfg))
            reason = cfg.unavailable()
            if reason is None and driver.needs_api_key and not cfg.api_key():
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

            self.cfg = cfg
            self.driver = driver
            self.model = model
            self.session_id = None
            run.lane = cfg.name
            run.provider = cfg.vendor
            run.driver = hop.driver
            run.guard = hop.guard
            run.cold_load = gate.cold_load
            hop.cold_load = gate.cold_load
            extra = self.settings.cold_load_seconds(cfg.name) if gate.cold_load else 0.0
            run.deadline = _now() + cfg.run_timeout + extra
            run.worked = False
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
            hop.message = refusal.message
            hop.reset_at = refusal.reset_at
            until = router.close_until(
                refusal,
                balance_close_hours=self.settings.balance_close_hours,
                throttle_close_minutes=self.settings.throttle_close_minutes,
            )
            if until is not None and self.lane_state is not None:
                self.lane_state.close(cfg.name, until, refusal.code, refusal.message)
                hop.closed_until = until
            self._hop(run, hop)
            last_refusal = refusal.message
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
        # an admission threshold is this machine's decision, not a refusal by
        # the backend, and leaves the run a plain no_lane.
        refused = [h for h in run.hops if h["outcome"] == router.HOP_REFUSED
                   and h.get("code") != router.ADMISSION]
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
            self._sample_begin(run)
            meter = _Meter(attempt, _now())
            proc = self._spawn(prompt, resume)
            self._current = proc
            collected: list[dict[str, Any]] = []
            watch = _Watch(run, self.settings, self.signatures, self.cfg.max_steps)
            try:
                for event in proc.events():
                    self.last_activity = _now()
                    meter.see(event, self.last_activity)
                    if self.on_event is not None:
                        try:
                            self.on_event(event)
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
                        proc.kill()
                        break
            finally:
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
        spent = meter.usage()
        for key in _USAGE_KEYS:
            setattr(run.usage, key, getattr(run.usage, key) + spent[key])
        if run.ttft_seconds is None and meter.first_assistant is not None:
            run.ttft_seconds = round(meter.first_assistant - meter.spawned_at, 3)
        for turn in meter.records():
            record = {
                "run_id": run.run_id,
                "agent_id": self.agent_id,
                "lane": self.cfg.name,
                "provider": self.cfg.vendor,
                "turn": len(run.turn_log),
                **turn,
                "model": turn["model"] or self.model,
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
            # Tokens were counted as the events arrived (_Meter, via _account).
            # Credits are whole-run (Copilot's session.usage_checkpoint), not
            # per message, so the result event is the only place they arrive.
            usage = event.get("usage")
            if isinstance(usage, dict):
                run.usage.add({"credits": usage.get("credits")})
            if isinstance(event.get("num_turns"), int):
                run.usage.turns += event["num_turns"]
            if _hit_max_turns(event) and run.trip is None:
                run.trip = (
                    KILL_STEPS,
                    f"reached --max-turns of {self.cfg.max_steps} ([core].max_steps) "
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

    def _finish(self, run: Run) -> None:
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
        run.phase = PHASE_DONE
        run.finished_at = _now()
        if self.trace is not None:
            try:
                self._summarize(run)
                self.trace.run(run, str(self.workspace), self.model)
            except Exception:  # noqa: BLE001
                log.warning("trace.write failed for %s", run.run_id, exc_info=True)
        run.done.set()


class Registry:
    """Owns every live agent for the lifetime of the MCP server process."""

    def __init__(
        self,
        settings: Settings,
        start_reaper: bool = True,
        trace: Trace | None = None,
        telemetry: Telemetry | None = None,
    ):
        self.settings = settings
        self.trace = trace if trace is not None else open_trace(
            settings.trace, settings.session_root
        )
        self.telemetry = telemetry if telemetry is not None else Telemetry(
            open_metrics(self.trace, settings.session_root)
        )
        protect(settings.session_root)
        self.lane_state = LaneState(settings.session_root)
        self._agents: dict[str, Agent] = {}
        self._archive: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()
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

    def reap_once(self) -> list[tuple[str, str]]:
        now = _now()
        acted: list[tuple[str, str]] = []
        for agent in self.agents():
            if agent.closed:
                continue
            active = [r for r in agent.runs() if r.state == WORKING and r.started_at]
            tripped = next((r for r in active if r.trip), None)
            overdue = next((r for r in active if r.deadline and now > r.deadline), None)
            if tripped is not None and tripped.trip is not None:
                kind, reason = tripped.trip
                agent.close(reason, kind=kind)
                acted.append((agent.agent_id, kind))
            elif overdue is not None:
                agent.close("run deadline exceeded", kind=KILL_TIMEOUT)
                acted.append((agent.agent_id, KILL_TIMEOUT))
            elif not active and not agent.busy:
                if now - agent.last_activity > agent.cfg.idle_timeout:
                    agent.close("idle", kind=KILL_IDLE)
                    acted.append((agent.agent_id, KILL_IDLE))
        self._evict_closed()
        return acted

    def _evict_closed(self) -> None:
        with self._lock:
            closed = [
                a for a in self._agents.values()
                if a.closed and all(r.state in TERMINAL_STATES for r in a.runs())
            ]
            for agent in closed:
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
        fallback: str = "full",
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Agent:
        chosen = self.select_provider(provider, fallback)
        refusal = self.settings.workspace_refusal(workspace)
        if refusal is not None:
            raise RegistryError(refusal)
        with self._lock:
            live = [a for a in self._agents.values() if not a.closed]
            if len(live) >= self.settings.max_agents:
                raise RegistryError(
                    f"agent limit reached ({self.settings.max_agents} live). "
                    "Cancel one with cancel, or raise [core].max_agents."
                )
            here = [a for a in live if a.cfg.name == chosen.name]
            if len(here) >= chosen.max_agents:
                raise RegistryError(
                    f"agent limit reached on provider {chosen.name!r} "
                    f"({chosen.max_agents} live). Cancel one with cancel, or raise "
                    f"[providers.{chosen.name}].max_agents."
                )
            agent_id = self._claim_agent_id()
            agent = Agent(
                agent_id=agent_id,
                name=name or f"subagent-{agent_id}",
                workspace=workspace,
                model=model or chosen.model or "",
                settings=self.settings,
                trace=self.trace,
                cfg=chosen,
                fallback=fallback,
                chain=self.settings.chain(chosen.name, fallback),
                lane_state=self.lane_state,
                provider_load=self._provider_load,
                telemetry=self.telemetry,
                on_event=on_event,
            )
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
