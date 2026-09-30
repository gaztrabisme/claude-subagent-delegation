# Grounded — 2026-09-18 — unified subagent MCP

Reader: the coordinator and the build lanes for this run.

## glm-subagent (G, main 96933a2, 231 tests)
- Claim: `claude -p` child with isolated CLAUDE_CONFIG_DIR, PreToolUse guard hook through a supervisor, schema-2 trace with session_id, z.ai 1308/1310 fail-fast and 1313 backoff, --max-turns = GSA_MAX_STEPS, continue reuses verification.
- Evidence: 231 tests; smoke_guard.py and hook_isolation_check.py PASS (review-guard-fixes.md).
- Constraint: `--bare` skips hooks (D9). Isolation is `--tools`, `--strict-mcp-config`, `--setting-sources ""`.
- Already a multi-backend runtime: the qwen lane is this binary with GSA_BASE_URL pointed at bppc :8080.

## deepseek-subagent (D, main 7b954b4, 506 tests)
- Child is the DeepSeek Harness runtime, not `claude -p`. Its guard has the same rules as G (both hardened in guard-fixes).
- DeepSeek balance empty as of 2026-09-15.

## Lanes probed today
- Codex CLI 0.153.4: `codex exec --json` and `codex mcp-server` exist. It authenticates with a ChatGPT account (auth.json), so `claude -p` cannot drive GPT models without a translation proxy. Quota closed until 2026-09-20 13:29.
- oMLX 0.7.0.dev2: `/v1/messages` answers with Anthropic-shaped errors. Model id `Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B`, not loaded (cold load on first call).
- bppc :8080: no answer on LAN or Tailscale.
- GLM: last runs have no status recorded; one exit 143 (cancel).

## MISSING
- ~~Whether oMLX `/v1/messages` returns tool_use blocks~~ — resolved 2026-09-18: REAP returned `tool_use Bash {"command":"ls"}`, stop_reason tool_use, 8.4 s including cold load. Streaming through `claude -p` is proven by U4 4.2.
- Whether DeepSeek's Anthropic endpoint serves deepseek-v4-pro with tool use; cannot test with no balance.
- ~~A guard for the Codex lane~~ — codex-cli 0.153 has stable hooks (`features list`: hooks stable) with PreToolUse in `$CODEX_HOME/hooks.json` and `--dangerously-bypass-hook-trust` for automation. U3 installs the guard there too; live proof waits for the 09-20 quota reset.
- Parent transcripts carry no subagent session id; the MCP server gets no parent session id in its env. The cost join keys on run_id found in the parent's tool_result.

Grounded: G and D wikis and code, report.md, ~/.claude.json lane env, live probes → one server with two child drivers (`claude -p`, `codex exec`); four of five lanes run through G's guarded runtime; Codex runs in its own sandbox.

# Grounded — 2026-09-28 — lanes

- Live server: uv tool install of 29ff214 (`subagent_mcp`, lanes.py: codex model None → copies ~/.codex/config.toml = gpt-6-sol/high). Launched by agent-capabilities/bin/subagent-mcp for Claude Code and Codex.
- fuse: no effort key anywhere; codex.py:324-326 drops user effort once `model` is set; mcp_server lists providers from config. Parallel-mode crash open (review.md:98-106). Baseline 4 failing tests (`counts`).
- omp-driver: c9698e6 + f73563c apply; 7531144 (Windows) conflicts on one import and is out of scope. Driver claims no hook; omp 18.0.11 has `--hook` with a blocking `tool_call` event. No endpoint wiring; omp refuses without models.yml.
- agy 1.2.12: stream-json = init / step_update (per step usage, tool_info) / result (status, response, usage). Probe fixture in session scratchpad. Hook support present in binary, format unknown.
- SAM_* → TOML map: review in plan; none migrates automatically; `supervisor="agent"` needs `supervisor_cmd`; oMLX key now `OMLX_API_KEY` env.
Grounded: live package lanes.py/health.py, fuse config.py/codex.py/mcp_server.py, omp-driver branch, agy probe, subagent.env → effort key and endpoint wiring are prerequisites; baseline must be made green first.

# Grounded — 2026-09-30 — v2 build

Reader: the coordinator and the build lanes for the v2 run.

## `wiki/active-work.md`, v2 feature list (2026-09-24)
- Claim: 18 items in six groups; the loop stays, v2 adds a goal layer, guard enforcement, any worker and the numbers.
- Evidence: agreed in conversation, never a goal file. Checked against the code today: no `provider` command, no `install` command, no `auth = "login"`, no `GOAL.md` handling; `init --from-env` and the `SAM_*` names still in `cli.py:169-261`; pricing still read from `scripts/pricing.toml` via `parents[3]` (`telemetry/cost.py:37`).
- Context: item 3's parallel-mode crash fixed 2026-09-28 (f6ebab1); item 16 partly done (live server cut over; uv tool `subagent-mcp` and the excluded shim `src/subagent_mcp/` remain).
- MISSING: a spec per item (each is one line); UAT rows; the new package name for item 17; the order of units that share `cli.py` and `bench/run.py`.

## efficient-pi `design/contracts/subagent-mcp-mapping.md` and `wiki/decisions.md` (2026-09-30)
- Claim: the harness builds serve mode to this repo's contract; release 1 acceptance needs only the six existing calls; the goal layer follows item 12 once approved here.
- Evidence: mapping lines 19-24, 58, 91-94, 151-158; decisions "Release 1 scope decisions" row 6.
- Implications: item 12's `goal` parameter and `uat` result shape (with `command` per row) are read by a second project; changing them later costs both sides. Carried to this repo: hop-code promotion into `router.py`, the final v2 shape, hosting `tests/test_harness_lane.py`.
- MISSING: serve mode itself (absent in the built core, `design/release1/R2-built-gaps.md:45`), so item 9 cannot be built or tested yet.

## `wiki/review.md`, `wiki/log.md` findings
- Open: U-B4 (Codex-orchestrated bench cell has no worker accounting: `run --wait` re-issued as new runs, no delegation record under the cell's session root), U-B1, U-L1, LOW hidden-test destination (`bench/run.py:87-97`).

## Lane check (21:40)
- `subagent doctor --provider <p> --prompt --json` with the key file loaded: codex PASS, glm PASS, gemini PASS, deepseek FAIL (`deepseek_balance`, 402).
- bppc: no answer on 192.168.1.17 or 100.106.185.34 (ssh timeout). omlx: server up, `models_loaded` 0, no prompt sent because `scripts/dwq_run.py --phase train` from the gsq-rco-mlx session is running on this Mac.
- Lane memory (`lane_state.json`): both recorded closures expired.
- `lane-plan.md`: the `subagent` server refuses this repo as a delegate workspace; units here run on direct `codex exec` in worktrees.

Baseline: `uv run pytest -q` 868 passed, 1 skipped; `ruff check src tests` clean; `fuse` at 6ae599a, 35 commits ahead of `origin/fuse`.

Grounded: active-work.md, decisions.md S11-S24, review.md, log.md, goals/2026-09-28-lanes.md, AGENTS.md, cli.py, telemetry/cost.py, efficient-pi contract and decisions, lane check → item 9 deferred with omp as stand-in; item 12's wire shape needs the owner's approval before build; item 15 cannot run until bppc, oMLX and DeepSeek are open; all code units go to Codex with GLM as the only open fallback.
