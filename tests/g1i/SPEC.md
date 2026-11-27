# G1i: a run whose primary vendor was spawned counts toward the cap, even with no work (RR-16)

Reader: the non-builder test writer and the builder. Decision: the coordinator's RR-16 (S125 residual not accepted).
Today (S123, G1h) a run record is left out of a lane's run caps when `refusal_code == "envelope"` and
`finish_reason == "no_lane"`. In a chain where the primary is spawned and its vendor refuses before any work (a hop with
outcome `refused` and a vendor code such as `zai_1308`) and the fallback is then envelope-refused, the run record carries
the primary's lane, `refusal_code` "envelope" (the last refused hop) and `no_lane`, so that spawned attempt is not
counted.

- **Trace (additive, schema 3 unchanged):** each `kind: "run"` record gains `refused_codes`, the list of the `code`
  of every hop with outcome `refused`, in hop order (`[]` when none). No existing field changes meaning.
- **Count rule:** a run record is left out of the day and week run counts only when `refusal_code == "envelope"`,
  `finish_reason == "no_lane"`, and every entry of `refused_codes` is `"envelope"`. A record that carries
  `refused_codes` with any other code counts once, on its `lane`.
- **Records written before this change** (no `refused_codes` key) keep the S123 rule.
- Everything in `tests/g1h/SPEC.md` and `tests/g1h_fix/SPEC.md` otherwise stands. The locked trace-schema tests stay
  green; if one pins the exact key set of a run record, that is a test change request, not an edit.
