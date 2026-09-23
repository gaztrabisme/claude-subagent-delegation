# Decisions

Reader: whoever changes this server next. Each entry says what was chosen, what was rejected and why. Decisions inherited from glm-subagent (child isolation, hook contract, continue by `--resume`, nested tools denied, no `--bare`) are in `inherited/glm-decisions.md` as D1–D10 and still hold unless an entry below replaces them.

## S1 — One server, five lanes, two child drivers

**Chosen:** one MCP server with one tool set (`delegate`, `await`, `continue`, `list`, `cancel`, `transcript`). Four lanes (deepseek, glm, bppc, omlx) run `claude -p` against an Anthropic-compatible endpoint; codex runs `codex exec --json`. The driver is chosen per hop (`ClaudeDriver`, `CodexDriver`), so one delegation can fall from a claude lane to codex or back; the first hop that runs binds the agent's lane and driver for every continue.

**Rejected:** a Responses↔Anthropic translation proxy so `claude -p` could drive GPT models. Codex authenticates with a ChatGPT account (no API key), and no maintained proxy handles that login. **Rejected:** an Agent subclass per driver (U3's first shape); it could not change driver mid-chain, which the fallback needs.

## S2 — DeepSeek runs through `claude -p`, not DeepSeek Harness

**Chosen:** `claude -p` against `https://api.deepseek.com/anthropic`, so every non-Codex lane shares one guard, one trace and one tool set, and cross-lane numbers are comparable. **Rejected:** keeping the Harness runtime; it would need a second guard and its own telemetry. deepseek-subagent stays installed until this lane passes live.

## S3 — Fallback only on refusal before work

**Chosen:** a lane is skipped only when it refuses before the child did any work (no tool call, zero output tokens): z.ai 1308/1310, 1313 after backoff, DeepSeek 402 Insufficient Balance, Codex usage limit, failed health gate. A child that fails after starting work is reported on its lane and never rerouted. **Rejected:** rerouting any failure; a second child would start in a workspace the first already changed.

Chain: the caller's primary (default glm), then the other cloud lanes in the order codex, deepseek, glm, then bppc, then omlx. `fallback` = `full` | `local` (primary, bppc, omlx) | `none`.

## S4 — Lane closures on disk

**Chosen:** a refusal with a reset time closes the lane in `<session_root>/lane_state.json` until then; a balance error closes it for `SAM_BALANCE_CLOSE_HOURS` (6); 1313 exhausted for `SAM_THROTTLE_CLOSE_MINUTES` (15). The router skips a closed lane without calling it. Health failures never close a lane. **Rejected:** memory-only state; every new session would pay a refused call per dead lane.

## S5 — Local lanes are health-gated, never started

**Chosen:** bppc resolves its host (tailscale-reported LAN address, then `SAM_BPPC_LAN_HOSTS`, default 192.168.1.17, then 100.106.185.34) and probes `:8080/health`; omlx probes `/api/status`. The server runs no `start-llm`, docker or llama.cpp command. A request through the owner's proxy that triggers the proxy's own cold start is allowed; `backend: "stopped"` (bppc) and a not-loaded model (omlx) extend the run deadline. **Rejected:** starting models from the server; it would fight the owner's GPU and memory use.

## S6 — The Codex lane has the guard hook as well as the sandbox

**Chosen:** codex-cli 0.153 has stable PreToolUse hooks. Each Codex child gets a per-agent `CODEX_HOME` (auth.json symlinked, a written config.toml, hooks.json with our guard) plus `-s workspace-write` and `--dangerously-bypass-hook-trust`. Codex rejects `permissionDecision: allow`, so the hook allows by silent exit 0. **Rejected:** sandbox only (the plan before the hooks were found). Live proof waits for the Codex quota reset on 2026-09-20 13:29.

## S7 — oMLX gets no sampling settings, and one child at a time

**Chosen:** the omlx lane sends no temperature/top_p/top_k; the oMLX dashboard owns them. Verified live: Claude Code's requests carry only `thinking` among model controls (`data/omlx-request-log-2026-09-18.jsonl`). Default `max_agents` 1, from `data/bench-omlx-2026-09-18.csv`: aggregate output tok/s 20.1 at 1 child, 18.3 at 2, 7.6 at 4, 8.2 at 8; p90 time to first token 3.9 s → 181 s; swap 0 throughout. Concurrency lowered throughput, most likely because each child's Claude Code prompt is prefilled from scratch and the prefills compete. One run per level; rerun before raising it. **Rejected:** 4 (the plan's starting guess) and 8 (oMLX's scheduler width).

## S8 — Telemetry: record first, enforce later

**Chosen:** trace schema 3 with kinds run, hop, turn, verdict, calibration, run_summary, and metrics.jsonl samples every 10 s on a local lane while a child is active. Tokens are counted per assistant message as they arrive, so failed and cancelled runs keep their usage. Admission thresholds (swap, pressure, queue, VRAM) default to log-only (`would_refuse` on the hop); `SAM_<LANE>_ADMIT_ENFORCE=1` makes them skip the hop. **Rejected:** enforcing guessed thresholds on day one; fallbacks nobody can explain.

## S9 — Cost is joined by run_id, not session_id

**Chosen:** `scripts/cost_join.py` finds each run's parent tool calls by the `run_id` in the parent transcript's tool results. Claude Code gives the MCP server no parent session id, and the trace's `session_id` is the child's own. Child tokens are priced at the counterfactual Claude model (default claude-opus-5); parent tokens at the parent message's own model; a parent message that issued calls for several runs is split evenly. Provider cost is null until known, never zero.

## S10 — Retirement of the old servers

**Chosen:** glm-subagent leaves `~/.claude.json` once the glm lane passes live on this server; qwen-subagent once bppc passes live; deepseek-subagent once deepseek passes live. Repos stay on disk.

## S11 — One tool: the delegation loop on the provider runtime (2026-09-22)

**Chosen:** the loop from khangzxrr/claude-to-copilot-delegation (plan → test outline → worker rounds → verify → cross-family review) runs its workers through this server's `Registry`/`Agent`, so every worker round gets the guard hook, the trace, server-side verification and the provider chain. Both git histories kept (`git merge --allow-unrelated-histories`). **Rejected:** keeping two tools with a shared library; the loop's worker spawn and the server's agent spawn were the same step and would have drifted.

## S12 — CLI plus SKILL.md is the primary interface; the MCP server is a wrapper

**Chosen:** `subagent` (console script) with `skills/delegate/SKILL.md` calling it; `subagent-mcp` keeps the six tools as a thin wrapper. Every harness runs shell commands, but only Claude Code takes an MCP config on the headless command line, and the default MCP tool-call timeouts on Codex (60 s), Gemini (10 min) and opencode kill a 30-minute delegation. **Rejected:** MCP-only (would need five config dialects and per-harness timeout tuning).

## S13 — TOML config, no built-in providers, no default fallback chain

**Chosen:** `~/.config/subagent/config.toml` → `<project>/.subagent/config.toml` → `$SUBAGENT_CONFIG`, deep-merged; `[providers.<name>]` declares driver, endpoint, model, `api_key_env`, health, probe, pricing; `[fallback].chain` is explicit or absent; `[core].default_provider` is required for the MCP tool. `subagent init` writes from `examples/`; `subagent doctor` probes. Secrets only by env var name. **Rejected:** JSON (no comments), a TUI or HTML settings page (a config file is what full-time programmers already keep), and any machine default (the bppc/Tailscale/oMLX values now live only in `examples/`).

## S14 — Provider protocol with translators to Claude stream-json

**Chosen:** one `Provider` protocol (`argv`, `env`, `boot`, `spawn`, `translator`, `refusal`, `guard`); Codex, Copilot and Gemini translate their native events into Claude stream-json so `_Meter`, the trace and the loop read one shape; Grok emits it natively. Copilot mints its session id up front; Gemini is `experimental` (fixture-tested only, no binary on this machine). **Rejected:** a per-driver metering path.

## S15 — Guard context for the loop

**Chosen:** the loop passes `guard_context = {protected, state_allow}` per round; the classifier denies writes to protected test paths (all write families incl. redirects, `sed -i`, `python -c`, heredocs), writes under `.subagent/` outside the allowlist, and any `refs/subagent/` plumbing; `TestGuard.release()` runs as `pre_verify` in a `finally` before the server's verification, so a tampered test never decides `completed`. Without a context the classifier is unchanged. **Rejected:** relying on the chmod lock alone (a hooked driver is denied before the write; the lock remains the backstop for hookless drivers).

## S16 — Cost priced at run end; subscription spend labelled

**Chosen:** `price_run()` at `Agent._finish` from `[providers.*.pricing]` (per_token, credits at $0.01 per Copilot credit, local, flat_plan spread over the month at report time) plus the Claude counterfactual; `subagent report` labels plan quota as "plan (API-equivalent)". Every record carries `bench_run_id` and `delegation_id`; one `delegation` record per loop run. **Rejected:** `total_cost_usd` from `claude -p` (wrong on non-Anthropic endpoints; includes resumed-session totals).

## S17 — Bench matrix and the meeting-scribe task

**Chosen:** `bench/run.py` over harness × config × task × mode with per-cell session roots; orchestrator parsers for claude (`modelUsage`), codex (`turn.completed`), gemini (`stats`), grok, copilot (credits); `bench/tasks/meeting-scribe` uses a `FakeTranscriber` on JSON fixtures so 41 hidden tests are deterministic without an STT model. **Rejected:** a real STT dependency in the benchmark.

## S18 — Business case takes measured parity and concurrency as inputs

**Chosen:** `bench/business_case.py` prices tokens at the decode rate, adjusts local cost by parity (local verified-pass ÷ cloud) and retry factor, sizes the fleet from measured concurrency, and reports break-even and 12/24/36-month ROI with a sensitivity grid; published prices are defaults with sources, replaced by `--from-report`. At the published defaults the break-even is 74 months and needs 12 RTX PRO 6000 units for 100 seats; flash-class APIs at $0.15/M undercut a 27B self-host on token price, so the case rests on measured parity against Opus-class pricing, privacy and rate limits, not list prices. **Rejected:** presenting list-price savings as the result.

## S19 — Lane rules learned in this run

**Chosen:** one GLM child at a time (two concurrent children trigger the z.ai 1313 throttle every time); DeepSeek for critical-path units when its balance is up; Claude agents only after GLM and DeepSeek refuse (Gary, 2026-09-22); Codex gpt-5.6-luna at xhigh for review and code once its quota returned (Gary, 2026-09-23). A unit that hits its step cap is continued in the same session with an ordered recovery (commit WIP, get the target tests green, report by a fixed step).
