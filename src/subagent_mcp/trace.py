"""Append-only trace of what this server decided, so it can be measured later.

The child's own runtime already persists a rich durable log -- every tool call
with arguments, every hook invocation with its exit code and duration, token
usage per step. What it does not hold is this server's side of the story: which
tier answered an escalation, what facts that tier was shown, whether the
verification command passed, how much a distilled answer actually compressed.
All of that lived in memory and stderr, and died with the process.

Three questions in wiki/active-work.md need exactly that data:

  - the classifier's false-positive rate (which escalations were routine)
  - whether SAM_SUMMARY_TOKENS and SAM_CHARS_PER_TOKEN are calibrated
  - whether SAM_MAX_STEPS and SAM_TURN_TOKEN_BUDGET are near real usage

So each decision and each finished run appends one JSON object here.
`scripts/trace_report.py` reads them back.

Two rules, both inherited from elsewhere in this codebase and both load-bearing:
the trace never touches stdout, which carries JSON-RPC frames; and it records
what the supervisor was shown, which is structured facts, never the prose the
child wrote.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .config import log

# 2: run records carry the Claude Code session_id (2026-09-18)
# 3: one "hop" record per lane the router tried; run and hop records carry
#    lane, provider, driver and guard (2026-09-18)
#    Same schema, more kinds (2026-09-18, U5): "turn" (one model response:
#    tokens, timing, tool calls), "run_summary" (a local-lane run's samples,
#    peaks, decode tok/s, energy), and "sample" rows in metrics.jsonl. Run
#    records gained ttft/wall seconds, turns, tool_calls, continues, end
#    state, verification_passed, guard_verdicts, refusal_code and the parent
#    context; hop records gained the admission snapshot. Every kind and its
#    required keys: tests/test_trace_schema.py.
SCHEMA = 3

KINDS = ("run", "hop", "turn", "sample", "run_summary", "verdict", "calibration")


class Trace:
    """One JSONL file, appended under a lock. Failures are logged, never raised."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        # Guard verdicts per agent since that agent's last run record. An
        # agent's runs are serial, so these belong to the run that ends next.
        self._verdicts: dict[str, Counter[str]] = {}
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.warning("trace disabled: cannot create %s", path.parent, exc_info=True)
                self.path = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {"schema": SCHEMA, "ts": round(time.time(), 3), "kind": kind, **fields}
        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
            with self._lock, open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:  # noqa: BLE001 - observability is never worth a delegation
            # Broad on purpose. This runs on the agent's worker thread, and an
            # exception escaping here kills that thread: the run whose trace
            # failed looks fine, and every later run on that agent hangs. A
            # trace is the least important thing happening in this process.
            log.warning("could not append to %s", self.path, exc_info=True)

    def verdict(
        self,
        *,
        agent_id: str | None,
        tool: str,
        action: str,
        tier: str,
        reason: str,
        facts: dict[str, Any],
        latency_ms: float,
    ) -> None:
        if agent_id:
            with self._lock:
                self._verdicts.setdefault(agent_id, Counter())[action] += 1
        self.write(
            "verdict",
            agent_id=agent_id,
            tool=tool,
            action=action,
            tier=tier,
            escalated=tier != "policy",
            reason=reason,
            facts=facts,
            latency_ms=round(latency_ms, 1),
        )

    def take_verdicts(self, agent_id: str) -> dict[str, int]:
        """allow/deny/escalate counts for `agent_id` since the last call, then reset."""
        with self._lock:
            seen = self._verdicts.pop(agent_id, Counter())
        return {action: seen.get(action, 0) for action in ("allow", "deny", "escalate")}

    def run(self, run: Any, workspace: str, model: str, guard: str | None = None) -> None:
        verification = run.verification_result
        started = run.started_at or 0
        finished = run.finished_at or 0
        refusal = next(
            (h.get("code") for h in reversed(getattr(run, "hops", []) or [])
             if h.get("outcome") == "refused"),
            None,
        )
        extra: dict[str, Any] = {}
        parent = getattr(run, "parent", None)
        if parent:
            extra["parent"] = parent
        distil: dict[str, Any] = {"distilled": run.distilled, "truncated": run.truncated}
        if run.distilled:
            distil["raw_chars"] = len(run.final_response)
        self.write(
            "run",
            run_id=run.run_id,
            agent_id=run.agent_id,
            session_id=run.session_id,
            model=model,
            lane=getattr(run, "lane", None),
            provider=getattr(run, "provider", None),
            driver=getattr(run, "driver", None),
            workspace=workspace,
            guard=guard or getattr(run, "guard", None) or "hook",
            state=run.state,
            finish_reason=run.finish_reason,
            elapsed_seconds=round(finished - started, 2),
            # Submit to finish, queue wait included; elapsed_seconds starts at dispatch.
            wall_seconds=round(finished - (run.created_at or started), 2),
            ttft_seconds=getattr(run, "ttft_seconds", None),
            turns=len(getattr(run, "turn_log", []) or []),
            tool_calls=run.usage.steps,
            continues=getattr(run, "continues", 0),
            end_state=run.state,
            verification_passed=verification.passed if verification else None,
            guard_verdicts=self.take_verdicts(run.agent_id),
            refusal_code=refusal,
            usage=run.usage.as_dict(),
            # Lengths, not text: the trace is for measuring, not for archiving
            # somebody's source code or a client's data.
            prompt_chars=len(run.prompt),
            result_chars=len(run.result_text),
            **distil,
            verification=verification.as_dict() if verification else None,
            error=run.error,
            **extra,
        )

    def turn(self, **fields: Any) -> None:
        """One model response (see runs._Meter for the fields)."""
        self.write("turn", **fields)

    def run_summary(self, *, run_id: str, agent_id: str, lane: str, **fields: Any) -> None:
        """A local-lane run's telemetry, from its samples and turn records."""
        self.write("run_summary", run_id=run_id, agent_id=agent_id, lane=lane, **fields)

    def hop(self, *, run_id: str, agent_id: str, hop: Any) -> None:
        """One lane the router tried for a run (a router.Hop)."""
        self.write(
            "hop",
            run_id=run_id,
            agent_id=agent_id,
            hop=hop.index,
            lane=hop.lane,
            provider=hop.provider,
            driver=getattr(hop, "driver", None),
            guard=getattr(hop, "guard", None),
            model=hop.model,
            outcome=hop.outcome,
            code=hop.code,
            reset_at=hop.reset_at.isoformat() if hop.reset_at else None,
            closed_until=hop.closed_until.isoformat() if hop.closed_until else None,
            admission=getattr(hop, "admission", None),
            would_refuse=getattr(hop, "would_refuse", None),
            admit_reason=getattr(hop, "admit_reason", None),
        )

    def calibration(self, *, run_id: str, chars: int, output_tokens: int, assumed: float) -> None:
        """The chars-per-token ratio a distillation turn actually produced."""
        self.write(
            "calibration",
            run_id=run_id,
            chars=chars,
            output_tokens=output_tokens,
            observed=round(chars / output_tokens, 3),
            assumed=assumed,
        )


def open_trace(raw: str | None, session_root: Path) -> Trace:
    """Resolve SAM_TRACE. Unset writes to the session root; `off` disables it."""
    if raw is not None and raw.strip().lower() in ("off", "0", "false", ""):
        return Trace(None)
    if raw:
        return Trace(Path(raw).expanduser().resolve())
    return Trace(session_root / "trace.jsonl")


