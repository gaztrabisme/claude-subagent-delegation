# G1h: a run refused at admission by the envelope does not count toward the run caps

Reader: the non-builder test writer and the builder. Decision: S118 (coordinator). Defect: `lane_status`'s rolling
run count (`_run_event` / `_run_counts` in `src/subagent/lane_status.py`) counts every trace record with
`"kind": "run"`, including a run the envelope refused at admission. So refusals use up `runs_day` and `runs_week`.
Seen on the live trace: `run-22b2087e70df` (gemini, 2026-11-27 07:20 local) has `refusal_code: "envelope"`,
`finish_reason: "no_lane"`, `turns: 0`, and was counted.

- A run record whose `refusal_code` is `"envelope"` is not counted in the per-lane day or week run counts.
- Every other run record is counted exactly as before. That includes a run that reached a vendor and failed, a run
  with `finish_reason` `"no_lane"` but no `refusal_code`, and a run with another `refusal_code`.
- The limits, the rolling windows, the offset cache and every other field of the snapshot stay as they are.
- Tests drive the real code path the envelope check uses (`LaneStatusStore.snapshot(settings, ...)` with
  `count_runs=True`, or `_run_counts`), on a synthetic trace file in `tmp_path`. No network.
