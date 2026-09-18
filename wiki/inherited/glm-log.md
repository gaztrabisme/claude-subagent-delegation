# Log

## 2026-09-18 — review fixes on guard-fixes

Closed the adversarial review's G1, N2–N5, N8, P4–P6, P8–P11, I0-a, I0-b and R1–R4 (D11). `scripts/hook_isolation_check.py` proves, against a mock endpoint, that planted workspace settings with `disableAllHooks` do not stop the hook (I0-c). The review's probe (`gprobe/run.py`, 367 cases): 10 cases are allow on the branch and not on `main`, all of them intended forms (`bash t.sh`, `npm test`, `cd sub …`, `python3 -mpytest`, `./gradlew`-through-a-path, `export X`). One of them was denied on `main`: `cd sub && echo hi > ../pwn`, which lands inside the workspace.

## 2026-09-18 — guard on, rules widened for verification, step and rate-limit semantics

Branch `guard-fixes`, from `lab/subagent-eda/report.md` §2.

- Guard (items 1, 2, 4, 5, 7). The splitter tokenizes with `shlex.shlex(posix, punctuation_chars)` and splits on `;`, `|`, `||`, `&&` and unquoted newlines; tokenizer errors escalate, and `$(…)`, backticks, `&`, `(` still escalate. `test` and `[` are read-only; a leading `!` is stripped. `cd` with no argument or one inside the workspace is allowed; later segments are judged in every directory the shell could be in (the cd target alone inside an `&&` chain, the old directory too after `;`, `||`, `|` or a newline), and the worst segment verdict wins. `uv run <runner>`, `pnpm test|vitest|exec tsc`, `npm test`, `npx tsc|vitest` and a workspace `./gradlew test|check|build` are allowed. `export NAME=value` and `NAME=value` prefixes are stripped unless the name is in `RISKY_ENV` (PATH, CDPATH, HOME, LD_*, DYLD_*, GIT_* and others). `bash script.sh` goes through the interpreter rule. Inline code (`python3 -c`) still escalates. Holes closed on the way: an unquoted newline hid everything after it from the policy; `find -exec/-delete`, `env <cmd>`, `sort -o`, `uniq in out`, `tree -o` and `rg --pre` passed as read-only; `1>/etc/x` and `>| /etc/x` were not seen as redirects; a `~name` word crashed the classifier.
- Hook on (item 0, D9). No `--bare`; explicit `--tools`, `--strict-mcp-config`, `--setting-sources ""`. `GSA_LOOP_STRIKES` default 3 → 8. `scripts/smoke_guard.py` passed against z.ai.
- Steps (item 6, D10). `GSA_MAX_STEPS` is `--max-turns` only; max-turns results are `failed/steps`; loop and budget trips kill mid-stream.
- Continue (item 3). `glm_continue` without `verification` reuses the delegate's command and says so in `verification_note`; `""` skips it.
- Rate limits (item 9). The z.ai code is parsed from the error text. 1308/1310 fail at once with the reset time when z.ai gives one. 1313 backs off on `GSA_THROTTLE_BACKOFF` (default 60 s, doubled per attempt, capped at 900 s) and the final error tells the caller to run fewer children.

## 2026-09-18 — fix "rate_limited: success" misclassification

Trace analysis (corrected same day): the 11 runs were real z.ai 429s — code
1313, Fair Usage throttle — and the server retried each 3 times (4 prompts,
re-sent 5/10/20 s after each error), which the retry gate only does when no
result event has `is_error: false`. So the CLI's final event was
`subtype: "success"`, `is_error: true`, with the "API Error: Request rejected
(429) · [1313][…Fair Usage Policy…]" text in `result` and no `error` field.
The `rate_limited` class was right; only the label was wrong: with no
`error` field, classify_exit's detail fell back to `or subtype` and used the
metadata string "success". Now the detail comes from the event's own error
fields (`error`, and `result` when `is_error`) — the live shape labels as
"rate_limited: API Error: Request rejected (429) · [1313]…". The old
whole-event JSON haystack did not cause these runs; it was a latent false
positive (a genuinely successful answer merely mentioning 429 would have
tripped the class) and is removed, with a guard test for that shape.

## 2026-09-18 — trace schema 2: run records carry session_id

`Trace.run` now writes the Claude Code `session_id` (`run.session_id`, captured
in `_ingest`) on every `run` record, so trace rows join back to the child
session's own durable log. `SCHEMA` bumped 1 → 2; test_trace.py asserts the new
record shape.

## 2026-09-02 — Plan Block

Grounded: gaztrabisme/claude-sub-proxy, gaztrabisme/deepseek-subagent-mcp (server/config/runs/guard/verify/decisions), docs.z.ai/devpack/tool/claude, code.claude.com/docs/en/headless + hooks. Local `claude` 2.1.252; `~/.claude/settings.json` has no GLM/Anthropic API override.

Gary locked: bypassPermissions + guard; `glm-5.3[1m]`; port DSA contract now.

## 2026-09-02 — Implemented

Package `glm-subagent-mcp` 0.1.0. Child is `claude -p` with GLM env, isolated `CLAUDE_CONFIG_DIR`, `--bare`, `--dangerously-skip-permissions`, PreToolUse guard. 95 unit tests, no network. DeepSeek hung three times generating large files; ported DSA modules by fetch+rename, wrote `runs.py` locally.

Live GLM key still unset in this process. Smoke against z.ai is the next gate.
