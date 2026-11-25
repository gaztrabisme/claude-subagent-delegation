## Usage envelopes (owner delegated the numbers, 2026-11-25)

Set by the coordinator from measured use on 2026-11-22 to 24; in force until the owner changes them. Inside an
envelope the coordinator and managers plan, ration and forecast without asking. the owner hears only when a
forecast would break an envelope or a reserve, or when money would be spent (a reset, extra usage, a top-up).

| Lane | Measured so far | Envelope | Reserve (never planned) |
|---|---|---|---|
| Claude (Ultra plan) | weekly all-models 6% and Fable 3% after the 11-24 operation and the review; extra usage off | operations up to 60% of the weekly window; Fable up to 25% weekly, critical security only | 40% weekly for the owner's own use; extra usage stays off |
| Codex | 30-day window; 11-24 burned about 140% of it in 29 runs (about 4.9% a run); 35% used now, resets 2026-12-24 | at most 4 runs a week, guard and security builds first | 15% of the window; no reset is planned |
| DeepSeek (DSH) | $0.61 this month, including four review runs | $10 a month; report at $5 | none (cap is the floor); on hold until R1-0 merges |
| Gemini (Hydrogen + Neon share one quota) | ran out once on 11-24 after about 16 runs in a day | at most 10 runs a day, of which Neon at most 3 (security cross-checks) | stop new work when a run is refused once that day |
| omlx (local) | $0; 63.9 GB model | at most 8 GPU hours a day, one model, 70 GB ceiling | none while another GPU tenant runs |
| glm, gpubox | closed | 0 | - |

Planning rules: on these numbers Claude (Sonnet for builds and tests, Opus for guard code and design) has
far more room than Codex, so Codex is kept for work where it is clearly better. Coordinator and manager
sessions are a lane too (S33): compact or hand off at about 60% of the context window. Procedure (forecast,
actuals, rationing, reroute): `skills/weave/operating-system/resource-management.md`, "Method: usage envelopes".
