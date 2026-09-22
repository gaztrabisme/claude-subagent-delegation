---
name: delegate
description: Delegate implementation work to a cheaper coding agent to save Claude tokens. Claude writes the plan and the tests, the worker implements on a configured provider (Codex, Copilot, GLM, DeepSeek, Grok, Gemini, or a local model), and Claude verifies by running the tests. Use when the user says /delegate or asks to hand implementation off to a subagent.
---

# Delegate implementation to a worker agent

Your job is to be the **planner and verifier**, not the implementer. Every token you spend reading
implementation code defeats the purpose of this skill.

`DELEGATE` below means `subagent` (or `python3 -m subagent.cli` when the console script is not
on PATH), run from the project root.
Every command prints one JSON object.

**Shell-safe commands** (the user's shell may be bash, zsh or fish): run each `DELEGATE` command as
a plain command with literal arguments. No `$(...)`, no `VAR=value cmd` prefixes, no heredocs.
**Feedback always goes through a file**: write it with your file-writing tool to
`.subagent/feedback.md` (overwrite each time), then pass `--feedback-file .subagent/feedback.md`.
Never put test output or other free text in a quoted command-line argument.

## Workflow

0. **Size check** (before any plan or tests). Delegation has a fixed overhead and only pays off
   for larger work (benchmarks: delegating ~60-200-line tasks cost 10-40% MORE, ~1000 lines saved 69%).
   Estimate from the task; don't read the codebase just to estimate.
   - **Do it yourself** (skip this skill) if ANY of: under ~200 lines; 1-2 files; a bug fix,
     rename, config or wiring change; plan + tests would be as long as the code.
   - **Delegate** if ANY of: ~200+ lines; 3+ files or new modules; non-trivial algorithms
     (parsers, state machines, graphs, protocols); likely several debug iterations.
   - **Tier** (maps to a provider and model via the config's `[loop.tiers]` table): `hard` for
     parsers, interpreters, graph/scheduling algorithms, concurrency, tricky state, performance,
     security, or specs with many subtle interacting rules; otherwise `normal`. When unsure, pick
     `normal`.
   - **Parallel?** Only if the work splits into 2+ parts with disjoint files and interfaces you can
     pin down in the plan (e.g. tokenizer / parser / evaluator). Otherwise one worker.
   - Tell the user in one line, e.g. `Size check: ~900 lines, 5 modules → delegating (tier: hard, 2 parallel workers).`
   - Overrides: `force` in the request always delegates; "do it yourself" never does.

1. **Detect**: `DELEGATE detect` (test command, protected test files, git). If detection is wrong,
   set `test_cmd` / `test_globs` in the project's `.subagent/config.toml` (`[loop]` table). Install
   the test framework first if needed (e.g. `npm install`).
2. **Plan**: write `.subagent/PLAN.md`: goal, files, public interfaces (signatures, types),
   behavior, edge cases, constraints. Be concrete: the worker cannot ask questions.
   For parallel work also write one plan per part plus `.subagent/parallel.json`:
   ```json
   {"tasks": [
     {"name": "parser", "plan": ".subagent/plans/parser.md", "files": ["src/parser.js", "src/tokenizer.js"], "tier": "hard",
      "test_cmd": "node --test test/parser.test.js"},
     {"name": "eval", "plan": ".subagent/plans/eval.md", "files": ["src/eval/**"]}
   ]}
   ```
   Each part's plan must include the shared interfaces it uses. `files` are the globs that part may
   change; anything else it changes is discarded. `test_cmd` (optional) runs only that part's tests.
3. **Tests**: they are the contract, and you own them. By default write a **test outline**, not test
   code: a test writer turns it into test files, and a reviewer checks them against the spec.
   Write `.subagent/TESTS.md`: one `## <test file path>` heading per file, one line per case with the
   exact input and expected result:
   ```markdown
   # Test outline
   ## test/sheet.test.js
   - empty cell: new Sheet().get("A1") -> null
   - formula: set A1="2", B1="=A1*3" -> get("B1") === 6
   - invalid address: get("A0") -> throws Error
   ```
   Cover each requirement and edge case once. Mention the framework only if it isn't the project's
   usual one. For parallel work, give each part its own test file(s) importing only that part.
   Write the test code yourself only when cases need setup too complex for one line; then run
   `DELEGATE detect` again and check your files are under `protected_files`.
4. **Delegate on autopilot** (one command does all rounds, retries and the review):
   - `DELEGATE run --plan .subagent/PLAN.md --test-outline .subagent/TESTS.md --tier <tier> --auto --wait 540`
     (parallel: `--parallel .subagent/parallel.json` instead of `--plan`; without an outline, omit
     `--test-outline`)
     with the Bash tool timeout set to 600000.
   - If it returns `"status": "running"`, call `DELEGATE wait --timeout 540` (same Bash timeout) until
     it returns something else. Never start another run while one is running.
   - Before the first round, the test writer turns your outline into test files (test files only;
     it gets one fix pass if the review finds transcription errors), and a model of another family
     checks the tests against the plan/spec (wrong expectations block the run; missing tests are only
     reported). Then the runner handles the loop itself:
     it re-runs the tests after each round, sends failing output
     back to the worker, moves to the hard tier after 2 failed rounds, and when the tests pass has a
     model of another family review the diff and sends high-severity issues back too (limits:
     `auto_max_rounds`, `auto_review_cycles`). You only get the final result.
   - On the first delegation, tell the user once that they can watch live with
     `DELEGATE watch` in another terminal (or `live_view = "auto"` in the config). Mention
     `live_view_error` once if present.
5. **Act on the final `status`** (`rounds` lists each round; `tests`, `review` and `changed_files`
   describe the end state):
   - `done`: tests pass and the review found no high-severity issue. Go to step 6.
   - `tests_questioned`: the test review found WRONG tests (expected values that contradict the
     spec); no worker round ran. Check each `wrong_test` issue in `test_review.issues` against the spec.
     Where the reviewer is right, fix the outline line (outline mode: the tests are rewritten from it)
     or the test; where it's wrong, leave it. Then run the same command again. The test review only
     repeats if the tests changed, so a rerun after disagreeing goes straight to the worker.
   - `bad_outline` / `test_writer_error` / `no_tests_written`: fix the outline (headings, one case per
     line) or the setup (`log_tail`), then run again.
   - `needs_test_change`: read `test_change_request`. As the test owner, either edit the outline line
     or the test (feedback: "Approved: <change>. Continue.") or keep it (feedback: "Rejected: the test is correct
     because <reason>."), then run again with `--auto --continue --feedback-file .subagent/feedback.md`.
   - `review_concerns`: high-severity review issues remain after the allowed rounds. Report them and
     ask the user whether to run another autopilot round (feedback file with the issues, `--continue`).
   - `tests_failed` / `violated_tests` / `failed` / `timeout` (with `stopped_because`, e.g.
     `max_rounds`): the autopilot is stuck. Tell the user briefly what fails (from `tests.output_tail`)
     and ask whether to improve the plan and run again, or have you fix it directly. Don't loop.
   - `backend_error` / `runner_error` / `crashed`: read `log_tail` / `error`, fix the setup, retry.
   - Parallel runs add `parallel_tasks`: `out_of_scope_files` were discarded (if a part needed them,
     e.g. a dependency in package.json, make that change yourself); `conflicts` mean two parts changed
     the same file. Failures after the merge are fixed by the autopilot with a combined plan.
   - **Undo**: `DELEGATE undo` restores the tree to before the last worker round; `first_checkpoint`
     restores everything (`DELEGATE undo --to <id>`).
6. **Report**: status, summary, changed files, number of rounds, review verdict (mention medium
   issues and `review.unverified` if present), medium `test_review` issues (e.g. missing tests), and
   `worker_credits` (the worker's credits or cost, including reviews).

Manual mode (only if the user asks to drive rounds themselves): omit `--auto`; each round then
returns on its own and you retry with `--continue --feedback-file`, and run `DELEGATE review
--tier <tier>` yourself once the tests pass.

## Token discipline

- Do NOT read implementation files, `git diff`, or `.subagent/logs/`. Use the JSON (`summary`,
  `changed_files`, `tests`, review `issues`).
- Exceptions: setup errors (`log_tail`), or the user explicitly asks you to review the code.
- Pass failing test output as feedback verbatim; only diagnose it yourself after repeated failures
  on the same error.

## Configuration (TOML)

Config files are layered, each overriding the one before: `~/.config/subagent/config.toml`
(all projects), then `<project>/.subagent/config.toml`, then the file `$SUBAGENT_CONFIG` names.
Run `DELEGATE init` to write a starter config from the examples in `examples/` (it prints the
`export NAME=...` lines for any key that is not yet in the environment), and `DELEGATE doctor`
to check each configured provider against this machine.

A provider is one backend a worker can run on. The tiers name a provider (and optionally a model
on it), so you can remap tiers without touching the skill:

```toml
[core]
default_provider = "glm"

[providers.glm]
driver = "claude"
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.3-flash[1m]"
api_key_env = "GLM_API_KEY"

[providers.codex]
driver = "codex"

[loop.tiers.normal]
provider = "glm"

[loop.tiers.hard]
provider = "codex"
model = "gpt-5.6-sol"
```

- `driver` is one of `claude` (any Anthropic-compatible endpoint: DeepSeek, GLM, llama.cpp, vLLM,
  oMLX), `codex`, `copilot`, `grok`, or `gemini`. `claude` providers need `base_url`, `model` and
  `api_key_env`; the CLI drivers (`codex`, `copilot`, `grok`, `gemini`) own their own login.
  `local = true` marks a provider as local (used by `--fallback local`).
- `[loop.tiers.normal]` / `[loop.tiers.hard]` map a tier to a `provider` and optional `model`;
  `[loop.review]` and `[loop.test_writer]` name the providers for the two reviews and the test
  writer.
- Loop knobs that stay the same: `auto_max_rounds` (4), `auto_review` (true), `auto_review_cycles`
  (2), `review_tests` (true), `keep_checkpoints` (20), `live_view`, and `fallback` (`full` | `local`
  | `none`); `[fallback].chain` lists the order a run tries providers when one refuses before doing
  work.
