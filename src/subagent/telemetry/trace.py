"""Append-only trace of what this server decided, so it can be measured later.

The child's own runtime already persists a rich durable log -- every tool call
with arguments, every hook invocation with its exit code and duration, token
usage per step. What it does not hold is this server's side of the story: which
tier answered an escalation, what facts that tier was shown, whether the
verification command passed, how much a distilled answer actually compressed.
All of that lived in memory and stderr, and died with the process.

Three questions in wiki/active-work.md need exactly that data:

  - the classifier's false-positive rate (which escalations were routine)
  - whether [core].summary_tokens and [core].chars_per_token are calibrated
  - whether [core].max_steps and [core].turn_token_budget are near real usage

So each decision and each finished run appends one JSON object here.
`scripts/trace_report.py` reads them back.

Two rules, both inherited from elsewhere in this codebase and both load-bearing:
the trace never touches stdout, which carries JSON-RPC frames; and it records
what the supervisor was shown, which is structured facts, never the prose the
child wrote.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import log

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
#    Additive service-provider fields (2026-10-08): native_usage, uat,
#    usage_unreported, credits, credits_unit, cash_usd, turn_usage, extra,
#    needs_human, human_action and plan_id. Native values are never repriced.
SCHEMA = 3

KINDS = (
    "run", "hop", "turn", "sample", "run_summary", "verdict", "calibration",
    "delegation", "egress_refused", "guard_tamper", "sandbox_bypass",
    "bypass_audit_unavailable",
)


class Trace:
    """One JSONL file, appended under a lock. Failures are logged, never raised."""

    def __init__(self, path: Path | None, context: dict[str, Any] | None = None):
        self.path = path
        self.context = dict(context or {})
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

    def set_context(self, **fields: Any) -> None:
        """Merge caller context (delegation_id, ...) into every record written.

        The loop sets `delegation_id` for the duration of a delegation so every
        run, hop, turn and verdict it produces can be grouped later.
        """
        self.context.update(fields)

    def write(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        # Bench runs tag every record with the harness's run id; delegation_id
        # comes from the loop via set_context. Explicit fields always win.
        context: dict[str, Any] = {}
        bench_run_id = os.environ.get("SUBAGENT_BENCH_RUN_ID")
        if bench_run_id:
            context["bench_run_id"] = bench_run_id
        context.update(self.context)
        record = {"schema": SCHEMA, "ts": round(time.time(), 3), "kind": kind, **context, **fields}
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

    def egress_refused(
        self, *, agent_id: str, run_id: str, provider: str, host: str, port: int,
    ) -> None:
        """Append the additive, destination-specific egress refusal record."""
        self.write(
            "egress_refused", type="egress_refused", agent_id=agent_id,
            run_id=run_id, provider=provider, host=host, port=port,
        )

    def guard_tamper(self, *, agent_id: str, run_id: str, path: str) -> None:
        """Append the additive DSH bootstrap tamper record."""
        self.write("guard_tamper", type="guard_tamper", agent_id=agent_id,
                   run_id=run_id, path=path)

    def sandbox_bypass(
        self, *, agent_id: str, run_id: str, provider: str, tool: str, command: str,
        source: str | None = None,
    ) -> None:
        """Append the additive record for a tool step that asked to leave agy's sandbox."""
        extra = {"source": source} if source is not None else {}
        self.write("sandbox_bypass", type="sandbox_bypass", agent_id=agent_id,
                   run_id=run_id, provider=provider, tool=tool, command=command[:500],
                   **extra)

    def bypass_audit_unavailable(
        self, *, agent_id: str, run_id: str, provider: str, reason: str,
    ) -> None:
        """Append the additive record for a bypass audit that could not read its record."""
        self.write("bypass_audit_unavailable", type="bypass_audit_unavailable",
                   agent_id=agent_id, run_id=run_id, provider=provider, reason=reason[:500])

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

    def run(
        self,
        run: Any,
        workspace: str,
        model: str,
        guard: str | None = None,
        *,
        entry: str = "mcp",
        exit_code: int | None = None,
        lock: str | None = None,
    ) -> None:
        verification = run.verification_result
        verification_dict = verification.as_dict() if verification else None
        started = run.started_at or 0
        finished = run.finished_at or 0
        refusal = next(
            (h.get("code") for h in reversed(getattr(run, "hops", []) or [])
             if h.get("outcome") == "refused"),
            None,
        )
        refused_codes = [
            h.get("code") for h in (getattr(run, "hops", []) or [])
            if h.get("outcome") == "refused"
        ]
        extra: dict[str, Any] = {}
        native = getattr(run, "native_outcome", None)
        if native is not None:
            native_verification = getattr(run, "native_verification", False)
            if native_verification:
                verification_dict = native.get("verification")
            for key in ("uat", "usage_unreported", "credits", "credits_unit", "cash_usd",
                        "turn_usage", "extra", "needs_human", "human_action", "plan_id",
                        "verification_note"):
                if key in ("uat", "verification_note") and not native_verification:
                    continue
                if key in native:
                    extra[key] = native[key]
            if not native_verification and getattr(run, "goal", None) is not None:
                extra["uat"] = run.uat
        if getattr(run, "usage_unreported", None):
            extra["usage_unreported"] = list(run.usage_unreported)
        if getattr(run, "native_usage", False):
            extra["native_usage"] = True
        verdicts = self.take_verdicts(run.agent_id)
        guard = guard or getattr(run, "guard", None) or "hook"
        if (
            getattr(run, "driver", None) in ("codex", "dsh")
            and "hook" in guard
            and run.usage.steps > 0
            and not any(verdicts.values())
        ):
            # Codex ran commands or patches and the hook was never asked: it
            # did not fire, and only the sandbox stood between the child and
            # the machine. A Claude child cannot get here: its hook must answer.
            guard = "sandbox (hook silent)"
            extra["hook_silent"] = True
        parent = getattr(run, "parent", None)
        if parent:
            extra["parent"] = parent
        # R7a/S65 facts belong under `extra`, the free-form bucket the schema
        # already allows on a run record.
        run_extra = extra.get("extra")
        run_extra = dict(run_extra) if isinstance(run_extra, dict) else {}
        if getattr(run, "overrun", None):
            # The per-run cap or watchdog limit this run hit (R7a).
            run_extra["overrun"] = dict(run.overrun)
        if getattr(run, "released_agents", None):
            # Idle agents the registry closed to admit this run (S65).
            run_extra["released_agents"] = list(run.released_agents)
        if run_extra:
            extra["extra"] = run_extra
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
            guard=guard,
            # Which route started this run: the MCP delegate tool, `subagent
            # run`, or the `subagent exec` wrapper. Additive to schema 3.
            entry=entry,
            exit_code=exit_code,
            lock=lock,
            state=run.state,
            finish_reason=run.finish_reason,
            elapsed_seconds=round(finished - started, 2),
            # Submit to finish, queue wait included; elapsed_seconds starts at dispatch.
            wall_seconds=round(finished - (run.created_at or started), 2),
            ttft_seconds=getattr(run, "ttft_seconds", None),
            turns=(run.usage.turns if getattr(run, "native_usage", False)
                   else len(getattr(run, "turn_log", []) or [])),
            tool_calls=run.usage.steps,
            continues=getattr(run, "continues", 0),
            end_state=run.state,
            verification_passed=(verification_dict.get("passed") if verification_dict else None),
            guard_verdicts=verdicts,
            refusal_code=refusal,
            refused_codes=refused_codes,
            usage=run.usage.as_dict(),
            cost=getattr(run, "cost", None),
            # Lengths, not text: the trace is for measuring, not for archiving
            # somebody's source code or a client's data.
            prompt_chars=len(run.prompt),
            result_chars=len(run.result_text),
            **distil,
            verification=verification_dict,
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
            message=hop.message,
            reset_at=hop.reset_at.isoformat() if hop.reset_at else None,
            closed_until=hop.closed_until.isoformat() if hop.closed_until else None,
            admission=getattr(hop, "admission", None),
            would_refuse=getattr(hop, "would_refuse", None),
            admit_reason=getattr(hop, "admit_reason", None),
            cold_load=getattr(hop, "cold_load", False),
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

    def delegation(self, **fields: Any) -> None:
        """One whole loop run (autopilot or a single round): rounds, totals, verdict."""
        self.write("delegation", **fields)


def open_trace(raw: str | None, session_root: Path) -> Trace:
    """Resolve [core].trace. Unset writes to the session root; `off` disables it."""
    if raw is not None and raw.strip().lower() in ("off", "0", "false", ""):
        return Trace(None)
    if raw:
        return Trace(Path(raw).expanduser().resolve())
    return Trace(session_root / "trace.jsonl")
