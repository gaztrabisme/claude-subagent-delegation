# Active work

Reader: the next session that picks this server up. Read with `decisions.md` and `review.md`.

## State (2026-09-18, master 197c681, 465 tests)

| Area | State |
|---|---|
| Server | Registered in `~/.claude.json` as `subagent` (six tools: delegate, await, continue, list, cancel, transcript). Loads at the next Claude Code restart. Backup: `~/.claude.json.bak-2026-09-18` |
| Old servers | `glm-subagent` and `qwen-subagent` removed from `~/.claude.json` (their lanes passed live here). `deepseek-subagent` kept until the deepseek lane passes live. Repos stay on disk |
| glm lane | Live PASS (guard deny, file written, turn and run tokens agree) |
| omlx lane | Live PASS; no sampling fields sent; `max_agents` 1 from the bench |
| bppc lane | Live PASS after the `fold_system` adapter (Claude Code puts a system message mid-conversation; the Qwen3.8 llama.cpp template rejects it) |
| deepseek lane | DEFERRED: live 402 Insufficient Balance, correctly classified and closed |
| codex lane | DEFERRED: quota closed until 2026-09-20 13:29; covered by a fake binary only |
| Telemetry | Schema 3: run, hop, turn, verdict, calibration, run_summary in `trace.jsonl`; samples in `metrics.jsonl` |
| Cost join | `scripts/cost_join.py` on 737 real runs, output in `data/cost-2026-09-18/`. Net saving (Opus 5 list price of child tokens − parent Claude spend − provider cost): glm $711.40 (1,104.22 − 312.82 − 80.00 plan), deepseek $172.82 (231.75 − 28.88 − 30.04 at peak rates), bppc $0.68. Verified-pass rate: glm 51%, deepseek 31%, bppc 50% |

## Next

1. Restart Claude Code so `subagent` loads; the first delegate writes to `~/.subagent-mcp/sessions/trace.jsonl`.
2. After 2026-09-20 13:29: `uv run python scripts/smoke_lanes.py --lane codex --out <dir>`. It proves the Codex hook fires from a per-agent CODEX_HOME, the real tool names, resume flag order and the symlinked login (review M4, M5, M6).
3. Top up DeepSeek, then `smoke_lanes.py --lane deepseek`; on PASS remove `deepseek-subagent` from `~/.claude.json`.
4. Rerun `bench_concurrency.py` on omlx with a larger task before raising `max_agents` above 1 (one run per level so far).

## Open

- Review M2, M5, M6, M7, L1, L2, L5 (see `review.md` for reasons). M7 means Codex time-to-first-token is not comparable with the other lanes.
- bppc cold start through the owner's proxy is tested against a mock only.
- bppc `energy_wh` needs at least two samples; a short run gets null. llama.cpp runs without `--metrics`, so token counters come from our own stream timing.
- The guard escalates `uv run --group <g> pytest` as a verification command (plain `uv run pytest` is allowed); use a verification without `--group` or extend the runner rule.
- `README.md` still describes glm-subagent's tools and `GSA_` variables.
- `pyproject.toml` homepage points at `github.com/gaztrabisme/subagent-mcp`, which may not exist. Nothing is pushed; there is no remote.
