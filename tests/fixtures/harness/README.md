# Harness CLI fixtures

Source commit: `63be7b4f` (epi branch `lane/e1`; the recordings committed there), recorded by the run coordinator outside any sandbox for run `epi-lane-2026-10-08`.

Regenerate from `core/` with:

```sh
CARGO_NET_OFFLINE=true cargo test -p cli record_harness_fixtures -- --ignored
```

Set `EPI_FIXTURE_OUT` to override this directory. Every stream goes through `parse_one_shot` and `run_one_shot`, the same code used by `epi call`, over a real serve Unix socket. Model replies and endpoint admission use scripted adapters. Every recorded command-verification and final plan-close output comes from the real confined check runner. Question jobs use the production decision-linked answer check. No scenario uses a scripted verification executor. Plan proposal and approval calibration remains scripted for the change, approval and goal scenarios: they seed board states without a worker, including the deliberately unverified command that exits 7. This calibration executor does not supply any recorded output_tail. Approval is external and all plans/tickets are real board records. There are no live endpoints, real keys, or real caller tokens. The fake token `test-token-canary` is read from a private file and must never appear in argv or output. Temporary project paths are normalized to `/fixture`; IDs and timestamps are recorded values.

The approval stream ends at `needs_human`; `await_after_approval.json` resumes the same job. Completed change fixtures retain a real ticket branch commit. UAT reports the real final plan-close checks. Output tails are raw captured text, without stream labels or synthetic truncation markers. Socket failure captures stderr and the client exit code.

Reader: the subagent epi-lane maintainer. All recordings, `list.json` included, are copied verbatim from that recording. `tests/fakes/fake_epi` remaps these fixture paths only when a test supplies `FAKE_EPI_SCOPE`. `cancel_by_request_id_admitted.json` and `cancel_by_request_id_unadmitted.json` record `epi call cancel --request-id` for an admitted and an unadmitted key.
