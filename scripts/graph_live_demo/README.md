# Durable graph live demo

Runs a small graph (plan, two parallel builds, review, join gate) on the durable graph engine,
restarts the Temporal dev server while a build is running, and checks the run still finishes
with no repeated work and consistent cost figures.

## Run

Offline, no provider (scripted fake dispatcher, the default, about one minute):

    uv run --extra graph python scripts/graph_live_demo/run_demo.py --dispatcher fake

Real providers (spends money or local compute; needs the `temporal` CLI on PATH or
`TEMPORAL_BIN`, and the provider configured in your user config):

    uv run --extra graph python scripts/graph_live_demo/run_demo.py --dispatcher registry --provider omlx --budget 0.50 --max-minutes 40

Options: `--provider` (default `omlx`), `--budget` (root cap, default 0.50), `--max-minutes`
(total wait, default 40). The provider and budget are written into a per-run copy of `demo.json`
(`provider` and `budget_usd` cannot be templated in a graph document).

Each run gets `scripts/graph_live_demo/.runs/<timestamp>/` holding `project/` (a copy of
`scratch_template/` plus a `[graph]` config), the graph state, the trace and worker logs. The
script stops the worker and the dev server it used before it exits.

## What it proves

Exit 0 only when all four hold:

1. The graph reaches `completed`.
2. The dev server was stopped and started again on the same database while a task was running
   or queued (read from the journal at the moment of the restart).
3. No request has more than one completed dispatch (journal, plus the fake audit log in fake mode).
4. The `graph status` spent total equals the cost summed from the run records of the subagent
   trace for the run ids in the journal, within $0.001. The fake dispatcher writes no trace, so
   fake mode compares against the journal's outcome costs instead.

Also printed: per-node status and attempts, and dispatch attempts per request.
