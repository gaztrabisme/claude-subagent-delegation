# Implementation brief — glm-subagent-mcp

Build a complete, installable Python MCP server in this repo. Empty workspace except `wiki/`. Follow `wiki/decisions.md`. Do not modify `~/.claude/settings.json`. Skip formatters as a last step if you want; the orchestrator will run pytest.

## Package

```
pyproject.toml
LICENSE                    # MIT
README.md
src/glm_subagent_mcp/__init__.py    # __version__ = "0.1.0"
src/glm_subagent_mcp/__main__.py    # from .server import main
src/glm_subagent_mcp/config.py
src/glm_subagent_mcp/guard.py
src/glm_subagent_mcp/verify.py
src/glm_subagent_mcp/trace.py
src/glm_subagent_mcp/supervisor.py
src/glm_subagent_mcp/runs.py
src/glm_subagent_mcp/server.py
src/glm_subagent_mcp/runtime/approval_hook.py
tests/test_guard.py
tests/test_verify.py
tests/test_runs.py
tests/test_config.py
tests/test_hook.py
.gitignore
```

`pyproject.toml`: name `glm-subagent-mcp`, python >=3.11, deps `mcp>=2.0.0` and `anyio` if not pulled in. Script `glm-subagent-mcp = "glm_subagent_mcp.server:main"`. hatchling, src layout, pytest + ruff in dependency-groups.dev. No `deepseek-harness-sdk`.

## Port from DSA (do not re-invent)

Fetch and adapt these files from https://github.com/gaztrabisme/deepseek-subagent-mcp (master). Keep behaviour, rename prefixes `DSA_` → `GSA_`, package `deepseek_subagent_mcp` → `glm_subagent_mcp`.

- `src/deepseek_subagent_mcp/guard.py` — port **entire file**. Add a Claude Code tool dispatcher on top of existing `classify(tool, payload, workspace)`:

  | tool_name | action |
  |---|---|
  | `Bash` | `classify_bash(tool_input.command)` |
  | `Write`, `Edit`, `NotebookEdit` | `classify_path_write(file_path or path)` |
  | `Read` | ALLOW unless `is_sensitive` → DENY |
  | `Glob`, `Grep`, `LS`, `TodoRead`, `TodoWrite` | ALLOW |
  | `WebFetch`, `WebSearch` | ESCALATE (network) |
  | `Agent`, `Task`, `Skill`, names starting `mcp__` | DENY (recursion / escape) |
  | unknown | ESCALATE |

  Accept both DSA names (`bash`/`write`/`edit`/`read`) and Claude Code names so DSA tests still pass.

- `verify.py` — port. `GSA_VERIFY_TIMEOUT`. Same `classify_verification` (only ALLOW runs).

- `trace.py` — port (stderr-never-stdout, swallow write errors).

- `supervisor.py` — port the unix-socket verdict server and the agent/sampling/elicitation/deterministic ladder. Env `GSA_SUPERVISOR`, `GSA_SUPERVISOR_CMD` default `claude -p --model sonnet`, `GSA_SUPERVISOR_TIMEOUT`, `GSA_APPROVAL_SOCKET`. Fail closed.

- `runtime/approval_hook.py` — same socket protocol, **but stdout must be Claude Code PreToolUse JSON**, not empty-allow:

  ```json
  {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"..."}}
  ```

  Deny: `"permissionDecision":"deny"`. Unreachable socket → deny JSON + exit 2. Always emit JSON. `--agent <id>` on argv. Timeout from `GSA_HOOK_TIMEOUT`.

- `tests/test_guard.py` — port as-is (imports updated). Add cases: `classify("Bash", {"command": "ls"}, ws)` ALLOW; `classify("Agent", {}, ws)` DENY; `classify("mcp__foo__bar", {}, ws)` DENY; `classify("Write", {"file_path": "out.txt"}, ws)` ALLOW.

## config.py (new)

`Settings.from_env()`, frozen dataclass. Knobs:

| env | default |
|---|---|
| `GLM_API_KEY` or `ZAI_API_KEY` or `ANTHROPIC_AUTH_TOKEN` | required at spawn time, not at import |
| `GSA_BASE_URL` | `https://api.z.ai/api/anthropic` |
| `GSA_MODEL` | `glm-5.3[1m]` |
| `GSA_FLASH_MODEL` | `glm-5.3-flash[1m]` |
| `GSA_WORKSPACE` | cwd |
| `GSA_SESSION_ROOT` | `<workspace>/.gsa-sessions` |
| `GSA_MAX_AGENTS` | 4 |
| `GSA_CLAUDE_BIN` | `claude` |
| `GSA_MAX_STEPS` | 40 (`--max-turns`) |
| `GSA_RUN_TIMEOUT` | 1800 |
| `GSA_IDLE_TIMEOUT` | 900 |
| `GSA_TURN_TOKEN_BUDGET` | unset |
| `GSA_LOOP_STRIKES` | 3 |
| `GSA_SUMMARY_TOKENS` | 2000 |
| `GSA_CHARS_PER_TOKEN` | 3.5 |
| `GSA_VERIFY_TIMEOUT` | 300 |
| `GSA_SUPERVISOR` | `auto` |
| `GSA_LOG_LEVEL` | `info` |
| `GSA_TRACE` | session_root/trace.jsonl or `off` |
| `GSA_RUN_ARCHIVE` | 200 |
| `GSA_TRANSCRIPT_LIMIT` | 400 |

`configure_logging` → stderr only.

`child_env(agent_id)` returns env for the `claude` subprocess:

- Copy `os.environ` then **pop** `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL` if we are about to set our own (do not leak parent OAuth).
- Set `CLAUDE_CONFIG_DIR`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_OPUS_MODEL`/`SONNET` = model, `HAIKU` = flash, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `API_TIMEOUT_MS=3000000`, `GSA_APPROVAL_SOCKET`, `GSA_HOOK_TIMEOUT`.

`write_child_settings(agent_id) -> Path`: write `<claude-home>/settings.json` with `hooks.PreToolUse` matcher `*` command = `sys.executable approval_hook.py --agent <id>`. Also write empty mcp if needed. Return the settings path for `--settings`.

Never write under the real `~/.claude`.

## runs.py (the new core)

One agent = one Claude Code session id + one worker thread + serial run queue. One run = one `claude -p` subprocess.

Pipeline (same as DSA): task turn → `run_verification` → distill if `len(final_response) > result_cap_chars`. Distill is a second `claude -p --resume` with the DSA seven-section prompt (copy `DISTIL_PROMPT` from DSA `runs.py`). `run.done` fires when all stages finish.

Spawn argv:

```
<claude_bin> -p <prompt>
  --output-format stream-json
  --verbose
  --bare
  --dangerously-skip-permissions
  --max-turns <max_steps>
  --model <model>
  --settings <settings.json>
```

If `workspace/CLAUDE.md` exists, add `--append-system-prompt-file <that>`.

cwd = agent workspace.

Continue: same argv plus `--resume <session_id>`.

Parse NDJSON stdout. Keep:

- `session_id` from `system` init or `result`
- assistant text / tool_use names into `run.transcript` (drop noisy stream_event deltas)
- `result.result` as `final_response`
- `result.is_error` → failed
- `result.usage` / `total_cost_usd` / `num_turns` into Usage
- identical tool fingerprints for loop strikes (tool_use name + hash of input)
- `num_turns` / tool_use count against `max_steps`

Kill: SIGTERM the process group (`start_new_session=True`). Timeout / loop / budget / steps = failure that outranks the child's result. Cancel = cancelled.

`wait_ready`: there is no handshake process. Creating the agent only mkdirs CLAUDE_CONFIG_DIR and writes settings. `wait_ready` returns None immediately (or after the write). Startup failure of `claude` (missing binary, missing key) is a **failed run** with `error_detail` from stderr, not a tool error on `glm_delegate` — unless the binary is missing, then `glm_delegate` raises `RegistryError` after a probe `claude --version` once per agent.

Missing `GLM_API_KEY` at spawn: fail the run with a clear error naming the env vars. Do not crash the MCP server at import.

Reaper: idle timeout closes agents (no process to kill if idle); archive terminal runs (`GSA_RUN_ARCHIVE`); `glm_await`/`glm_transcript` still work; `glm_continue` does not.

Fake Claude in unit tests: monkeypatch a function that records argv/env and yields a list of NDJSON dicts, then a result. Tests must not call the real `claude` or the network.

Required tests in `test_runs.py`:

- delegate with `verification="true"` and a fake one-line result → `completed`, result text verbatim (under cap, no distill).
- result over cap → distill prompt sent via `--resume`, distilled text returned, `raw=True` still has original.
- verification `false` command (use `true`/`false` binaries) → `completed_unverified`.
- fake process that repeats the same tool_use → killed as loop.
- cancel mid-run sets `cancelled`.
- child env has `ANTHROPIC_BASE_URL` = z.ai anthropic path, `CLAUDE_CONFIG_DIR` under session_root, and does **not** write `Path.home()/".claude"/"settings.json"`.
- argv contains `--bare` and `--dangerously-skip-permissions`.
- continue passes `--resume` with the captured session_id.

## server.py

Copy DSA `server.py` structure. Tools renamed `glm_*`. Server name `glm-subagent`. Instructions rewritten for Claude Code + GLM. `main()`: `configure_logging`, then `app.run()` (stdio). Lifespan starts supervisor socket.

`glm_delegate` requires `verification`. Workspace must be an existing directory.

## README.md

Install via `uvx --from git+… glm-subagent-mcp`. Claude Code `.mcp.json` example with `GLM_API_KEY` and `GSA_WORKSPACE`. Tool table. Isolation warning: never put GLM into `~/.claude/settings.json`. Link z.ai Claude Code docs for the endpoint. Env table. Limits: needs `claude` on PATH; `--bare` skips host MCP (intentional); cancel is SIGTERM.

## Constraints

- stdout of the MCP server is JSON-RPC only. All logs stderr.
- Do not add extra features (HTTP transport, Docker, a CLI configurator like claude-sub-proxy).
- Do not commit secrets.
- Unit tests: no network, no real `claude` binary required (probe can be mocked).
- File length: keep modules focused; `runs.py` will be large — that is fine if it matches DSA's.

## Done when

`uv sync && uv run pytest -q` exits 0 from this repo, covering guard + verify + runs + hook JSON shape + config isolation.
