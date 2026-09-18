# subagent-mcp wiki

Reader: someone picking this server up cold.

subagent-mcp is an MCP server that hands a coding task to a separate agent process (a "child") on one of five lanes and falls back to the next lane when one refuses. The parent (a Claude Code session) keeps its own login; the child runs on another provider's account or on a local model, so the work does not spend the parent's Claude plan.

| Lane | Child | Endpoint |
|---|---|---|
| codex | `codex exec --json`, workspace-write sandbox plus guard hook | ChatGPT-account Codex |
| deepseek | `claude -p` | `api.deepseek.com/anthropic`, deepseek-v4-pro |
| glm | `claude -p` | `api.z.ai/api/anthropic`, glm-5.3-flash[1m] |
| bppc | `claude -p` | Qwen3.8-27B on the RTX 5080 box, llama.cpp behind a proxy on :8080 |
| omlx | `claude -p` | Qwen3.6-35B-A3B REAP on this Mac's oMLX, :8000 |

Every child tool call passes a guard (a classifier plus a reviewer agent) before it runs. Every run, hop and model turn is written to `~/.subagent-mcp/sessions/trace.jsonl`; local lanes also write resource samples to `metrics.jsonl`.

## Pages

- `decisions.md` — what was chosen and rejected (S1–S10); inherited choices in `inherited/glm-decisions.md`.
- `active-work.md` — current state, what is next, what is open.
- `log.md` — dated record of runs, with ledgers.
- `review.md` — adversarial review and live-test findings, with outcomes.
- `goals/` — goal files with their acceptance checks.
- `grounded.md` — what was read and probed before the build.
- `data/` — benchmark CSVs and the oMLX request log.
- `inherited/` — glm-subagent's wiki as it was at 96933a2.
