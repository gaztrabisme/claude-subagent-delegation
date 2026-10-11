# Goal: Windows guard gap and Windows test triage

Run ID: `win-guard-2026-10-11`. Branch `windows-guard`, cut from `windows-compat` at `485e14c`.
Owner: Huy. Coordinator: Claude (conductor). Code units: Codex through the subagent runtime.
Written before dispatch; amended only by dated owner amendments below.

Decisions taken at go (2026-10-11): F1 worktree, F2 tests run outside the Codex sandbox,
F3 unknown Windows shell commands escalate, F4 Unix-only tests get `skipif` with a stated reason
and cheap fake CLIs are ported, F5 push the branch and open a PR into `windows-compat`.

| ID | Criterion | Source | Artifact | UAT |
|---|---|---|---|---|
| G1 | On Windows, a shell command that reads a sensitive home path written with backslashes (`type C:\Users\<u>\.ssh\id_rsa`, `Get-Content ~\.ssh\id_rsa`, `gc $HOME\.codex\auth.json` with `.codex` protected) is not allowed | session check 2026-10-11 | `src/subagent/guard/classify.py` | coordinator's case script: every sensitive case returns deny or escalate |
| G2 | Ordinary workspace commands on Windows still pass: `python -m pytest`, `uv run pytest`, a read of a workspace file | no regression | same | same case script: these return allow |
| G3 | A Windows shell command the policy cannot read escalates; it is never allowed by default | F3 | same | case script: an unknown cmdlet returns escalate |
| G4 | The new behaviour has tests that run on Windows and are skipped elsewhere only where they cannot apply | F4 | `tests/test_guard_windows.py` | `uv run pytest -q tests/test_guard_windows.py` passes on this machine |
| G5 | POSIX guard behaviour is unchanged: no existing guard test changes its expectation | Huy, "universal across OS" | `tests/` | `git diff windows-compat -- tests/` shows no changed or removed `assert` in existing guard tests |
| G6 | Every Windows failure of the full suite at `485e14c` (149 failed, 6 errors) is classified as real bug, Unix-only, or fake CLI not runnable on Windows | item 3 | `wiki/data/windows-test-triage.md` | the report lists each failing test id once with a category; coordinator samples 10 |
| G7 | Unix-only tests are skipped on Windows with a stated reason; no assertion is weakened or removed | F4 | `tests/` | full `uv run pytest -q` on Windows: no test fails because it is Unix-only; diff check as G5 |
| G8 | An independent reviewer of another model family finds no high-severity bypass in the G1 change, or each one found is fixed | gate 8 | review notes in `wiki/review.md` | reviewer report read by the coordinator; each High has a fix commit or an owner decision |

Not in scope: a full PowerShell parser, merging into `main`, editing the live install,
running the suite on macOS or Linux (stated open after the run).

## Owner amendments

(none)
