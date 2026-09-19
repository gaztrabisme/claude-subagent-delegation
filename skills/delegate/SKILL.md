---
name: delegate
description: Delegate implementation work to a cheaper coding agent (GitHub Copilot CLI by default) to save Claude tokens. Claude writes the plan and the tests, the worker implements, Claude verifies by running the tests. Use when the user says /delegate or asks to hand implementation off to Copilot.
---

# Delegate implementation to a worker agent

Your job is to be the **planner and verifier**, not the implementer. Every token you spend reading
implementation code defeats the purpose of this skill.

The runner is `delegate.py` in this skill's base directory. Below, `DELEGATE` means
`python3 <skill base directory>/delegate.py`. Run it from the target project's root.

## Workflow

0. **Size check** (before writing any plan or tests). Delegation has a fixed overhead (plan, tests,
   extra turns) and only pays off for larger work. Benchmarks: a ~60-line change cost 40% MORE when
   delegated, ~200 lines broke even, ~1000 lines cost 66% less. Estimate the implementation from the
   task and what you already know about the code; don't read the codebase just to estimate.
   - **Do it yourself** (normal Claude Code workflow, skip the rest of this skill) if ANY of:
     estimated implementation under ~200 lines; 1-2 files touched; a bug fix, rename, config or
     wiring change; or the plan + tests you would write are about as long as the code itself.
   - **Delegate** if ANY of: ~200+ lines; 3+ files or new modules; non-trivial algorithms
     (parsers, state machines, graphs, protocols); likely to need several debug iterations.
   - If delegating, also pick the worker's **tier** (it maps to a model via config `models`):
     - `hard` (default `claude-opus-5`): parsers/compilers/interpreters, graph or scheduling algorithms,
       concurrency, tricky state or cache invalidation, performance constraints, security-sensitive
       code, or cross-cutting changes in an unfamiliar existing codebase. Also when the spec has many
       subtle interacting rules (precise error/coercion semantics, many edge cases).
     - `normal` (default `claude-sonnet-5`): everything else, e.g. CRUD/API endpoints, UI
       components, data mapping, glue code, well-specified modules with straightforward logic.
     - When unsure, pick `normal`: failed rounds escalate to `hard` (step 5), and the hard model
       uses more of the user's Copilot quota.
   - Tell the user the decision in one line, e.g.
     `Size check: ~900 lines across 5 modules (parser, evaluator, graph) → delegating (tier: hard).`
   - Overrides: if the user's request contains `force` (e.g. `/delegate force ...`), always delegate.
     If they asked you to do it yourself, don't delegate.

1. **Detect**: `DELEGATE detect` shows the test command and which files count as tests
   (auto-detected from package.json / pyproject.toml / go.mod / Cargo.toml / Makefile).
   If detection is wrong or empty, write `.delegate/config.json` (e.g. `{"test_cmd": "...", "test_globs": ["..."]}`).
   The test framework must be installed (e.g. `npm install`) before delegating.
2. **Plan**: write `.delegate/PLAN.md`: goal, files to create/modify, public interfaces
   (function signatures, types), behavior and edge cases, constraints. Be concrete: the worker
   is a weaker model and cannot ask questions.
3. **Tests**: write the tests yourself. They are the contract. Cover each requirement and edge case
   from the plan once; keep them concise (tests are the biggest Claude-side cost of delegating).
   Run `DELEGATE detect` again and check that your test files appear under `protected_files`.
   If not, add them via `extra_protected` in the config.
4. **Delegate**: `DELEGATE run --plan .delegate/PLAN.md --tier <normal|hard>`. It can take many
   minutes: always run it in the foreground with the Bash tool's maximum timeout (600000 ms).
   It prints a JSON status, including the `model` used. If it contains `model_fallback`, the hard
   model was unavailable and the normal one ran instead; mention this to the user once.
   Every retry below repeats the same `--tier` unless it says to escalate.
5. **Act on `status`**:
   - `done`: go to step 6.
   - `needs_test_change`: read `test_change_request` in the JSON. Decide as the test owner:
     - Approve: edit the test yourself, then
       `DELEGATE run --plan .delegate/PLAN.md --continue --feedback "Approved: <what you changed>. Continue."`
     - Reject: `DELEGATE run --plan .delegate/PLAN.md --continue --feedback "Rejected: the test is correct because <reason>. Fix the implementation."`
   - `violated_tests`: the worker edited protected tests; they were already reverted. Re-run with
     `--continue --feedback "You modified test files; that is forbidden and was reverted. Use .delegate/test_change_request.md if a test is wrong."`
   - `failed` / `no_report` / `timeout`: re-run with `--continue` and feedback from the `summary`.
   - `backend_error`: read `log_tail` and fix the setup (auth, model name, missing CLI), then retry.
6. **Verify**: `DELEGATE test`. This is the source of truth; never trust the worker's "done" alone.
   - `passed: true`: report to the user: status, summary, `changed_files`. Done.
   - `passed: false`: `DELEGATE run --plan .delegate/PLAN.md --continue --feedback "<output_tail>"`.
   - **Escalation**: if two `normal`-tier rounds in a row end with failing tests (or `failed`),
     switch to `--tier hard` for the next round (keep `--continue` and the feedback) and tell the
     user in one line.
7. **Stop after 3 delegation rounds** without passing tests. Tell the user what is failing and ask
   whether to try another round, adjust the plan, or have Claude fix it directly.

## Token discipline

- Do NOT read implementation files, `git diff`, or the worker logs in `.delegate/logs/`.
  Rely on the JSON output (`summary`, `changed_files`, `output_tail`).
- Exceptions: `backend_error` (read `log_tail`), or the user explicitly asks for a code review.
- Pass failing test output as feedback verbatim; don't diagnose it yourself unless the worker
  has failed repeatedly on the same error.

## Configuration (`.delegate/config.json`, all optional)

```json
{
  "backend": "copilot",
  "models": {"normal": "claude-sonnet-5", "hard": "claude-opus-5"},
  "model": null,
  "timeout": 1800,
  "extra_args": [],
  "test_cmd": "npm run test:unit",
  "test_globs": ["tests/**"],
  "extra_protected": ["src/**/*.fixture.json"]
}
```

`models` maps tiers to Copilot model names (see `copilot help config` for the list). Setting `model`
pins one model for every tier and disables tier selection.

`"backend": "command"` with `"command": ["some-cli", "..."]` runs any other agent CLI; it gets the
prompt in `$DELEGATE_PROMPT` and must write `.delegate/result.json` the same way.
