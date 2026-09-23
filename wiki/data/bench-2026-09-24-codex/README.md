# Bench cell 2026-09-24 — Codex orchestrator

`bench/run.py --harness codex --config <private config with glm tiers> --tasks cron --modes delegate --runs 1 --timeout 5400`, run after M5 (Codex cells use `-s danger-full-access`, the delegate skill is copied into the cell, the venv is on PATH). The orchestrator was `gpt-6-luna` at xhigh reasoning; workers were glm.

Result: hidden tests 22/22; orchestrator 193,422 input / 40,675 output / 10,768,128 cached tokens over 4,988 s. Worker columns are empty: the orchestrator ran `subagent run … --wait 540` four times (each returned `running` with a new run id) instead of `subagent wait`, its `--tier hard` pointed at the reviewer/hard provider that was closed on balance, and no `delegation` record landed under the cell's session root. The code it delivered passes the hidden tests; the accounting for the worker side of this cell is missing (finding U-B4 in `log.md`).
