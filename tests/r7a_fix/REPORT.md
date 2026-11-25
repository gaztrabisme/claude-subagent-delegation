# R7a fix-round tests (tests/r7a_fix/test_h_fix_round.py)

15 tests collected; on base 14 red, 1 green (guard). Ruff clean. Other r7a folders: 54 pass, 1 known failing.

| Test | Base result and reason |
|---|---|
| h1_a_deadline_ended_run_still_working... | red: second reap_once closes the agent (`closed` is True) |
| h2_a_submit_landing_before_the_release_is_not_cancelled | red: new run cancelled by "released for a new run" |
| h2_the_create_agent_that_lost_the_race_raises_a_cap_error | red: DID NOT RAISE RegistryError (new agent created) |
| h2_submit_on_an_agent_claimed_for_release_raises | red: submit succeeded before close ("submitted") |
| h3_a_bad_cap_is_refused_before_any_idle_agent_is_released x3 (timeout 0, -2, max_steps 0) | red: idle agent was closed before the cap check |
| h4_non_finite_nan_caps_are_refused[timeout nan] | red: DID NOT RAISE |
| h4_non_finite_nan_caps_are_refused[max_steps nan] | red: ValueError, not RegistryError |
| h4_infinite_timeout_is_clamped | GREEN on base: min(inf, run_timeout) already clamps; regression guard only |
| h4_infinite_max_steps_is_clamped | red: OverflowError from int(inf) |
| h5_a_fallback_hop_to_a_lower_step_lane... | red: hop 2 gets --max-turns 8, expected 3 (driven offline with two fake-claude lanes) |
| h6_a_failing_close_does_not_stop_the_pass | red: the exception escapes reap_once |
| h6_the_close_bound_defaults_to_twenty_seconds | red: no `reap_close_timeout` / `reap_stuck` attributes |
| h6_a_hanging_close_is_bounded... | red: pass blocked the full 6 s on the hung close |

Interfaces the fix must provide: `Registry.reap_close_timeout` (default 20, instance-settable), `Registry.reap_stuck` (set of agent ids), release claim made under the registry lock so `Agent.submit` raises RegistryError once claimed.
