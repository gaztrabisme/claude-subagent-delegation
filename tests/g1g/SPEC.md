# G1g: `subagent promote` checks each cloud lane's route and probes it with one doctor prompt

Reader: the test writer (non-builder) and the builder. Decisions: S94 (no route after promote), S103 (agy needed hosts
the allowlist lacked; only a real prompt showed it), S98 and S107 (G1e's static check held under S90). Offline tests
only: the probe goes through promote's `runner`, which the tests replace.

## 1. Static route check (as G1e part B)

- `src/subagent/promote.py` gains `missing_routes(settings) -> list[str]`: the names, in config order, of configured
  providers with `local` false and no network route. A route is a non-empty `network_allow`, or a `base_url` with a
  non-empty hostname (the derivation of `Agent._start_egress_proxy` in `runs.py`).
- `promote(..., require_routes: bool = False)`. When true, after the commit resolves (an unknown commit still raises
  `UnknownCommit`, exit 2, first) and before anything is installed or written, a non-empty `missing_routes` raises
  `PromoteError` (exit 1): `"no network route for <a>, <b>; add network_allow or set local = true"`. `force` does
  not bypass it.

## 2. Lane probes (the S103 lesson)

- `promote(..., probe: bool = False, skip_probe: Iterable[str] = (), allow_failed: Iterable[str] = ())`.
- When `probe` is true, after the install succeeds and **before** `served.json` is written, promote runs one probe per
  configured provider with `local` false whose name is not in `skip_probe`, in config order. A probe is one call
  `runner([sys.executable, "-m", "subagent", "doctor", "--provider", <name>, "--prompt"], capture_output=True,
  text=True, timeout=400)`. Exit code 0 is a pass. A non-zero exit, or `subprocess.TimeoutExpired`, is a failure.
- If any probed lane fails and is not in `allow_failed`, promote raises `PromoteError` (exit 1) with
  `"lane probe failed: <name> (<first non-empty line of stderr, else of stdout, else 'exit N'>)"`, one `; `-joined part
  per failed lane in config order, followed by
  `"; fix the lane, or pass --allow-failed-lane NAME or --skip-probe NAME"`. `served.json` is not written or changed.
  `force` does not bypass it.
- A failed lane in `allow_failed` and a lane in `skip_probe` are each printed to stdout on their own line:
  `warning: lane probe failed for <name>, allowed` and `warning: lane probe skipped for <name>`.
- A name in `skip_probe` or `allow_failed` that is not a configured provider raises `PromoteError` (exit 1),
  `"unknown provider <name>"`, before anything is installed.
- An "already served" early return (same commit, same install path, venv present) still runs the static check but runs
  no probes.

## 3. CLI

- `subagent promote COMMIT` passes `require_routes=True, probe=True`.
- New options: `--no-probe` (probe false; prints `warning: lane probes skipped (--no-probe)`), `--skip-probe NAME` and
  `--allow-failed-lane NAME` (both repeatable). Errors go to stderr as `error: <message>` with the error's exit code.

## Locked test this changes

`tests/r6/test_d_promote.py`: three CLI tests run promote with the default test providers, and codex there has no
route. TCR-G1e-2 (Opus fail-closed; its Swift vote no longer counts, S99) gives those three tests settings with a
routed codex and changes no assertion. With probes, the tests' recording runner answers each doctor call with exit 0,
so no assertion's meaning changes: install failure still fails before any probe runs. TCR-G1e-2 needs one more
qualifying pass (Gemini) under S90.
