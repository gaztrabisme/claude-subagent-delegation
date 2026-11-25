VERDICT: PASS
TESTS: 23
RED_ON_BASE: 23/23

23 collected tests from 15 test functions (parametrized ones expanded). Base is this checkout (3c08b61): R1a and R1a-agy both unbuilt. Run: `uv run --no-sync pytest -q -p no:cacheprovider tests/r1a_agy` gives 23 failed in about 16 s (the longest test, the G3.2 kill check, takes 12 s on base because the fake agy sleeps; it ends in under 2 s once the run is killed). `ruff check tests/r1a_agy` is clean.

| test node id | requirement id | one-line reason it fails on base |
|---|---|---|
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_darwin_argv_starts_with_sandbox_exec_and_keeps_agy_flags | G1 | argv is `agy -p ...` with no `/usr/bin/sandbox-exec -p <profile>` wrap |
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_1_write_roots_and_profile_write_policy | G1.1 | no profile on argv, and no `sandbox_write_roots` on the driver |
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_2_socket_dir_write_denied_and_other_agents_unreadable | G1.2 | no profile, so socket dir and sibling agent dirs are not denied |
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_3_owner_credential_homes_unreadable_but_gemini_home_readable | G1.3 | no profile, so ~/.ssh, ~/.dsh, ~/.codex, ~/.claude, ~/.aws, ~/.config/gh stay readable |
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_4_network_policy_and_proxy_env | G1.4 | child env has no HTTPS_PROXY/HTTP_PROXY/ALL_PROXY/NO_PROXY; no network rules |
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_4_e2e_fake_agy_uses_proxy_allowlist_and_refusal_is_traced | G1.4 | fake agy finds no HTTPS_PROXY (no proxy started), exits 1, no egress_refused record |
| tests/r1a_agy/test_g1_outer_profile.py::test_g1_off_macos_antigravity_refuses_to_start | G1 | with platform patched to linux the driver still spawns agy instead of refusing |
| tests/r1a_agy/test_g2_bypass_confined.py::test_g2_real_profile_confines_a_bypassing_child | G2 | no sandbox-exec profile on the spawn argv to confine a bypassing child |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_1_true_values_emit_exactly_one_sandbox_bypass_record (4 params: True, "true", "TRUE", "True") | G3.1 | translator ignores BypassSandbox: zero sandbox_bypass records |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_1_any_step_state_is_flagged (PENDING, DONE) | G3.1 | no record and no sandbox_bypass finish_reason for a non-ACTIVE bypassing step |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_1_command_is_clipped_to_500_characters | G3.1 | no sandbox_bypass record exists to check |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_2_run_is_killed_and_failed_with_finish_reason | G3.2 | run is not killed: it waits out the fake agy's 30 s sleep and never ends |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_2_later_steps_are_not_translated_as_work | G3.2 | run completes; state is "completed" and later steps are translated as work |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_3_result_text_names_the_bypass | G3.3 | result text is the agent's own response, with no mention of a bypass |
| tests/r1a_agy/test_g3_bypass_flagged.py::test_g3_negative_false_or_absent_changes_nothing (3 params: false, "false", absent) | G3 | the G3 behaviour here is unchanged on base; the test also asserts the child was spawned under the outer profile (G1), which base lacks |
| tests/r1a_agy/test_g4_guard_label.py::test_g4_guard_reports_sandbox_on_macos | G4 | `guard()` returns "none" |
| tests/r1a_agy/test_g4_guard_label.py::test_g4_doctor_shows_server_profile_as_boundary | G4 | doctor row for antigravity carries no boundary/guard field naming the server profile |

Tests green on base: none. The three G3 negative cases would pass on base on their G3 content alone (nothing is flagged because nothing is ever flagged); each also asserts the spawned argv is wrapped in the outer profile, so they are red on base and stay a real check afterwards (a correct build must keep benign steps running under the profile).

Platform: `test_g3_*` and the G1 end-to-end test skip off darwin (the driver refuses there). The G2 test is darwin-only and returns early after the argv check when this process cannot nest `sandbox-exec` (the R1a A9 pattern). I checked that the fake agy script runs and writes `pid.txt` under a real Seatbelt profile built with `sandbox.build_profile`.

## Ambiguities

1. Trace key: the fixed interface says `{"type": "sandbox_bypass", ...}`; `Trace.write(kind, ...)` writes `"kind"`. R1a's locked A10 test reads `type`. My helper accepts a record whose `type` or `kind` equals the name, then checks `agent_id`, `run_id`, `provider`, `tool`, `command`, `ts`. If the builder uses `Trace.write`, `tests/test_trace_schema.py` `REQUIRED` needs the new kind, or the autouse `trace_records` fixture fails at teardown with "unknown kind" (same applies to R1a's `egress_refused`). That file is not locked; it is the builder's to extend.
2. `record["provider"]`: tested as the provider (lane) name ("gemini"), as R1a's A10 test does for `egress_refused`, not the vendor.
3. G3.1 "the translator emits": the translator has no trace handle, so the record is tested end to end through a real run with a fake agy, not on `Translator.feed`. "Exactly one" is tested with an ACTIVE and a DONE step for the same bypassing call, followed by more events in the same pipe. Per-step translator output for the bypass step itself (whether a `tool_use` event is still emitted) is unspecified and not asserted; the test only requires `run.usage.steps <= 1`.
4. G3.2 "state is failed": tested as `run.state == "failed"`, `run.finish_reason == "sandbox_bypass"`, and the agy pid gone within 5 s. The 12 s wait is for the fake's 30 s sleep; any correct kill ends it within about 2 s.
5. G3.3 "names the bypass": tested as a case-insensitive "bypass" in `run.result_text` or `run.final_response`. The exact wording is the builder's choice; `run.error` is not accepted alone.
6. G3 "any state": tested with PENDING, DONE and ACTIVE (the state names come from the fixtures; PENDING is assumed to be a possible state and the translator is expected to read the parameters before looking at the state).
7. G1.1 "each with a reason in `sandbox_write_roots`": `sandbox_write_roots` returns `list[str]`, so the reason comments cannot be seen through public behaviour; untested. Own-agent writability is tested as `<session_root>/agents/<agent_id>/scratch.txt` being writable, which assumes the agent state directory is that path (as R1a A9.3 does for reads). The temp directory root is not asserted (its path is not fixed).
8. G1.1 runtime state: tested as `$HOME/.gemini/antigravity-cli` with HOME and `Path.home()` both pointed at a temp home. The "or the directory the driver resolves" alternative is not testable without the driver's resolution rule. Writes elsewhere under `~/.gemini` are not asserted either way (the accepted limit says agy writes its login there).
9. G1.3 "every other provider home the server protects": the server's list is not fixed, so only the six named homes are tested, plus that `~/.gemini` and `~/.gemini/antigravity-cli` stay readable.
10. G1.4: the loopback `base_url` port (8123 in the test) is checked only as appearing in the profile text; the proxy port likewise. Real network denial is tested in G2 (192.0.2.1 refused, proxy port reachable). The G1.4 end-to-end test uses `network_allow = ["127.0.0.1:<port>"]` with a local origin because no real host can be reached offline.
11. G1 off macOS: "refuses to start" does not fix where. The test accepts a raised error at agent creation or submit, or a run that ends failed, and requires that no child process is ever started.
12. G4 doctor: no JSON key is fixed for "boundary". The test flattens the antigravity row (minus name, driver, vendor, binary fields) and requires some string value equal to "sandbox", or containing "server profile" or "sandbox-exec". A builder that names the boundary differently would have to align with the manager on a key; a test change request is the route.
13. G5 (suite, ruff, grep) and G6 (live smoke) are not covered here (regression and manual, as the list says). The existing `tests/test_providers_antigravity.py` guard-is-none and argv-equality tests will need a test change request after the build.
