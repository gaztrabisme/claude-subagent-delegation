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
