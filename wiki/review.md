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

## 2026-09-23 — fused tool review

Reviewed the requested README, delegate skill, full example config, and implementation paths for providers, routing, run registry, guard, loop, telemetry, report, CLI/MCP, and benchmark. Findings below are source-traced. A probe script could not be staged because the command reviewer rejected `mkdir -p /tmp/review-ws` as not being on its read-only list; no tests were run. Existing regression coverage was inspected where relevant.

### 1. Guard context and protected state

**MED — direct file tools can overwrite the checkpoint refs that shell checks protect.** [`classify.py:1567`](../src/subagent/guard/classify.py#L1567), [`classify.py:1143`](../src/subagent/guard/classify.py#L1143), [`checkpoint.py:53`](../src/subagent/loop/checkpoint.py#L53)

`_state_git_denial()` runs only from `classify_bash()`. A `Write`/`Edit` tool call instead checks protected tests and `.subagent/` and then reaches `classify_path_write()`. That function protects `.git/hooks` and `.git/config`, but not `.git/refs/subagent/`. In a normal checkout with a `.git` directory, a worker can overwrite `refs/subagent/checkpoints/<id>` with a file tool call; checkpoint restore then cannot resolve the saved ref, so the loop loses that undo point.

Reproduction: with context `{"protected": [], "state_allow": [".subagent/result.json"]}`, classify `Write` to `<workspace>/.git/refs/subagent/checkpoints/<existing-id>`; the path is inside the workspace and the verdict is allow. The equivalent `git update-ref ... refs/subagent/...` shell command is denied.

Suggested fix: apply the refs rule to every write surface, including file tools, and resolve the repository's actual gitdir before checking paths (worktrees may use a `.git` file).
Outcome: fixed (src/subagent/guard/classify.py:463)

No separate repro was found for the reviewed case-folding, resolved-symlink, literal heredoc, inline `python -c`, or shell git-plumbing checks. The direct file-tool gap above is the concrete exception.

### 2. Loop, Registry, and approval lifecycle

**MED — `Registry.adopt()` trusts persisted IDs for both map keys and agent-home paths.** [`runs.py:1531`](../src/subagent/runs.py#L1531), [`runs.py:1574`](../src/subagent/runs.py#L1574), [`config.py:420`](../src/subagent/config.py#L420)

Adoption neither rejects an ID already in the registry nor validates its path components. A second record with the same ID replaces the registered agent, while `Settings.agent_home()` joins the raw ID into `session_root/agents/` and provider boot writes under that path. A crafted `.subagent/session.json` resumed with `--continue` can therefore reuse another agent's home or use an ID such as `../../outside` to put that home outside the session root. Direct guard rules deny edits to that state file, but an allowed workspace script can modify it and the later continue trusts the record.

Reproduction: call `adopt()` twice with the same declared provider and `agent_id`; the second `self._agents[agent_id] = agent` replaces the first. For path escape, adopt a record with `agent_id="../../outside"`; `agent_home()` resolves outside `session_root` and creates the resulting provider home on boot.

Suggested fix: accept only validated opaque IDs, require resolved homes to remain under the agents root, reject live ID collisions, and verify any recorded home instead of deriving it from unchecked input.
Outcome: fixed (src/subagent/runs.py:1549)

**MED — parallel mode turns an agent limit into a runner crash and leaked worktrees.** [`loop.py:599`](../src/subagent/loop/loop.py#L599), [`loop.py:611`](../src/subagent/loop/loop.py#L611), [`loop.py:620`](../src/subagent/loop/loop.py#L620), [`runs.py:1498`](../src/subagent/runs.py#L1498)

`run_parallel()` launches one thread per task without limiting the count. With two manifest tasks and `max_agents = 1`, one `Worker.run_round()` can raise `RegistryError` in its thread; the thread never sets `w["run"]`, and the join loop later indexes that missing key. The top-level command records `runner_error`; worktree cleanup is below the failing access and is skipped.

Reproduction: run a valid two-task `--parallel` manifest with `[core].max_agents = 1`; one worker starts and the other hits the registry cap, after which the run reports `KeyError: 'run'` and leaves worktree registrations behind.

Suggested fix: validate parallel task count against available agent capacity before creating worktrees, and put per-worker result initialization plus worktree/guard cleanup in `finally` paths.

Outcome: open — parallel mode is not exercised by the benchmark yet; fix scheduled with the parallel-worker unit (active-work). Until then a provider with `max_agents = 1` must not be used with `--parallel`.
**MED — a configured approval socket path breaks `run --background` after the parent exits.** [`supervisor.py:339`](../src/subagent/guard/supervisor.py#L339), [`supervisor.py:349`](../src/subagent/guard/supervisor.py#L349), [`loop.py:1109`](../src/subagent/loop/loop.py#L1109), [`cli.py:557`](../src/subagent/cli.py#L557)

The parent and respawned CLI child load the same explicit `[guard].approval_socket`. The child unlinks and rebinds that pathname in `serve()`. The parent then reaches `server.stop()` and `cleanup()` unlinks the child's socket path. Subsequent hook calls fail closed because no socket is reachable, so the background worker cannot use guarded tools.

Reproduction: set a fixed `approval_socket`, run `subagent run --background`, and let the parent return; inspect the configured socket path before the child finishes. Parent cleanup removes it even though the child listener is active on the now-unlinked socket inode.

Suggested fix: allocate a per-process socket path for every server regardless of config, or transfer socket ownership to the child and make cleanup inode/owner-aware.
Outcome: fixed (src/subagent/cli.py:554)

**No finding: `pre_verify` ordering on normal completion, cancellation, timeout, and kill.** `Agent._execute()` calls `_run_pre_verify()` after `_turn()` returns and before it checks terminal/cancel state or starts verification ([`runs.py:879`](../src/subagent/runs.py#L879)); the timeout path kills the child before `_turn()` returns. The loop also releases the test guard on its exception safety path. I found no sequence in these paths where verification runs against the still-locked test tree.

### 3. Provider drivers and secrets

**HIGH — provider subprocesses inherit undeclared secrets from the server environment.** [`config.py:431`](../src/subagent/config.py#L431), [`config.py:492`](../src/subagent/config.py#L492), [`codex.py:376`](../src/subagent/providers/codex.py#L376), [`grok.py:138`](../src/subagent/providers/grok.py#L138)

All these child environments start as a copy of `os.environ` and remove only configured `api_key_env` names plus a short fixed Claude list. Variables such as `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, or a provider key not declared in this config survive into the worker CLI. The worker can write and run a workspace script that reads `os.environ` and prints the value into its tool output; the model then receives the secret.

Reproduction: launch with `AWS_SECRET_ACCESS_KEY=sentinel`, configure only a provider key such as `GLM_API_KEY`, and run a workspace script containing `print(os.environ.get("AWS_SECRET_ACCESS_KEY"))`. The configured key is stripped/remapped, but the sentinel remains in the child environment and prints. This follows from the child-env construction even without a network call.

Suggested fix: construct child environments from an allowlist of required runtime variables and explicit provider auth, rather than inheriting the server's full environment.
Outcome: fixed (src/subagent/config.py:437)
Regression follow-up: fake CLI controls and record paths are listed by test fixtures via `[core].child_env_passthrough`; the setting is documented in `README.md`, while secret-shaped names remain filtered.

**MED — Copilot's documented loop exception is never set by the loop.** [`copilot.py:247`](../src/subagent/providers/copilot.py#L247), [`copilot.py:255`](../src/subagent/providers/copilot.py#L255), [`loop.py:158`](../src/subagent/loop/loop.py#L158)

Copilot boot requires either global `allow_unguarded = true` or `cfg.extra["loop"]`; its comment says the delegate loop sets the latter. `Worker._make_agent()` passes the configured provider unchanged, and no loop path sets that extra. With the default guard setting, choosing Copilot for a loop worker fails at boot before the loop's own test/checkpoint protections can run. Turning on the global option also permits ordinary MCP Copilot delegation.

Reproduction: configure Copilot as a loop tier with default `allow_unguarded = false`, then run a loop round using that tier; provider boot returns the unguarded-child error although this is the delegate loop.

Suggested fix: pass a scoped loop capability into provider boot, without mutating shared config or broadening the MCP setting.

Outcome: fixed (src/subagent/providers/copilot.py:52,287 — `allow_loop_agent()` registers the loop's own agent ids and `boot()` admits exactly those; src/subagent/loop/loop.py:59 registers before every create/adopt)

No separate finding: Copilot mints a UUID session ID at boot and uses that same ID for `--session-id` and saved-session resume. The Grok driver removes the configured key variable and maps its configured value to `XAI_API_KEY`. Refusal classification passes the vendor into the router's vendor-specific rules. Codex translates one `turn.completed` usage object into one run result; the source and fixtures do not establish that resumed Codex usage is cumulative, so I am not reporting double counting as a defect.

### 4. Configuration and initialization

**MED — Claude providers without any key declaration load without a missing-key warning.** [`config.py:231`](../src/subagent/config.py#L231), [`config.py:604`](../src/subagent/config.py#L604), [`runs.py:871`](../src/subagent/runs.py#L871)

`api_key_env` and literal `api_key` are optional in `_provider()`. The loader warns about an absent environment key only when `api_key_envs` is nonempty. A Claude provider with `base_url` and `model`, but neither auth field, therefore loads silently and fails only when a run reaches the late `needs_api_key` check.

Reproduction: load a config with a Claude driver, reachable `base_url`, and model but no `api_key_env` or `api_key`; `load()` returns settings with no warning, then the first run fails with `missing API key ... (none configured)`.

Suggested fix: validate required driver fields and warn or fail at load time when a key-required driver has no declared key source.

Outcome: fixed (src/subagent/config.py:231,622 — load() warns when a driver whose driver object needs a key declares neither `api_key_env` nor `api_key`)

No finding: the requested tier/review/test-writer provider names are checked against declarations by `_validate()`; config layering follows the documented user → project → `SUBAGENT_CONFIG` order; and `subagent init` writes key variable names/placeholders, not secret values ([`cli.py:128`](../src/subagent/cli.py#L128)).

### 5. Telemetry and cost

**MED — auto delegation totals omit test-writer and test-review runs that carry the same delegation ID.** [`loop.py:850`](../src/subagent/loop/loop.py#L850), [`loop.py:1031`](../src/subagent/loop/loop.py#L1031), [`loop.py:1067`](../src/subagent/loop/loop.py#L1067), [`loop.py:412`](../src/subagent/loop/loop.py#L412)

`autopilot()` sets the trace delegation context before `_prepare_tests()`. The test writer and test reviewer therefore emit run records tagged with that delegation ID, and their credits are added to the returned `worker_credits`. However, `_delegation_record()` totals only `all_blocks` from implementation rounds and code reviews; setup runs are not returned as `_runs` and never enter that list. The delegation `usage_total`, `credits_total`, and `cost_total` consequently disagree with the run records (and with `worker_credits`). The early `tests_questioned` return writes an empty block list despite those setup runs.

Reproduction: invoke `run --auto --test-outline ...`; compare tagged test-writer/test-review `run` records with the resulting `delegation` record. Their usage and credits appear in run records and returned `worker_credits`, but not in the delegation totals.

Suggested fix: retain run blocks for setup workers/reviewers and include them in the same totals, with explicit phases so reports can distinguish them.

Outcome: fixed (src/subagent/loop/loop.py:437,1013,1122 — the test writer's runs are now `_runs` phase-tagged `test_writer`, `_prepare_tests` collects them, and `_delegation_record(setup=...)` sums them into `usage_total`/`credits_total`/`cost_total`, including the `tests_questioned` hand-back)

**MED — flat-plan costs are spread per provider/month but never into delegation or bench totals.** [`cost.py:207`](../src/subagent/telemetry/cost.py#L207), [`cost.py:481`](../src/subagent/telemetry/cost.py#L481), [`loop.py:360`](../src/subagent/loop/loop.py#L360), [`report.py:313`](../src/subagent/report.py#L313)

Run-end flat-plan cost is `None`. The provider report later allocates monthly plan cost across runs, but `_delegation_record()` sums the original run costs and returns `provider_usd: null` if any block is null. The delegation table and bench CSV therefore show no worker cost even while the provider table shows a nonzero allocated cost.

Reproduction: configure one provider with `pricing.kind = "flat_plan"`, create a delegation with a run in the month, then compare provider and delegation report rows: provider cost is allocated; delegation cost remains null and `bench/run.py` converts it to no `worker_usd`.

Suggested fix: apply one documented allocation policy before grouping both provider and delegation costs, or report allocated cost at both levels as unavailable.

Outcome: fixed (src/subagent/telemetry/cost.py:477 — `provider_costs` spreads from the config's `PricingSpec` even when the legacy `[pricing]` table builds no `Pricing`; src/subagent/report.py:237,367,391,448 — one policy at report time: run-end cost, then the spread, then the recorded delegation total shared over its runs; delegation rows and the bench's `worker_usd` both go through it)

No finding: time-of-day handling is explicitly a peak/off-peak annotation, not a rate multiplier; the code does not claim to apply a discounted rate. Trace writes are locked within one `Trace` instance, and I found no concrete concurrent-cell `bench_run_id` bleed because benchmark cells run as separate processes with distinct environments.

### 6. Report and dashboard

**HIGH — provider names and delegation IDs reach SVG `innerHTML` without escaping.** [`report.py:367`](../src/subagent/report.py#L367), [`report_template.html:240`](../src/subagent/report_template.html#L240), [`report_template.html:259`](../src/subagent/report_template.html#L259), [`report_template.html:267`](../src/subagent/report_template.html#L267)

The HTML report safely embeds JSON against a literal `</script>` close, and table cells use `textContent`. The chart path does not: provider names and delegation IDs are concatenated into SVG markup and assigned to `svg.innerHTML`. A malicious or externally supplied trace can therefore inject active SVG/HTML when a user opens the dashboard.

Reproduction: put provider name `</text><image href=x onerror=alert(1) />` in a trace, run `subagent report --trace malicious.jsonl --html report.html`, and open the report. `JSON.parse()` restores the string, `drawBars()` concatenates it into the `<text>` label, and the SVG parser creates the injected image with an error handler.

Suggested fix: build SVG nodes with `createElementNS()` and assign labels with `textContent`; do not interpolate trace strings into markup.
Outcome: fixed (src/subagent/report.py:367)

No finding: empty traces produce empty chart/table inputs, and the chart returns before division when labels are empty; provider rate aggregation groups only nonempty run sets, so its denominator is nonzero. Task text is not persisted into the report data path reviewed.

**MED — the report's provider table is keyed by vendor (`zai`) while delegations say `glm`.** [`report.py`](../src/subagent/report.py#L159), [`report.py:267`](../src/subagent/report.py#L267)

Run rows key the provider table off the trace's raw `provider`/`lane` tag, so records written under a vendor tag group separately from the same provider named in the delegation records, and no row says which vendor stands behind it.

Reproduction: feed a trace whose `run` records say `"provider": "zai"` alongside `delegation` records whose `rounds` say `"provider": "glm"`; the tables disagree.

Suggested fix: key both tables by provider name (mapping a vendor tag to the one provider that declares it) and show the vendor as a column.

Outcome: fixed (src/subagent/report.py:152,174,293,310 — `_aliases()` maps vendor tags to the provider name, run and delegation rows are keyed through it, and each provider row carries a `vendor` column; test_report.py::test_vendor_tagged_runs_group_under_the_provider_name)

**MED — `prov_usd` is null for a flat-plan provider declared in the TOML config (`[providers.glm.pricing] monthly_usd = 80`).** [`cost.py:481`](../src/subagent/telemetry/cost.py#L481), [`report.py:237`](../src/subagent/report.py#L237)

Spreading read only the legacy `Pricing.providers` table, and `_price_row` reset `provider_usd` to null whenever the run record carried no `cost` dict — so a flat-plan provider declared in the config's `[providers.<n>.pricing]` still reported null in the lane table.

Reproduction: `subagent report --trace <trace>` with the provider's plan declared under `[providers.glm.pricing]`; `prov_usd` is null although the spread had the monthly fee.

Suggested fix: spread from the config's `PricingSpec` and stop clobbering a spread value at `_price_row` time.

Outcome: fixed (src/subagent/telemetry/cost.py:477; src/subagent/report.py:237,417 — the spread uses the config's PricingSpec with no `Pricing` required, `_price_row` keeps it, and with no config at all the recorded delegation total is shared over its runs as a fallback)

### 7. Benchmark and business case

**MED — different config files can generate the same cell ID and collide on one workspace.** [`run.py:65`](../bench/run.py#L65), [`run.py:267`](../bench/run.py#L267), [`run.py:452`](../bench/run.py#L452)

`cell_id` uses only `config_label()`, which returns the file stem. Two configs in different directories with the same stem produce identical cell IDs for the same harness/task/mode/run. The first `copytree()` target is reused; with `--jobs > 1` the workers race, and with serial jobs the later cell still raises `FileExistsError`.

Reproduction: pass `--config /tmp/a/config.glm.toml /tmp/b/config.glm.toml` for the same task and run; both expand to the same output directory and `cell_id`.

Suggested fix: include a stable hash of the resolved config path/content in the cell ID and all result filenames.

Outcome: fixed (bench/run.py:73,450 — `config_tag()` is a sha256 of the resolved path + content and every cell id/result filename carries it)

**MED — `--from-report` ignores the report's measured wall time and verified rate fields.** [`report.py:267`](../src/subagent/report.py#L267), [`report.py:271`](../src/subagent/report.py#L271), [`report.py:281`](../src/subagent/report.py#L281), [`business_case.py:485`](../bench/business_case.py#L485), [`business_case.py:496`](../bench/business_case.py#L496)

The report JSON emits `wall_p50` and `verified_rate`, while the business-case loader reads `wall_p50_s` and `verified_pass_rate`. Feeding the tool's own report JSON therefore silently skips measured throughput/parity inputs and keeps defaults, despite finding the requested provider rows.

Reproduction: pass `subagent report --json` output to `bench/business_case.py --from-report`; inspect `report_overrides()`: values under the emitted field names are ignored.

Suggested fix: align the report schema and consumer names, and reject or visibly warn when requested measured fields are absent.

Outcome: fixed (bench/business_case.py:501,515,719 — `report_overrides` reads both spellings (`wall_p50_s`/`wall_p50`, `verified_pass_rate`/`verified_rate`) and `build_inputs` prints a warning when a report row carries none of the measured fields)

**MED — ROI is calculated for a fleet that cannot serve the declared demand.** [`business_case.py:297`](../bench/business_case.py#L297), [`business_case.py:417`](../bench/business_case.py#L417), [`business_case.py:430`](../bench/business_case.py#L430)

With `hardware_count` fixed below `units_needed`, `capacity()` reports a nonzero shortfall but `model()` still prices that undersized fleet and computes break-even/ROI against the full cloud bill. A positive ROI can thus be shown for a local option that cannot handle the modeled workload.

Reproduction: choose workload needing two units, set `hardware_count = 1`, and use cloud costs above the one-unit local cost; the output simultaneously reports a capacity shortfall and positive ROI.

Suggested fix: mark ROI and break-even infeasible when installed capacity is below demand, or calculate the served workload and cloud remainder explicitly.

Outcome: fixed (bench/business_case.py:426-460 — `undersized_fleet` is set when a stated `hardware_count` leaves a shortfall; break-even and ROI become None while the priced fleet's costs are still reported, and the Markdown says so)

**LOW — an agent-created hidden-test destination aborts the cell instead of producing a hidden-test result.** [`run.py:87`](../bench/run.py#L87), [`run.py:92`](../bench/run.py#L92), [`run.py:97`](../bench/run.py#L97)

After the worker finishes, hidden tests are copied into fixed paths inside its workspace with `copytree()` and no collision handling. If a worker or task has created `_hidden_tests/` (Python) or `.hidden/` (Node), the copy raises; the future then aborts result collection rather than recording that hidden tests could not run.

Reproduction: leave the corresponding reserved directory in the cell workspace before `run_hidden_tests()`; `shutil.copytree()` raises `FileExistsError`.

Suggested fix: use a fresh external temp directory for the hidden suite and report copy/run failures as explicit failed cells.

No finding: each orchestrator parser has a malformed-output test that returns a failed-cell result rather than raising on plain garbage. Business-case ROI sign formulas are consistent with the stated cash basis; the concrete arithmetic defect found is the capacity shortfall being ignored above. No additional time-of-day pricing defect was found.

### Verdict

Outcome: open — cosmetic for the matrix (the cell is reported as failed rather than as a hidden-test result); fix scheduled with the next bench change.
**FAIL for unattended use on a developer's machine.** A worker subprocess receives undeclared environment secrets, the HTML dashboard can execute markup supplied by a trace, and direct file tools can corrupt the loop's checkpoint refs despite the shell guard. The Registry and background-socket lifecycle also have reproducible state/path and availability failures, while benchmark and accounting defects can silently produce misleading results. The test-release ordering and several reviewed parser/config paths appear sound, but these remaining issues are enough that unattended runs are not safe until the high-severity exposures and state-integrity gaps are closed.
