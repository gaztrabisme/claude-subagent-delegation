# R6 Acceptance Tests Report

**Verdict on base commit:** 30 failed (red), 4 passed (green in `test_c_workspace_refusal.py`), 0 collection errors, `ruff check tests/r6` passed.
**Environment note:** Inside this sandboxed worktree, `.venv` does not exist and `/Users/owner/miniconda3/bin/python3` has `mcp==1.22.0` (whereas `pyproject.toml` requires `mcp>=2.0.0`, which lives in the main repo `.venv` outside the sandbox). `UV_CACHE_DIR=/private/tmp/subagent-uv-cache uv run --no-sync pytest` creates an empty `.venv` without `subagent` or `mcp>=2.0.0`. Therefore:
- `ruff check tests/r6` ran directly and passed (`All checks passed!`).
- `PYTHONPATH=src python3 -m pytest tests/r6 --tb=short` ran all 34 collected items: 32 executed to their intended red/green states on base, and the 2 tests importing `subagent.mcp_server` (`test_b_mcp_delegate_records_entry_mcp` and `test_d_mcp_list_agents_reports_served_commit`) hit `ImportError: cannot import name 'MCPServer' from 'mcp.server'` because of the `mcp==1.22.0` package in miniconda3.
- Manager commands to run in the unsandboxed `.venv`:
  - `UV_CACHE_DIR=/private/tmp/subagent-uv-cache uv run --no-sync pytest tests/r6 -q`
  - `uv run --no-sync ruff check tests/r6`

## Load Report

| Item | Used? | How it was used |
|---|---|---|
| `AGENTS.md` | Yes | Followed offline test rule, no secrets/machine-specific values, trace schema contract, `.subagent/result.json` requirement. |
| `wiki/operation-refactor-2026-11.md` (R6 row) | Yes | Checked R6 scope and closed finding IDs (A-01, A-06, A-14, A-19, A-24, B-04) and acceptance expectations. |
| `tests/conftest.py` | Yes | Used `make_settings`, `write_config`, `fake_codex`, and autouse `trace_records` / `_provider_keys` fixtures. |
| `tests/test_cli_doctor.py` | Yes | Copied `main(["doctor", ...])`, `_set_config` (`SUBAGENT_CONFIG`), `fake_codex`, and `--no-probe`/`--json` assertion patterns. |
| `tests/test_cli_loop_commands.py` | Yes | Used `SUBAGENT_CONFIG` / `--root` CLI invocation patterns for `subagent run` and `detect`. |
| `tests/test_mcp_server.py` | Yes | Copied `importlib.import_module("subagent.mcp_server")` + `monkeypatch.setattr(module, "settings"/"registry", ...)` pattern. |
| `tests/test_trace_schema.py` | Yes | Imported `REQUIRED` and `problems()` in `test_a_exec.py` and `test_b_entry.py` without modifying the locked file. |
| `tests/test_lane_status.py` | Yes | Copied `_fake_claude_provider` / `FAKE_CLAUDE` / `SCENARIOS` pattern and lazy `importlib.import_module` pattern (`_lane_status`). |
| `tests/r1_0/` | Yes | Followed finished package folder layout (`__init__.py`, `_helpers.py`, `test_<criterion>_*.py`, `REPORT.md` table format). |
| `src/subagent/config.py` | Yes | Inspected `Settings.workspace_refusal`, `APPROVAL_HOOK`, `sys.prefix`, `CONFIG_ENV`, and `load()`. |
| `src/subagent/cli.py` | Yes | Inspected `_parser()`, `cmd_doctor()`, `_print_table()`, `_project_config_root()`, and `main()`. |
| `src/subagent/mcp_server.py` | Yes | Inspected `delegate()`, `list_agents()`, and module-level `settings`/`registry`. |
| `src/subagent/runs.py` | Yes | Inspected `Registry.create_agent`, `Agent.delegate`, `self.trace.run(...)`, and `vendor`/`provider`/`lane` fields. |
| `src/subagent/telemetry/trace.py` | Yes | Inspected `SCHEMA == 3`, `Trace.write`, and `Trace.run` payload shape. |
| `src/subagent/lane_status.py` | Yes | Inspected `LaneStatusStore.record_run` (sets `checked_at`, `state="open"`, `source="run"`), `active_run`, `active_count`, `snapshot`. |
| `src/subagent/loop/testguard.py` | Yes | Checked `TestGuard` / `protected_paths` and default protected globs via `loop/detect.py` for `subagent exec --lock-commit`. |

## Per-Criterion Summary

### (a) `subagent exec` — `tests/r6/test_a_exec.py`

| Test | Asserts | Status on base & reason |
|---|---|---|
| `test_a_exec_subcommand_registered_in_cli` | `subagent exec --help` exits 0 (silent-absence check) | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_unknown_lane_exits_2_without_tracing` | Unknown `<lane>` exits 2, does not run argv, writes no trace | **RED**: `AssertionError: 'invalid choice' not in stderr` |
| `test_a_exec_success_traces_run_and_charges_lane_status` | Runs in `--root`, preserves exit 0, writes 1 `run` trace (`entry="exec-wrapper"`, `REQUIRED["run"]`, `problems()==[]`, zero usage, `lock=None`), `LaneStatusStore.snapshot` has `checked_at` set, `source="run"`, `state="open"` | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_nonzero_exit_preserves_code_and_traces_failed` | Non-zero exit 7 preserved; 1 `run` trace with `state="failed"`, `finish_reason="error"`, `exit_code=7` | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_missing_binary_exits_127_and_traces_failed` | Missing binary exits 127; 1 `run` trace with `state="failed"`, `finish_reason="error"`, `exit_code=127` | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_holds_active_lane_lease_while_running` | `active_count("glm") == 1` while argv runs, `0` after | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_lock_commit_unknown_rev_exits_2_before_running` | Bad `--lock-commit` exits 2 before running argv and writes no trace | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_lock_commit_unchanged_default_globs_returns_ok` | Unchanged default protected files -> argv exit 0, `lock="ok"`, `state="completed"`, `finish_reason="complete"` | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_lock_commit_violation_exits_3_and_prints_paths` (4 cases: `modified`, `deleted`, `added`, `modified`+exit 1) | Violation exits 3 even when argv exits 1, prints path on stderr, traces `lock="violated"`, `state="failed"`, `finish_reason="violated_tests"` | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_a_exec_locked_explicit_glob_scopes_lock_check` | Explicit `--locked` globs ignore changes outside glob (`lock="ok"`) and catch changes matching glob (exit 3, `lock="violated"`) | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |

### (b) `entry` on every run record — `tests/r6/test_b_entry.py`

| Test | Asserts | Status on base & reason |
|---|---|---|
| `test_b_mcp_delegate_records_entry_mcp` | MCP `delegate` via `fake_claude.py` writes `run` trace with `entry="mcp"`, `SCHEMA==3` | **RED (env-blocked in sandbox)**: `ImportError: cannot import name 'MCPServer'` under miniconda3 `mcp==1.22.0`; will fail on missing `entry` in `.venv` |
| `test_b_cli_subagent_run_records_entry_cli` | `subagent run` via `fake_claude.py` writes `run` trace with `entry="cli"`, `SCHEMA==3` | **RED**: `AssertionError: missing 'entry' on run record` |
| `test_b_exec_wrapper_records_entry_exec_wrapper` | `subagent exec` writes `run` trace with `entry="exec-wrapper"`, `SCHEMA==3` | **RED**: `SystemExit(2)` (`invalid choice: 'exec'`) |
| `test_b_registry_direct_defaults_entry_to_mcp_and_schema_unchanged` | Direct `Registry` run via `fake_claude.py` defaults `entry="mcp"`, `SCHEMA==3`, `problems()==[]` | **RED**: `AssertionError: missing 'entry' on run record` |

### (c) `workspace_refusal` — `tests/r6/test_c_workspace_refusal.py`

| Test | Asserts | Status on base & reason |
|---|---|---|
| `test_c_worktree_with_repo_layout_outside_pinned_install_is_accepted` | Tmp git worktree holding `src/subagent/guard/approval_hook.py` and `.venv` is accepted (`None`) when server hook & `sys.prefix` are outside | **GREEN**: passes on base commit as expected (already implemented via `APPROVAL_HOOK` / `sys.prefix` path checks) |
| `test_c_workspace_containing_approval_hook_is_refused` | Workspace containing patched `APPROVAL_HOOK` is refused naming `"guard hook"` | **GREEN**: existing behaviour |
| `test_c_workspace_containing_sys_prefix_is_refused` | Workspace containing patched `sys.prefix` is refused naming `"server's python"` | **GREEN**: existing behaviour |
| `test_c_served_json_install_path_refuses_containing_workspace` | `<session_root>/served.json` `install_path` inside workspace refuses with `"pinned install"`, sibling workspace accepted | **RED**: `AssertionError: assert None is not None` (`workspace_refusal` does not read `served.json` on base) |
| `test_c_registry_create_agent_refuses_disallowed_workspace` | `Registry.create_agent` into refused workspace raises `RegistryError` | **GREEN**: existing behaviour |

### (d) `subagent promote` and `served_commit` — `tests/r6/test_d_promote.py`

| Test | Asserts | Status on base & reason |
|---|---|---|
| `test_d_promote_cli_and_module_exist` | Lazy-imported `subagent.promote` defines `promote`/`served` and `subagent promote --help` exits 0 | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_promote_exports_exact_commit_non_editable_and_writes_served_json` | Exports exact sha even with dirty tree, installs non-editably (`-e`/`--editable` absent) into `<session_root>/installs/<sha>`, writes atomic `served.json`, `served(settings)` matches | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_promote_unknown_commit_exits_2_and_writes_nothing` | Unknown commit exits 2 and writes neither `served.json` nor `installs/` | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_promote_cli_prints_short_sha_and_exits_0` | CLI `promote` exits 0, prints short sha, writes `served.json` | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_promote_runner_failure_exits_1_and_leaves_served_json_unchanged` | Runner non-zero exit leaves existing `served.json` intact and exits 1 | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_promote_refuses_live_runs_unless_force` | Refuses (exit 1, names lane `"glm"`) while `active_run("glm")` is held; `--force` exits 0, mentions live runs, writes `served.json` | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_served_returns_none_when_missing` | `served(settings)` returns `None` when `served.json` is absent | **RED**: `ModuleNotFoundError: No module named 'subagent.promote'` |
| `test_d_mcp_list_agents_reports_served_commit` | `mcp_server.list_agents` includes `served_commit` (`None` when absent, full sha when present) | **RED (env-blocked in sandbox)**: `ImportError: cannot import name 'MCPServer'` under miniconda3 `mcp==1.22.0`; will fail on missing `served_commit` in `.venv` |

### (e) `subagent doctor` workspace line — `tests/r6/test_e_doctor.py`

| Test | Asserts | Status on base & reason |
|---|---|---|
| `test_e_doctor_json_accepted_workspace` | `doctor --json` includes `workspace` (`path`, `accepted: True`, `reason: None`) for `--root` and default cwd | **RED**: `AssertionError: assert 'workspace' in data` |
| `test_e_doctor_json_refused_workspace_exits_0` | Refused workspace still exits 0 (`ok: True`), `workspace.accepted is False`, `reason` names guard hook | **RED**: `AssertionError: assert 'workspace' in data` |
| `test_e_doctor_no_probe_includes_workspace` | `doctor --no-probe --json` includes `workspace` key | **RED**: `AssertionError: assert 'workspace' in data` |
| `test_e_doctor_table_prints_workspace_line` | Table output prints `workspace: accepted` and `workspace: refused: <reason>` with exit 0 | **RED**: `AssertionError: assert 'workspace: accepted' in ...` |
