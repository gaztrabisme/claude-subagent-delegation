VERDICT: PASS
TESTS: 101
RED_ON_BASE: 69/101

## Acceptance tests (`tests/r1a/`)

| test node id | requirement id | one-line reason it fails on base |
|---|---|---|
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[missing]` | A1 | green on base: regression (R1-0 already denies missing `agent_id` in `_handle`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[empty]` | A1 | green on base: regression (R1-0 already denies empty `agent_id` in `_handle`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[unknown]` | A1 | green on base: regression (R1-0 already denies unknown `agent_id` in `_handle`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[closed]` | A1 | green on base: regression (`_handle` already denies closed `agent_id`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[int]` | A1 | green on base: regression (`_handle` already denies non-string `int` `agent_id`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[list]` | A1 | green on base: regression (`_handle` already denies non-string `list` `agent_id`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[dict]` | A1 | green on base: regression (`_handle` already denies non-string `dict` `agent_id`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_handle_denies_non_live_and_non_string_agent_ids[bool]` | A1 | green on base: regression (`_handle` already denies non-string `bool` `agent_id`) |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_workspace_for_raises_value_error_for_non_string_before_lookup[list]` | A1 | `TypeError: unhashable type: 'list'` in `Registry.find_agent` instead of `ValueError` before lookup |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_workspace_for_raises_value_error_for_non_string_before_lookup[dict]` | A1 | `TypeError: unhashable type: 'dict'` in `Registry.find_agent` instead of `ValueError` before lookup |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_workspace_for_raises_value_error_for_non_string_before_lookup[int]` | A1 | `workspace_for` called `registry.find_agent(123)` before rejecting non-string `agent_id` |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_workspace_for_raises_value_error_for_non_string_before_lookup[bool]` | A1 | `workspace_for` called `registry.find_agent(True)` before rejecting non-string `agent_id` |
| `tests/r1a/test_a1_live_agent_only.py::test_a1_supervisor_uses_agent_workspace_never_server_or_caller_workspace` | A1 | green on base: regression (`workspace_for` already ignores caller fallback and server workspace) |
| `tests/r1a/test_a2_no_fallback_guard_context.py::test_a2_live_agent_without_guard_context_is_denied[missing]` | A2 | `AssertionError: assert 'allow' == 'deny'` (`workspace_for` falls back to `default_guard_context`) |
| `tests/r1a/test_a2_no_fallback_guard_context.py::test_a2_live_agent_without_guard_context_is_denied[none]` | A2 | `AssertionError: assert 'allow' == 'deny'` (`workspace_for` falls back to `default_guard_context`) |
| `tests/r1a/test_a2_no_fallback_guard_context.py::test_a2_live_agent_without_guard_context_is_denied[empty]` | A2 | `AssertionError: assert 'allow' == 'deny'` (`workspace_for` falls back to `default_guard_context`) |
| `tests/r1a/test_a2_no_fallback_guard_context.py::test_a2_workspace_for_does_not_return_default_guard_context` | A2 | `Failed: DID NOT RAISE ValueError` (`workspace_for` returns `default_guard_context`) |
| `tests/r1a/test_a3_hook_catch_all.py::test_a3_hook_os_getcwd_failure_before_socket_denies_exit_2` | A3 | `approval_hook.py` exits `1` with `FileNotFoundError` from `os.getcwd()` and empty stdout |
| `tests/r1a/test_a3_hook_catch_all.py::test_a3_hook_unexpected_exception_in_main_denies_exit_2[dialect_args0]` | A3 | `RuntimeError` escapes `approval_hook.main()` instead of emitting deny with exit `2` |
| `tests/r1a/test_a3_hook_catch_all.py::test_a3_hook_unexpected_exception_in_main_denies_exit_2[dialect_args1]` | A3 | `RuntimeError` escapes `approval_hook.main()` in codex dialect instead of exit `2` |
| `tests/r1a/test_a4_socket_outside_temp_roots.py::test_a4_temp_session_root_uses_short_socket_root[gettempdir]` | A4 | `AssertionError` (line 44): expected socket under the short root outside temp roots, got a socket under `gettempdir()` (`/private/var/folders/...`) |
| `tests/r1a/test_a4_socket_outside_temp_roots.py::test_a4_temp_session_root_uses_short_socket_root[slash-tmp]` | A4 | `AssertionError` (line 44): expected socket under the short root outside temp roots, got a socket under `/private/tmp` |
| `tests/r1a/test_a4_socket_outside_temp_roots.py::test_a4_symlink_into_temp_root_uses_short_socket_root` | A4 | `AssertionError`: symlink resolving into `/tmp` stays under `/private/tmp` instead of `SHORT_SOCKET_ROOT` |
| `tests/r1a/test_a4_socket_outside_temp_roots.py::test_a4_registry_create_agent_refuses_workspace_containing_socket_dir` | A4 | `Failed: DID NOT RAISE RegistryError` when workspace contains `approval_socket` directory |
| `tests/r1a/test_a5_nonce_memory.py::test_a5_used_nonces_dropped_on_close_cancel_and_evict` | A5 | `AssertionError`: closed agent's id remains in `supervisor._used_nonces` |
| `tests/r1a/test_a5_nonce_memory.py::test_a5_nonce_cap_100000_fails_closed_without_evicting_old_nonces` | A5 | `AssertionError: assert 'allow' == 'deny'` (no 100,000-nonce fail-closed cap) |
| `tests/r1a/test_a6_key_file_removed.py::test_a6_approval_key_deleted_on_close_cancel_and_evict` | A6 | `AssertionError`: `agents/<id>/approval.key` still exists after `agent.close()` |
| `tests/r1a/test_a6_key_file_removed.py::test_a6_closed_agent_can_no_longer_obtain_allow` | A6 | `AssertionError`: `approval.key` is not deleted when agent is closed |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_1_workspace_symlink_to_ssh_is_denied[cat link_ssh]` | A7, A7.1 | `AssertionError: classify_bash('cat link_ssh') returned allow: \`cat\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_1_workspace_symlink_to_ssh_is_denied[cat linkdir/config]` | A7, A7.1 | green on base: regression (`linkdir/config` contains `/` so `is_sensitive` resolves it) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_1_workspace_symlink_to_ssh_is_denied[cat linkdir/id_rsa]` | A7, A7.1 | green on base: regression (`linkdir/id_rsa` resolves under `~/.ssh`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_2_globs_reaching_protected_home_are_denied[cat ~/.ss?/*]` | A7, A7.2 | `AssertionError: classify_bash('cat ~/.ss?/*') returned allow: \`cat\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_2_globs_reaching_protected_home_are_denied[cat ~/.cl*/settings.json]` | A7, A7.2 | `AssertionError: classify_bash('cat ~/.cl*/settings.json') returned allow: \`cat\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_3_indirect_parameter_expansion_is_denied[x=SOME_SECRET_NAME; echo ${!x}]` | A7, A7.3 | `AssertionError: classify_bash(...) returned allow: 2 segments, each allowed by policy` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_3_indirect_parameter_expansion_is_denied[x=OTHER_VAR; echo ${!x}]` | A7, A7.3 | `AssertionError: classify_bash(...) returned allow: 2 segments, each allowed by policy` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_4_ansi_c_quoting_spelling_protected_path_is_denied[cat $'\\x2eenv']` | A7, A7.4 | `AssertionError: classify_bash("cat $'\\x2eenv'") returned allow: \`cat\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_4_ansi_c_quoting_spelling_protected_path_is_denied[cat $'\\x7e/.ssh/id_rsa']` | A7, A7.4 | `AssertionError: classify_bash(...) returned escalate: names a path that cannot be resolved` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_5_dev_tcp_and_udp_redirects_are_denied[cat < /dev/tcp/1.2.3.4/80]` | A7, A7.5 | `AssertionError: classify_bash('cat < /dev/tcp/1.2.3.4/80') returned allow: \`cat\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_5_dev_tcp_and_udp_redirects_are_denied[exec 3<>/dev/tcp/1.2.3.4/80]` | A7, A7.5 | green on base: regression (`<>` already caught by `redirect_targets_outside`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_5_dev_tcp_and_udp_redirects_are_denied[cat < /dev/udp/1.2.3.4/53]` | A7, A7.5 | `AssertionError: classify_bash('cat < /dev/udp/1.2.3.4/53') returned allow: \`cat\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_5_dev_tcp_and_udp_redirects_are_denied[echo hi > /dev/udp/1.2.3.4/53]` | A7, A7.5 | green on base: regression (`>` already caught by `redirect_targets_outside`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_6_ps_environment_forms_are_denied[ps -Eww]` | A7, A7.6 | `AssertionError: classify_bash('ps -Eww') returned allow: \`ps\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_6_ps_environment_forms_are_denied[ps eww]` | A7, A7.6 | `AssertionError: classify_bash('ps eww') returned allow: \`ps\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_6_ps_environment_forms_are_denied[ps auxe]` | A7, A7.6 | `AssertionError: classify_bash('ps auxe') returned allow: \`ps\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_6_ps_environment_forms_are_denied[ps -E]` | A7, A7.6 | `AssertionError: classify_bash('ps -E') returned allow: \`ps\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_7_recursive_search_over_home_is_denied` | A7, A7.7 | `AssertionError: classify_bash('grep -r BEGIN ~') returned allow: \`grep\` is read-only` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[declared_secret-python3]` | A7, A7.8 | `AssertionError: classify_bash('python3 secret.tok') returned allow: \`python3\` running workspace code` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[declared_secret-node]` | A7, A7.8 | green on base: regression (`shell_paths` already checks `node` operands against `secret_files`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[declared_secret-ruby]` | A7, A7.8 | green on base: regression (`shell_paths` already checks `ruby` operands against `secret_files`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[declared_secret-perl]` | A7, A7.8 | green on base: regression (`shell_paths` already checks `perl` operands against `secret_files`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[declared_secret-bash]` | A7, A7.8 | green on base: regression (`shell_paths` already checks `bash` operands against `secret_files`) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[protected_home-python3]` | A7, A7.8 | green on base: regression (`~/.ssh/config` contains `/` so `is_sensitive` catches it) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[protected_home-node]` | A7, A7.8 | green on base: regression (`~/.ssh/config` contains `/` so `is_sensitive` catches it) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[protected_home-ruby]` | A7, A7.8 | green on base: regression (`~/.ssh/config` contains `/` so `is_sensitive` catches it) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[protected_home-perl]` | A7, A7.8 | green on base: regression (`~/.ssh/config` contains `/` so `is_sensitive` catches it) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[protected_home-bash]` | A7, A7.8 | green on base: regression (`~/.ssh/config` contains `/` so `is_sensitive` catches it) |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[symlink_to_secret-python3]` | A7, A7.8 | `AssertionError: classify_bash('python3 link_ssh') returned escalate` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[symlink_to_secret-node]` | A7, A7.8 | `AssertionError: classify_bash('node link_ssh') returned escalate` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[symlink_to_secret-ruby]` | A7, A7.8 | `AssertionError: classify_bash('ruby link_ssh') returned escalate` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[symlink_to_secret-perl]` | A7, A7.8 | `AssertionError: classify_bash('perl link_ssh') returned escalate` |
| `tests/r1a/test_a7_classifier_holes.py::test_a7_8_interpreter_running_protected_file_is_denied[symlink_to_secret-bash]` | A7, A7.8 | `AssertionError: classify_bash('bash link_ssh') returned escalate` |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[claude_api-True]` | A8, A8.1 | `AssertionError: assert 'allow' == 'deny'` (`CLAUDE_CONFIG_DIR=D` not protected in `guard_context`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[claude_api-False]` | A8, A8.1 | green on base: regression (`~/.claude` already in `SENSITIVE_HOME`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[claude_login-True]` | A8, A8.1 | `AssertionError: assert 'allow' == 'deny'` (`CLAUDE_CONFIG_DIR=D` unprotected outside active run) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[claude_login-False]` | A8, A8.1 | green on base: regression (`~/.claude` already in `SENSITIVE_HOME`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[codex_p-True]` | A8, A8.1 | `AssertionError: assert 'allow' == 'deny'` (`CLAUDE_CONFIG_DIR=D` not protected for `codex`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[codex_p-False]` | A8, A8.1 | green on base: regression (`~/.claude` already in `SENSITIVE_HOME`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[omp_p-True]` | A8, A8.1 | `AssertionError: assert 'allow' == 'deny'` (`CLAUDE_CONFIG_DIR=D` not protected for `omp`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[omp_p-False]` | A8, A8.1 | green on base: regression (`~/.claude` already in `SENSITIVE_HOME`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[dsh_p-True]` | A8, A8.1 | `AssertionError: assert 'allow' == 'deny'` (`CLAUDE_CONFIG_DIR=D` not protected for `dsh`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_1_claude_config_dir_denied_for_all_hook_drivers[dsh_p-False]` | A8, A8.1 | green on base: regression (`~/.claude` already in `SENSITIVE_HOME`) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_2_case_variants_of_claude_config_dir_and_dot_claude_denied` | A8, A8.2 | `AssertionError: assert 'allow' == 'deny'` (case variant of `CLAUDE_CONFIG_DIR` not matched) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_3_recursive_search_and_glob_reaching_claude_config_dir_denied` | A8, A8.3 | `AssertionError: assert 'allow' == 'deny'` (`grep -r` over ancestor of `CLAUDE_CONFIG_DIR` allowed) |
| `tests/r1a/test_a8_claude_config_dir.py::test_a8_4_overlapping_login_runs_keep_protection_when_first_ends` | A8, A8.4 | `AssertionError: assert 'allow' == 'deny'` (`CLAUDE_CONFIG_DIR` not in `guard_context`) |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_darwin_all_hook_drivers_wrapped_even_without_deny_read[claude]` | A9 | `AssertionError`: `claude` argv not wrapped with `/usr/bin/sandbox-exec -p` without `deny_read` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_darwin_all_hook_drivers_wrapped_even_without_deny_read[codex]` | A9 | `AssertionError`: `codex` argv not wrapped with `/usr/bin/sandbox-exec -p` without `deny_read` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_darwin_all_hook_drivers_wrapped_even_without_deny_read[omp]` | A9 | `AssertionError`: `omp` argv not wrapped with `/usr/bin/sandbox-exec -p` without `deny_read` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_darwin_all_hook_drivers_wrapped_even_without_deny_read[dsh]` | A9, A9.3 | `AssertionError`: `dsh` profile allows `file-read*` on other agents' directories under `agents/` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_1_each_driver_exposes_sandbox_write_roots[claude]` | A9, A9.1 | `AssertionError`: `claude` does not expose `sandbox_write_roots` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_1_each_driver_exposes_sandbox_write_roots[codex]` | A9, A9.1 | `AssertionError`: `codex` does not expose `sandbox_write_roots` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_1_each_driver_exposes_sandbox_write_roots[omp]` | A9, A9.1 | `AssertionError`: `omp` does not expose `sandbox_write_roots` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_1_each_driver_exposes_sandbox_write_roots[dsh]` | A9, A9.1 | `AssertionError`: `dsh` does not expose `sandbox_write_roots` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_3_real_seatbelt_blocks_cross_agent_read_and_outside_write[claude]` | A9, A9.3 | `AssertionError`: `claude` argv not wrapped with `/usr/bin/sandbox-exec -p` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_3_real_seatbelt_blocks_cross_agent_read_and_outside_write[codex]` | A9, A9.3 | `AssertionError`: `codex` argv not wrapped with `/usr/bin/sandbox-exec -p` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_3_real_seatbelt_blocks_cross_agent_read_and_outside_write[omp]` | A9, A9.3 | `AssertionError`: `omp` argv not wrapped with `/usr/bin/sandbox-exec -p` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_3_real_seatbelt_blocks_cross_agent_read_and_outside_write[dsh]` | A9, A9.3 | `AssertionError`: `dsh` profile allows `file-read*` on `sibling-agent/secret.txt` |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_antigravity_keeps_sandbox_flag` | A9 | green on base: regression (`antigravity` argv already includes `--sandbox`) |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_off_macos_behaves_as_base_without_deny_read[claude]` | A9 | green on base: regression (`claude` off-macOS without `deny_read` is unwrapped) |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_off_macos_behaves_as_base_without_deny_read[codex]` | A9 | green on base: regression (`codex` off-macOS without `deny_read` is unwrapped) |
| `tests/r1a/test_a9_os_sandbox_drivers.py::test_a9_off_macos_behaves_as_base_without_deny_read[omp]` | A9 | green on base: regression (`omp` off-macOS without `deny_read` is unwrapped) |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_config_network_allow_loads_and_defaults` | A10 | `AttributeError: 'ProviderConfig' object has no attribute 'network_allow'` |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_1_sandbox_network_loopback_ports_and_external_deny` | A10, A10.1 | `TypeError: build_profile() got an unexpected keyword argument 'network_loopback_ports'` |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_2_child_env_sets_proxy_vars_and_no_proxy` | A10, A10.2 | `AssertionError`: child env lacks `HTTPS_PROXY` pointing at `http://127.0.0.1:<port>` |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_3_egress_proxy_connect_and_plain_http_allow_and_refusal` | A10, A10.3 | `ModuleNotFoundError: No module named 'subagent.egress'` |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_3_e2e_fake_claude_refused_connect_writes_egress_refused_trace` | A10, A10.3 | `AssertionError`: `fake_claude` failed with `KeyError: 'HTTPS_PROXY'` (no proxy running/wired) |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_4_proxy_starts_on_first_spawn_and_stops_on_close` | A10, A10.4 | `AssertionError: assert [False] == [True]` (no proxy listening during spawn) |
| `tests/r1a/test_a10_egress_proxy.py::test_a10_5_doctor_allowlist_cloud_warning_examples_and_vendor_map` | A10, A10.5 | `AssertionError`: `doctor --json` provider row has neither `network_allow` nor `allowlist` |
| `tests/r1a/test_a11_codex_egress_block.py::test_a11_codex_uses_danger_full_access_only_when_wrapped_with_network_deny` | A11 | `AssertionError`: codex on macOS is not wrapped with `sandbox-exec` / A10 network deny |
| `tests/r1a/test_a11_codex_egress_block.py::test_a11_codex_argv_method_keeps_workspace_write_even_with_deny_read_off_macos` | A11 | `AssertionError: assert 'danger-full-access' == 'workspace-write'` off macOS |
| `tests/r1a/test_a12_dsh_bootstrap.py::test_a12_dsh_cordis_tamper_denies_remaining_requests_and_traces_guard_tamper` | A12 | `AssertionError` (line 164): `tampered cordis.yml must deny`, base returned `'allow'` (reason `read-only tool`) instead of `'deny'`; sync folder is now inside the workspace so the fake child reaches the tamper step |
| `tests/r1a/test_a13_classifier_freeze.py::test_a13_classifier_allowlists_are_subsets_of_base_commit` | A13 | green on base: regression (base allow-lists match frozen `0ba5249` literals) |

## Clauses not exercised end-to-end offline (and why)

- **A14 & A15**: Out of scope for `tests/r1a/` per the brief (A14 is whole-repo `pytest`/`ruff`/rule-2 gating; A15 is live smoke/egress/MCP-shaped runs).
- **Live nested `sandbox-exec` in `test_a9_3` / `test_a10_1` / `test_a10_3_e2e` / `test_a12`**: macOS `sandbox-exec` cannot nest inside an already-sandboxed runner (`sandbox_apply: Operation not permitted`), so those tests verify the SBPL rules offline and run the live `sandbox-exec` probe only when `sandbox-exec` can initialize (full live enforcement runs when pytest is invoked from an unsandboxed macOS shell).
- **A12 `AF_UNIX` hook transport in sandboxed runners**: When `AF_UNIX` `socket.connect` is blocked by an outer sandbox (`PermissionError`), `test_a12` exercises `Supervisor._handle` directly via in-memory `_MemoryStream` alongside the real `DshProcess` readiness handshake.
