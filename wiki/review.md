# Adversarial review — subagent-mcp at 750218f

Reader: whoever changes the router, the guard or the Codex driver next. Reviewer: a Claude agent, 2026-09-18, on `git diff 751e783..750218f -- src scripts` (751e783 is the verbatim import of glm-subagent 96933a2, whose guard was reviewed in `~/Documents/Work/lab/subagent-eda/review-guard-fixes.md`). Baseline 384 tests passing. The Outcome column is the coordinator's, filled at close.

## Findings

| ID | Sev | Area | file:line | Defect | Reproduction | Suggested fix | Outcome |
|---|---|---|---|---|---|---|---|
| H1 | HIGH | Codex guard | runtime/approval_hook.py:71-77 | The hook maps a Codex shell call to `Bash {"command": …}` and drops `workdir`; the guard resolves relative paths against the workspace, so `exec_command {"cmd":"cat id_ed25519","workdir":"~/.ssh"}` is allowed, and workspace-write does not limit reads. | `requests()` then `classify()` → allow | Forward workdir as cwd to `classify_bash`; deny a workdir outside the workspace or under a sensitive root | FIXED 5cbca1c (test_h1_*, test_hook_codex_forwards_workdir_and_payload_cwd) |
| H2 | HIGH | Secrets | guard.py:88-89 | `~/.codex/` and `~/.git-credentials` are not sensitive roots; any child can `cat ~/.codex/auth.json` (OpenAI OAuth and refresh tokens); Read is allowed too | `classify("Bash",{"command":"cat ~/.codex/auth.json"},ws)` → allow | Add both roots; one regression test per path | FIXED 5cbca1c (test_h2_*) |
| H3 | HIGH | Isolation | runs.py:1587,1689; config.py:164 | Agent ids restart at a1 per process and the session root is shared, so two server processes share `agents/a1/claude-home` and `codex-home`; B rewrites settings.json with its lane's base URL while A's child reads it, so A's z.ai key can go to DeepSeek | two `Settings` → same path; file ends with the other lane's URL | Unique agent ids across processes, or a per-process session subdirectory | FIXED 0a92c98 (test_h3_two_registries_on_one_session_root_never_share_an_agent_home) |
| M1 | MED | Lane memory | router.py:41,235; 231-232 | Refusal classification ignores which lane refused: any error containing `402` closes that lane 6 h as deepseek_balance; a `[1308]` body with any reset date closes the lane until then with no cap (tested: 2099); no tool reopens a lane | omlx "timed out after 402 s" → closed 6 h; glm reset 2099 → closed until 2099 | z.ai codes only on glm, balance only on deepseek, 402 only as HTTP status, cap 7 days, add a reopen path | FIXED 95a4c10 (test_m1_*); reopen via scripts/reopen_lane.py |
| M2 | MED | Lane memory | router.py:192-199 | z.ai reset time read as local (+07); if z.ai quotes +08 the lane stays closed 1 h late | `zai_reset_at("2026-09-18 15:00:00")` → +07:00 | Confirm z.ai's zone from a real 1308 body | OPEN: needs one real z.ai 1308 body to learn its time zone; effect is a lane reopening up to 1 h late |
| M3 | MED | bppc resolver | health.py:98-117 | Any peer named `bppc-*` is accepted and the first non-100.x address (often the public STUN endpoint) is used; prompt and key go over plain HTTP to it | crafted peer `bppc-evil` → 203.0.113.9 first | Match the peer by tailnet IP 100.106.185.34 or node ID; accept only RFC1918 | FIXED 3d66c99 (test_m3_*) |
| M4 | MED | Codex guard | codex_driver.py:381-396; guard.py:258-274 | Codex guard never run live; a hook that silently does not fire is invisible (trace still says sandbox+hook); `<workspace>/.codex/` not protected | not run (codex binary off limits) | Mark runs with command items and zero verdicts as unguarded; protect `.codex/`; pin web_search off in per-agent config; live smoke after 09-20 | PARTLY FIXED 197c681 (.codex/ protected; silent hook recorded as guard 'sandbox (hook silent)'); OPEN: live Codex proof after 2026-09-20 13:29 |
| M5 | MED | Codex auth | codex_driver.py:366-369 | auth.json shared by symlink across concurrent Codex children and the user's own Codex; refresh-token rotation can invalidate the login, and a rename-write turns the symlink into a stale copy | inference | Codex max_agents 1 or serialise refresh; check the symlink after the first live run | OPEN: needs a live Codex run; mitigation is SAM_CODEX_MAX_AGENTS=1 if logins break |
| M6 | MED | Telemetry | codex_driver.py:220-231; runs.py:1433-1437 | If Codex usage is cumulative across `exec resume`, every continue re-counts earlier turns | needs one live delegate + continue | Keep the last total per thread and record the difference | OPEN: needs a live Codex delegate + continue to see whether usage is cumulative |
| M7 | MED | Telemetry | codex_driver.py:206-216; runs.py:588,607 | Codex TTFT is time to first completed item (includes command runtime), not first token; cross-lane TTFT comparison is wrong | translator drops item.started/updated | TTFT from first item.started, or null and label not comparable | OPEN: Codex TTFT not comparable to claude lanes; exclude codex from TTFT charts until fixed |
| M8 | MED | Health/omlx | health.py:181-183 | cold_load uses server-wide `models_loaded == 0`, ignoring `loaded_models`; with another model loaded, the lane's model cold-loads with no extra time | live status shows loaded_models list | Check the lane's model in loaded_models | FIXED 3d66c99 (test_m8_omlx_cold_when_its_model_is_not_loaded) |
| L1 | LOW | Codex hook | approval_hook.py:32-34 | `write_stdin` unmapped; every keystroke escalates without context | mapping table | Deny, or show the session's command | OPEN: low; escalations only |
| L2 | LOW | Guard | config.py:132 | Approval socket in `$TMPDIR`, writable by a Codex child; unlinking it fails every hook closed (DoS) | inference | Socket under the protected session root | OPEN: low; denial of service only, fails closed |
| L3 | LOW | Lane memory | lane_state.py:82-98 | Load-modify-replace locked per thread only; two processes can lose a closure | code reading | fcntl.flock | FIXED 95a4c10 |
| L4 | LOW | Router | router.py:43-48; codex_driver.py:50-54 | Two Codex reset parsers; neither accepts a date without a year | `parse_reset("try again at Sep 20th 1:29 PM")` → None | One parser; missing year = current year, roll forward | FIXED 95a4c10 (test_l4_codex_reset_without_a_year) |
| L5 | LOW | Telemetry | telemetry.py:195-212 | bppc ssh probe 3 s total may be too short over DERP; metrics.jsonl never rotates | not timed | 6 s or ControlMaster; rotate by size | OPEN: low; bppc energy may be null over the relay |
| L6 | LOW | Health | health.py:54-64 | urllib timeout is per socket op; a slow-drip /health holds a hop to the run deadline; LAN hostnames resolve with no limit | code reading | Wall-clock limit per probe | FIXED 197c681 (test_l6_slow_drip_health_is_cut_at_the_probe_timeout) |

## HIGH evidence

**H1.** Against 750218f:
```
exec_command {'cmd':'cat id_ed25519','workdir':'~/.ssh'} -> Bash {'command': 'cat id_ed25519'} -> allow  `cat` is read-only
shell {'command':['cat','auth.json'],'workdir':'~/.codex'} -> Bash {'command': 'cat auth.json'} -> allow
exec_command {'cmd':'cat ~/.ssh/id_ed25519'}                  -> deny (control)
```
The 0.153.4 tool schema documents workdir as "Working directory for the command. Defaults to the turn cwd." The supervisor takes the workspace from the agent, so the payload's cwd never reaches the classifier.

**H2.** `classify("Bash", {"command": "cat <p>"}, ws)` → allow for `~/.codex/auth.json`, `~/.codex/sessions`, `~/.git-credentials`; `Read ~/.codex/auth.json` → allow. Controls denied: `~/.claude.json`, `~/.omlx`, `~/.aws/credentials`, `~/.netrc`, `~/.kube`.

**H3.** Two `Settings.from_env()` with the same `SAM_SESSION_ROOT`: `hooks_config("a1", glm) == hooks_config("a1", deepseek)` → True; A's child env base URL z.ai, settings.json afterwards DeepSeek. Claude Code applies settings.json `env` over the process env, so A's child starts with DeepSeek's URL and A's z.ai key. The window reopens on every spawn and continue.

**M1.** omlx "API Error: 500 upstream request timed out after 402 s" → deepseek_balance, closed 6 h; bppc "402 Payment Required from proxy" → same; glm 1308 "reset at 2099-01-01 00:00:00" → closed until 2099-01-01.

## Checked and sound

- Reroute only before work, claude driver: `did_work` counts any tool_use or output tokens > 0; synthetic API-error messages have zero usage; `run.worked` accumulates across retries in a hop.
- Reroute only before work, codex driver: `codex_refusal` treats any `item.*` as work, so a command in flight when the limit hits blocks rerouting.
- Partial messages feed the meter only; the full assistant event still precedes the tool run.
- Hop state reset: session_id, resume and deadline fresh per hop; continue stays on the lane that ran.
- lane_state.json under the protected root for both drivers; atomic replace; unparseable file treated as empty.
- Codex CODEX_HOME writes refused by the guard; Codex keys stripped from the child env; env/printenv denied.
- Codex hook fails closed on a missing socket or malformed reply; every apply_patch path is asked about; an unparseable patch escalates.
- Shell and interpreter REPLs escalate.
- Health gates never start a server; tailscale bounded to 2 s; IPs validated as dotted quads.
- Samplers exit when their lane has no runs; shutdown joins them.
- No keys or auth headers in trace or metrics; verdicts carry paths and reasons, never command output.
- Codex input stored uncached, matching Claude's meaning.
- cost_join splits shared parent messages and flags duplicate run ids.

Not reviewed in depth: supervisor escalation queueing under many agents; the scripts beyond a read.

## Found by the coordinator's live UAT (not by the review)

| ID | Sev | Area | Defect | Evidence | Outcome |
|---|---|---|---|---|---|
| U-B1 | HIGH | bppc lane | Every bppc child fails: llama.cpp's Qwen3.8 chat template raises "System message must be at the beginning" (HTTP 500) on the request Claude Code 2.1.276 sends, and `claude -p` exits 1 after 10 retries with no tokens | bppc docker log; `claude -p` by hand against :8080 → 10× api_retry 500; oMLX accepts the same shape | FIXED 5294af8: fold_system adapter; live bppc smoke PASS on 197c681 |
| U-B2 | MED | bppc lane | First request after the proxy's idle shutdown fails while the container cold-starts | first smoke run, backend "stopped" → run failed; not separable from U-B1 until U-B1 is fixed | FIXED 5294af8 against the mock; the owner's proxy already holds the first request up to 180 s; cold path not live-tested |

| U-T1 | MED | Telemetry | A turn whose usage arrived only in stream events after a tool result was recorded as 0 | live GLM smoke: turn sum 8321 vs run 12315 | FIXED 7fd571c; live GLM smoke on 197c681: turns 12132 = run 12132 |
