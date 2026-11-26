"""Graph trace record writers (spec section 10). Additive: schema stays 3, kinds stay local."""

from __future__ import annotations

from typing import Any

from ..telemetry.trace import Trace

GRAPH_KINDS: dict[str, tuple[str, ...]] = {
    "graph_run": ("root_run_id", "run_path", "graph_id", "graph_hash", "event", "state", "depth",
                  "cap_usd", "spent_usd"),
    "graph_node": ("root_run_id", "run_path", "node_id", "kind", "scope", "status", "attempts",
                   "request_id", "cost_usd", "error"),
    "graph_admission": ("root_run_id", "attempt_id", "lane", "event", "reason", "active", "cap"),
    "graph_budget": ("run_path", "attempt_id", "event", "amount_usd", "cost_source",
                     "remaining_usd"),
    "graph_exception": ("exception_id", "root_run_id", "kind", "event", "needs_answer"),
}


_FIELD_RENAMES = {"graph_node": {"kind": "node_kind"},
                  "graph_exception": {"kind": "exception_kind"}}


def _write(trace: Trace, kind: str, fields: dict[str, Any]) -> None:
    """Write exactly the required keys; a missing or unknown key (e.g. prompt text) is refused."""
    required = set(GRAPH_KINDS[kind])
    if set(fields) != required:
        raise TypeError(f"{kind} takes exactly {sorted(required)}, got {sorted(fields)}")
    # Spec section 10 lists a `kind` field for graph_node and graph_exception; it collides with
    # the record's own `kind`, so it is written as `node_kind` / `exception_kind`.
    rename = _FIELD_RENAMES.get(kind, {})
    trace.write(kind, **{rename.get(k, k): v for k, v in fields.items()})


def write_graph_run(trace: Trace, **fields: Any) -> None:
    _write(trace, "graph_run", fields)


def write_graph_node(trace: Trace, **fields: Any) -> None:
    _write(trace, "graph_node", fields)


def write_graph_admission(trace: Trace, **fields: Any) -> None:
    _write(trace, "graph_admission", fields)


def write_graph_budget(trace: Trace, **fields: Any) -> None:
    _write(trace, "graph_budget", fields)


def write_graph_exception(trace: Trace, **fields: Any) -> None:
    _write(trace, "graph_exception", fields)
