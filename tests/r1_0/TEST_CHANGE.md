VERDICT: PASS
CHANGED_TESTS: 15 (14 named protocol tests + 1 unmapped-omp /tmp socket fix; 16 pytest items; plus 1 revised Q1 test, 2 r1_0 defect fixes, and gates/r1-0.sh)
PASS_ON_BUILD: 15/15 (16/16 pytest items on 23713ee; tests/r1_0 is 31/32 with 1 expected pre-builder Q1 failure)
FAIL_ON_BASE: 14/15 (15/16 pytest items; 14/14 old-protocol tests fail on efe7d40; revised Q1 test also fails on efe7d40)

## Shared Helper

- `tests/signed_supervisor.py`: added `write_key_file`, `sign_request`, `private_socket_path`, and `signed_supervisor_socket` using `subagent.guard.approval_auth.{sign,valid_nonce,verify}` (with a `ModuleNotFoundError` fallback only when running against pre-R1-0 base commit `efe7d40` that lacks `approval_auth`).

## Restored Tests (New Q1 Home-Fallback Design)

Restored to committed versions (removed from change set via `git checkout -- tests/test_config.py tests/test_core.py tests/test_lane_status_review.py`):

- `tests/test_config.py::test_default_approval_socket_falls_back_when_tmpdir_path_is_too_long`: expected failure on current build `23713ee` (`AttributeError: module 'subagent.config' has no attribute 'SHORT_SOCKET_ROOT'`) until builder restores `SHORT_SOCKET_ROOT` under the user's real home.
- `tests/test_core.py::test_in_process_server_uses_short_socket_for_long_session_root`: expected failure on current build `23713ee` (`AttributeError: module 'subagent.config' has no attribute 'SHORT_SOCKET_ROOT'`).
- `tests/test_lane_status_review.py::test_review_5_probe_stops_promptly_on_shutdown`: expected failure on current build `23713ee` (`subagent.config.ConfigError: approval socket path exceeds the Unix socket limit`) until builder implements the `SHORT_SOCKET_ROOT` home fallback.

## Changed Existing Tests

| Test | What changed | Assertions (kept / added / removed) | Result on build (`23713ee`) | Result on base (`efe7d40`) |
|---|---|---|---|---|
| `tests/test_hook.py::test_allow_and_deny_from_unix_socket` | Write 0600 key file, pass `--key-file` to `APPROVAL_HOOK`, serve via `private_socket_path` + `signed_supervisor_socket`. | 5 kept / 2 added (`len(seen) == 2`, `verify(key, req)`) / 0 removed | PASS | FAIL (`tests/test_hook.py:69: assert 0 == 2`) |
| `tests/test_codex_driver.py::test_hook_claude_shape_allows_with_explicit_decision` | Updated shared `supervisor_socket` and `_hook` to create a 0600 key file, pass `--key-file`, use `private_socket_path`, and run `signed_supervisor_socket`. | 4 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_codex_driver.py:588: assert 2 == 0`, `permissionDecisionReason: "denied: invalid approval request"`) |
| `tests/test_codex_driver.py::test_hook_codex_shape_allows_silently` | Uses updated `supervisor_socket` and `_hook` with `--key-file` and signed listener. | 3 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_codex_driver.py:608: assert 2 == 0`, `permissionDecisionReason: "denied: invalid approval request"`) |
| `tests/test_codex_driver.py::test_hook_codex_turn_id_detected_without_flag` | Uses updated `supervisor_socket` and `_hook` with `--key-file` and signed listener. | 4 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_codex_driver.py:616: assert (2 == 0)`, `permissionDecisionReason: "denied: invalid approval request"`) |
| `tests/test_codex_driver.py::test_hook_codex_deny_has_reason` | Uses updated `supervisor_socket` and `_hook` with `--key-file` and signed deny reply. | 3 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_codex_driver.py:629: AssertionError: assert 'denied: invalid approval request' == 'nope'`) |
| `tests/test_codex_driver.py::test_hook_codex_apply_patch_asks_per_file` | Uses updated `supervisor_socket` and `_hook` with `--key-file` and signed allow+deny replies. | 3 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_codex_driver.py:643: AssertionError: assert [] == [{'file_path': 'a.py'}, {'file_path': '../outside.py'}]`) |
| `tests/test_codex_driver.py::test_hook_codex_forwards_workdir_and_payload_cwd` | Uses updated `supervisor_socket` and `_hook` with `--key-file` and signed listener. | 3 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_codex_driver.py:672: assert 2 == 0`, `permissionDecisionReason: "denied: invalid approval request"`) |
| `tests/test_providers_omp.py::test_hook_blocks_omp_write_to_a_protected_test_path` | Use `private_socket_path`, write 0600 `approval.key` under `session_root/agents/hook-agent/`, update `_classification_socket` to `signed_supervisor_socket`. | 13 kept / 1 added (`SUBAGENT_GUARD_KEY_FILE == key_path`) / 0 removed | PASS | FAIL (`tests/test_providers_omp.py:286: KeyError: 'SUBAGENT_GUARD_KEY_FILE'`) |
| `tests/test_providers_omp.py::test_hook_maps_ast_edit_paths_to_multi_edit_before_classification` | Use `private_socket_path`, write 0600 `approval.key` under `session_root/agents/ast-agent/`, pass `key` to `_classification_socket`. | 5 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_providers_omp.py:338: assert 0 == 1`) |
| `tests/test_providers_omp.py::test_hook_denies_unmapped_omp_tool_without_asking_supervisor` | **Outside approved list**: switched `approval_socket` from `/tmp/omp-...` to `private_socket_path` because `gates/r1-0-tchange-check.sh` runs `tests/test_providers_omp.py` as a whole file and Q1's `prepare_private_directory` rejects `/tmp`. | 3 kept / 0 added / 0 removed | PASS | PASS (denies in `omp_hook.ts` before opening socket) |
| `tests/test_providers_omp.py::test_hook_maps_other_omp_write_tools_into_classifier_shapes` (`lsp-rename-file`, `eval-inline-code`) | Use `private_socket_path`, write 0600 `approval.key` under `session_root/agents/mapped-agent/`, pass `key` to `_classification_socket`. | 5 kept / 0 added / 0 removed (per param) | PASS (2/2) | FAIL (2/2: `tests/test_providers_omp.py:422: assert 0 == 1`) |
| `tests/test_providers_omp.py::test_real_omp_hook_blocks_protected_write_against_local_stub` | Use `private_socket_path`, write 0600 `approval.key` under `session_root/agents/real-hook-agent/`, pass `key` to `_classification_socket`. | 10 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_providers_omp.py:462: assert 0 == 1`) |
| `tests/test_dsh_guard_bridge.py::test_bridge_blocks_protected_test_write_and_allows_workspace_read_through_real_approval_hook` | Write 0600 key file, pass `--key-file` in `SUBAGENT_GUARD_HOOK`, use `private_socket_path`, update `_classification_socket` to `signed_supervisor_socket`. | 10 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_dsh_guard_bridge.py:688: AssertionError: assert ('test' in 'denied: invalid approval request' or 'read-only' in 'denied: invalid approval request')`) |
| `tests/test_providers_dsh_login.py::test_login_protects_owner_dsh_home_through_bridge_during_run_and_releases_after` | Write 0600 key file, pass `--key-file` in `SUBAGENT_GUARD_HOOK`, use `private_socket_path`, pass `key` to `_classification_socket`. | 15 kept / 0 added / 0 removed | PASS | FAIL (`tests/test_providers_dsh_login.py:911: assert False is True`) |
| `tests/test_dsh_review4.py::test_r4_4_registry_agent_guard_context_carries_provider_homes_and_denies_reads` | Sign both `_send_guard_request` payloads with `sign_request(agent.approval_key, ...)` and verify signed replies carry echoed nonce and non-auth denial reason. | 6 kept / 6 added (nonce, `verify`, non-auth reason for both replies) / 0 removed | PASS | FAIL (`tests/test_dsh_review4.py:393: AttributeError: 'Agent' object has no attribute 'approval_key'`) |

## Revised `tests/r1_0` Q1 Test and Defect Fixes

- `tests/r1_0/test_q1_socket_location.py::test_q1_too_long_session_root_raises_config_error_without_temp_fallback`: updated for the new Q1 home-fallback design — allows either `ConfigError` or a fallback socket shorter than `UNIX_SOCKET_PATH_MAX` outside `tempfile.gettempdir()`, `/tmp`, `/private/tmp`, `/var/folders`, and `/private/var/folders` (both path and `.resolve()`) whose parent is a non-symlink `0700` directory owned by `os.getuid()`, cleans up any directory created under `SHORT_SOCKET_ROOT` only if this test created it, and asserts `ConfigError` when `config.SHORT_SOCKET_ROOT` is monkeypatched to `Path("/tmp")`.
  - Result on current build (`23713ee`): FAIL (expected until builder implements `SHORT_SOCKET_ROOT`: `tests/r1_0/test_q1_socket_location.py:135: AttributeError: module 'subagent.config' has no attribute 'SHORT_SOCKET_ROOT'`).
  - Result on base (`efe7d40`): FAIL (`tests/r1_0/test_q1_socket_location.py:115: AssertionError: fallback approval_socket .../T/subagent-approval-...sock must not be under ...`).
- `(a)` `tests/r1_0/test_q5_hook_and_drivers.py::test_q5_guard_hook_command_and_all_hook_drivers_carry_key_file`: added `max_agents: int = 4` parameter to `_make_driver_settings` and passed `max_agents=8` (PASS on `23713ee`).
- `(b)` `tests/r1_0/test_q7_process_tree.py`: added early `if "--version" in sys.argv[1:]: print("1.0.0-fake", flush=True); sys.exit(0)` in `_FAKE_CLI_TEMPLATE` (`test_q7_cancelled_run_kills_setsid_and_double_fork_descendants` PASS on `23713ee`).
- `(c)` `gates/r1-0.sh`: added `TMPDIR="$TMPDIR"` to the `env -i` invocation (`test_q6_dsh_profile_and_env_use_per_agent_tmp_only[api-key|login]` PASS on `23713ee`).

## `git diff --stat`

```text
 gates/r1-0.sh                          |   2 +-
 tests/r1_0/test_q1_socket_location.py  |  51 ++++++++-
 tests/r1_0/test_q5_hook_and_drivers.py |   7 +-
 tests/r1_0/test_q7_process_tree.py     |   4 +
 tests/test_codex_driver.py             |  49 +++-----
 tests/test_dsh_guard_bridge.py         |  76 ++++---------
 tests/test_dsh_review4.py              |  17 ++-
 tests/test_hook.py                     |  52 ++++-----
 tests/test_providers_dsh_login.py      |  94 ++++++++--------
 tests/test_providers_omp.py            | 199 +++++++++++++++------------------
 10 files changed, 267 insertions(+), 284 deletions(-)
```

## Revision 2

- **Test**: `tests/test_config.py::test_default_approval_socket_falls_back_when_tmpdir_path_is_too_long`
- **What changed**: Kept `SHORT_SOCKET_ROOT` monkeypatch to `<repo>/.t`, `XDG_CONFIG_HOME` isolation, and `.t` cleanup (`shutil.rmtree(fallback_root, ignore_errors=True)`). Removed the `config.tempfile.gettempdir` monkeypatch and instead configured `[core].session_root` in `socket.toml` to `tmp_path / ("s" * 110)` so `<session_root>/s/a<pid>.sock` exceeds `config.UNIX_SOCKET_PATH_MAX`. Imported `tempfile` in `tests/test_config.py`.
- **Assertions**: 4 kept / 1 added / 0 removed.
  - Kept unchanged: `socket_path.parent == per_user_dir`, `socket_path.name.startswith("a-") and socket_path.suffix == ".sock"`, `len(os.fsencode(socket_path)) < 104`, `stat.S_IMODE(per_user_dir.stat().st_mode) == 0o700`.
  - Added: `assert not socket_path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())`.
  - Removed: 0.
- **Result on base (`efe7d40`)**: FAIL (`PYTHONPATH=.scratch/base-efe7d40/src uv run --no-sync pytest -q -p no:cacheprovider "tests/test_config.py::test_default_approval_socket_falls_back_when_tmpdir_path_is_too_long"`):
  `tests/test_config.py:82: AssertionError: assert PosixPath('/var/folders/yy/s3_y_d5x2_34qb0k9f6qvjqr0000gn/T') == PosixPath('/Users/owner/src/mcp/subagent-mcp-wt/ref-r1-0-tchange/.t/subagent-501')`
- **Gate check (`bash gates/r1-0-tchange-check.sh`) on `76a780d` (`src` at `23713ee`)**: `105 passed, 4 failed` (exit 1) — the 4 expected pre-builder Q1 `SHORT_SOCKET_ROOT` failures (`test_q1_too_long_session_root_raises_config_error_without_temp_fallback`, `test_default_approval_socket_falls_back_when_tmpdir_path_is_too_long`, `test_in_process_server_uses_short_socket_for_long_session_root` with `AttributeError: ... has no attribute 'SHORT_SOCKET_ROOT'`, and `test_review_5_probe_stops_promptly_on_shutdown` with `ConfigError`), since `src/subagent/config.py` on `23713ee` does not yet define `SHORT_SOCKET_ROOT`.
