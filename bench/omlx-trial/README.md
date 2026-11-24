# omlx lane trial kit

The acceptance kit for trusting a local model on the `omlx` lane. Criteria and the first results:
[wiki/omlx-swift-trial-2026-11-24.md](../../wiki/omlx-swift-trial-2026-11-24.md).

- `text/`: 20 support tickets, known answers, schema and scorer (tier L3a).
- `code/p1..p3`: three small packages with 12 locked tests each and empty stubs (tiers L3b and L4).
  The reference solutions are kept out of the repository on purpose; the model under test never sees them.
- `LOCK.txt`: SHA-256 of every kit file as locked on 2026-11-24 (Haiku wrote the kit; the coordinator
  checked it against the reference and locked it). Check with `shasum -a 256 -c LOCK.txt` from this folder.
- `text_trial.py`, `code_trial.sh`, `plan-p*.md`: the drivers used on 2026-11-24. `code_trial.sh` still
  names that session's scratchpad paths; point `N` at a fresh working folder before reuse.
