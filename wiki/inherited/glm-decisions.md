# Decisions

## D1 — Product is an MCP that spawns Claude Code, not a proxy

**Chosen:** Six MCP tools (`glm_delegate` / `glm_await` / `glm_continue` / `glm_list` / `glm_cancel` / `glm_transcript`) wrapping `claude -p` with GLM env.

**Rejected — claude-sub-proxy shape.** That product reroutes Haiku/Sonnet API calls while the main model stays on Anthropic. This product is the inverse: the *parent* stays on its own auth; grunt work is a separate Claude Code process on the GLM subscription.

## D2 — Port the DSA product contract, swap the child runtime

Port from `gaztrabisme/deepseek-subagent-mcp`: required `verification`, distillation on overflow, four ceilings, PreToolUse guard + supervisor ladder, stderr-only logging, run archive after idle reap.

Do not depend on `deepseek-harness-sdk`. Child is the `claude` CLI (v2.1.252+ on this machine).

## D3 — Isolate GLM credentials; never touch `~/.claude/settings.json`

This Mac's `~/.claude/settings.json` has no `ANTHROPIC_BASE_URL`. Parent is on Anthropic OAuth. Writing GLM into the global settings would steal the parent's traffic — the failure mode `claude-sub-proxy claude install` exists to cause, on purpose.

Child spawn:

- `CLAUDE_CONFIG_DIR=<session_root>/agents/<id>/claude-home`
- `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`
- `ANTHROPIC_AUTH_TOKEN` from `GLM_API_KEY` / `ZAI_API_KEY` / `ANTHROPIC_AUTH_TOKEN`
- ~~`--bare`~~ removed by D9: `--tools`, `--strict-mcp-config`, `--setting-sources ""`
- `--settings` JSON we write (hooks + model env)
- `--dangerously-skip-permissions` (Gary, 2026-09-02)
- `--output-format stream-json --verbose`
- `--max-turns` = `GSA_MAX_STEPS` (model turns; see D10)
- `--model` default `glm-5.3[1m]`
- If `workspace/CLAUDE.md` exists: `--append-system-prompt-file` that path

The recursion kill is `--strict-mcp-config` with no `--mcp-config` (D9): a project `.mcp.json` that mounts this server must not be inherited by the child.

## D4 — Continue via `--resume session_id`, not a long-lived process

Each turn is a new `claude -p` process. `session_id` comes from the stream-json `system/init` / `result` event. `glm_continue` passes `--resume`. Cancel = SIGTERM the in-flight process (Claude Code documents exit 143; resume of a SIGTERM'd session is allowed — we still treat cancel as ending the agent, same as DSA D5, unless a later measurement says otherwise).

No process is held between turns. `GSA_MAX_AGENTS` bounds concurrent in-flight processes.

## D5 — Claude Code PreToolUse JSON decision, not DSA exit-2-only

Hook stdin is Claude Code's PreToolUse payload (`tool_name`, `tool_input`, `cwd`).

Stdout:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "write inside the workspace"
  }
}
```

Deny uses `"deny"`. Unreachable supervisor → deny (fail closed). Exit 0 with empty stdout is **not** a decision and would allow under bypassPermissions — the hook must always emit JSON.

Unix socket + `supervisor.py` ladder is the same as DSA (agent → sampling → elicitation → deterministic deny). Prefix env `GSA_*` / `GSA_APPROVAL_SOCKET`.

## D6 — Default model `glm-5.3[1m]`

Gary, 2026-09-02. Flash is `glm-5.3-flash[1m]`, via `glm_delegate(model=...)` or `GSA_MODEL`. Haiku-slot in child settings maps to Flash; Sonnet/Opus slots map to `glm-5.3[1m]`.

## D7 — Python package, MCP 2.x, no Node

Same distribution story as DSA minus the harness wheel. `uvx --from git+… glm-subagent-mcp`. Requires `claude` on PATH.

## D8 — Nested Agent/Task/Skill/MCP tools denied

A child that spawns Agent or talks to MCP can recurse or escape the guard. Classifier denies `Agent`, `Task`, `Skill`, and any `mcp__*` tool name.

## D9 — No `--bare`; the guard hook runs on every child tool call

2026-09-18, report item 0 (`lab/subagent-eda/report.md`). `--bare` skips hooks, so the PreToolUse hook installed through `--settings` never fired: every GLM child call ran unchecked, and the trace held no verdict records. The coordinator's probe (`eda/bare_probe/run.sh`) showed a deny-all hook skipped under `--bare` and firing without it.

The child now launches without `--bare`, and the isolation it gave is explicit:

- `--tools Bash,Read,Edit,Write,Grep,Glob`. Bash, Read and Edit are what `--bare` left; Write, Grep and Glob are file tools the guard already classifies.
- `--strict-mcp-config` and no `--mcp-config`: no MCP server loads (the recursion kill).
- `--setting-sources ""`: only the per-agent `--settings` file applies. Checked on Claude Code 2.1.276: a workspace `.claude/settings.json` setting `model` is ignored with `""` and applied with `project`. Without this, a child could write its own hooks into the workspace and have the next run load them.
- `CLAUDE_CONFIG_DIR` stays per-agent, so user settings, plugins, keychain entries and memory are the agent's own.
- `CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1`: every Bash call starts in the workspace. The guard resolves relative paths from the workspace (or from a `cd` earlier in the same line); a cwd carried over from an earlier call's `cd` would make `../x` point somewhere the guard did not check.
- `--dangerously-skip-permissions` stays. The hook is the gate.

Before the hook went on, the guard rules the report ranked (items 1, 2, 4, 5, 7) landed, and `GSA_LOOP_STRIKES` rose from 3 to 8, so routine child calls are not all escalated. `scripts/smoke_guard.py` is the live check: it delegates `cat ~/.ssh/config` through a fresh server and passes only on a deny verdict in the scratch trace. First run, 2026-09-18, z.ai `glm-5.3-flash`: passed, tier `policy`.

Verified without paid calls by `scripts/hook_isolation_check.py` (2026-09-18, Claude Code 2.1.276): against a local mock Anthropic endpoint, a workspace `.claude/settings.json` and `.claude/settings.local.json` with `disableAllHooks: true` and a Bash allow rule do not stop the hook under this argv (the hook fired and denied `echo hi > x`, and `x` was not written). The control run with `--setting-sources project,local` loads the same files, the hook does not fire and `x` is written, so the planted files are live.

Not verified: whether Claude Code without `--bare` auto-loads `CLAUDE.md` from the workspace's parent directories when `--setting-sources` is empty. The workspace's own `CLAUDE.md` is still passed with `--append-system-prompt-file`.

## D10 — `GSA_MAX_STEPS` counts model turns; trips act while the stream is read

2026-09-18, report item 6. The server counted tool calls against `GSA_MAX_STEPS` while Claude Code stopped at `--max-turns` model turns. A child that made parallel tool calls could finish under the turn cap and over the tool-call count; the server then relabelled it `failed` and skipped verification. 31 of 78 GLM runs labelled "step ceiling" had finished on their own.

- `GSA_MAX_STEPS` is only `--max-turns`. The tool-call step trip is gone. `usage.steps` still counts tool calls; `usage.turns` records Claude Code's `num_turns`.
- Claude Code's max-turns result (`subtype: error_max_turns`, `terminal_reason: max_turns`, then exit 1) is `failed` with `finish_reason: steps`, and verification is skipped.
- The loop detector and the token budget are decided per event inside `_collect`, so `proc.kill()` fires mid-run. Before, `_turn` ingested only after the stream ended and the kill in the read loop could never run. The live budget sums per-response usage from assistant events (deduplicated by message id); Claude Code streams `output_tokens: 0` there, so it undercounts output, and the result event's usage stays authoritative.

## D11 — Guard rules after the adversarial review

2026-09-18, `lab/subagent-eda/review-glm-raw.md`. The rules D9 turned on had holes the review found by running 384 commands through both branches. The rules now read arguments, not only the program name.

- **Unresolvable paths.** `$VAR`, `~+` (`$PWD`), `~-` (`$OLDPWD`) and `~name` for an unknown user cannot be resolved statically. `$HOME` and `${HOME}` are expanded. A tilde form is denied in sensitive-path and redirect checks and in deletes, and escalates in `cd`. Any other unexpanded path escalates, and a delete of one is denied.
- **Values are paths too.** The sensitive check reads `--opt=value`, a glued `-fVALUE`, `NAME=value` and curl's `@file`, and compares case-folded, because APFS is case-insensitive. `~/.claude`, `~/.glm-subagent`, `~/Library/Keychains`, shell histories and `.env.*` are sensitive. `printenv`, bare `env` and any `$ANTHROPIC_AUTH_TOKEN`-style reference are denied. `GLM_API_KEY` and `ZAI_API_KEY` are no longer passed to the child. `ANTHROPIC_AUTH_TOKEN` is, because Claude Code needs it, so a script the child writes can still read it.
- **Environment prefixes are an allowlist** (`SAFE_ENV`: `CI`, `NO_COLOR`, locale, colour and a few Python/Rust/Node display variables), plus single-letter names, which no tool reads. A denylist of dangerous variables was always one name short: `PYTHONPATH`, `PYTEST_ADDOPTS`, `RUSTC_WRAPPER`, `GOFLAGS`, `MAKEFLAGS`, `npm_config_*`, `XDG_CONFIG_HOME`. The single-letter rule exists for `export A=1 && pytest -q` in the UAT.
- **Interpreters.** A program from stdin, a here-string or a redirect escalates, and so does an inline-code letter inside a cluster (`-ic`, `-lc`, `-pe`, `-E`). Any option before the script that is not in a per-interpreter no-value table escalates, so `-X importtime /tmp/evil` cannot be mistaken for a script. The script must be an existing file inside the workspace. `python -m` runs only test modules.
- **Runners.** `pytest`, `tox`, `make`, `cargo`, `go`, and the `uv run`/`npm`/`pnpm`/`npx`/`gradlew` forms allow flags, test selectors, and paths inside the workspace, checked both textually and after following symlinks. They escalate on options that load code or config (`RUNNER_OPTIONS`), on variable assignments, on cargo or go subcommands other than test/check/build/vet, and on make install/publish targets.
- **Read-only verbs and git.** Short-flag clusters are parsed (`sort -uo`, `tree -o/x`). `sort --compress-program`, `man` with any option, and git global options escalate, and so do `--output`, `--ext-diff` and `--contents`. `config`, `stash`, `branch` and `remote` pass only in their listing forms.
- **`cd`** must land inside the workspace both after bash removes `..` textually and after following symlinks.
- **Protected write targets.** `.git/hooks/*`, `.git/config`, and every root passed to `guard.protect` are refused for file tools, redirects and write commands. The Registry protects its session root, which now defaults to `~/.glm-subagent/sessions`, outside every workspace. It holds the `--settings` file that installs the hook. `create_agent` refuses a workspace that contains the approval hook or `sys.prefix`, because that would make the guard's own code writable.
- **Runs.** Max-turns is detected during the attempt, so it cannot be retried as a rate limit. A backoff that would outlast the run deadline is skipped. `GSA_RATE_LIMIT_RETRIES=0` is accepted. z.ai codes are read only from stderr, the event's `error`, or an `API Error` result. Loop strikes are counted per agent across continues and reset after a loop trip.

Deliberately allowed, and different from `main`: `cd sub && echo hi > ../pwn`. `main` denied it by resolving `../pwn` from the workspace. It writes `<workspace>/pwn`, and this is the `cd` semantics D9's rules set out. Left open by the review's own call: N6 (`cd sub && bash ../t.sh`) and N7 (heredoc bodies escalate).

## Open

- Live GLM key is not in this process env. Unit tests must not need the network. Smoke against z.ai is a later gate.
- Whether SIGTERM'd Claude Code sessions should be resumable via `glm_continue` is unverified. v1: cancel ends the agent.
