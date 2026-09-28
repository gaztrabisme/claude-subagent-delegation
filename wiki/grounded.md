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
