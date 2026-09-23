# AGENTS.md

Read this before changing anything in this repository. It is written for a coding agent
(Claude Code, Codex, Gemini CLI, Copilot or similar) and for a person who has never seen
the code.

## What this repository is

`subagent` is a Python tool that lets an AI coding assistant (the "orchestrator") hand
implementation work to a cheaper AI coding process (the "worker") and verify the result
by running tests. The orchestrator writes a plan and a test outline; the worker implements
in rounds; the tool runs the tests, asks a reviewer of a different model family to check
the diff, and reports tokens and cost per provider.

A **provider** is one configured worker backend: a command-line coding agent (Claude Code,
Codex, Copilot, Grok, Gemini) pointed at a model endpoint (Anthropic, DeepSeek, z.ai GLM,
a local llama.cpp, vLLM or oMLX server). There are no built-in providers. Every provider
comes from the user's config file.

## Layout

| Path | What lives there |
|---|---|
| `src/subagent/config.py` | TOML config loading and layering, `Settings`, the child-process environment allowlist |
| `src/subagent/providers/` | One module per driver (`claude`, `codex`, `copilot`, `grok`, `gemini`); `base.py` holds the `Provider` protocol and `ProviderConfig` |
| `src/subagent/runs.py` | `Registry` and `Agent`: spawning a worker, collecting its events, verification, cost at run end |
| `src/subagent/router.py`, `health.py`, `lane_state.py` | Provider choice, endpoint health, throttle/closure memory |
| `src/subagent/guard/` | The PreToolUse guard: a deterministic classifier that allows or denies every worker tool call, plus an optional supervisor for the undecided ones |
| `src/subagent/loop/` | The delegation loop: plan, test outline, worker rounds, test lock, checkpoints, review |
| `src/subagent/telemetry/` | JSONL trace (schema 3), host/GPU sampler, pricing |
| `src/subagent/report.py` | `subagent report`: tables and a self-contained HTML dashboard |
| `src/subagent/cli.py`, `mcp_server.py` | The `subagent` command and the thin MCP wrapper `subagent-mcp` |
| `skills/delegate/SKILL.md` | The skill the orchestrator follows; it calls the CLI |
| `examples/config.*.toml` | One working config per provider kind; the only place machine-specific values are allowed |
| `bench/` | The benchmark matrix (orchestrator × config × task), the meeting-scribe task and the business-case model |
| `tests/` | pytest; `tests/loop/` are ported unittest suites; `tests/fakes/` are fake CLIs (`fake_claude`, `fake_copilot`, ...) |
| `docs/` | Reader-facing documents (benchmark, business case) |
| `wiki/` | Internal notes: decisions, run ledger, review findings, goal files. Not reader documentation |

## Commands

```sh
uv sync --group dev              # install with test tools (Python 3.11 or newer)
uv run pytest -q                 # the whole suite; must stay green (no network needed)
uv run ruff check src tests      # lint
uv run subagent doctor           # check the providers in the current config
uv run subagent report --html out.html
```

Config is read from `~/.config/subagent/config.toml`, then `<project>/.subagent/config.toml`
on top, then the file named by `SUBAGENT_CONFIG` on top of both. `subagent init --from glm`
writes a starter file from `examples/`.

## Rules

1. **No secrets in files.** A provider names its key with `api_key_env`; the tool reads the
   variable at run time. Never write a key value into a config, a test, a fixture or a log.
2. **No machine-specific values under `src/`.** Hostnames, LAN or Tailscale addresses, ports,
   home-directory paths and vendor key files belong in `examples/` and `wiki/` only.
   `grep -rn "100.106\|192.168\|Tailscale.app\|\.omlx/settings" src/` must print nothing.
3. **The guard denies by default.** Changes under `src/subagent/guard/` land with tests first.
   A worker must never be able to write its protected test files, the `.subagent/` state
   outside the allowlist, or anything under `refs/subagent/`.
4. **Workers never inherit the parent's credentials.** `Settings.child_env` is an allowlist.
   Add a variable there only with a reason in the commit message.
5. **The trace is a contract.** `tests/test_trace_schema.py` pins the record shapes. A new
   field is additive; a changed meaning bumps the schema version.
6. **Tests run offline.** Anything that needs a real CLI or endpoint lives in `scripts/`
   (smoke and lane harnesses) and is run by hand.
7. **Adding a provider** means: a module in `providers/` implementing the `Provider`
   protocol with a translator to Claude's stream-json event format, registration in
   `providers.for_driver`, an `examples/config.<name>.toml`, a fake CLI under `tests/fakes/`
   and tests on fixtures. Mark it `experimental = true` until it has run for real.
8. **Runtime state is not source.** `.subagent/`, `bench/results/` and session roots are
   ignored; do not commit them.
9. **Documents name one reader.** README and `docs/` are for users; decision history and
   run ledgers go to `wiki/`.

## Working in this repository as a worker

If you were started by `subagent` itself: the test files listed by `subagent detect` are
read-only for you. Request a test change by writing `.subagent/test_change_request.md`.
Write your report to `.subagent/result.json` before you run out of steps, even when the
verdict is FAIL.
