# glm-subagent-mcp

MCP server that delegates work to a Claude Code subprocess billed against a [GLM Coding Plan](https://docs.z.ai/devpack/tool/claude).

Parent agent keeps its own auth. The child `claude -p` process is isolated (its own `CLAUDE_CONFIG_DIR`, `--strict-mcp-config`, `--setting-sources ""`) so it cannot steal the parent's Anthropic OAuth or recurse into this MCP server.

## Install

```sh
uvx --from git+https://github.com/gaztrabisme/glm-subagent-mcp glm-subagent-mcp
```

### Claude Code

```json
{
  "mcpServers": {
    "glm-subagent": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/gaztrabisme/glm-subagent-mcp",
        "glm-subagent-mcp"
      ],
      "env": {
        "GLM_API_KEY": "your-z.ai-key",
        "GSA_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

Requires `claude` on PATH (Claude Code CLI). Do **not** put `ANTHROPIC_BASE_URL` into `~/.claude/settings.json` — that would reroute the parent. This server injects GLM credentials only into the child process.

## Tools

| Tool | What it does |
|---|---|
| `glm_delegate` | Start a new Claude Code subagent on a task. Returns `agent_id` and `run_id` immediately. |
| `glm_await` | Block until a run finishes; returns the result. |
| `glm_continue` | Follow-up work in the same Claude Code session (`--resume`). |
| `glm_list` | Every agent this server owns, with state, cost, and run history. |
| `glm_cancel` | SIGTERM the in-flight process and close the agent. |
| `glm_transcript` | Activity log — tool calls, messages, raw response. |

Typical loop: `glm_delegate` → `glm_await` → (`glm_continue`) → `glm_cancel`.

Every delegation needs a `verification` command. The server runs it after the child finishes. Exit 0 → `completed`; otherwise `completed_unverified`. Pass `"true"` if there is nothing to check.

## Isolation

- Child env: `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`, `ANTHROPIC_AUTH_TOKEN=$GLM_API_KEY`
- `CLAUDE_CONFIG_DIR` under `~/.glm-subagent/sessions/agents/<id>/claude-home` (`GSA_SESSION_ROOT`); the guard refuses writes there, and a workspace that contains this server's hook script or Python is refused
- `--strict-mcp-config` with no `--mcp-config`, so no MCP server is loaded (recursion kill)
- `--setting-sources ""`, so only the per-agent file passed with `--settings` applies; a workspace `.claude/settings.json` does not
- `--tools Bash,Read,Edit,Write,Grep,Glob`
- No `--bare`: it skips hooks, and the PreToolUse hook is the guard
- `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1`, so every Bash call starts in the workspace the guard resolves paths against
- `--dangerously-skip-permissions` plus a PreToolUse guard (same policy as [deepseek-subagent-mcp](https://github.com/gaztrabisme/deepseek-subagent-mcp))
- Default model: `glm-5.3[1m]` (`GSA_MODEL` / `glm_delegate(model=...)`)

## Configuration

Every setting is an environment variable on the server process. See `wiki/implementation-brief.md` for the full table. Common ones:

| Variable | Default | Meaning |
|---|---|---|
| `GLM_API_KEY` | — | Z.ai API key (also `ZAI_API_KEY` or `ANTHROPIC_AUTH_TOKEN`) |
| `GSA_BASE_URL` | `https://api.z.ai/api/anthropic` | Anthropic-compatible GLM endpoint |
| `GSA_MODEL` | `glm-5.3[1m]` | Model for delegated work |
| `GSA_FLASH_MODEL` | `glm-5.3-flash[1m]` | Haiku-slot mapping |
| `GSA_WORKSPACE` | server cwd | Directory the child reads and writes |
| `GSA_MAX_AGENTS` | `4` | Concurrent in-flight children |
| `GSA_MAX_STEPS` | `40` | Model turns, passed as `--max-turns`; a run that hits it is `failed`/`steps` |
| `GSA_LOOP_STRIKES` | `8` | Identical tool calls, counted across one agent's runs, before the run is killed |
| `GSA_RATE_LIMIT_RETRIES` | `3` | Retries after a transient 429/529; `0` turns retries off |
| `GSA_RATE_LIMIT_BACKOFF` | `5` | Seconds before the first retry, doubled per attempt, capped at 300 |
| `GSA_THROTTLE_BACKOFF` | `60` | Same, for z.ai fair-use throttling (code 1313), capped at 900. Plan quota (1308/1310) is never retried |
| `GSA_RUN_TIMEOUT` | `1800` | Seconds before a run is killed |
| `GSA_SUPERVISOR` | `auto` | `auto` / `agent` / `sampling` / `elicitation` / `off` |

## Limits

- `claude` must be on PATH. This server does not bundle Claude Code.
- The workspace `CLAUDE.md`, if it exists, is passed via `--append-system-prompt-file`.
- `scripts/smoke_guard.py` checks against a live backend that the guard denies a child's `cat ~/.ssh/config`.
- `scripts/hook_isolation_check.py` checks, against a local mock endpoint and with no paid calls, that a workspace `.claude/settings.json` with `disableAllHooks` does not switch the guard off.
- Cancel is SIGTERM. Edits already written stay on disk.
- A GLM Coding Plan key is required at spawn time, not at import.

## Development

```sh
uv sync
uv run pytest -q
```
