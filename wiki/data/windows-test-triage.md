# Windows test triage: win-guard-2026-10-11 U2

Reader: the Windows guard coordinator. Baseline: `485e14c`, `wiki/data/windows-pytest-485e14c.txt`.

The baseline contains 145 `FAILED` lines, four `SUBFAILED` entries (two scenarios for each of two test methods), and six `ERROR` lines: 149 failed outcomes plus six errors. Every entry is listed once below; SUBFAILED labels distinguish the four subtest outcomes. All 151 ids on lines beginning FAILED or ERROR are covered exactly once.

Classification names the first evidenced Windows failure, rather than promising the test will pass after a harness fix. Further failures can emerge once startup works. Product bugs are suspected causes, left unmodified and unskipped. No existing assertion was changed, removed, or weakened.

Validation: Python cannot start in this sandbox, so pytest was not run. The repository-local standalone Ruff executable passes on all 15 changed test files. `git diff --check`, baseline/table coverage, and assertion-diff checks pass. External `uv run pytest -q scripts/windows_regression_check.py` and the coordinator suite recount remain pending.

The fake-cli category includes similar harness issues: TOML escaping, shell utilities used for setup, Unicode fixture transport, and no-op acceptance commands. Windows-only true/false stubs run as real subprocesses and preserve their respective exit codes; no product execution or classification is mocked by this port.

## tests/loop/test_loop_autopilot.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_auto_wait_returns_done` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_disputed_test_hands_back_at_once` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_escalates_to_hard_then_stops` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_hard_tier_uses_hard_reviewer_and_wait` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_retry_then_review_fix` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_undo_skips_review_checkpoints` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_unfixed_concerns` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_copilot::test_watch_run_follows_every_round_and_review` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_auto_wait_returns_done` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_disputed_test_hands_back_at_once` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_escalates_to_hard_then_stops` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_hard_tier_uses_hard_reviewer_and_wait` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_retry_then_review_fix` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_undo_skips_review_checkpoints` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_unfixed_concerns` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_autopilot.py::Autopilot_claude::test_watch_run_follows_every_round_and_review` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/loop/test_loop_outline.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_outline.py::Outline_copilot::test_bad_input` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_copilot::test_files_outside_usual_patterns_are_protected` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_copilot::test_fix_pass_for_transcription_errors` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_copilot::test_still_wrong_after_fix_pass_goes_back_to_claude` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_copilot::test_unchanged_outline_is_not_rewritten` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_copilot::test_writes_tests_and_reverts_implementation` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_claude::test_bad_input` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_claude::test_files_outside_usual_patterns_are_protected` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_claude::test_fix_pass_for_transcription_errors` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_claude::test_still_wrong_after_fix_pass_goes_back_to_claude` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_claude::test_unchanged_outline_is_not_rewritten` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_outline.py::Outline_claude::test_writes_tests_and_reverts_implementation` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/loop/test_loop_parallel.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_parallel.py::Parallel_copilot::test_autopilot_fixes_failed_merge` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_parallel.py::Parallel_copilot::test_merge_owned_files_only` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_parallel.py::Parallel_copilot::test_needs_git` | **fake-cli** | Sandbox TOML paths were unescaped and setup invoked rm; TOML/PATH/launchers and shutil removal (including read-only Git objects) ported without changing assertions. |
| `tests/loop/test_loop_parallel.py::Parallel_claude::test_autopilot_fixes_failed_merge` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_parallel.py::Parallel_claude::test_merge_owned_files_only` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_parallel.py::Parallel_claude::test_needs_git` | **fake-cli** | Sandbox TOML paths were unescaped and setup invoked rm; TOML/PATH/launchers and shutil removal (including read-only Git objects) ported without changing assertions. |

## tests/loop/test_loop_projects.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_projects.py::Projects_copilot::test_monorepo_subfolder` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_projects.py::Projects_copilot::test_python_project` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_projects.py::Projects_copilot::test_undo_without_git` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_projects.py::Projects_claude::test_monorepo_subfolder` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_projects.py::Projects_claude::test_python_project` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_projects.py::Projects_claude::test_undo_without_git` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/loop/test_loop_registry.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_registry.py::ContinueSession_copilot::test_continue_reuses_the_agent_session` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_registry.py::ContinueSession_copilot::test_pre_verify_restores_tests_before_the_suite_runs` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_registry.py::ContinueSession_claude::test_continue_reuses_the_agent_session` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_registry.py::ContinueSession_claude::test_pre_verify_restores_tests_before_the_suite_runs` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/loop/test_loop_reviews.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_reviews.py::CodeReview_copilot::test_no_verdict_is_retried_then_unverified` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::CodeReview_copilot::test_review_sees_code_not_tests_and_cannot_edit` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::CodeReview_copilot::test_verdict_given_in_chat_is_recovered` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::CodeReview_claude::test_no_verdict_is_retried_then_unverified` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::CodeReview_claude::test_review_sees_code_not_tests_and_cannot_edit` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::CodeReview_claude::test_verdict_given_in_chat_is_recovered` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_copilot::test_by_hand` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_copilot::test_can_be_disabled` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `SUBFAILED[medium] tests/loop/test_loop_reviews.py::TestReview_copilot::test_missing_tests_never_block` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `SUBFAILED[highmissing] tests/loop/test_loop_reviews.py::TestReview_copilot::test_missing_tests_never_block` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_copilot::test_rerun_after_disagreeing_skips_test_review` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_copilot::test_wrong_test_blocks_then_rereviewed_after_fix` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_claude::test_by_hand` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_claude::test_can_be_disabled` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `SUBFAILED[medium] tests/loop/test_loop_reviews.py::TestReview_claude::test_missing_tests_never_block` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `SUBFAILED[highmissing] tests/loop/test_loop_reviews.py::TestReview_claude::test_missing_tests_never_block` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_claude::test_rerun_after_disagreeing_skips_test_review` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_reviews.py::TestReview_claude::test_wrong_test_blocks_then_rereviewed_after_fix` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/loop/test_loop_rounds.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_disputed_test` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_edited_and_added_tests_are_reverted` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_feedback_file_reaches_worker_verbatim` | **fake-cli** | Harness writes an emoji feedback file using the Windows default encoding, alongside invalid TOML/shebang/Bash setup; cheap TOML/PATH/launchers ported, full Unicode/Bash harness port left active. |
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_good_round` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_hard_tier_falls_back_when_model_unavailable` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_runner_catches_failing_tests` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_copilot::test_test_config_restored_but_other_edits_kept` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_disputed_test` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_edited_and_added_tests_are_reverted` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_feedback_file_reaches_worker_verbatim` | **fake-cli** | Harness writes an emoji feedback file using the Windows default encoding, alongside invalid TOML/shebang/Bash setup; cheap TOML/PATH/launchers ported, full Unicode/Bash harness port left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_good_round` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_hard_tier_falls_back_when_model_unavailable` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_runner_catches_failing_tests` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::Rounds_claude::test_test_config_restored_but_other_edits_kept` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_copilot::test_background_busy_wait` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_copilot::test_background_child_uses_its_own_socket_with_configured_parent_path` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_copilot::test_run_with_wait_in_one_call` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_copilot::test_undo_and_redo` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_claude::test_background_busy_wait` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_claude::test_background_child_uses_its_own_socket_with_configured_parent_path` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_claude::test_run_with_wait_in_one_call` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/loop/test_loop_rounds.py::BackgroundAndUndo_claude::test_undo_and_redo` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/loop/test_loop_units.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/loop/test_loop_units.py::LiveLogFormatting::test_assistant_text_and_tool_use_render` | **product-bug** | src/subagent/loop/events.py LogSink.__init__/write: default Windows file encoding cannot encode the Unicode render markers; left active. |
| `tests/loop/test_loop_units.py::LiveLogFormatting::test_tool_result_marks` | **product-bug** | src/subagent/loop/events.py LogSink.__init__/write: default Windows file encoding cannot encode check/cross markers; left active. |

## tests/test_bench_harness.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_bench_harness.py::test_a_cell_gets_the_skill_the_path_and_a_cell_json` | **product-bug** | bench/run.py venv_bin: looks for subagent without the Windows .exe suffix, so PATH prepend is None; left active. |

## tests/test_cli_doctor.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_cli_doctor.py::test_doctor_table_reports_binary_and_key` | **fake-cli** | Harness writes native binary paths into TOML without escaping backslashes; JSON quoting ported; fake Codex also gets a .cmd launcher. |
| `tests/test_cli_doctor.py::test_doctor_json` | **fake-cli** | Harness writes native binary paths into TOML without escaping backslashes; JSON quoting ported; fake Codex also gets a .cmd launcher. |
| `tests/test_cli_doctor.py::test_doctor_healthy_local` | **fake-cli** | Harness writes native binary paths into TOML without escaping backslashes; JSON quoting ported; fake Codex also gets a .cmd launcher. |

## tests/test_cli_init.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_cli_init.py::test_from_env_codex_on_path` | **fake-cli** | PATH discovery fake codex is a suffixless shebang script; Windows codex.cmd added with the same successful status. |

## tests/test_codex_driver.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_codex_driver.py::test_success_maps_usage_text_and_thread` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_argv_is_sandboxed_and_never_bypasses` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_no_model_means_no_m_flag` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_resume_passes_thread_id` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_usage_limit_is_a_refusal_with_reset` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_context_full_is_failed_not_refused` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_codex_home_is_per_agent_and_isolated` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_trace_marks_guard_per_driver` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_codex_driver.py::test_hook_claude_shape_allows_with_explicit_decision` | **unix-only** | Uses the AF_UNIX supervisor_socket fixture at /tmp; skipped. |
| `tests/test_codex_driver.py::test_hook_codex_shape_allows_silently` | **unix-only** | Uses the AF_UNIX supervisor_socket fixture at /tmp; skipped. |
| `tests/test_codex_driver.py::test_hook_codex_turn_id_detected_without_flag` | **unix-only** | Uses the AF_UNIX supervisor_socket fixture at /tmp; skipped. |
| `tests/test_codex_driver.py::test_hook_codex_deny_has_reason` | **unix-only** | Uses the AF_UNIX supervisor_socket fixture at /tmp; skipped. |
| `tests/test_codex_driver.py::test_hook_codex_apply_patch_asks_per_file` | **unix-only** | Uses the AF_UNIX supervisor_socket fixture at /tmp; skipped. |
| `tests/test_codex_driver.py::test_hook_codex_forwards_workdir_and_payload_cwd` | **unix-only** | Uses the AF_UNIX supervisor_socket fixture at /tmp; skipped. |

## tests/test_config.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_config.py::test_delegate_records_provider_and_fallback` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_config.py::test_omlx_child_gets_its_provider_values_and_no_sampling` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## tests/test_guard.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_guard.py::test_an_escalating_command_names_the_paths_it_touches` | **unix-only** | Asserts literal Unix absolute /etc root paths; Windows resolves them against a drive; skipped. |
| `tests/test_guard.py::test_a_redirect_outside_is_named_even_inside_a_compound_line` | **unix-only** | Asserts literal Unix absolute /etc root paths; Windows resolves them against a drive; skipped. |
| `tests/test_guard.py::test_a_nonexistent_user_home_does_not_crash_the_classifier` | **unix-only** | Requires Unix shell ~user account lookup rather than Windows home substitution; skipped. |

## tests/test_guard_rules.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_guard_rules.py::test_inline_python_naming_a_protected_path_is_denied` | **product-bug** | src/subagent/guard/classify.py _path_spellings/_inline_named_protected: Windows relative backslashes do not match forward slashes in inline source; left active. |

## tests/test_hook.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_hook.py::test_allow_and_deny_from_unix_socket` | **unix-only** | Binds an AF_UNIX supervisor under the Unix /tmp root; skipped. |

## tests/test_limits.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_limits.py::test_a_child_that_finishes_under_the_turn_cap_is_not_relabelled` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_limits.py::test_continue_without_verification_reuses_the_delegates` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_limits.py::test_fair_use_throttle_backs_off_on_its_own_clock` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## tests/test_live_scripts.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_live_scripts.py::test_hook_isolation_check_holds_with_a_fake_claude` | **unix-only** | Fake supervisor and fake hook client require socket.AF_UNIX; skipped. |
| `tests/test_live_scripts.py::test_hook_isolation_check_points_the_provider_at_the_mock` | **unix-only** | Fake supervisor and fake hook client require socket.AF_UNIX; skipped. |
| `tests/test_live_scripts.py::test_hook_isolation_check_takes_a_config_naming_the_provider` | **unix-only** | Fake supervisor and fake hook client require socket.AF_UNIX; skipped. |

## tests/test_providers_copilot.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_providers_copilot.py::test_run_carries_credits_and_resumes_the_minted_session` | **fake-cli** | fake_copilot is a suffixless Python shebang script (WinError 193); .cmd launcher and no-op verification stubs added. |

## tests/test_providers_grok.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_providers_grok.py::test_402_balance_is_a_refusal_that_closes_the_lane` | **fake-cli** | fake_grok cannot replay the 402 fixture as a suffixless shebang script; .cmd launcher added, refusal assertions unchanged. |

## tests/test_rate_limit.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_rate_limit.py::test_rate_limit_retries_then_succeeds` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_rate_limit.py::test_live_fair_usage_429_shape_is_retried` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_rate_limit.py::test_events_drains_stderr_concurrently_and_classifies` | **fake-cli** | Pipe-pressure harness invokes sh and seq, absent on Windows; replaced with Python emitting the same stderr volume, result and exit 3. |

## tests/test_review_fixes.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_review_fixes.py::test_g1_tilde_plus_minus_are_unresolved` | **unix-only** | Requires Unix shell ~user, ~+ and ~- expansion semantics; skipped. |
| `tests/test_review_fixes.py::test_i0a_session_root_is_outside_the_workspace_and_protected` | **product-bug** | src/subagent/guard/classify.py classify_bash/_protected_target: POSIX shlex strips backslashes from the native protected redirect path; left active. |
| `tests/test_review_fixes.py::test_p9_sensitive_path_bypasses` | **product-bug** | src/subagent/guard/classify.py _sensitive_argument/is_sensitive: POSIX tokenization and native path spelling miss a Windows home secret; left active. |
| `tests/test_review_fixes.py::test_p8_uploads_of_a_secret_are_denied` | **product-bug** | src/subagent/guard/classify.py _sensitive_argument: POSIX tokenization strips backslashes from curl @Windows-secret arguments; left active. |
| `tests/test_review_fixes.py::test_r4_loop_strikes_count_per_agent_across_continues` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## tests/test_router.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_router.py::test_router_deepseek_balance_reroutes_from_deepseek` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_refusal_reroutes_to_next_lane[zai_1308]` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_refusal_reroutes_to_next_lane[zai_1310]` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_refusal_reroutes_to_next_lane[zai_1313_exhausted]` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_health_failed_reroutes` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_lane_memory_second_delegate_skips_closed_lane` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_malformed_lane_state_is_empty` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_router.py::test_router_src_never_starts_a_local_model` | **fake-cli** | Source-audit harness invokes Unix grep, absent on Windows; Python recursive regex scan preserves matching output and grep exit statuses. |
| `tests/test_router.py::test_router_codex_usage_limit_then_glm_and_continue_stays` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_router.py::test_router_glm_refused_then_codex_runs_and_continue_resumes_thread` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_router.py::test_router_codex_closed_second_delegate_spawns_no_codex` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_router.py::test_router_codex_message_then_usage_limit_is_a_refusal_that_closes_codex` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |
| `tests/test_router.py::test_router_codex_usage_limit_after_work_fails_without_reroute` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |

## tests/test_runs.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_runs.py::test_completed_verbatim_under_cap` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## tests/test_telemetry.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_telemetry.py::test_admission_is_log_only_by_default` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_telemetry.py::test_admission_under_threshold_records_would_refuse_false` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## tests/test_trace_delegation.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_trace_delegation.py::DelegationTrace_copilot::test_bench_run_id_and_delegation_id_tag_every_record` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/test_trace_delegation.py::DelegationTrace_copilot::test_one_delegation_record_sums_the_run_usage` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/test_trace_delegation.py::DelegationTrace_claude::test_bench_run_id_and_delegation_id_tag_every_record` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |
| `tests/test_trace_delegation.py::DelegationTrace_claude::test_one_delegation_record_sums_the_run_usage` | **fake-cli** | Sandbox emits unescaped Windows paths in TOML and uses shebang fakes; TOML/PATH/launchers ported; Bash scenario scripts still need a noncheap harness port, left active. |

## tests/test_trace_schema.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_trace_schema.py::test_records_from_a_local_lane_run_satisfy_the_schema` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## tests/test_turns.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_turns.py::test_the_result_usage_wins_when_it_is_larger` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_turns.py::test_a_retried_rate_limited_attempt_still_counts` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_turns.py::test_run_record_fields` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_turns.py::test_stream_events_are_not_kept_in_the_attempt` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |
| `tests/test_turns.py::test_codex_turn_gets_the_turn_completed_usage` | **fake-cli** | fake_codex is a suffixless Python shebang script (WinError 193 and missing replay/call records); .cmd launcher and no-op verification stubs added. |

## tests/test_verify.py

| Test id | Category | Reason / disposition |
|---|---|---|
| `tests/test_verify.py::test_exit_zero_passes` | **fake-cli** | The verification harness uses the unavailable Unix true utility; Windows true.cmd/false.cmd stubs preserve real subprocess exit-status checks. |

## U2 report

Verdict: implementation complete within U2 scope; external validation pending. Unclassified entries: **0**. No commits made.

Changed test files: `tests/conftest.py`, `tests/loop/helpers.py`, `tests/loop/test_loop_parallel.py`, `tests/loop/test_loop_rounds.py`, `tests/test_cli_doctor.py`, `tests/test_cli_init.py`, `tests/test_codex_driver.py`, `tests/test_guard.py`, `tests/test_hook.py`, `tests/test_live_scripts.py`, `tests/test_providers_copilot.py`, `tests/test_providers_grok.py`, `tests/test_rate_limit.py`, `tests/test_review_fixes.py`, `tests/test_router.py`. Also this triage file and one appended `wiki/log.md` line.

Remaining harness work: the loop fakes now have launchers, but their scenarios run Bash scripts and Unix utilities; porting that entire scenario language is not cheap. The emoji feedback harness also needs an end-to-end encoding port. These tests remain active. The seven product-bug outcomes remain active for the product-code unit.

Counts (all 155 baseline outcomes):

| Category | Count |
|---|---:|
| unix-only | 14 |
| fake-cli | 134 |
| product-bug | 7 |
| Total | 155 |

For the 151 FAILED/ERROR-line ids alone: unix-only 14, fake-cli 130, product-bug 7. The additional four subtest outcomes are fake-cli.
