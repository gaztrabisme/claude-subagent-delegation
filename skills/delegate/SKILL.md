---
name: delegate
description: Delegate implementation work to a cheaper coding agent (GitHub Copilot CLI by default) to save Claude tokens. Claude writes the plan and the tests, the worker implements, Claude verifies by running the tests. Use when the user says /delegate or asks to hand implementation off to Copilot.
---

# Delegate implementation to a worker agent

Your job is to be the **planner and verifier**, not the implementer. Every token you spend reading
implementation code defeats the purpose of this skill.

`DELEGATE` below means `python3 <skill base directory>/delegate.py`, run from the project root.
Every command prints one JSON object.

**Shell-safe commands** (the user's shell may be bash, zsh or fish): run each `DELEGATE` command as
a plain command with literal arguments. No `$(...)`, no `VAR=value cmd` prefixes, no heredocs.
**Feedback always goes through a file**: write it with your file-writing tool to
`.delegate/feedback.md` (overwrite each time), then pass `--feedback-file .delegate/feedback.md`.
Never put test output or other free text in a quoted command-line argument.

## Workflow

0. **Size check** (before any plan or tests). Delegation has a fixed overhead and only pays off
   for larger work (benchmarks: delegating ~60-200-line tasks cost 10-40% MORE, ~1000 lines saved 69%).
   Estimate from the task; don't read the codebase just to estimate.
   - **Do it yourself** (skip this skill) if ANY of: under ~200 lines; 1-2 files; a bug fix,
     rename, config or wiring change; plan + tests would be as long as the code.
   - **Delegate** if ANY of: ~200+ lines; 3+ files or new modules; non-trivial algorithms
     (parsers, state machines, graphs, protocols); likely several debug iterations.
   - **Tier** (maps to a model via config `models`): `hard` (claude-opus-5) for parsers,
     interpreters, graph/scheduling algorithms, concurrency, tricky state, performance, security,
     or specs with many subtle interacting rules; otherwise `normal` (claude-sonnet-5). When unsure,
     pick `normal`.
   - **Parallel?** Only if the work splits into 2+ parts with disjoint files and interfaces you can
     pin down in the plan (e.g. tokenizer / parser / evaluator). Otherwise one worker.
   - Tell the user in one line, e.g. `Size check: ~900 lines, 5 modules → delegating (tier: hard, 2 parallel workers).`
   - Overrides: `force` in the request always delegates; "do it yourself" never does.

1. **Detect**: `DELEGATE detect` (test command, protected test files, git). If detection is wrong,
   write `.delegate/config.json` (`{"test_cmd": "...", "test_globs": ["..."]}`). Install the test
   framework first if needed (e.g. `npm install`).
2. **Plan**: write `.delegate/PLAN.md`: goal, files, public interfaces (signatures, types),
   behavior, edge cases, constraints. Be concrete: the worker cannot ask questions.
   For parallel work also write one plan per part plus `.delegate/parallel.json`:
   ```json
   {"tasks": [
     {"name": "parser", "plan": ".delegate/plans/parser.md", "files": ["src/parser.js", "src/tokenizer.js"], "tier": "hard",
      "test_cmd": "node --test test/parser.test.js"},
     {"name": "eval", "plan": ".delegate/plans/eval.md", "files": ["src/eval/**"]}
   ]}
   ```
   Each part's plan must include the shared interfaces it uses. `files` are the globs that part may
   change; anything else it changes is discarded. `test_cmd` (optional) runs only that part's tests.
3. **Tests**: write them yourself: the contract. Cover each requirement and edge case once; keep them
   concise (tests are the biggest Claude-side cost). For parallel work, put each part's tests in its
   own file(s) importing only that part, so each worker can run them in its copy (its `test_cmd`).
   Run `DELEGATE detect` again and check your test files are under `protected_files` (else add
   `extra_protected` to the config).
4. **Delegate on autopilot** (one command does all rounds, retries and the review):
   - `DELEGATE run --plan .delegate/PLAN.md --tier <tier> --auto --wait 540`
     (parallel: `DELEGATE run --parallel .delegate/parallel.json --tier <tier> --auto --wait 540`)
     with the Bash tool timeout set to 600000.
   - If it returns `"status": "running"`, call `DELEGATE wait --timeout 540` (same Bash timeout) until
     it returns something else. Never start another run while one is running.
   - The runner handles the loop itself: it re-runs the tests after each round, sends failing output
     back to the worker, moves to the hard tier after 2 failed rounds, and when the tests pass has a
     model of another family review the diff and sends high-severity issues back too (limits:
     `auto_max_rounds`, `auto_review_cycles`). You only get the final result.
   - On the first delegation, tell the user once that they can watch live with
     `python3 <skill base directory>/delegate.py watch` in another terminal (or
     `"live_view": "auto"` in `~/.config/delegate/config.json`). Mention `live_view_error` once if present.
5. **Act on the final `status`** (`rounds` lists each round; `tests`, `review` and `changed_files`
   describe the end state):
   - `done`: tests pass and the review found no high-severity issue. Go to step 6.
   - `needs_test_change`: read `test_change_request`. As the test owner, either edit the test
     (feedback: "Approved: <change>. Continue.") or keep it (feedback: "Rejected: the test is correct
     because <reason>."), then run again with `--auto --continue --feedback-file .delegate/feedback.md`.
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
   issues and `review.unverified` if present), and `worker_credits` (Copilot AI credits, including
   reviews).

Manual mode (only if the user asks to drive rounds themselves): omit `--auto`; each round then
returns on its own and you retry with `--continue --feedback-file`, and run `DELEGATE review
--tier <tier>` yourself once the tests pass.

## Token discipline

- Do NOT read implementation files, `git diff`, or `.delegate/logs/`. Use the JSON (`summary`,
  `changed_files`, `tests`, review `issues`).
- Exceptions: setup errors (`log_tail`), or the user explicitly asks you to review the code.
- Pass failing test output as feedback verbatim; only diagnose it yourself after repeated failures
  on the same error.

## Configuration (`.delegate/config.json` per project, `~/.config/delegate/config.json` global)

`models` ({"normal", "hard"}), `model` (pin one), `timeout` (s per worker run, default 1800),
`test_cmd`, `test_globs`, `extra_protected`, `count_tests` (default true), `review_models`
({"normal": "gpt-5.6-sol", "hard": "gpt-5.6-sol"}; "gpt-6-astra" is more thorough, ~4.5x the credits), `review_model` (pin one), `auto_max_rounds` (4),
`auto_review` (true), `auto_review_cycles` (2), `live_view`, `builtin_mcps` (default false), `keep_checkpoints` (20),
`extra_args`, and `"backend": "command"` with `"command": [...]` for another agent CLI (prompt in
`$DELEGATE_PROMPT`; it must write `.delegate/result.json`).
