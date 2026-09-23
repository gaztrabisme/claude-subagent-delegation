# Goal — 2026-09-22 — fuse-delegation

**Set by:** Gary, via `/goal`. **Audited by:** the coordinator at close, against this file only.

Repo: `R` = this checkout, branch `fuse`, remote `origin` = `github.com/gaztrabisme/claude-subagent-delegation`. Plan: `~/.claude/plans/open-a-new-branch-atomic-tower.md`.

## Deliverable P0 — fused tree, package laid out

| # | Success criterion | UAT |
|---|---|---|
| 0.1 | Both histories on `fuse`; README is the delegation tool's; old server README parked in `wiki/inherited/` | `git log --oneline fuse \| grep -c "Fuse subagent-mcp"` is 1; `git log --oneline \| grep -c "Wiki: cost figures"` is 1 |
| 0.2 | Package renamed `src/subagent` with sub-packages `providers/`, `guard/`, `loop/`, `telemetry/`; `mcp_server.py`; no `subagent_mcp` import anywhere | `grep -rn subagent_mcp src tests scripts` is empty |
| 0.3 | Target's loop files under `src/subagent/loop/` with relative imports; its tests under `tests/loop/` and fakes under `tests/fakes/`, collected by pytest | `uv run pytest -q` exits 0 (≥ 500 tests) |
| 0.4 | `pyproject.toml`: name `subagent`, scripts `subagent` and `subagent-mcp`, `requires-python >= 3.11`, group `report` | `uv run subagent-mcp --help` or a stdio probe lists six tools |
| 0.5 | CI workflow runs `uv sync --group dev && uv run pytest -q` on 3.11/3.12/3.13 with node + zsh/fish | file content check |

## Deliverable P1 — config file, providers, no machine defaults

| # | Success criterion | UAT |
|---|---|---|
| 1.1 | TOML config layered user → project → `$SUBAGENT_CONFIG`; zero built-in providers; no default fallback chain | `SUBAGENT_CONFIG=/dev/null subagent doctor` exits non-zero with "no providers configured" |
| 1.2 | `Provider` interface with claude, codex, copilot, grok drivers (gemini fixture-only, `experimental` gated) | `uv run pytest tests/test_providers*.py -q` exits 0 |
| 1.3 | No machine-specific literal in `src/` | `grep -rn "100.106.185.34\|192.168.1\|Tailscale.app\|\.omlx/settings\|bppc@" src/` is empty |
| 1.4 | Health, refusal classification and telemetry probes driven by config fields, not lane names | `uv run pytest tests/test_health.py tests/test_router.py tests/test_telemetry.py -q` exits 0 |
| 1.5 | `subagent init --yes --from <example>` writes a file `subagent doctor` accepts; eight `examples/config.*.toml` | for each example: `SUBAGENT_CONFIG=<example> subagent doctor --no-probe` exits 0 |
| 1.6 | Live: glm and grok providers answer one cheap turn with tokens counted | `subagent doctor --provider glm --prompt` and `--provider grok --prompt` print non-zero output tokens (DEFERRED with reason if a lane is closed) |

## Deliverable P2 — the loop on providers

| # | Success criterion | UAT |
|---|---|---|
| 2.1 | `subagent run/wait/test/review/undo/checkpoints/watch/detect` with the target's flags, one JSON object on stdout, same exit codes | `uv run pytest tests/loop -q` exits 0 |
| 2.2 | Worker rounds run through `Registry` (guard hook, trace, verification); `pre_verify` releases the test lock before verification in a `finally` | a test proves a tampered test is restored before the suite runs |
| 2.3 | Guard denies writes to protected test paths and to `.subagent/` outside the allowlist; `cheat.sh` denied at the chmod on the claude fake | `uv run pytest tests/test_guard*.py -q`; scenario test passes on `driver=claude` |
| 2.4 | Copilot is a provider; loop tests pass on both `copilot` and `claude` fakes | parametrised loop tests green |
| 2.5 | SKILL.md and install.sh call `subagent`; MCP server lists providers from config; `lane=` alias works | `grep -c "subagent run" skills/delegate/SKILL.md` ≥ 1; stdio probe |
| 2.6 | Live loop: `cron` task with `[loop.tiers.normal] provider = "glm"` returns `status: done` | command output (DEFERRED with reason if glm closed) |

## Deliverable P3 — telemetry, cost, report

| # | Success criterion | UAT |
|---|---|---|
| 3.1 | `delegation` trace kind; every record carries `bench_run_id` when set | `uv run pytest tests/test_trace_schema.py -q` |
| 3.2 | Cost priced at run end from config; flat plans spread at report time; subscription spend labelled API-equivalent | `uv run pytest tests/test_cost.py -q` |
| 3.3 | `subagent report` tables; `--html` self-contained | `uv run pytest tests/test_report.py -q`; `grep -c 'src="http' out.html` is 0 |

## Deliverable P4 — bench and business case

| # | Success criterion | UAT |
|---|---|---|
| 4.1 | `bench/run.py` matrix over harness × config × task × mode; `bench/harness.py` parsers for claude, codex, gemini, grok, copilot | parser tests on fixtures green |
| 4.2 | `bench/tasks/meeting-scribe` spec + hidden tests; hidden tests pass on a reference implementation | reference impl passes `hidden/test_scribe_hidden.py` |
| 4.3 | `bench/business_case.py` closed-form tests; `docs/business-case.md` states assumptions | `uv run pytest tests/test_business_case.py -q` |
| 4.4 | One real cell per installed harness (claude, codex, grok) on `cron`; rows join to `delegation` records | `results.csv` exists with ≥ 3 rows and each `bench_run_id` appears in the trace |

## Deliverable R — review, record

| # | Success criterion | UAT |
|---|---|---|
| R.1 | Adversarial review after P2 and P4; every HIGH fixed or recorded with a reason | `wiki/review.md` has a 2026-09-22 section, no OPEN-HIGH without a reason |
| R.2 | Wiki: decisions S11+, active-work, log with the run ledger | files updated with a 2026-09-22 entry |
| R.3 | Nothing pushed to origin unless Gary says so | `git status -sb` shows `fuse` without an upstream push |

## Working pattern (binds this run)

Lanes at start: GLM closure expired 2026-09-21 19:33 (1313 yesterday; watch for it), Codex open (quota reset 09-20), DeepSeek closed (balance), copilot and gemini binaries absent on this Mac, grok and codex and claude installed. GLM primary for mechanical units, Codex for critical-path core, Claude agents (≤ 8) for review and as fallback. The coordinator runs every UAT itself and never does unit work.
