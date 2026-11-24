VERDICT: PASS
TESTS: 32
RED_ON_BASE: 32/32

## Acceptance tests (`tests/r1_0/`)

| Test | Requirement | One-line reason it fails on base |
|---|---|---|
| `tests/r1_0/test_q1_socket_location.py::test_q1_default_socket_under_session_root_with_secure_perms` | Q1 | `AssertionError: default approval_socket /var/folders/.../subagent-approval-47138.sock must live under session_root .../.q138` |
| `tests/r1_0/test_q1_socket_location.py::test_q1_too_long_session_root_raises_config_error_without_temp_fallback` | Q1 | `Failed: DID NOT RAISE ConfigError` |
| `tests/r1_0/test_q1_socket_location.py::test_q1_configured_socket_refuses_0777_parent_dir` | Q1 | `Failed: DID NOT RAISE any of (ConfigError, OSError, RuntimeError, ValueError)` |
| `tests/r1_0/test_q1_socket_location.py::test_q1_configured_socket_refuses_symlink_parent_dir` | Q1 | `Failed: DID NOT RAISE any of (ConfigError, OSError, RuntimeError, ValueError)` |
| `tests/r1_0/test_q1_socket_location.py::test_q1_configured_socket_missing_parent_created_with_0700` | Q1 | `assert 493 == 448` (`0o755` created instead of `0o700`) |
| `tests/r1_0/test_q2_sandbox_socket_deny.py::test_q2_dsh_argv_profile_denies_write_on_socket_directory` | Q2 | `assert '(deny file-write* (subpath ".../sockdir"))' in '(version 1)\n(allow default)...'` |
| `tests/r1_0/test_q2_sandbox_socket_deny.py::test_q2_runs_deny_read_wrap_denies_write_on_socket_directory` | Q2 | `assert '(deny file-write* (subpath ".../sockdir"))' in '(version 1)\n(allow default)...'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_agent_gets_32_byte_approval_key_and_0600_key_file` | Q3 | `AttributeError: 'Agent' object has no attribute 'approval_key'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_approval_auth_sign_and_verify_contract` | Q3, Q4 | `ModuleNotFoundError: No module named 'subagent.guard.approval_auth'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_valid_signed_request_matches_decide` | Q3 | `AttributeError: 'Agent' object has no attribute 'approval_key'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_wrong_mac_denied_without_escalation_or_denials` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_mac_with_another_agents_key_is_denied` | Q3 | `AttributeError: 'Agent' object has no attribute 'approval_key'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[missing-nonce]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[empty-nonce]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[short-nonce]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[63-hex]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[65-hex]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[non-hex-64]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_missing_or_malformed_nonce_is_denied[int-nonce]` | Q3 | `AssertionError: assert 'allow' == 'deny'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_replayed_nonce_is_denied_without_escalation_or_denials` | Q3 | `AttributeError: 'Agent' object has no attribute 'approval_key'` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q3_empty_missing_unknown_evicted_agent_id_denied_without_side_effects` | Q3 | `AssertionError: expected deny for agent_id=None, got {'action': 'allow', 'reason': 'read-only tool'}` |
| `tests/r1_0/test_q3_q4_supervisor_auth.py::test_q4_supervisor_signs_allow_deny_and_error_replies_for_known_agent` | Q4 | `AssertionError: assert None == '05822e6a558096a8cbcc9f45c544a97506e44e85146be6aed05a65d68a7bb91d'` |
| `tests/r1_0/test_q5_hook_and_drivers.py::test_q5_hook_denies_when_key_file_missing_unreadable_or_malformed` | Q5 | `AssertionError: expected deny exit 2 for ['--agent', 'a1'], got 0` |
| `tests/r1_0/test_q5_hook_and_drivers.py::test_q5_hook_denies_unsigned_wrong_key_wrong_nonce_and_non_object_replies` | Q5 | `AssertionError: unsigned: expected exit 2, got 0` |
| `tests/r1_0/test_q5_hook_and_drivers.py::test_q5_hook_allows_verified_allow_and_denies_with_reason_on_signed_deny` | Q5 | `assert (False)` (`isinstance(None, str)` because hook sent no `nonce`) |
| `tests/r1_0/test_q5_hook_and_drivers.py::test_q5_guard_hook_command_and_all_hook_drivers_carry_key_file` | Q5 | `AssertionError: missing --key-file in hook command: '.../approval_hook.py --agent a1-6b373e'` |
| `tests/r1_0/test_q5_hook_and_drivers.py::test_q5_approval_key_never_leaks_in_argv_env_files_or_trace` | Q5 | `AttributeError: 'Agent' object has no attribute 'approval_key'` |
| `tests/r1_0/test_q6_dsh_tmpdir.py::test_q6_dsh_profile_and_env_use_per_agent_tmp_only[api-key]` | Q6 | `AssertionError: expected per-agent tmp dir at .../sessions/agents/agent-q6/tmp` |
| `tests/r1_0/test_q6_dsh_tmpdir.py::test_q6_dsh_profile_and_env_use_per_agent_tmp_only[login]` | Q6 | `AssertionError: expected per-agent tmp dir at .../sessions/agents/agent-q6/tmp` |
| `tests/r1_0/test_q7_process_tree.py::test_q7_process_normal_exit_kills_setsid_and_double_fork_descendants` | Q7 | `AssertionError: descendants survived normal exit: [47171, 47173]` |
| `tests/r1_0/test_q7_process_tree.py::test_q7_cancelled_run_kills_setsid_and_double_fork_descendants` | Q7 | `AssertionError: descendants survived cancel: [47181, 47183]` |
| `tests/r1_0/test_q7_process_tree.py::test_q7_dsh_process_normal_exit_kills_setsid_and_double_fork_descendants` | Q7 | `AssertionError: DshProcess descendants survived normal exit: [47204, 47206]` |

## Existing tests that conflict

| Test | Line | What it asserts | Minimal change needed |
|---|---|---|---|
| `tests/test_config.py::test_default_approval_socket_falls_back_when_tmpdir_path_is_too_long` | 63 | Asserts `config.load` falls back to `SHORT_SOCKET_ROOT / f"subagent-{os.getuid()}"` when `tempfile.gettempdir()` exceeds 104 bytes | Update or replace to assert a too-long `session_root` raises `ConfigError` with no `/tmp` fallback (Q1). |
| `tests/test_core.py::test_in_process_server_uses_short_socket_for_long_session_root` | 33 | Asserts `InProcessServer` falls back to `config.SHORT_SOCKET_ROOT` when `session_root / "approval.sock"` exceeds `UNIX_SOCKET_PATH_MAX` | Update to assert `ConfigError` is raised for a too-long `session_root` (Q1), or pass a short explicit `approval_socket` if testing `InProcessServer` startup. |
| `tests/test_lane_status_review.py::test_review_5_probe_stops_promptly_on_shutdown` | 593 | Calls `config._approval_socket_path(tmp_path / "a.sock")` expecting `/tmp/subagent-<uid>` fallback when `tmp_path / "a.sock"` >= 104 bytes | Pass a short socket path inside a `0700` directory (e.g. via `tmp_path_factory.mktemp("s")`) or let `InProcessServer`/default `session_root` pick a short socket path (Q1). |
| `tests/test_hook.py::test_allow_and_deny_from_unix_socket` | 53 | Invokes `approval_hook.py` with `["--agent", "a1"]` (no `--key-file`) against a fake socket returning unsigned `{"action": "allow", ...}` and `{"action": "deny", ...}` and asserts exit 0 / `"allow"` | Create a key file, pass `--key-file` to `_run_hook`, verify the incoming request's `nonce` + `mac`, and sign replies with echoed `nonce` (Q5). |
| `tests/test_codex_driver.py::test_hook_claude_shape_allows_with_explicit_decision` | 602 | Invokes `approval_hook.py` via `_hook()` without `--key-file` against `supervisor_socket` returning unsigned `{"action": "allow", "reason": "ok"}` | Update `supervisor_socket` + `_hook` (lines 554–599) to supply a key file (`--key-file`) and sign replies with `nonce` + `mac` (Q5). |
| `tests/test_codex_driver.py::test_hook_codex_shape_allows_silently` | 624 | Same unsigned `supervisor_socket` and `--key-file`-less `_hook()` helper, expecting exit 0 | Same update to `supervisor_socket` + `_hook` (Q5). |
| `tests/test_codex_driver.py::test_hook_codex_turn_id_detected_without_flag` | 632 | Same unsigned `supervisor_socket` and `--key-file`-less `_hook()` helper, expecting exit 0 | Same update to `supervisor_socket` + `_hook` (Q5). |
| `tests/test_codex_driver.py::test_hook_codex_deny_has_reason` | 640 | Same `_hook()` helper without `--key-file` against unsigned `{"action": "deny", "reason": "nope"}`, expecting reason `"nope"` | Same update to `supervisor_socket` + `_hook` so the deny reply is signed and carries echoed `nonce` (Q5). |
| `tests/test_codex_driver.py::test_hook_codex_apply_patch_asks_per_file` | 651 | Same `_hook()` helper without `--key-file` against unsigned allow+deny replies, expecting `"outside"` | Same update to `supervisor_socket` + `_hook` (Q5). |
| `tests/test_codex_driver.py::test_hook_codex_forwards_workdir_and_payload_cwd` | 687 | Same `_hook()` helper without `--key-file` against unsigned allow reply, expecting exit 0 | Same update to `supervisor_socket` + `_hook` (Q5). |
| `tests/test_providers_omp.py::test_hook_blocks_omp_write_to_a_protected_test_path` | 275 | Uses `_classification_socket` (line 181) returning unsigned `{"action": ..., "reason": ...}` and asserts `Path(process.env["SUBAGENT_GUARD_HOOK"]).name == "approval_hook.py"` (line 307) | Register/create the agent so `approval.key` exists, sign `_classification_socket` replies with the agent's key + echoed `nonce`, and adjust line 307 if `SUBAGENT_GUARD_HOOK` becomes a full command (Q5). |
| `tests/test_providers_omp.py::test_hook_maps_ast_edit_paths_to_multi_edit_before_classification` | 326 | Uses `_classification_socket` returning unsigned replies while running `omp_hook.ts` -> `approval_hook.py` | Create the agent key and sign `_classification_socket` replies with `nonce` + `mac` (Q5). |
| `tests/test_providers_omp.py::test_hook_maps_other_omp_write_tools_into_classifier_shapes` | 411 | Uses `_classification_socket` returning unsigned replies while running `omp_hook.ts` -> `approval_hook.py` | Create the agent key and sign `_classification_socket` replies with `nonce` + `mac` (Q5). |
| `tests/test_providers_omp.py::test_real_omp_hook_blocks_protected_write_against_local_stub` | 449 | Uses `_classification_socket` returning unsigned replies while running real `omp` + `omp_hook.ts` -> `approval_hook.py` | Create the agent key and sign `_classification_socket` replies with `nonce` + `mac` (Q5). |
| `tests/test_dsh_guard_bridge.py::test_bridge_blocks_protected_test_write_and_allows_workspace_read_through_real_approval_hook` | 668 | Passes `SUBAGENT_GUARD_HOOK` without `--key-file` (line 682) and uses `_classification_socket` (line 617) returning unsigned replies, asserting `"allow"` on `README.md` | Generate a temp key file, pass `--key-file` in `SUBAGENT_GUARD_HOOK`, and sign `_classification_socket` replies with echoed `nonce` + `mac` (Q5). |
| `tests/test_providers_dsh_login.py::test_login_protects_owner_dsh_home_through_bridge_during_run_and_releases_after` | 836 | Passes `SUBAGENT_GUARD_HOOK` without `--key-file` (line 854) and uses `_classification_socket` returning unsigned replies, asserting `"allow"` on workspace/post-run reads | Generate a temp key file, pass `--key-file` in `SUBAGENT_GUARD_HOOK`, and sign `_classification_socket` replies with echoed `nonce` + `mac` (Q5). |
| `tests/test_dsh_review4.py::test_r4_4_registry_agent_guard_context_carries_provider_homes_and_denies_reads` | 354 | Sends unsigned requests (no `nonce`, no `mac`) to `Supervisor._handle` expecting classification `DENY`; under Q3 these deny at auth before classification | Add `nonce` and `sign(agent.approval_key, ...)` to the `_send_guard_request` payloads so the classification path is exercised (Q3). |
