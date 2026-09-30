# V2 Build Specification

This page is the build contract for items 5, 6, 7, 8, 10, 11, 12, 13 and 14 in the v2 goal. A harness is the coding-agent command-line app that starts the orchestrator. A provider is one configured worker backend; its driver is the program adapter that starts that worker. The owner is the person running subagent. The command-line interface (CLI) is the subagent command. The Model Context Protocol (MCP) server exposes the same runtime as tools. A trace is the append-only JSON Lines file of run events; its schema version pins the record shapes. The guard is the deterministic classifier that allows or denies worker tool calls. A provider family is a vendor/driver family; a cross-family reviewer comes from a different family than the worker. A UAT row is one user acceptance check attached to a goal. In the invariant lists, identity names the authoritative input, effect durability means results persist, state after failure names the terminal state and cleanup, guarantee wired in means the control reaches the runtime, and units names the measurement units.

The package supports Python 3.11 and later. Keep tests offline. Do not put secret values or machine-specific endpoints in source. The owner’s Claude login described in Item 7 is the only exception to the child credential allowlist, and its scope is stated there. Existing trace schema version 3 remains compatible through additive fields.

## Item 5

### Behaviour

Add provider configuration commands and a command for selecting the default provider.

- **subagent provider add NAME --from PRESET [--project] [--force]** copies the provider table from examples/config.PRESET.toml. PRESET is an example filename stem. NAME is the key written under providers. If the preset has a different default provider name, rewrite that one provider key to NAME. Never copy environment variable values.
- **subagent provider list [--json]** lists providers in the effective layered config and marks the default. JSON is one object with default_provider and providers, with the same names and non-secret values; redact api_key and never print a value obtained from api_key_env.
- **subagent provider remove NAME [--project]** removes that provider table. Refuse to remove the current default until another provider is selected.
- **subagent provider test NAME [--prompt] [--json]** runs the same binary, key-presence and configured health checks as doctor for NAME. --prompt additionally runs one provider turn, as doctor --prompt does.
- **subagent use NAME [--project]** sets core.default_provider to NAME.
- --project on a mutating command writes only <project-root>/.subagent/config.toml; otherwise writes the user config at $XDG_CONFIG_HOME/subagent/config.toml, or ~/.config/subagent/config.toml when XDG_CONFIG_HOME is unset. Merge into the selected file without flattening other config layers.
- --force permits replacing an existing provider table during add. It does not permit replacing unrelated tables.

Success exits 0. An unknown preset prints error: unknown example 'NAME'; examples: LIST and exits 1. An unknown provider prints error: unknown provider 'NAME' and exits 1. Adding an existing NAME without --force prints error: provider 'NAME' already exists; use --force and exits 1. A provider add with neither --from nor --url prints error: provider add requires --from PRESET or --url URL and exits 2. A missing required flag or an invalid option combination otherwise is argparse usage output and exits 2. A config read or write error prints error: MESSAGE and exits 1.

### Seams

- src/subagent/cli.py: _parser (526), main (616), _example_names (62), _load_example (76), _write_config (143), _dest_path (68), and the existing config merge helpers (85–126).
- src/subagent/config.py: read_config (143), Settings.provider (441), and the existing TOML layer order (128–153).
- New: src/subagent/commands/providers.py, with parser registration and add/list/remove/test/use handlers. cli.py imports and registers this command group in one place.
- New: tests/test_cli_provider.py.

### Records

No trace fields. A provider table is an ordinary [providers.NAME] TOML table based on an example. --project writes .subagent/config.toml under the CLI project root; it does not change the user config.

### Invariants

N/A; this unit only writes local configuration. tests/test_provider_add_never_writes_secret_values verifies that neither api_key nor an environment value is written.

### Tests

tests/test_cli_provider.py: test_provider_add_copies_preset_without_secret_values, test_provider_add_never_writes_secret_values, test_provider_add_project_writes_project_config, test_provider_add_existing_requires_force, test_provider_list_json_redacts_credentials, test_provider_remove_keeps_other_tables, test_use_sets_default_provider, test_provider_test_uses_doctor_checks.

Acceptance: uv run pytest tests/test_cli_provider.py -q; uv run subagent provider --help; uv run subagent use --help.

### Out of scope

Interactive settings UI, hand-authoring arbitrary driver tables, provider migration, and changing config layer precedence.

## Item 6

### Behaviour

subagent init discovers installed subscription command-line tools and offers their provider configurations in the existing wizard. Detection uses shutil.which for claude, codex, copilot, gemini and grok; it does not launch them or contact a network. Interactive init lists detected tools alongside examples and asks which to include. With --yes, include each detected tool. A missing executable is not an error and is not offered. Generated provider tables name the existing driver, contain no api_key or api_key_env value, and use pricing.kind = "flat_plan" with monthly_usd omitted because flat-plan pricing records subscription use without assigning a known cash price to the run. The Claude entry uses auth = "login" as described in Item 7. The existing --path selects the config destination; --force keeps its existing overwrite meaning. No new flag is required.

Successful writes exit 0. No detected tools prints No supported subscription CLI detected. and continues through the normal example flow. Interactive init asks which detected tools to add. Non-interactive init with --yes adds all detected tools to the selected example seed; when --from is omitted, use the full example seed. An unknown example prints error: unknown example 'NAME'; examples: LIST and exits 2. An unwritable destination prints error: cannot write config 'PATH': REASON and exits 1. Invalid CLI syntax exits 2.

### Seams

- src/subagent/cli.py: DRIVER_BINARY (41), _example_names (62), _wizard (205), _write_config (143), cmd_init (259), and _parser init arguments (526–547).
- src/subagent/config.py: ProviderConfig construction through _provider (273) and pricing parsing through _pricing (246).
- New: src/subagent/commands/init.py, which detects and renders the detected provider choices; cli.py owns its one-line registration.
- New tests: tests/test_cli_init.py.

### Records

No trace changes. Init writes provider tables and no credential values. Each discovered subscription uses flat_plan pricing with no monthly_usd value.

### Invariants

N/A; detection is local and read-only. test_init_detect_does_not_run_or_write_to_detected_clis verifies detection only checks executable presence.

### Tests

tests/test_cli_init.py: test_init_detect_does_not_run_or_write_to_detected_clis, test_init_detect_offers_installed_clis_without_keys, test_init_detect_skips_missing_clis, test_init_detect_yes_adds_detected_subscription_tables.

Acceptance: uv run pytest tests -q -k init_detect.

### Out of scope

Installing or logging in to provider CLIs, testing network availability, and inferring a paid plan’s dollar price.

## Item 7

### Behaviour

Add the optional provider key auth = "login" for the claude driver. If auth is absent, retain the current API-key behavior. The only accepted auth value is login, and it is valid only with driver = "claude". Login providers do not require api_key_env and must reject that key when it is configured, with error: provider 'NAME' auth = 'login' cannot set api_key_env and exit 1. Their child runs claude -p against the normal Anthropic service, using the owner’s Claude Code login and quota. Doctor prints the warning "warning: provider 'NAME' uses the owner's Claude login and Claude plan quota" on every check of that provider, including JSON output as a warning field.

For a login provider, the child receives the normal base allowlist already defined by Settings.base_child_env: PATH, HOME, LANG, TERM, TMPDIR, LC_* and explicitly configured non-secret child_env_passthrough names that pass the existing secret-name filter. Set CLAUDE_CONFIG_DIR to the resolved parent CLAUDE_CONFIG_DIR when present, otherwise to $HOME/.claude. Keep the generated hook/settings file at the agent’s separate session-root path. Do not pass or populate ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN or another provider’s key. The config directory is the sole added credential path: Claude Code reads its existing login there. This opt-in lets that process use the owner’s account and plan quota, so the doctor warning and flat-plan label are required.

examples/config.claude.toml declares a claude provider with auth = "login" and [providers.claude.pricing] kind = "flat_plan"; monthly_usd is omitted. Run accounting stays token based, and report labels it as plan usage with unknown cash price. Unsupported auth prints error: provider 'NAME' auth = 'login' requires driver = 'claude' and exits 1. An invalid auth value prints error: provider 'NAME' auth must be 'login' and exits 1.

### Seams

- src/subagent/config.py: _PROVIDER_KEYS and _provider (253–311), Settings.guard_secret_env_names (483), base_child_env (495), hooks_config (511), child_env (557), and _validate (619).
- src/subagent/providers/base.py: ProviderConfig (116).
- src/subagent/providers/claude.py: ClaudeProvider.argv (235), env (281), and spawn (291).
- src/subagent/cli.py: _doctor_row (351), cmd_doctor (432), and _print_table (390).
- src/subagent/telemetry/cost.py: price_run (163), its flat_plan branch (209), and provider_costs (477).
- New: examples/config.claude.toml and tests/test_claude_auth.py.

### Records

No trace shape change. Existing run pricing records keep kind = "flat_plan" and a note that the provider uses a subscription plan; absent monthly_usd means no cash price is claimed. Do not record a credential path or credential value in trace output.

### Invariants

- Identity: only the selected login provider receives CLAUDE_CONFIG_DIR pointing to the owner’s Claude config; test_auth_login_is_scoped_to_selected_provider.
- Effect durability: an attempted login run records its normal provider usage and flat-plan label, including when the child fails; test_auth_login_plan_use_is_recorded_on_failure.
- State after failure: missing or rejected login leaves the run failed with finish_reason = "auth" and does not copy or replace the owner’s config; test_auth_login_failure_keeps_owner_config_unchanged.
- Guarantee wired in: ClaudeProvider.env passes the selected login directory to the actual child process; test_auth_login_context_reaches_claude_child.
- Units: provider usage remains integer token counts and flat-plan cash stays unknown when monthly_usd is absent; test_auth_login_flat_plan_keeps_unknown_cash_unknown.

### Tests

tests/test_claude_auth.py: test_auth_login_is_opt_in, test_auth_login_is_scoped_to_selected_provider, test_auth_login_doctor_warns_about_owner_quota, test_auth_login_context_reaches_claude_child, test_auth_login_failure_keeps_owner_config_unchanged, test_auth_login_plan_use_is_recorded_on_failure, test_auth_login_flat_plan_keeps_unknown_cash_unknown.

Acceptance: uv run pytest tests -q -k auth_login; test -f examples/config.claude.toml.

### Out of scope

Making login the default, importing or copying the owner’s credential files, using the login for another provider, and charging an estimated monthly price.

## Item 8

### Behaviour

Add **subagent provider add local --url URL [--project] [--force]**. URL is the user-supplied base URL and is normalized without a trailing slash. Probe, in order, URL/v1/models, URL/api/status, URL/metrics and URL/slots, with a two-second bound per request and no environment proxy. An OpenAI-compatible models response must contain at least one model id; use its first id. An oMLX local server status response must be JSON with status = "ok"; use the configured model id from loaded_models when present, otherwise use the model id returned by /v1/models. If /metrics or /slots answers, store its URL in the probe table. The generated provider is named local, driver = "omp", local = true, and includes the discovered model plus matching health and probe tables. Do not start, load or wake the server.

Success exits 0. If no models endpoint answers with a usable model, print error: no supported local model endpoint at 'URL' and exit 1. If a responding server does not expose a usable model, print error: local endpoint at 'URL' returned no model id and exit 1. Invalid URLs and unsupported flags are usage errors and exit 2. Network failures are summarized; response bodies and credentials are not printed.

### Seams

- src/subagent/cli.py: _parser (526) and command dispatch in main (616).
- src/subagent/commands/providers.py: new add-local handler in the provider command group.
- src/subagent/health.py: _http_get (75), check_http (194), check_omlx (209), and omlx_cold (231).
- src/subagent/config.py: _health (219), _probe (233), and _provider (273).
- src/subagent/providers/omp.py: OMP driver and model argument construction (385).
- New tests are in tests/test_cli_provider.py and use the existing mock_endpoint fixture.

### Records

The written provider is [providers.local], with driver = "omp", base_url = URL, model = MODEL, local = true, [providers.local.health] set to kind = "omlx" and its status URL when oMLX is detected (otherwise kind = "http" and the discovered health URL), and [providers.local.probe] containing only metrics_url and slots_url that answered. No trace record is created by discovery.

### Invariants

N/A; discovery has no model-run side effect. test_add_local_does_not_start_or_load_server verifies that discovery is limited to bounded GET probes.

### Tests

tests/test_cli_provider.py: test_add_local_reads_openai_models_response, test_add_local_detects_omlx_health, test_add_local_fills_health_and_probe_urls, test_add_local_rejects_empty_model_list, test_add_local_does_not_start_or_load_server. Every function selected by -k add_local includes that keyword.

Acceptance: uv run pytest tests -q -k add_local.

### Out of scope

Cloud-provider discovery, choosing among several models interactively, editing existing health endpoints, starting a model server, and setting a machine-specific default URL.

## Item 10

### Behaviour

Add **subagent install --for {claude,codex,gemini,copilot} [--target-dir DIR] [--timeout SECONDS] [--force]**. `--target-dir` wins over `SUBAGENT_INSTALL_ROOT`; if neither is set, use the global `--root` project directory or the current directory. DIR is the project root for all files written in this install. Every install test supplies a temporary target directory or sets `SUBAGENT_INSTALL_ROOT` to one.

Install `skills/delegate/SKILL.md` under `.claude/skills/delegate` for Claude, or `.agents/skills/delegate` for Codex, Gemini and Copilot, matching the skill locations used by the benchmark harness. Where the harness needs a project instruction file, add one concise paragraph pointing to that skill: `.gemini/GEMINI.md` for Gemini and `.github/copilot-instructions.md` for Copilot. The `/plan` prompt asks the orchestrator to write a plan and finish it with a `## Goal block` containing the required header and consecutive rows; it does not implement the plan. The `/goal` prompt writes workspace-root `GOAL.md` with only that goal block, wrapping executable checks in one pair of inline backticks and leaving prose checks unwrapped. Store these prompt texts in skills/plan/SKILL.md and skills/goal/SKILL.md. Install `/plan` and `/goal` as the harness's native project prompt files: `.claude/commands/{plan,goal}.md`, `.codex/prompts/{plan,goal}.md`, `.gemini/commands/{plan,goal}.toml`, and `.github/prompts/{plan,goal}.prompt.md` respectively. The Gemini TOML command has `description` and `prompt` string keys; Copilot prompt names are `plan` and `goal`.

Register one stdio MCP server named `subagent` in the selected harness's project config. Claude and Copilot use `<target>/.mcp.json` under `mcpServers.subagent`; Claude's entry has `command = "subagent-mcp"` and `timeout = SECONDS * 1000`, while Copilot's has `type = "local"`, `command = "subagent-mcp"`, `args = []`, `tools = ["*"]`, and `timeout = SECONDS * 1000`. Codex uses `<target>/.codex/config.toml` under `[mcp_servers.subagent]`, with `command = "subagent-mcp"` and `tool_timeout_sec = SECONDS`. Gemini uses `<target>/.gemini/settings.json` under `mcpServers.subagent`, with `command = "subagent-mcp"` and `timeout = SECONDS * 1000`. Merge each file as structured JSON or TOML, preserving unrelated keys. Copilot's timeout has a 60-second minimum; `--for copilot --timeout` below 60 is rejected because the CLI would apply that floor instead of the requested timeout. Claude's per-server timeout is in milliseconds and requires Claude Code 2.1.203 or later. See the [Claude Code MCP guide](https://code.claude.com/docs/en/mcp), [Codex configuration reference](https://developers.openai.com/codex/config-reference/), [Gemini CLI MCP guide](https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html), and [Copilot CLI MCP guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) for those client-defined field names and units.

`--timeout` is a positive integer in seconds, defaulting to the effective `[core].run_timeout` or 1800 seconds if no configuration supplies it. `--force` permits replacing only the existing `subagent` entry and the installer-owned instruction/command files. It preserves all unrelated config entries and file content outside its managed block. The success output lists the harness, target root, each path written, the registration config path and effective timeout in seconds.

Success exits 0. Invalid `--for`, a non-positive timeout, incompatible timeout for Copilot, or incompatible flags print argparse usage and exit 2. An existing `subagent` entry without `--force` prints `error: MCP server 'subagent' already exists` and exits 1. Invalid existing JSON/TOML or a filesystem failure prints `error: cannot update 'PATH': REASON` and exits 1; if earlier files were already written, include those paths in the error so the user can retry.

### Seams

- src/subagent/cli.py: `_parser` (526) and `main` dispatch (616).
- src/subagent/config.py: `Settings.run_timeout` (405) is the default timeout source.
- New: src/subagent/commands/install.py for parser and handler; new src/subagent/install.py for target resolution, harness path maps, structured file merge and per-harness MCP registration serialization.
- Source content: skills/delegate/SKILL.md plus new skills/plan/SKILL.md and skills/goal/SKILL.md, embedded or copied by install.py. Harness destinations are the paths listed above.
- New tests/test_install.py.

### Records

No trace changes. Installer output identifies the harness, resolved target directory, written instruction/command paths, registration config path, server name `subagent`, command `subagent-mcp`, and timeout in seconds. It writes no auth token or provider secret.

### Invariants

N/A; install tests use temporary directories and never invoke the installer against the user's home. `test_install_uses_only_temporary_target` proves the selected root controls every write.

### Tests

tests/test_install.py: test_install_claude_writes_skill_plan_goal_and_timeout, test_install_codex_writes_instruction_and_timeout, test_install_gemini_writes_instruction_and_timeout, test_install_copilot_writes_instruction_and_timeout, test_install_claude_registers_subagent_timeout_milliseconds, test_install_codex_registers_subagent_timeout_seconds, test_install_gemini_registers_subagent_timeout_milliseconds, test_install_copilot_registers_subagent_timeout_milliseconds, test_install_target_dir_overrides_environment, test_install_uses_environment_target, test_install_uses_only_temporary_target, test_install_preserves_unrelated_mcp_entries, test_install_existing_server_requires_force, test_install_config_write_failure_reports_partial_files.

Acceptance: uv run pytest tests/test_install.py -q; uv run subagent install --help.

### Out of scope

Installing package dependencies, authenticating a harness, registering any provider credential, Windows support, and executing installation against a real home directory in this build run.

## Item 11

### Behaviour

Add loop.auto as an enum with values ask, always and never; the default is ask. Doctor --json includes the non-secret field loop_auto. The delegate skill description says which task shapes trigger it: work expected to span at least about 200 lines, three files, or non-trivial branching, I/O, parsing, state or security behavior. When such a task is recognized, the skill reads loop_auto from doctor --json: ask requests user approval before delegation; always runs the existing delegate workflow without that extra prompt; never keeps the work with the orchestrator. A skill must not infer always from missing or malformed JSON; it reports the config error.

Invalid values print error: [loop].auto must be 'ask', 'always' or 'never' and exit 1. The JSON response includes loop_auto even when it is the default. No additional command is introduced.

### Seams

- src/subagent/config.py: LoopSettings (322), _loop_settings (350), and _validate (619).
- src/subagent/cli.py: cmd_doctor (432) and _print_table (390) JSON output.
- skills/delegate/SKILL.md: description front matter and top-level trigger workflow.
- New tests/test_loop_auto.py.

### Records

No trace change. [loop].auto is a TOML string; default ask. Doctor’s JSON field is loop_auto with one of ask, always or never.

### Invariants

N/A; this setting chooses whether the existing workflow starts. test_loop_auto_never_does_not_start_run verifies never cannot launch a worker.

### Tests

tests/test_loop_auto.py: test_loop_auto_defaults_to_ask, test_loop_auto_accepts_ask_always_never, test_loop_auto_rejects_other_values, test_loop_auto_is_reported_in_doctor_json, test_loop_auto_never_does_not_start_run.

Acceptance: uv run pytest tests -q -k loop_auto; grep -n "^description:" skills/delegate/SKILL.md.

### Out of scope

Changing the delegation plan, size-check thresholds, or the existing run/wait/review lifecycle.

## Item 12

### Behaviour

Add an optional goal parameter to **subagent run --goal VALUE** and the MCP tool **delegate(..., goal: str | None = None)**. VALUE is either inline goal-block text or a path to GOAL.md relative to the selected workspace. A goal block is UTF-8 text with the exact header Execute plan "<plan name>" (<plan file path or reference>). Goal rows: and consecutive rows in the form <n> <label>: <check>. If the input contains a line whose stripped value is ## Goal block, validate the text after it up to the next line beginning ## ; otherwise validate the whole input. Trim blank lines at the edges, cap the extracted block at 4,000 characters, require a non-empty plan name and reference in the header, require rows starting at 1 with no gaps, require a non-empty check, reject prose before row 1, and reject rows after trailing prose. Blank lines between rows are allowed. Reuse the validation rules and problem text of /Users/GaryT/Documents/Work/tools/pi-plan-goal/bin/goal-block-check.

Resolve a path against the workspace, require it to be a file inside that workspace, read it as UTF-8 and protect its canonical relative path from worker writes. A value beginning with the required header, or a non-file value containing a newline, is inline text; a single-line existing file path is a goal file. A single-line non-file value is treated as a path. Reject a non-UTF-8 file with error: input is not valid UTF-8. For a block problem, print error: goal block is invalid: PROBLEM for each validator problem and exit 1; preserve the validator text, including too long: N/4000 characters, missing header line: expected Execute plan "<plan name>" (<plan file path or reference>). Goal rows:, free text before row 1, rows not consecutive: expected N got M, row N: expected "<n> <label>: <check>", row N: missing check after ':', row N: row after trailing free text, and no goal rows. Invalid paths print error: goal file must be inside workspace or error: goal file not found: PATH. CLI path/block errors exit 1; argparse errors exit 2. MCP argument errors raise RegistryError with the same message and no numeric code.

Classify a check surrounded by one pair of inline Markdown backticks as a command and remove that pair before execution. Every other check is prose. This keeps command/prose classification explicit and lets the block keep the required row grammar. Run each command check in the selected workspace on the server after the final worker round, with [core].verify_timeout (default 300 seconds), shortened if the run deadline has less time. The existing verification guard classifier, which applies the worker tool-call policy to a command, must allow it; a denied or escalated command is a failed check and is not run. The result is the check against the final workspace state. Send each prose check to the existing cross-family reviewer as an explicit checklist row. A prose row passes when the cross-family reviewer finds no issue for it, fails on an explicit reviewer finding, and remains null if the review did not reach a verdict. A command UAT row passes only when its command exits 0. The prose verdict does not replace the ordinary command acceptance gate.

Add **subagent doctor --goal VALUE** to validate the same inline block or GOAL.md path and print warning: goal has no command rows; completion has no server-side goal check when it contains only prose. The warning exits 0; invalid input exits 1. A run with no command rows remains governed by the existing job verification.

Each result and the delegation trace record includes uat with exactly these fields per row: row (integer), label (string), kind (command or prose), command (the de-backticked command, or null for prose), passed (true, false or null while pending), and output_tail (string, capped at 2,000 characters; empty output is an empty string). Command failures produce completed_unverified and keep the failing row and output_tail. A run is completed only if the existing job verification passes and every command UAT row passes. The report dashboard displays the same UAT rows.

### Seams

- src/subagent/cli.py: _loop_parsers (470) adds --goal; cmd_doctor (432) adds --goal; _run_loop_command (568) forwards the parsed value.
- src/subagent/mcp_server.py: _result (158) and delegate (180) add the optional goal argument.
- src/subagent/loop/loop.py: build_prompt (106), run_round (525), cmd_run (1413), and _delegation_record (485) carry goal and UAT through the CLI workflow.
- src/subagent/runs.py: Run.detail (532), Agent.delegate (688), Agent._execute (864), Agent._run_pre_verify (921), Agent._finish (1339), and Registry.create_agent (1489) carry and gate the same goal in the MCP workflow.
- src/subagent/verify.py: VerificationResult.as_dict (47) and run_verification (63) provide server-side command execution and output tails.
- src/subagent/loop/testguard.py: TestGuard.__init__ (20), lock (39), release (45), and protected_paths (84) protect the goal path along with the existing protected tests.
- src/subagent/report.py: _delegation_row (333), _report_data (496), render_html (509); src/subagent/report_template.html renders the UAT list.
- New: src/subagent/goal.py exposes parse_goal(value, workspace) and immutable Goal/GoalRow objects with row, label, kind, and command fields. New: src/subagent/goal_runtime.py exposes evaluate_goal(goal, workspace, settings, reviewer) and returns the UAT list used by CLI and MCP.
- New tests/test_goal.py. tests/test_trace_schema.py changes to validate the additive delegation uat field.

### Records

Result field: uat = [{row: int, label: str, kind: "command" | "prose", command: str | null, passed: bool | null, output_tail: str}]. The same list is added to kind = delegation trace records. No other key changes meaning; tests/test_trace_schema.py changes and SCHEMA remains 3. Existing callers that omit goal receive no uat field.

### Invariants

- Identity: only the caller-supplied goal or its workspace file defines UAT rows; the worker cannot replace the source; test_goal_identity_uses_request_block_not_worker_text.
- Effect durability: after a check runs, its final result is written in the returned UAT and delegation trace; test_goal_command_outcome_is_written_to_delegation_trace.
- State after failure: a nonzero command leaves a usable terminal run in completed_unverified with its failing row; test_goal_nonzero_command_returns_completed_unverified.
- Guarantee wired in: both CLI run and MCP delegate reach the shared server-side goal evaluator before completed; test_goal_cli_and_mcp_share_server_evaluator.
- Units: command output tails are at most 2,000 characters, and verification timeouts are seconds; test_goal_uat_output_tail_is_bounded_chars.

### Tests

tests/test_goal.py: test_goal_inline_and_path_use_same_validator, test_goal_command_rows_run_server_side, test_goal_command_rows_gate_completed, test_goal_prose_rows_reach_reviewer, test_goal_result_and_delegation_record_have_contract_uat, test_goal_identity_uses_request_block_not_worker_text, test_goal_command_outcome_is_written_to_delegation_trace, test_goal_nonzero_command_returns_completed_unverified, test_goal_cli_and_mcp_share_server_evaluator, test_goal_uat_output_tail_is_bounded_chars, test_goal_protected_path_is_denied_and_restored, test_goal_no_command_rows_warns_doctor. The last two function names contain the exact -k selectors goal_protected and goal_no_command_rows.

Acceptance: uv run pytest tests/test_goal.py -q; uv run pytest tests -q -k goal_protected; uv run pytest tests/test_trace_schema.py -q; uv run pytest tests -q -k goal_no_command_rows.

### Out of scope

Changing the existing plan format, adding a new goal language, running command checks inside the worker, letting worker-written goal text replace caller input, changing the MCP tool contract outside this additive parameter and result field, and making prose reviewer judgments substitute for the command verification gate.

## Item 13

### Behaviour

For every model turn, retain the existing provider-reported token counts and add source_usage so the trace shows where the known prompt text came from. Use exactly five source names: harness_prompt, plan, tool_results, test_output and model_output. The first four have input_chars and input_tokens_est; estimate input tokens as round(input_chars / core.chars_per_token), whose default is 3.5. model_output has output_tokens and reasoning_tokens copied from provider-reported usage. Unknown or absent source text is recorded as zero. Keep the existing input/output/cache/reasoning fields unchanged; they are the provider’s authoritative totals. Source estimates describe only the subagent-generated content visible to this process and are not a claim about hidden provider system text.

This adds no command or config key and does not alter a run’s pass/fail result. Source accounting must not raise into the worker thread; if a source cannot be attributed, record zeros for that source and retain the provider totals.

### Seams

- src/subagent/runs.py: _Meter._turn (315), _Meter.see (336), _Meter.records (414), Agent._account (1224), and Agent._ingest (1273).
- src/subagent/loop/loop.py: build_prompt (106), _tests_from_verification (357), run_round (525), and run_parallel (635) identify harness, plan and test-output text.
- src/subagent/telemetry/trace.py: Trace.turn (200) writes each additive turn field.
- tests/test_trace_schema.py: REQUIRED['turn'] and problems (27–115) validate turn records.
- New: src/subagent/telemetry/source_usage.py for source tagging and estimates.

### Records

Every kind = turn record adds:

source_usage = {
  harness_prompt: {input_chars: int, input_tokens_est: int},
  plan: {input_chars: int, input_tokens_est: int},
  tool_results: {input_chars: int, input_tokens_est: int},
  test_output: {input_chars: int, input_tokens_est: int},
  model_output: {output_tokens: int, reasoning_tokens: int}
}

This is additive to schema 3; schema version does not change. tests/test_trace_schema.py changes to require all five sources on a newly written turn and validate non-negative integer units.

### Invariants

N/A; source attribution is observability metadata and cannot affect execution.

### Tests

tests/test_trace_schema.py: test_turn_source_has_five_named_buckets, test_turn_source_estimates_tokens_from_characters, test_turn_source_preserves_provider_totals, test_records_from_a_local_lane_run_satisfy_the_schema. Each new selector test contains turn_source.

Acceptance: uv run pytest tests -q -k turn_source; uv run pytest tests/test_trace_schema.py -q.

### Out of scope

Changing provider token accounting, claiming exact source tokenization, adding a trace kind, changing schema version, and running the benchmark matrix.

## Item 14

### Behaviour

Add these opt-in knobs while preserving the current behavior when a key is absent:

| Key | Type and default | Effect |
|---|---|---|
| [loop].test_output_cap | positive integer characters; unset | When set, maximum test output copied into worker feedback. When absent, keep the existing 30-line tail. The full output remains in its existing log file. |
| [core].compact_window and [providers.NAME].compact_window | positive integer tokens; existing default 1,000,000, with provider value overriding core | Existing Claude context-compaction window; keep the spelling and inheritance rule. |
| [loop].parallel_tool_calls | positive integer; unset | When set, maximum concurrent worker calls started by one loop parallel round. When absent, keep the current scheduler concurrency. |
| [loop].no_narration | boolean; false | When true, add a short instruction to return only implementation results and the required final report, without progress narration. |
| [providers.NAME].thinking | string; omitted by default | Accepts only off or low. The omp driver maps both values to its native thinking option. Codex maps low to model_reasoning_effort=low; Antigravity maps low to --effort low. Off on those two drivers, and either value on other drivers, is unsupported: doctor warns with warning: provider 'NAME' driver 'DRIVER' does not support thinking = 'VALUE'; provider default used. An accepted thinking value overrides the provider's existing effort value. |
| [loop].delegate_test_writer | boolean; false | When true, send the existing test outline to [loop.test_writer].provider to generate tests, then run the existing orchestrator test review before execution. Requires [loop.review_tests] = true and a configured test-writer provider. |

When supplied, test_output_cap, parallel_tool_calls and compact_window must be greater than zero. Invalid type/value prints error: [loop].KEY must be TYPE and exits 1. thinking outside off|low prints error: provider 'NAME' thinking must be 'off' or 'low' and exits 1. delegate_test_writer without a provider or with review_tests = false prints error: [loop].delegate_test_writer requires [loop.test_writer].provider and [loop].review_tests = true and exits 1.

### Seams

- src/subagent/config.py: LoopSettings (322), _loop_settings (350), _provider (273), and _validate (619).
- src/subagent/providers/base.py: ProviderConfig (116) carries thinking.
- src/subagent/loop/detect.py: run_tests (232) applies test-output cap while preserving full logs.
- src/subagent/loop/loop.py: build_prompt (106), run_parallel (635), _prepare_tests (1060), and TEST_WRITER_PROMPT (867) apply runtime knobs.
- Provider translations: src/subagent/providers/omp.py: argv (385) maps off and low; codex.py: argv (426) maps low; antigravity.py: argv (311) maps low. U1’s config validation warns for unsupported provider/value pairs.
- New tests/test_knobs.py.

### Records

No trace schema change. Effective values are configuration only and secrets are not added. Existing provider compact_window remains a token count; test_output_cap is characters; parallel_tool_calls is a concurrency count.

### Invariants

N/A; these knobs tune existing orchestration. tests/test_knobs.py verifies that defaults preserve current behavior and configured values reach the intended call sites.

### Tests

tests/test_knobs.py: test_knobs_defaults_preserve_existing_behavior, test_knobs_test_output_cap_is_characters_and_keeps_full_log, test_knobs_compact_window_provider_overrides_core, test_knobs_parallel_tool_calls_caps_concurrency, test_knobs_no_narration_changes_worker_prompt_only, test_knobs_thinking_accepts_off_and_low, test_knobs_codex_thinking_low_maps_to_native_effort, test_knobs_antigravity_thinking_low_maps_to_native_effort, test_knobs_unsupported_thinking_warns_and_uses_native_default, test_knobs_delegate_test_writer_requires_orchestrator_review, test_knobs_delegate_test_writer_uses_configured_provider.

Acceptance: uv run pytest tests/test_knobs.py -q.

### Out of scope

The lean driver, a benchmark run, renaming the package, changing the existing test outline/review protocol, and claiming that provider-native thinking controls behave identically across vendors.

## Build units

A worktree is an isolated git working copy. Each unit has exclusive write ownership of its listed files. Other units may read those files but must send changes through the owning unit. U1 owns the shared CLI and config integration points; new command handlers live in one module per command group with one registration in cli.py. U2 consumes the config fields and args.goal interface specified here and does not edit cli.py or config.py. The two units may implement in parallel; merge U1 first so U2’s runtime tests run against the CLI/config interfaces, then merge U2 and run the combined acceptance set.

| Unit id | Items | Files owned (exclusive) | Files it may read | Dependencies on other units | Merge order |
|---|---|---|---|---|---|
| U1 surface | 5, 6, 7, 8, 10, 11, 12 CLI/config/goal parser surface, 14 config surface | src/subagent/cli.py; src/subagent/config.py; src/subagent/goal.py; src/subagent/providers/base.py; src/subagent/providers/claude.py; src/subagent/commands/providers.py; src/subagent/commands/init.py; src/subagent/commands/install.py; src/subagent/install.py; examples/config.claude.toml; skills/delegate/SKILL.md; skills/plan/SKILL.md; skills/goal/SKILL.md; tests/test_cli_provider.py; tests/test_cli_init.py; tests/test_claude_auth.py; tests/test_install.py; tests/test_loop_auto.py | all other source, examples, skills/delegate/SKILL.md references, tests and goal contract | Owns parse_goal(value, workspace) for doctor --goal and CLI validation; U2 consumes Goal and the LoopSettings/ProviderConfig fields defined here. The exact signatures and field names are fixed above. | 1 |
| U2 runtime | 12 goal runtime/results, 13, 14 runtime | src/subagent/goal_runtime.py; src/subagent/runs.py; src/subagent/mcp_server.py; src/subagent/verify.py; src/subagent/loop/loop.py; src/subagent/loop/detect.py; src/subagent/loop/testguard.py; src/subagent/providers/codex.py; src/subagent/providers/omp.py; src/subagent/providers/antigravity.py; src/subagent/telemetry/source_usage.py; src/subagent/telemetry/trace.py; src/subagent/report.py; src/subagent/report_template.html; tests/test_goal.py; tests/test_trace_schema.py; tests/test_knobs.py | all source, configs, examples, skills and contract files; U1’s CLI/config interfaces are read-only inputs | Uses U1’s Goal/GoalRow parser API; runtime reads but does not edit U1-owned CLI/config files. U1 and U2 can code in parallel against the signatures and shapes above. U2’s goal and knob acceptance tests run after U1 is merged. | 2 |

The owning unit keeps each existing shared file whole: no other unit edits any region of cli.py or config.py. A command group gets its own commands module, then U1 adds the single registration in cli.py. Goal runtime and turn metering share runs.py and tests/test_trace_schema.py, so U2 owns those files together. U1 owns goal parsing; U2 owns execution, result gating and trace writes. This avoids same-file worktree collisions without a plugin loader, command registry, or new base class.

## Key decisions

- Store provider edits in the existing layered TOML files; rejected a second provider database because it would create another source of truth.
- Require an explicit example preset for provider add and use a special local --url path; rejected an interactive arbitrary-driver editor because the existing examples are the supported provider shapes.
- Detect subscription CLIs with executable lookup only; rejected running them during init because initialization must not consume plan quota or make network calls.
- Limit Claude login to one provider’s CLAUDE_CONFIG_DIR and keep the per-agent hook settings isolated; rejected copying credentials or exposing parent environment tokens.
- Probe only the URL the user supplies and write discovered addresses only to that selected config; rejected baked-in local addresses because they vary by machine.
- Use ask as loop.auto’s default and read it from doctor --json; rejected silent auto-delegation because the default should preserve a user decision point.
- Use inline backticks to distinguish executable goal checks from prose; rejected guessing from shell-looking words because the row grammar otherwise cannot classify reliably.
- Carry UAT additively on existing result and delegation records, retain trace schema 3, and gate completed only on command checks plus job verification; rejected a new trace kind or replacing the shared verification contract.
- Estimate source input tokens from visible source characters using the configured chars_per_token; rejected reporting provider token totals as if the provider attributed them by source.
- Keep missing knob settings at today’s runtime behavior and use explicit opt-ins; rejected changing parallelism, narration or test-writer routing for existing configs.
- Keep cli.py and config.py under one exclusive unit owner, use one command module per command group, and serialize MCP registration in each harness’s native config; rejected a shared registration command because harness config formats and timeout units differ.
