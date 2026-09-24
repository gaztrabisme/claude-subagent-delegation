# Active work

Reader: the next session that picks this repo up. Read with `decisions.md` (S11–S19) and `review.md`.

## State (2026-09-24, branch `fuse` at 61114ac, nothing pushed)

| Area | State |
|---|---|
| Repo | `origin` = github.com/gaztrabisme/claude-subagent-delegation; `fuse` carries both histories (this server's and the delegation loop's) plus P0–P4; `master` is the pre-fusion server. 805 tests pass (`uv run pytest -q`) |
| Package | `src/subagent/`: config (TOML, no built-in providers), providers (claude, codex, copilot, grok, gemini-experimental), router, health, guard (+context rules), loop (on the Registry), telemetry (trace schema 3 + `delegation` kind, cost at run end, sampler), report + HTML dashboard, cli (`init doctor detect run wait test review undo checkpoints watch report`), mcp_server (six tools, `lane=` alias), core (in-process server) |
| Skill | `skills/delegate/SKILL.md` calls `subagent`; `install.sh` links the skill and installs the package |
| Examples | `examples/config.{copilot,codex,glm,deepseek,llama.cpp,omlx,vllm,full}.toml`; all pass `subagent doctor --no-probe` |
| Bench | `bench/run.py` matrix (harness × config × task × mode), `bench/harness.py` parsers for claude/codex/gemini/grok/copilot, `bench/concurrency.py`, `bench/business_case.py` + `docs/business-case.md`, task `meeting-scribe` (41 hidden tests, validated reference) |
| Live | `doctor --provider glm --prompt` PASS; grok classifies its 402 as `grok_balance`; the autopilot loop on the cron task ended `done` (9 outline tests written by glm, 2 rounds, review refused on DeepSeek balance and reported as unverified); one bench cell with Claude orchestrating and glm workers: hidden 22/22, orchestrator $0.68, worker 70k in / 39k out / 1.0M cache-read tokens, counterfactual $1.84, wall 1065 s (`wiki/data/bench-2026-09-23/`) |
| Bench (codex) | Codex gpt-6-luna orchestrating the fused skill built cron and passed hidden 22/22 (4,988 s, 193k/41k/10.8M orchestrator tokens); worker accounting missing (U-B4, `data/bench-2026-09-24-codex/`) |
| Review | `review.md` 2026-09-23: 2 HIGH + 13 MED + 1 LOW; both HIGH and 12 MED fixed (F1a, F1b, M5, M6); 1 MED (parallel mode crash) and 1 LOW open with reasons |
| Servers | The MCP servers Claude Code currently runs are a uv tool install of the pre-fusion package (`~/.local/share/uv/tools/subagent-mcp`); this checkout carries an untracked shim at `src/subagent_mcp/runtime/approval_hook.py` so their children's hook still works. Switch `~/.claude.json` to `subagent-mcp` from this checkout with `SUBAGENT_CONFIG` when convenient |

## Next

1. Fix U-B4 (make the orchestrator prompt and SKILL.md steer to `subagent wait`; refuse a `--tier` whose provider is closed; find why the cell's session root got no trace), U-B1, U-L1. Then run one real bench cell per installed harness (`claude`, `codex`, `grok` when its balance is back) on `cron`, then `meeting-scribe` on a local provider, and look at `bench/results/<ts>/dashboard.html`.
2. Feed the measured rows into `bench/business_case.py --from-report` and rewrite the README's benchmark section with the fused numbers.
3. Retire the uv-tool-installed server: `uv tool install --editable .` from this checkout, `~/.claude.json` env `SUBAGENT_CONFIG=<file>`; then delete the shim.
4. Push `fuse` when Gary says so; open the PR against `main`.

## Open

- Gemini and Copilot drivers are fixture-tested only (no binaries on this Mac); the Copilot PreToolUse hook spike (P2.29) is DEFERRED to a machine with `copilot`.
- The loop reports `test_writer_error`/`backend_error` without the provider's own error text (finding U-L1); the run record has it.
- The guard's `agent` supervisor tier runs `claude -p`, so it fails closed when the coordinator's Claude quota is out (U-G1); `deterministic` is the safe CLI default.
- Hook path and env names were bound to the checkout the server started from (U-H1); the new package copies nothing yet, so restart the server after a layout change.
- Open review findings: parallel mode with `max_agents = 1` (MED), hidden-test destination created by the agent (LOW).
- Bench parsers: the claude parser fills `orch_cost_usd` but leaves `orch_tokens_*` empty (U-B1); Codex orchestrator cells need `-s danger-full-access` (M5) because its sandbox cannot bind the approval socket (U-C3), which also stops Codex children from running socket-backed tests or committing in a worktree (U-C1).
- Not yet measured: Gemini (not installed), Grok (balance exhausted), Copilot (not installed), and the two self-hosted providers (bppc down, oMLX without a loaded model) as workers; the matrix over harnesses × providers is the next run.

## v2 feature list (2026-09-24, agreed in conversation, not yet a goal file)

Khang's plan-tests-rounds-review loop stays as is. v2 adds a goal layer, a guard that enforces both, any worker, and the numbers.

Correctness first (small, gates every number)
1. U-B4: `run` refuses while a run is live; `wait` is what the skill says; trace lands under the cell's session root.
2. U-B1 (claude bench parser leaves orchestrator tokens empty), U-L1 (test_writer error text).
3. Review MED parallel-mode crash; LOW hidden-test destination.
4. Pricing ships inside the package (today `scripts/pricing.toml` via three parent dirs, absent in a wheel); drop `init --from-env` and the `SAM_*` names.

Setup and providers
5. `subagent provider add|list|remove|test`, `subagent use <name>`; presets from `examples/`; `--project` writes `.subagent/config.toml`.
6. `subagent init` detects installed CLIs (claude, codex, copilot, gemini, grok) and offers them as subscription providers with no keys.
7. `auth = "login"` on the claude driver: a Claude seat as worker; `examples/config.claude.toml`; flat-plan pricing.
8. Local discovery: `provider add local --url` probes `/v1/models`, oMLX `/api/status`, `/metrics`, `/slots` and fills model, health and probe.
9. Lean worker driver: in-package agent loop, four tools, sub-1k system prompt, OpenAI and Anthropic wire formats, guard in-process, stable prefix for local KV cache. Replaces `claude -p` for API-key and local providers; vendor CLIs stay for seats. omp is not merged.

Harness reach
10. `subagent install --for claude|codex|gemini|copilot`: skill or instruction paragraph, the `/plan` and `/goal` commands, MCP registration on every harness with an add command, per-server timeout set.
11. Natural triggering: skill description on task shape; `[loop] auto = ask|always|never`.

Outcomes
12. `/plan` and `/goal`: `GOAL.md` with UAT rows; command rows run as server-side verification, prose rows go to the reviewer; goal file is a protected path; `uat` in the delegation record and dashboard; doctor warns on a goal with no command rows.

Measurement (replaces the plain matrix)
13. Per-turn instrumentation: usage per turn tagged by source (harness prompt, plan, tool results, test output, model output).
14. Token knobs, each a config flag: test-output cap, compaction window, parallel tool calls, no-narration rule, per-provider `thinking = off|low`, test writer delegated with orchestrator review.
15. Task ladder: single function, cron, meeting-scribe, change inside an existing codebase, protocol/parser with a subtle spec. Models: bppc 27B, oMLX, GLM, DeepSeek, Codex Luna, Claude. Three runs per cell; knobs A/B on two tasks x three models, local and GLM first, winners confirmed on SOTA. Output: per model class, knob -> tokens saved and pass-rate change; `business_case.py --from-report`; README rewritten on the numbers.

Housekeeping
16. Retire the uv-tool pre-fusion server and the hook shim; `~/.claude.json` to `subagent-mcp` with `SUBAGENT_CONFIG`.
17. Rename before publishing (`subagent` taken on PyPI; repo name says "claude").
18. README: Khang's loop + goal layer + guard + any worker + measured cost; limitations (guard per driver, Windows unsupported); drop the pre-fusion benchmark section once 15 lands.

Not in v2: Windows, opencode driver, omp driver merge, any settings UI.

## Paused (2026-09-25)

Gary's decision: build his own lean coding-agent harness (a more token-efficient pi) first; `subagent` v2 waits for it. The spike that led here is at `wiki/data/spike-harness-2026-09-24.md` (verdict: wrap pi as a driver; build-own as fallback; two unverified points: prompt-prefix stability for local KV reuse, `enable_thinking` passthrough to oMLX/llama.cpp).

When this resumes: item 9 in the v2 list becomes "driver for Gary's harness" (the omp-driver branch's event translator is the starting point); everything else in the list stands. State at pause: `fuse` = `origin/main` = 84732bb plus four local wiki commits; the pre-fusion uv-tool server and the hook shim are still what `~/.claude.json` runs.
