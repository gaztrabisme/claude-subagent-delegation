# R7a lifecycle acceptance tests (addendum S45)

Same rules and helpers as `tests/r7a` (`tests/r7a/_helpers.py`). Base `fuse`.

## (d) agent cap error (test_d_agent_cap.py) - all 4 GREEN on base
The base text already satisfies the contract ("agent limit reached (N live). Cancel one with cancel ..." and the
provider variant names the provider). They pin it so it stays that way.
| Test | Asserts |
|---|---|
| test_d_create_agent_at_the_core_cap_names_cap_count_and_cancel | `RegistryError`, text has the cap number, "live", "cancel" |
| test_d_create_agent_at_the_provider_cap_names_the_provider | text names `glm`, the limit, "cancel" |
| test_d_the_mcp_delegate_tool_at_the_provider_cap_returns_a_named_refusal | MCP `delegate` raises `RegistryError`/`ToolError` with provider, number, "cancel" |
| test_d_the_mcp_delegate_tool_at_the_core_cap_names_cap_and_cancel | same at the core cap; nothing half-created |
At the cap the cap number and the live count are the same number, so one number is asserted.

## (e) idle reap (test_e_idle_reap.py) - all 4 GREEN on base
`Registry.reap_once` already closes an idle, non-busy agent past `idle_timeout` (kind `idle`) and eviction frees its
slot. Not listed in the spec as possibly green; reported as found. Slot count read as the `list` tool does
(`lane_status.snapshot(..., live_by_lane=<non-closed agents>)["glm"]["slots"]["live"]`).
| Test | Asserts |
|---|---|
| test_e_an_agent_idle_past_its_timeout_is_reaped_without_a_cancel | `(id, "idle")` returned, agent closed and gone from `agents()` |
| test_e_a_reaped_agent_frees_its_provider_slot | slots.live 1 -> 0; `create_agent` succeeds again with provider and core cap 1 |
| test_e_an_agent_younger_than_the_timeout_is_not_reaped | nothing acted, still live |
| test_e_a_busy_agent_is_not_reaped_however_old_its_last_activity | working run, not reaped |

## (f) exec edges (test_f_exec_edges.py) - all 3 RED on base
| Test | Asserts | Base failure |
|---|---|---|
| test_f_a_lane_probe_lock_timeout_is_one_failed_trace_record_and_exit_1 | `active_run` patched to raise `TimeoutError`: rc 1, "error" on stderr, no traceback, command not run, exactly one `exec-wrapper` run record, failed | raw `TimeoutError` escapes `cli.main` |
| test_f_a_command_killed_by_a_signal_exits_128_plus_the_signal | `subprocess.run` patched (only for the sentinel command) to return -9: rc 137, trace `exit_code` 137 | rc is -9 |
| test_f_a_symlink_created_under_a_locked_glob_is_a_violation | symlink `contracts/new.json` created after the lock commit: rc 3, path on stderr, `lock == "violated"` | rc 0 (`walk_files` skips symlinks) |
