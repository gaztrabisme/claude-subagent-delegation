# U3 Core Report

The baseline failure came from Node v24.16.0: its default `node:test` reporter emits summary lines with `ℹ` instead of TAP's `#`. The parser recognized only `#`, so test counts were omitted even though tests passed. The parser now reads both formats.

Changes are in `src/subagent/loop/detect.py`, `src/subagent/providers/base.py`, `src/subagent/config.py`, `src/subagent/providers/codex.py`, and `src/subagent/loop/loop.py`, with regression coverage in `tests/loop/test_loop_units.py`, `tests/loop/test_loop_parallel.py`, `tests/test_codex_driver.py`, and `tests/test_config.py`. Provider effort now round-trips through TOML and Codex argv; parallel tasks queue to available core/provider capacity, report per-task exceptions, and clean up worktrees in `finally`. Review finding fixed in commit `f6ebab1`.

## Acceptance

- `uv run pytest -q` — `797 passed, 1 skipped, 19 subtests passed in 241.48s (0:04:01)`
- `uv run pytest tests/test_codex_driver.py -q -k effort` — `2 passed, 20 deselected in 0.87s`
- `uv run pytest tests/loop/test_loop_parallel.py -q -k max_agents` — `2 passed, 6 deselected in 4.56s`
- `uv run ruff check src tests` — exit 1, `Found 168 errors.` The remaining repository-wide lint findings are outside the added lines; the new lines have no Ruff diagnostics.

Open: the repository-wide Ruff backlog remains, so that acceptance command is not green.
