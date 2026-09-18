# Goal — 2026-09-18 — unified-subagent

**Set by:** Gary, via `/goal`. **Audited by:** the coordinator at close, against this file only.

Repo: `S` = `~/Documents/Work/tools/mcp/subagent-mcp`. Source of the copied runtime: `G` = `~/Documents/Work/tools/mcp/glm-subagent` (main `96933a2`).

## Deliverable U1 — runtime copied, lanes registered

| # | Success criterion | UAT |
|---|---|---|
| 1.1 | S holds G's runtime renamed to `subagent_mcp`, one tool set (`delegate/await/continue/list/cancel/transcript`), `SAM_` config with per-lane overrides; G's tests ported and passing | `uv run pytest -q` exits 0 in S |
| 1.2 | No `--bare`; claude child argv carries `--tools`, `--strict-mcp-config`, `--setting-sources ""` | `grep -rc '"--bare"' S/src` totals 0; a test asserts the argv |
| 1.3 | Five lanes registered: `codex`, `deepseek` (claude -p → `api.deepseek.com/anthropic`), `glm`, `bppc` (Qwen3.8-27B :8080), `omlx` (`Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B`, no sampling params sent) | a test enumerates the registry and each lane's driver, base URL and model |

## Deliverable U2 — router and fallback

| # | Success criterion | UAT |
|---|---|---|
| 2.1 | Chain = caller's primary (default `glm`), other cloud lanes in order codex → deepseek → glm, then bppc, then omlx; `fallback` = `full`/`local`/`none` | router tests pass |
| 2.2 | Fallback fires only on refusal before work: z.ai 1308/1310, 1313 after backoff exhausted, "Insufficient Balance", Codex usage limit, failed health gate. A child that fails after starting work is reported, never rerouted | tests with a mock endpoint returning each refusal, and one mid-run failure that must not reroute |
| 2.3 | A refusal with a reset time or a balance error closes the lane on disk until then; the router skips it without a call | test: second dispatch makes zero calls to the closed lane |
| 2.4 | Local lanes health-gated (bppc LAN then Tailscale, `:8080/health`; omlx `/api/status`), never started by the server | tests; `grep -c start-llm S/src` is 0 |

## Deliverable U3 — Codex driver

| # | Success criterion | UAT |
|---|---|---|
| 3.1 | `codex exec --json` with `--sandbox workspace-write`, events to transcript, `resume` for continue, usage-limit detection, trace marks the run `guard: sandbox` | tests against a fake `codex` binary pass |
| 3.2 | Live Codex run | DEFERRED until 2026-09-20 13:29; recorded as deferred, not passed |

## Deliverable U4 — live checks

| # | Success criterion | UAT |
|---|---|---|
| 4.1 | Hook isolation holds on the claude driver | `S/scripts/hook_isolation_check.py` exits 0 |
| 4.2 | A real tool-use turn completes on oMLX REAP, and the request body oMLX receives is logged to show whether temperature is sent | `S/scripts/smoke_lanes.py --lane omlx` exits 0; the log file names the sampling fields sent |
| 4.3 | Live GLM guard deny | `smoke_lanes.py --lane glm` exits 0 with a `kind: verdict` deny record |
| 4.4 | bppc and DeepSeek live | run if the lane is open at close; otherwise DEFERRED with the reason |

## Deliverable U5 — telemetry

| # | Success criterion | UAT |
|---|---|---|
| 5.1 | Trace schema 3: per-turn and per-hop records with lane, provider, model, session_id, tokens in/out/cache-read/cache-write, wall time, TTFT, turns, tool calls, end state, verification result, continues, guard verdicts, refusal code | a test validates every record kind against the schema |
| 5.2 | Local lanes: admission snapshot at dispatch (oMLX status, memory, swap, pressure; bppc VRAM, slots), a 10 s sampler only while a child is active on that lane, a per-run summary (peak memory, peak swap, mean/p10 decode tok/s, bppc energy) | tests with stubbed probes; `metrics.jsonl` rows present after the 4.2 smoke run |
| 5.3 | Admission thresholds are log-only by default (`would_refuse` recorded), enforceable per lane by env flag | test for both modes |
| 5.4 | Cost join: trace plus parent transcripts (`~/.claude/projects/**/*.jsonl`) on session_id, with a pricing table (Claude list price counterfactual, provider actual cost, local 0), written to parquet | `S/scripts/cost_join.py` exits 0 on this machine's real runs and prints net saving per lane |

## Deliverable U6 — concurrency benchmark

| # | Success criterion | UAT |
|---|---|---|
| 6.1 | `bench_concurrency.py` runs one task at 1/2/4/8 concurrent children on oMLX, CSV of aggregate throughput, per-child TTFT, peak memory | CSV exists with 4 rows; the oMLX `max_agents` default is set from it and recorded in `wiki/decisions.md` |

## Deliverable R — review, record, retire

| # | Success criterion | UAT |
|---|---|---|
| R.1 | Adversarial review of router, fallback, lane memory, Codex sandbox, telemetry; every HIGH fixed or recorded open with a reason | `S/wiki/review.md` exists; no `OPEN-HIGH` without a reason |
| R.2 | Committed locally, nothing pushed | `git -C S log --oneline` non-empty; no remote configured or `git status -sb` shows no upstream push |
| R.3 | Wiki: `index`, `decisions` (with rejected options), `active-work`, `log` with this run's ledger | files exist; log has a 2026-09-18 entry |
| R.4 | `~/.claude.json`: `subagent` server added; `glm-subagent` and `qwen-subagent` removed once their lanes pass on S (`glm` 4.3; `bppc` 4.4 or kept until it passes); `deepseek-subagent` kept until 4.4 passes live for deepseek. Backup taken first | `~/.claude.json.bak-2026-09-18` exists; `jq '.mcpServers \| keys'` matches the rule |

## Working pattern (binds this run)

Codex closed until 2026-09-20 13:29, DeepSeek balance empty, bppc :8080 down at plan time, GLM intermittently throttled (1313). Build units: GLM primary, Claude Agent fallback; U3 on a Claude Agent. Review on a Claude Agent. The coordinator runs every UAT itself and never does unit work. Old repos stay on disk; nothing pushed.
