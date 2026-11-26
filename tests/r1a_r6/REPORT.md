VERDICT: PASS (tests written; all red on base as intended)
TESTS: 14 (in tests/r1a_r6/test_r6_homes_and_allowlist.py; helpers in _lanes.py)
RED_ON_BASE: 14 of 14 fail on 7f8e7ea; 0 pass, 0 skipped (Seatbelt nests on this machine, so the live probes ran)

Run: `UV_CACHE_DIR=/private/tmp/subagent-uv-cache uv run --no-sync pytest -q -p no:cacheprovider tests/r1a_r6`

Setup: HOME and Path.home() point at a tmp owner home holding fake ~/.claude (settings, CLAUDE.md, credentials,
statsig, todos, shell-snapshots, projects/*/memory), ~/.claude.json[.backup], ~/.codex, ~/.omp, ~/.dsh and loose files.
Each lane builds the real spawn argv and env through Registry (Process._start captured, nothing runs). Lanes:
claude-api, claude-login, codex, omp, dsh-api, dsh-login. Seatbelt probes use real sandbox-exec and skip with a
reason when it cannot nest. `<ws>/.codex/` exists in the workspace fixture.

| Test | H-id | Why it fails on base |
|---|---|---|
| test_h1_live_seatbelt_write_allowlist[claude-api] | H1 | `<ws>/.codex/*` is writable (workspace root covers it) |
| test_h1_live_seatbelt_write_allowlist[claude-login] | H1 | owner `~/.claude/{.credentials.json,statsig,todos,debug}` writable; `<ws>/.codex/*` writable |
| test_h1_live_seatbelt_write_allowlist[codex] | H1 | `<ws>/.codex/*` writable; `agents/<id>/` itself is not writable (only its subdirs) |
| test_h1_live_seatbelt_write_allowlist[omp] | H1 | `<ws>/.codex/*` writable |
| test_h1_live_seatbelt_write_allowlist[dsh-api] | H1 | `/dev` subpath grants /dev/random; agent dir and dsh-home not writable; `<ws>/.codex/*` writable |
| test_h1_live_seatbelt_write_allowlist[dsh-login] | H1 | same as dsh-api |
| test_h1_profile_write_roots_are_exactly_ws_agent_dir_and_devices | H1 | claude-login roots lie in the owner home; dsh roots include `/dev` |
| test_h2_child_homes_live_under_the_agent_directory | H2 | claude-login child env has no CLAUDE_CONFIG_DIR (owner dir used) |
| test_h3_claude_login_home_holds_only_the_credential_brought_in | H3 | no per-agent home for login; credential not brought in |
| test_h3_login_lanes_bring_in_no_owner_content_beyond_credentials | H3 | claude-login has no per-agent home (codex and dsh-login parts already hold on base) |
| test_h4_owner_home_exceptions_is_a_dict_of_path_reason_lists | H4 | `sandbox.OWNER_HOME_EXCEPTIONS` does not exist |
| test_h4_every_owner_home_write_root_is_a_listed_exception | H4 | table missing; claude-login owner-home roots unlisted |
| test_h5_autoload_paths_and_guard_files_are_write_denied | H5 | `<ws>/.codex/*` writable in all six profiles |
| test_h5_claude_login_child_reads_the_guard_hook_from_its_own_home | H5 | login child reads its hook from the owner's `~/.claude`, not an agent home |

Ambiguities (builder: the tests fix these readings; raise a test change request if you disagree)
1. H4 table keys are read as the driver name ("claude", "codex", "omp", "dsh"), not the lane id.
2. H1 "agent dir writable" is tested on `<session_root>/agents/<id>/` itself and on the child home (so dsh-home,
   codex-home and the like are writable as a whole, apart from H5 guard files). A builder that keeps the dsh and
   codex homes as narrow subroots fails this by design: H1 says the write root is the agent directory.
3. H3 login home: only `.credentials.json` (claude) plus the server's own `settings.json` is allowed in
   `claude-login`'s home; the test insists the credential file is present and equal to the owner's (copy or link).
   If the CLI also needs `.claude.json`, that is an H4 exception, and this test would need a change request.
   Codex (auth.json link) and dsh-login (credential read in place, not copied) are checked only for absence of
   copied or linked owner content other than the credential, so they pass on base inside the aggregate test.
4. H2 does not test HOME in the child env ("HOME-relative tools ... if needed" is conditional). Not asserted.
5. H5 guard files for dsh are taken as `dsh-home/dsh-guard/cordis.patch.yml`. cordis.yml writability is H6 (a
   report statement, not tested). The locked R2/A12 tests stay in force.
6. H1 probes the standard-device rule with /dev/random (writable by Unix mode, so only the profile can deny it)
   and a profile text check for `(subpath "/dev")`; `/dev/ttys000` may not exist on a runner.
7. Several tests aggregate all six lanes into one result: after the fix, one failing lane fails the test (intended).
8. TCR-7 (task 2) is in `TCR7-revision.patch`, not applied. It differs from the round-4 draft (ref/r1a-tcr4): HEAD's
   original already strips the profile and runs the chain; the revision only adds a trailing skip for the profile part.
