# Active work

Reader: the next session that picks this repo up. Read with `decisions.md` (S11–S24) and `review.md`.

## State (2026-09-28, branch `fuse` at the `lanes` merge, nothing pushed)

| Area | State |
|---|---|
| Repo | `fuse` carries the lanes run: U3 core (provider `effort`, parallel limit, node `ℹ` test counts), U1 omp driver, U2 antigravity driver, U4 lint backlog cleared, U5/U6 smoke script, U7 review fixes, U8 in-process socket path. 866 passed, 1 skipped; `ruff check src tests` clean |
| Live server | Claude Code and Codex launch `agent-capabilities/bin/subagent-mcp`, which sources `~/.config/agent-capabilities/subagent.env` (keys only; `SAM_*` lines commented out, backup `subagent.env.bak-2026-09-28`) and runs `~/.local/bin/subagent-mcp` = editable uv tool install of this checkout (`uv tool list`: `subagent`). Config: `~/.config/subagent/config.toml` (copy of `wiki/data/config-mcbob-2026-09-28.toml`) |
| Providers | `codex` (default; gpt-6-luna, effort xhigh), `glm`, `deepseek` (`claude -p`), `bppc` and `omlx` (omp 18.0.11, hook-guarded), `gemini` (agy 1.2.12, gemini-3.8-flash-high, effort high; experimental, `--sandbox`, no guard) |
| Live proof | `subagent doctor --prompt` and `scripts/smoke_lanes.py` PASS on all six with the installed config (see `log.md` 2026-09-28 close) |
| Old install | uv tool `subagent-mcp` (29ff214) is still installed and still running inside sessions opened before 17:51; its children's hook points at the untracked shim `src/subagent_mcp/`. Retire both after every session has restarted its MCP server |

## Next

1. After all sessions restart the `subagent` MCP server: delete the untracked directory `src/subagent_mcp/`, run `uv tool uninstall subagent-mcp`, then `uv tool install --editable . --force` again (the uninstall can remove the shared `subagent-mcp` link).
2. agy guard: find a deny-capable pre-tool hook format (binary has `PreToolHooks`/`HooksJson`); until then `gemini` stays experimental and sandboxed.
3. The v2 list below stands (paused items unchanged); U-B4, U-B1, U-L1 still open.
4. Push `fuse` when Gary says so.

## Open

- Gemini and Copilot drivers are fixture-tested only (no binaries on this Mac); the Copilot PreToolUse hook spike (P2.29) is DEFERRED to a machine with `copilot`.
- The loop reports `test_writer_error`/`backend_error` without the provider's own error text (finding U-L1); the run record has it.
- The guard's `agent` supervisor tier runs `claude -p`, so it fails closed when the coordinator's Claude quota is out (U-G1); `deterministic` is the safe CLI default.
- Hook path and env names were bound to the checkout the server started from (U-H1); the new package copies nothing yet, so restart the server after a layout change.
- Open review findings: an agent-created hidden-test destination (LOW); the accepted Antigravity guard limitation remains an explicit experimental-driver risk.
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
16. Retire the uv-tool pre-fusion server; set `SUBAGENT_CONFIG` in its MCP environment.
17. Rename before publishing (`subagent` taken on PyPI; repo name says "claude").
18. README: Khang's loop + goal layer + guard + any worker + measured cost; limitations (guard per driver, Windows unsupported); drop the pre-fusion benchmark section once 15 lands.

Not in v2: Windows, opencode driver, omp driver merge, any settings UI.

## Resumed (2026-09-30)

Gary lifted the pause below (`decisions.md` S25): the v2 list goes ahead, item 9 deferred with omp as the stand-in. Grounding and the lane check are in `grounded.md` (2026-09-30). The Plan Block was put to Gary on 2026-09-30; no goal file is written and no unit is dispatched until Gary answers it.

## Paused (2026-09-25)

Gary's decision: build his own lean coding-agent harness (a more token-efficient pi) first; `subagent` v2 waits for it. The spike that led here is at `wiki/data/spike-harness-2026-09-24.md` (verdict: wrap pi as a driver; build-own as fallback; two unverified points: prompt-prefix stability for local KV reuse, `enable_thinking` passthrough to oMLX/llama.cpp).

When this resumes: item 9 in the v2 list becomes "driver for Gary's harness" (the omp-driver branch's event translator is the starting point); everything else in the list stands. State at pause: `fuse` = `origin/main` = 84732bb plus four local wiki commits; the pre-fusion uv-tool server is still what `~/.claude.json` runs.

## Harness serve-mode contract (2026-09-25)

The rust-harness-core-design session (efficient-pi, branch `core`) mapped its serve mode onto our MCP contract: `design/contracts/subagent-mcp-mapping.md` (cf4fdf4), our answers beside it in `design/briefs/subagent-mcp-contract.md`. Verdicts sent on its 12 open points: accept 1, 2 (with the rule that `verification.command` is the caller's string or the approved substitute plus a `verification_note`), 5, 7 (`credits_unit` outside `usage`; turn records with timestamps, not ms), 8, 10, 11 (add `command` per uat row; the goal-block wire format is a recommendation pending Gary), 12 (we host `tests/test_harness_lane.py` and `tests/fakes/fake_agent.py`); change 3 (`rejected` only for provider selection, RegistryError for argument validation) and 6 (ints plus top-level `usage_unreported`, never null); reject 4 (`lane` stays an alias of `provider`; profile read-only under `providers[].extra`); transport 9: recommend `agent call ... --json --follow` JSONL on stdout so the driver is argv + translator.

When the repo resumes: "driver for the harness" replaces v2 item 9; its units are points 1, 2, 5, 6, 7, 12 plus `_wait_for` returning on `needs_human`.

## U1 omp driver (2026-09-28, branch `lanes-omp`)

Cherry-picked P1a `c9698e6` and P1b `f73563c` as `11903ba` and `b978783`; the Windows commit `7531144` is not included. Endpoint config is generated per agent in the session root, the `--hook` bridge calls the shared approval hook, and both example providers (`bppc`, `omlx`) are wired. Fake and real omp hook tests pass. Final suite: 824 passed, 1 skipped, 19 subtests; focused omp suite: 25 passed. The repository-wide Ruff command reports 168 findings in untouched files; changed Python files pass targeted Ruff. The live sampling proxy recorded no temperature/top-p/top-k/min-p/penalty/seed/max-token/reasoning-effort fields, but did record `preserve_thinking=true` in two fields. The oMLX `/api/status` endpoint returned 401 because `OMLX_API_KEY` is unset, so the live prompt produced zero tokens. See the 2026-09-28 `wiki/log.md` entry and `omp-report.md`.
