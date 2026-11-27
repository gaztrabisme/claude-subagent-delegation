# Risk register (fixture)

| id | residual | class | accepted by | fix package | expires |
|---|---|---|---|---|---|
| RR-01 | Credential file readable in-process (S30) | high | the owner, 2026-11-24 | none scheduled | next security review |

| RR-02 | Kill can miss a reparented child | low | Opus + Swift, 2026-11-25 | R7a | 2026-12-25 |
| RR-03 | Socket dir shared | HIGH | the owner, 2026-11-26 | R2 | 2026-12-30 |
| RR-04 | Stale lock file | medium | Opus, 2026-11-26 | R6 | 2026-12-30 |

Not residuals: other defects with a fix package.

**Open defect, high (S44, 2026-11-25), not accepted:** agy workers can switch their own sandbox off per command.
It takes a flag that nothing records. Hold: no agy runs.
Fix: R1a-agy.

**Open defect, low (S45, 2026-11-25), not accepted:** a cosmetic problem. Fix: later.

**Open defect, high (S51, 2026-11-26), not accepted:** the lock check is skipped for one lane without any Fix named here.
