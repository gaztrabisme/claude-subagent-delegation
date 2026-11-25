# R7a slot release (S65) acceptance tests

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/r7a_slot -q` (11 collected; ruff clean).
Pattern: scripted in-process children, patched `subagent.runs._now` (Clock), `Registry.create_agent`, `trace_records`.
Base result: 8 red, 3 green.

| Test | Red/green on base | Reason |
|---|---|---|
| g_a_finished_agent_idle_past_grace_is_released_for_a_new_one | red | create refused at the lane cap where it should succeed |
| g_an_agent_idle_inside_grace_is_kept_and_the_request_refused | green | refusal is today's behaviour; guards that nothing is released early |
| g_the_default_grace_is_120_seconds_when_the_key_is_omitted | red | `ProviderConfig` has no `slot_release_grace_seconds` |
| g_a_busy_agent_is_never_released_however_old_its_clock | green | refusal is today's behaviour; regression guard |
| g_an_agent_with_a_working_run_after_a_finished_one_is_not_released | green | same; grace 0 so only the working run protects it |
| g_only_as_many_agents_as_needed_are_released_oldest_first | red | create refused at the 2-slot cap |
| g_agents_on_another_lane_are_never_released_for_this_lane | red | glm create refused where its own finished agent should be released |
| g_grace_zero_releases_a_finished_agent_at_once | red | create refused |
| g_a_negative_grace_is_a_config_refusal[-1, -0.5] | red | DID NOT RAISE ConfigError (unknown key is ignored today) |
| g_a_released_agent_cannot_continue_but_its_result_stays_readable | red | create refused, so no release happens |

Notes: the trace key `extra.released_agents` is asserted on the new run's `run` record (matched by run_id).
The three green tests pass today because refusal is the current behaviour; they stay as guards against over-release.
Idle age is measured by moving the patched `_now` after the old run finishes.
