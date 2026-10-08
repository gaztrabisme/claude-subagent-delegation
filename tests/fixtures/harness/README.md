# Harness CLI fixtures

Source commit: `f84eea8f007a9d71b7fa308e92cef4981117df26` plus the uncommitted E1 patch in run `epi-lane-2026-10-08`. The coordinator commits this patch and its fixtures together.

Regenerate from `core/` with:

```sh
CARGO_NET_OFFLINE=true cargo test -p cli record_harness_fixtures -- --ignored
```

Set `EPI_FIXTURE_OUT` to override this directory. Every stream goes through `parse_one_shot` and `run_one_shot`, the same code used by `epi call`, over a real serve Unix socket. Model replies, admission and verification outcomes use scripted test adapters and a test-only deterministic verification executor. Approval is external and all plans/tickets are real board records. There are no live endpoints, real keys, or real caller tokens. The fake token `test-token-canary` is read from a private file and must never appear in argv or output. Temporary project paths are normalized to `/fixture`; IDs and timestamps are recorded values.

The approval stream ends at `needs_human`; `await_after_approval.json` resumes the same job. Completed change fixtures retain a real ticket branch commit. UAT reports the final scripted checks. Socket failure captures stderr and the client exit code.
