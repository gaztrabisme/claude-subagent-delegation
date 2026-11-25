VERDICT: PASS
TESTS: 17
RED_ON_BASE: 15/17

| test node id | requirement id | one-line reason it fails on base |
|---|---|---|
| `tests/r1b/test_b1_verify_env.py::test_b1_run_verification_strips_server_secrets_and_non_allowlisted_env` | B1, B1.1 | `AssertionError: SECRET_CANARY_TOKEN leaked into verification env` (`run_verification` inherits full `os.environ`) |
| `tests/r1b/test_b1_verify_env.py::test_b1_goal_command_row_strips_server_secrets` | B1, B1.2 | `AssertionError: SECRET_CANARY_TOKEN leaked into goal command row env` (`evaluate_goal` calls `run_verification` without allowlist env) |
| `tests/r1b/test_b2_verify_sandbox.py::test_b2_macos_denies_non_loopback_network_with_permission_error` | B2, B2.1 | `AssertionError: expected permission error, got {'status': 'timeout', 'type': 'TimeoutError'}` (unsandboxed connect times out instead of EPERM/EACCES) |
| `tests/r1b/test_b2_verify_sandbox.py::test_b2_macos_allows_loopback_server_and_sets_sandboxed` | B2, B2.2 | `AttributeError: 'VerificationResult' object has no attribute 'sandboxed'` |
| `tests/r1b/test_b2_verify_sandbox.py::test_b2_macos_allows_workspace_and_temp_writes_but_denies_outside` | B2, B2.3 | `AssertionError: exited 1` (write outside workspace and `TMPDIR` succeeded instead of failing with `PermissionError`) |
| `tests/r1b/test_b2_verify_sandbox.py::test_b2_runs_in_new_session_and_kills_process_tree_on_timeout` | B2, B2.4 | `AssertionError: expected verification to run in a new session` (`subprocess.run` lacks `start_new_session=True` and process-tree kill) |
| `tests/r1b/test_b2_verify_sandbox.py::test_b2_off_macos_runs_unsandboxed_and_reports_sandboxed_false` | B2, B2.5 | `AttributeError: 'VerificationResult' object has no attribute 'sandboxed'` |
| `tests/r1b/test_b3_copilot_gemini_env.py::test_b3_copilot_env_uses_allowlist_and_excludes_secrets` | B3, B3.1 | `AssertionError: SECRET_CANARY_TOKEN leaked into CopilotProvider.env()` (copies `os.environ` instead of `base_child_env()`) |
| `tests/r1b/test_b3_copilot_gemini_env.py::test_b3_copilot_env_includes_declared_api_key` | B3, B3.2 | `AssertionError: assert None == 'copilot-key-...'` (`CopilotProvider.env()` pops `settings.leaked_keys` instead of keeping its own `api_key_env`) |
| `tests/r1b/test_b3_copilot_gemini_env.py::test_b3_gemini_env_uses_allowlist_and_excludes_secrets` | B3, B3.3 | `AssertionError: SECRET_CANARY_TOKEN leaked into GeminiProvider.env()` (copies `os.environ` instead of `base_child_env()`) |
| `tests/r1b/test_b3_copilot_gemini_env.py::test_b3_gemini_env_includes_declared_api_key` | B3, B3.4 | `AssertionError: assert None == 'gemini-key-...'` (`GeminiProvider.env()` pops `settings.leaked_keys` instead of keeping its own `api_key_env`) |
| `tests/r1b/test_b4_worktree_deps.py::test_b4_parallel_worktrees_have_no_dependency_symlinks` | B4, B4.1 | `AssertionError: worktree .../wt has symlink for node_modules` (`_parallel_work` symlinks `DEPENDENCY_DIRS`) |
| `tests/r1b/test_b4_worktree_deps.py::test_b4_install_in_one_worktree_does_not_mutate_root_or_sibling` | B4, B4.2 | `AssertionError: ['sibling saw node_modules/installed.txt', ...]` (shared symlinks propagate writes to root and sibling worktree) |
| `tests/r1b/test_b5_linux_sandbox.py::test_b5_deny_read_refuses_off_macos` | B5, B5.1 | green on base: regression (`sandbox.check_platform` already refuses `deny_read` off macOS) |
| `tests/r1b/test_b5_linux_sandbox.py::test_b5_dsh_refuses_to_start_off_macos` | B5, B5.2 | `AssertionError: dsh driver boot() must refuse off macOS` (`DSH_PROVIDER.boot()` returns `None` on `linux`) |
| `tests/r1b/test_b5_linux_sandbox.py::test_b5_claude_codex_omp_start_off_macos` | B5, B5.3 | green on base: regression (`claude`, `codex`, `omp` without `deny_read` already boot and spawn on `linux`) |
| `tests/r1b/test_b5_linux_sandbox.py::test_b5_doctor_json_reports_os_boundary_by_platform` | B5, B5.4 | `AssertionError: missing os_boundary on linux for c` (`subagent doctor --json` rows lack `os_boundary`) |

## Clauses not tested offline

- **B6 (No regression):** Full-suite, `ruff`, `AGENTS.md` rule 2 grep, and the test-change-request workflow for conflicting existing tests (e.g. `tests/loop/test_loop_parallel.py`) are wave-2 builder / gate checks rather than unit tests in `tests/r1b/`.
- **B7 (Live):** `scripts/smoke_lanes.py` on the `gemini` lane and live `delegate` verification with canary check require live network/CLI access and are run by the manager in wave 2 (copilot live-smoke is waived per B7 because the `copilot` CLI is not installed).
- **Environment note for B2 network checks:** When `tests/r1b/test_b2_verify_sandbox.py` is run inside an outer network-denying Seatbelt sandbox, `127.0.0.1` loopback connect in `test_b2_macos_allows_loopback_server_and_sets_sandboxed` is blocked by the outer sandbox (`PermissionError`); outside that outer sandbox, all five B2 tests fail on base for the exact reasons listed above.
