# G1h fix: an envelope refusal is excluded only when no lane ran

Reader: the non-builder test writer and the builder. Found by both G1h reviews (Fable, Opus): the trace stamps a run
record with the last refused hop's `refusal_code` even when a later fallback hop ran (`telemetry/trace.py`), and
`lane` is the lane that ran. G1h's rule (exclude `refusal_code == "envelope"`) therefore drops a fallback lane's real
runs from its own cap.

- A run record is excluded from the run caps only when `refusal_code == "envelope"` **and**
  `finish_reason == "no_lane"` (runs.py sets `no_lane` only when no lane took the run).
- Every other run record counts, including a completed or failed run on a fallback lane after the primary was refused
  by the envelope (`refusal_code: "envelope"`, `finish_reason` other than `no_lane`, `turns` above 0).
- Everything in `tests/g1h/SPEC.md` otherwise stands.
