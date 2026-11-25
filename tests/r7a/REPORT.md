# R7a acceptance tests: watchdog, caps, continue after a deadline

Written against base `fuse` (R6 merged). Offline, no sleep above a 10 ms poll. Children are in-process scripted
objects handed to `subagent.providers.claude._spawn_claude`; no fake CLI file was needed. Time-based tests patch
`subagent.runs._now` with `tests/r7a/_helpers.Clock`.

## Load report
Read: AGENTS.md; the R7a row of `wiki/operation-refactor-2026-11.md`; `src/subagent/runs.py` (`_Watch`, `KILL_*`,
`Run.timeout_expired`, `Agent.submit/delegate/follow_up/close`, `_collect`, `_finish_locked`, `Registry.reap_once`,
`create_agent`); `config.py` (`_provider`, `_PROVIDER_KEYS`; unknown provider keys fall into `extra`, so on base an unknown
`no_progress_*` key is silently ignored); `mcp_server.py` (`delegate`, `continue_agent`, `list`);
`telemetry/trace.py` (`run`); `tests/test_runs.py` (`FakeProcess`, `_result`), `tests/test_limits.py`,
`tests/test_config.py` (server fixture), `tests/conftest.py` (`make_settings`, `trace_records` autouse fixture),
`tests/r6/_helpers.py`.

## Interface decisions the tests pin (where the spec was open)
- Config attributes: `ProviderConfig.no_progress_steps` (int) and `ProviderConfig.no_progress_seconds` (float), default 0.
- `Run` has no `as_dict()`; the result is read from `run.detail()` (what the MCP `_result` returns): key `no_progress`
  = `{"reads": [...], "steps": n}`; key `caps` as in the spec.
- `timeout` / `max_steps` are keyword arguments of `Agent.delegate(...)` and of the MCP `delegate` tool. They are NOT
  pinned on `Registry.create_agent` (the spec said "create_agent/Agent.delegate"; the MCP tool calls both, so
  `Agent.delegate` is the one the tests use).
- Watchdog stop point: "after N calls" is accepted at the 5th or the 6th call for `no_progress_steps=5`; the tests
  assert `steps in (5, 6)` and `reads == dedup(first steps paths)`.
- The trace record is read through the autouse `trace_records` fixture (every record any Trace writes); the schema check
  in that fixture also validates the new `extra.overrun` record.
- A deadline is ended by `Registry.reap_once()` in the tests (the reaper is not started); an in-stream check by the
  builder also satisfies them, since they only assert the outcome.
- Own-cap timeout test: `timeout=5` against a provider `run_timeout=30`, clock advanced 60 s.

## (a) watchdog (tests/r7a/test_a_watchdog.py)
| Test | Asserts | Base |
|---|---|---|
| test_a_six_reads_with_a_limit_of_five_stop_the_run | 6 Reads, limit 5: failed, `no_progress`, error names `no_progress_steps` and 5, `no_progress.reads/steps`, child killed | red: no attribute `no_progress_steps` |
| test_a_read_list_is_in_order_and_deduplicated | repeated path listed once, order kept | red: same |
| test_a_a_write_or_edit_satisfies_the_watchdog_for_the_rest_of_the_run[5 tool names] | 3 reads, one of Write/Edit/MultiEdit/NotebookEdit/apply_patch, 10 reads: completed, no `no_progress` | red: no attribute (the config key must be accepted; without that check the test would be green on base) |
| test_a_seconds_variant_stops_a_run_that_only_reads | fake clock, 1 s per event, limit 3.5 s: stopped, error names `no_progress_seconds` | red: no attribute |
| test_a_seconds_variant_is_satisfied_by_a_write | an Edit early: not stopped | red: no attribute |
| test_a_both_keys_default_to_zero_and_never_stop_a_run | defaults 0, 30 reads over 30 simulated minutes: completed | red: no attribute |
| test_a_negative_limits_are_a_config_refusal[2] | negative values raise `ConfigError` naming the key | red: DID NOT RAISE |
| test_a_trace_run_record_names_the_overrun | `finish_reason` and `extra.overrun == {"kind": "no_progress", "limit": 5}` | red: run completed |

## (b) caps (tests/r7a/test_b_caps.py)
| Test | Asserts | Base |
|---|---|---|
| test_b_max_steps_is_the_smaller_of_request_and_provider_limit[4,99,10] | `--max-turns` is the effective value; `caps.max_steps` | red: unexpected keyword `max_steps` |
| test_b_timeout_is_the_smaller_of_request_and_provider_limit[5,500] | deadline lifetime ~ effective; `caps.timeout` | red: unexpected keyword `timeout` |
| test_b_zero_or_negative_requests_are_refused_by_the_registry[4] | `RegistryError` naming the argument, nothing spawned | red: unexpected keyword |
| test_b_omitting_both_keeps_the_provider_defaults | `--max-turns` 10, lifetime ~30, no overrun | **green on base** (regression guard) |
| test_b_a_run_that_hits_its_own_step_cap_records_the_overrun | `steps`, `extra.overrun == {"kind":"steps","limit":2}` | red: unexpected keyword |
| test_b_a_run_that_hits_its_own_timeout_records_the_overrun | `timeout`, `extra.overrun == {"kind":"timeout","limit":5}` | red: unexpected keyword |
| test_b_the_mcp_delegate_tool_takes_timeout_and_max_steps | MCP result `caps`, argv | red: unexpected keyword |
| test_b_the_mcp_delegate_tool_clamps_a_request_above_the_provider_limit | clamped, no error, `caps` shows both | red: unexpected keyword |
| test_b_the_mcp_delegate_tool_refuses_zero_or_negative[2] | `RegistryError`/`ToolError`, nothing spawned | red: unexpected keyword |

## (c) continue after a deadline (tests/r7a/test_c_continue_deadline.py)
| Test | Asserts | Base |
|---|---|---|
| test_c_the_agent_survives_a_deadline_and_continue_resumes_its_session[reaper] | run failed `timeout`; agent not closed; `follow_up` completes; child 2 argv `--resume sess-1`; session ids equal (child echoes the resumed id) | red: agent closed |
| ...[own-cap] | same with `timeout=5` on delegate | red: unexpected keyword `timeout` |
| test_c_the_timed_out_agent_is_still_listed_as_live | still in `reg.agents()` | red: evicted |
| test_c_a_cancelled_run_still_closes_its_agent | cancel closes, `follow_up` raises `RegistryError` | **green on base** (regression guard) |

Green on base: 2 of 33 collected in this folder, both guards named above.
