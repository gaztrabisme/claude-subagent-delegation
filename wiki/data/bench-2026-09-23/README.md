# bench-2026-09-23 — first cell of the fused benchmark

One cell, one run: the claude harness running the cron task in delegate mode, cell id
`claude-config-0367aa72-cron-delegate-1`. `results.csv` is the cell's row (costs, tokens,
rounds, reviews, hidden tests, wall), `summary.md` the means and per-cell tables,
`providers.csv` the per-provider aggregation, `delegations.csv` the delegation trace records
behind the row, and `dashboard.html` the `subagent report` over those traces. The run went
through a private config — providers `glm`, `deepseek` and `grok` declared, `glm` as the
`normal` tier and the test writer, `deepseek` as the reviewer, which refused on balance,
`run_timeout = 1800` — that is not committed here; the `config-0367aa72` stem in the cell id
identifies it in the bench output.
