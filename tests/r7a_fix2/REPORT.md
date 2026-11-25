# R7a fix round 2 tests (tests/r7a_fix2/test_k_fix_round2.py)

Offline, in-process; service-job fake driver (`cancel` capability, stream that ignores kill) via patched
`providers.for_driver`; `Agent.END_RUN_WAIT` patched to 0.2 s. Base: ref/r7a (1afb1cf). 7 tests, all red; ruff clean;
tests/r7a_fix still 15 passed.

| Test | Asserts | Base |
|---|---|---|
| k1_an_agent_whose_stuck_close_returned_is_reaped_on_a_later_pass | after the stuck close thread returned, next pass idle-closes the agent and drops it from `reap_stuck` | red: agent skipped forever, `acted == []` |
| k1_an_error_the_stuck_thread_raised_is_logged_naming_the_agent | the late `RuntimeError` is logged (WARNING+) naming the agent id | red: nothing logged |
| k2_a_later_cancel_does_not_write_a_second_run_record_or_change_the_outcome | one trace `run` record; state/finish_reason stay failed/timeout | red: two records, second `cancelled/cancel` |
| k3_the_lane_records_one_observation_for_that_run | one run observation (`["timeout"]`) | red: `["timeout", "cancel"]` |
| k4_a_deadline_ended_run_is_finished_by_the_server_when_the_child_ignores_kill | plain driver, stream never drains: run done, failed/timeout, agent not busy, `_active_lane` None | red: run left WORKING, done unset |
| k4_the_stuck_agent_does_not_hold_its_lane_slot | a new agent on the one-slot provider is admitted (old one released) | red: "agent limit reached on provider" |
| k5_a_refused_submit_does_not_lose_the_released_agents_list | submit refused at the locked re-check, retry still carries `released_agents == [old id]` | red: list is `[]` |

## K4 choice
Pinned "the server finishes the run itself after end_run's bounded wait", plus slot freed. A "continue starts while the
stuck worker is still blocked" test was NOT written: the agent's single serial worker thread is blocked inside the
stream iterator, so a continue can only start by replacing the worker thread, a larger design change. Slot freedom is
tested through `create_agent` releasing the finished agent (its close joins the worker up to 8 s, so the test releases the
stuck stream 0.5 s in).

## Interface notes for the fix
- `Registry.reap_stuck` becomes `{agent_id: thread}`. The thread of a stuck pass is found in tests by its name
  `sam-reap-<agent_id>`, so keep that thread name.
- CONFLICT to resolve: tests/r7a_fix/test_h_fix_round.py:329 asserts `reg.reap_stuck == set()`. A plain empty dict is
  not equal to `set()`. Either make the mapping a dict subclass that compares equal to an empty set, or change that one
  assertion to `len(reg.reap_stuck) == 0` (a weakening-free edit, still needs the test-change route). `in` and `len`
  usages elsewhere (line 348) work with a dict.
- K1 error log: any WARNING+ record naming the agent id with the exception text (message or traceback) satisfies it,
  whether logged by the thread itself or by the next pass.
- K5 test reaches into `agent._wake` and `agent._closing` to land the claim before the re-check; renaming them breaks it.
