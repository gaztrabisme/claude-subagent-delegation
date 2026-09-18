# Active work

Handoff state for a fresh session. Read with `wiki/decisions.md` and `wiki/log.md`.

## State (2026-09-18)

glm-subagent-mcp: an MCP server that runs `claude -p` against the GLM Coding Plan (z.ai) as a supervised child. It also serves the local Qwen lane: `~/.local/bin/qwen-subagent-mcp` runs this server with `GSA_BASE_URL` pointed at the bppc llama.cpp proxy.

| Area | State |
|---|---|
| Six MCP tools over stdio | live, installed at user scope as `glm-subagent` and `qwen-subagent` |
| GLM key | set in `~/.claude.json` env; live since 2026-09-02 |
| Trace | schema 2: run records carry the Claude Code `session_id` (`417e74e`) |
| Rate limits | retried with backoff (`a03d13e`); the label is taken from the event's error fields and carries the real 429 text, such as code 1313 (`40fcf34`, `ad78e6b`) |
| Guard | refuses `pkill`/`killall` by bare pattern (`1fc7608`) |
| Instructions | `GSA_INSTRUCTIONS` overlay and `GSA_COMPACT_WINDOW` (`2c74c49`); every delegation requires a parked await (`c1981ca`) |
| Guard | quote-aware splitter, `cd` inside the workspace, test and runner forms, env allowlist, review findings closed (`684afba`, `01bcba4`); hook on, `--bare` removed (`0aec307`) |
| Limits | `GSA_MAX_STEPS` = `--max-turns`; loop and token trips live; loop strikes 8, per agent (`1dffd1c`, `fab4437`) |
| Unit tests | 231 passing |

Usage to 2026-09-18: 569 runs (411 on `glm-5.3-flash[1m]`), 52% completed. Full analysis: `~/Documents/Work/lab/subagent-eda/report.md`.

## Next

1. Restart the MCP processes so the guard hook (no `--bare`), the new guard rules, turn-based `--max-turns`, continue-verification reuse and z.ai code handling reach the live server. After the restart, children's tool calls go through the guard. Escalations go to the agent reviewer, whose median review on DeepSeek took 6.9 s.
2. Watch the first real runs' verdict records (`kind: verdict` in the trace) for new false positives, and adjust the rules.
3. `scripts/smoke_guard.py` (live, one z.ai call) and `scripts/hook_isolation_check.py` (mock endpoint, no paid call) are the checks to rerun after any change to argv or hooks.

## Open

- A session ended by `glm_cancel` is not resumed (D4). Whether Claude Code could resume it is unverified.
- Not pushed: local commits ahead of `origin/main` (`git status -sb`).
