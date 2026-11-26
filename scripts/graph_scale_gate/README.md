# Durable-graph scale gate

Measures 2 roots x 100 role requests (200 children) through Temporal on real lanes and
audits the result against six conditions (`LOG_SCHEMA.md`, `audit_gate.py`).

## Run

```sh
uv sync --extra graph
# Offline rehearsal: fake children, about 75 seconds, ends with six PASS lines
uv run --extra graph python scripts/graph_scale_gate/orchestrate.py --dispatch fake
# Real lanes (omlx and deepseek from your normal subagent config). Run from the repo root.
uv run --extra graph python scripts/graph_scale_gate/orchestrate.py --dispatch real
```

Useful flags: `--deepseek-total 20` (the rest are omlx), `--budget-cap 0.30`,
`--deepseek-estimate 0.05`, `--timeout <seconds>`, `--run-root <dir>`, `--port <n>`,
`--trace <file>` (real mode defaults to the Settings trace). `TEMPORAL_BIN` overrides
`/opt/homebrew/bin/temporal`. Each run writes everything under
`scripts/graph_scale_gate/.runs/gate-<mode>-<time>/` (git-ignored): `audit.jsonl`, logs,
`temporal.db`, `ledger.sqlite3`, `fake_trace.jsonl` (fake only), `workspaces/` (real only).
Exit code 0 means all six conditions passed.

Real mode starts the worker with the user's normal Settings and dispatches 200 real runs
(180 omlx, 20 deepseek, one at a time per lane): it takes hours and spends deepseek money
up to the budget cap. Fake mode never touches a lane or a key.

## Files

| File | Role |
|---|---|
| `orchestrate.py` | Entry point: dev server, worker, clients, kill/restart choreography, shutdown, runtime-error ingest, runs the audit |
| `gate_common.py` | Audit writer (one O_APPEND line), run config, plan and workflow-spec builder |
| `gate_workflow.py` | Interpreter workflow: root -> level-2 -> level-3 child workflows, role requests, owner decision |
| `gate_activities.py` | `run_request` (journal, attempts, reserve, dispatch, settle) and `audit_marker` |
| `gate_ledger.py` | SQLite (BEGIN IMMEDIATE): budget reservations, attempts, request-id journal, marker dedupe |
| `gate_dispatch.py` | `fake` dispatcher and `real` dispatcher (subagent Registry) |
| `gate_worker.py` | Singleton worker: control queue plus one queue per lane, `max_concurrent_activities` = cap |
| `gate_client.py` | Client process: submit or re-attach a root; deliver the owner signal |
| `audit_gate.py`, `LOG_SCHEMA.md`, `LOCK.txt` | Locked contract and audit; do not edit |

## Real mode

`RealDispatcher.__init__` loads `config.load(Path.cwd())`, builds `Registry(settings)` and a
`Supervisor(settings, registry, trace=registry.trace)` and serves its approval socket, as
`mcp_server` does. Per child: a fresh workspace under the run dir, `select_provider(lane,
"none")`, `Agent.validate_caps`, `Registry.create_agent(... fallback="none")`,
`agent.wait_ready`, `agent.delegate(task, "test -s ok.txt", timeout=300, max_steps=6)`, wait
on `run.done`, read `run.run_id`, `run.cost["provider_usd"]`, `run.state == "completed"`,
then `agent.close` to free the slot.
