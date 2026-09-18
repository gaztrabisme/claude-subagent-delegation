# Log

Reader: anyone tracing why the server is the way it is. Newest first.

## 2026-09-18 — built from glm-subagent; five lanes, router, telemetry

Goal: `goals/2026-09-18-unified-subagent.md`. Coordinator: Claude Code (conductor). Build lanes: GLM planned; GLM hit a z.ai 1313 account-level limit on the first two units, so every unit ran on Claude agents (8 of the 8-agent cap).

UAT at close:

| Row | Result |
|---|---|
| 1.1 | PASS (465 tests) |
| 1.2 | PASS |
| 1.3 | PASS |
| 2.1–2.4 | PASS (router, lane_state, health tests; no start-llm in src) |
| 3.1 | PASS (fake codex) |
| 3.2 | DEFERRED to 2026-09-20 13:29 |
| 4.1 | PASS |
| 4.2 | PASS (sampling fields sent: none) |
| 4.3 | PASS |
| 4.4 | bppc PASS; deepseek DEFERRED (402 Insufficient Balance) |
| 5.1–5.3 | PASS |
| 5.4 | PASS: net saving glm $711.67, deepseek $172.82, bppc $0.68 (`data/cost-2026-09-18/`) |
| 6.1 | PASS (max_agents 1, decisions S7) |
| R.1 | PASS (3 HIGH fixed; open items carry reasons) |
| R.2 | PASS (no remote; nothing pushed) |
| R.3 | PASS |
| R.4 | PASS |

### Run ledger

| Time (UTC) | Unit | Lane | Event |
|---|---|---|---|
| 06:14 | setup | coordinator | S created from `git archive` of G 96933a2 (751e783); Codex fixtures staged; oMLX tool_use probe PASS (607a9dd) |
| 06:15 | U1 | GLM a1 | dispatched run-148d7b3983c6 |
| 06:20 | U5b | GLM a2 | dispatched run-e43668a13707 in worktree cost-join |
| 06:44 | U1 | GLM | FAILED rate_limited 1313 after 1763 s. The z.ai text asks the account holder to file a request to regain access: an account-level fair-use limit, not a short throttle. No edits on disk. |
| 06:45 | U5b | GLM | cancelled after 1390 s; only scripts/pricing.toml written |
| 06:45 | U1, U5b | fork decided | GLM closed for this run (1313; three children on one plan, one from the e-launcher session). Rerouted to Claude Agent, the lane plan's fallback |
| 06:45 | telemetry | finding | Both GLM runs reported usage 0 despite real work (a2: 42095 activity events, 6 steps). Schema 3 must count tokens on failed and cancelled runs; fed into U5 |
| 06:52 | U5b | Claude Agent | done 964969a on cost-join. Coordinator UAT: 12 tests pass; real-data run exit 0 in 2.4 s, 735 runs, 112 unmatched. glm cf $1103.82 vs parent $311.87; deepseek $231.75 vs $28.88. Net saving null until GLM plan price and DeepSeek rates are in pricing.toml. Parent messages that issued calls for several runs are split evenly (agent's decision, kept; unsplit column retained) |
| 06:58 | U1 | Claude Agent | done 7f57188. Coordinator UAT: 248 passed; no glm_subagent_mcp/GSA_ left; no "--bare" outside comments; test_lanes 17 passed. Accepted deviations: global SAM_MODEL/SAM_BASE_URL not applied across lanes; all model aliases map to the run's model; ANTHROPIC_AUTH_TOKEN no longer a GLM key source. README still GLM-era (R.3 rewrites it) |
| 07:00 | merge | coordinator | cost-join merged c8f9188 (uv.lock regenerated); 260 passed with the analysis group |
| 07:01 | U2 | Claude Agent | dispatched on main; brief file sent by message after the prompt went out truncated |
| 07:02 | U3 | Claude Agent | dispatched in worktree codex-driver; guard hook in a per-agent CODEX_HOME plus workspace-write sandbox (decided forward: codex-cli has stable PreToolUse hooks) |
| 07:10 | bppc | coordinator | Gary: bppc on. LAN SSH ok, Tailscale route down. Coordinator ran start-llm (GPU idle before); :8080 health ok, :8081/slots 200, :8081/metrics 501 (no --metrics). 4.4 bppc now runnable |
| 07:21 | U3 | Claude Agent | done bce9ee3 on codex-driver. Coordinator UAT: 266 passed 1 skipped; test_codex_driver 19 passed; no bypass-sandbox flag in src. Found from the binary: Codex PreToolUse payload has Claude keys plus turn_id; Codex rejects permissionDecision allow, so allow = silent exit 0. Unverified until live (3.2): hooks load from custom CODEX_HOME, real tool_name values, resume flag order, symlinked auth. Accepted: input = input_tokens - cached_input_tokens |
| 07:35 | U2 | Claude Agent | done b2d01af. Coordinator UAT: 330 passed (analysis group); no start-llm in src; 71 router/lane_state/health cases PASSED by name; oMLX /api/status returns status ok (agent had not checked). Accepted: outcome "refused" + code field; outcomes unavailable and no_lane; no backoff on balance/usage-limit; cold load extends the run deadline; 1308/1310 without reset do not close. Risk for review: z.ai reset time read as local time |
| 07:37 | merge | fork decided | codex-driver into main conflicts in runs.py by design: the router walks lanes inside one Agent, the codex driver is an Agent subclass. Merge aborted; integration unit M1 dispatched to a Claude Agent (coordinator does not resolve design conflicts) |
| 07:50 | M1 | Claude Agent | done ce66ebd (merge). Coordinator UAT: 352 passed; 11 codex+router cases PASSED. Design: per-hop driver objects (ClaudeDriver, CodexDriver); first hop that runs binds lane/driver for continues. Branch is master, not main |
| 08:08 | U5 | Claude Agent | done 750218f. Coordinator UAT: 384 passed; probes present in telemetry.py. Usage-0 cause: tokens only taken from the final result event; now per assistant message, deduped. Accepted: --include-partial-messages added for real TTFT; retried and summary turns now counted. Open: codex tokens all-or-nothing (turn.completed only); admission only on delegate. bppc :8080 proxy reports backend stopped at 08:08 |
| 08:15 | U4/U6 | Claude Agent | dispatched (scripts: hook isolation port, smoke_lanes with oMLX logging proxy, bench_concurrency); added: bppc proxy cold start (backend stopped) treated like oMLX cold load |
| 08:18 | R.1 | Claude Agent | review dispatched on 750218f in parallel with U4/U6 (7th Claude agent of the 8 cap) |
| 08:40 | U4/U6 | Claude Agent | done 5279102. Coordinator UAT: 423 passed; hook_isolation_check PASS (a/b/c); smoke omlx PASS (deny Bash ~/.ssh/config, hello.txt, 4 turns 17196 tokens, 2 samples, run_summary, sampling fields sent: none) |
| 08:45 | 4.4 bppc | coordinator | smoke bppc FAIL twice (cold and warm): llama.cpp Qwen template raises "System message must be at the beginning" (HTTP 500) on Claude Code 2.1.276 requests; oMLX accepts them. Recorded as U-B1 HIGH in review.md |
| 08:50 | R.1 | Claude Agent | review done: 3 HIGH (H1 codex workdir, H2 ~/.codex and ~/.git-credentials readable, H3 agent ids shared across processes), 8 MED, 6 LOW. Saved to wiki/review.md with U-B1/U-B2 |
| 08:52 | F1 | Claude Agent | fix unit dispatched: U-B1, U-B2, H1–H3, M1, M3, M8, cheap LOWs (8th and last Claude agent of the cap) |
| 08:58 | 6.1 | coordinator | bench omlx 1/2/4/8 exit 0, 4 rows, 15 children all verified. Aggregate tok/s 20.1/18.3/7.6/8.2; p90 TTFT 3.9/10.5/26.5/181 s; swap 0. Rule chose max_agents=1; default change sent to F1. Data in wiki/data/ |
| 09:05 | 4.3 glm | coordinator | smoke glm PASS live: deny Bash ~/.ssh/config, hello.txt, 12315 tokens. The 1313 limit had cleared. Found U-T1: first turn record 0 tokens (usage only in stream deltas); sent to F1 |
| 09:08 | 4.4 deepseek | coordinator | smoke deepseek DEFERRED exit 3: live 402 Insufficient Balance classified deepseek_balance (first live proof of refusal classification) |
| 09:45 | F1 | Claude Agent | done 5294af8..197c681. Coordinator UAT: 465 passed; hook_isolation PASS; smoke bppc PASS (8940 tokens, deny ~/.ssh/config, run_summary VRAM 15762 MB, decode 94.4/78.5 tok/s); smoke omlx PASS; smoke glm PASS (turns 12132 = run 12132, U-T1 fixed live) |
| 09:50 | 5.4 | coordinator | cost_join on final code exit 0: 736 runs; glm cf $1103.89 vs parent $312.22 |
| 09:52 | R.4 | coordinator | ~/.claude.json backed up to ~/.claude.json.bak-2026-09-18; subagent added (keys copied from old entries, tuned knobs carried over); glm-subagent and qwen-subagent removed; deepseek-subagent kept. stdio probe: server "subagent", six tools |
| 10:25 | 5.4 | coordinator | DeepSeek peak rates from api-docs.deepseek.com (1.32/3.96/0.044 per 1M); GLM plan $80/month (Gary). Net saving glm $711.67, deepseek $172.82. Test that read the real pricing file moved to a fixture by GLM (4c5cd5e; GLM lane open again); 466 passed |
