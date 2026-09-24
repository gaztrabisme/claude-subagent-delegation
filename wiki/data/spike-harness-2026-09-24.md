# Spike: existing lean coding-agent harnesses as the `subagent` worker

Date: 2026-09-24. Time-boxed; 33 web fetches. Reader: the subagent maintainer deciding
whether to adopt, wrap, vendor, or build the lean worker harness.

Baseline being replaced: `claude -p` with `ANTHROPIC_BASE_URL`, measured at ~32k tokens
re-read per turn, ~16k of which is Claude Code's own system prompt and tool definitions,
over ~27 turns for a 200-line task (~430k context-tokens per task attributable to the
harness alone).

Legend: **V** = verified on the cited page this spike; **R** = recalled from prior use, not
re-verified here; **unknown** = not established.

## Comparison table

| Candidate (URL) | 1. Tools / system prompt | 2. Endpoints | 3. Pre-tool gate | 4. Usage reporting | 5. Headless + JSON | 6. Resume | 7. Thinking control | 8. Lang / deps / license / maintenance | 9. Security incidents |
|---|---|---|---|---|---|---|---|---|---|
| **pi coding agent** (github.com/earendil-works/pi, formerly badlogic/pi-mono; docs: packages/coding-agent/docs/cli.md, extensions.md) | 8 built-ins: `read, bash, powershell, edit, write, grep, find, ls` (V). `-t/--tools <list>` allowlist, `--no-tools`, `--system-prompt <text|path>` replaces the default (V). Default prompt size unknown; replaceable, so the floor is what we write. | pi-ai "unified multi-provider LLM API (OpenAI, Anthropic, Google)" (V). Custom providers in `models.json` with `api` = `openai-completions` / `openai-responses` / `anthropic-messages` and `baseUrl` (R). | Extension event `tool_call` "fires before tool execution ... can mutate input or block execution" (V). Extensions are TS/JS files loaded with `-e/--extension` (V). | Per-turn usage with cost and cacheRead/cacheWrite in JSON events; `models.json` cost fields include cacheRead/cacheWrite (R). | `-p/--print`; `--mode json` "write JSONL events to stdout, then exit"; `--mode rpc` JSONL in/out (V). | `-c/--continue`, `--session <path|id>`, `--no-session` (V). | `--thinking off|minimal|low|medium|high|xhigh|max` (V); per-provider mapping inside pi-ai (R). | TypeScript, Node 22.19+ (V). MIT (V). 109.1k stars on the new org (V). Release date not captured; very active (R). | None found. |
| **Codex CLI** (github.com/openai/codex; learn.chatgpt.com/docs/config-file/config-reference; learn.chatgpt.com/docs/non-interactive-mode) | Tools not enumerated on fetched pages; shell/exec, apply_patch, update_plan, web search, MCP, view_image ≈ 6–8 (R). Base instructions are several thousand tokens (R, size unknown). | `model_providers.<id>`: `base_url`, `env_key`, `wire_api`, `query_params`, `http_headers`, `env_http_headers`, `requires_openai_auth` (V). Reference lists `wire_api = "responses"` only (V); `"chat"` was accepted in earlier versions (R) — **must verify before relying on it for DeepSeek/llama.cpp**. No Anthropic messages. | `hooks.PreToolUse` (plus PostToolUse, PermissionRequest, Stop, …) (V). `approval_policy`, `sandbox_mode` read-only/workspace-write/danger-full-access (V). | `codex exec --json` emits `turn.completed` with `input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens` (V). | `codex exec --json` JSONL: `thread.started, turn.*, item.*, error` (V); `-o` last message; `--output-schema` (V); `codex exec -` reads prompt from stdin (V). | `codex exec resume --last` / `resume <SESSION_ID>` (V). | `model_reasoning_effort` low…ultra, `model_reasoning_summary` (V). OpenAI-shaped only; no `enable_thinking` passthrough for llama.cpp/oMLX (R). | Rust binary (R). Apache-2.0 (R). Very active. | None found. Note: the nx "s1ngularity" npm compromise (Aug 2025) drove installed Claude/Gemini/Q CLIs with auto-approve flags to harvest credentials — a reason for the child-env allowlist, not a harness bug (R). |
| **mini-swe-agent** (github.com/SWE-agent/mini-swe-agent) | 1 tool: bash only, via `subprocess.run`; "does not have any tools other than bash"; agent ≈100 lines Python (V). Prompt is a YAML template (R); size unknown. No function calling — action is a code block (R). | "Supports all models via litellm, openrouter, portkey", "/completion and /response endpoints, interleaved thinking" (V). Anthropic reached through litellm (R). | No documented hook (V). Python API `DefaultAgent(...).run()` (V); override `execute_action` in a subclass to gate (R). | litellm cost in trajectory `model_stats` (R); cache tokens unknown. | Batch mode + trajectory `.traj.json` (V/R). No streaming JSON on stdout (R). | Not documented (V). | Whatever litellm passes through (R). | Python, MIT (V), 7.9k stars (V). **Pulls litellm** (V). | None found. |
| **opencode** (opencode.ai/docs/cli, /plugins, /providers) | ~12 tools (bash, read, write, edit, glob, grep, list, patch, todo*, webfetch, task) (R). Prompt size unknown. | Custom provider via `@ai-sdk/openai-compatible` with `options.baseURL`; llama.cpp/LM Studio/Ollama examples (V). Anthropic-compatible custom `baseURL` not documented on that page (V); `@ai-sdk/anthropic` with `baseURL` works in practice (R). | Plugin hook `tool.execute.before`; throwing blocks the call (V). Plugin is TS/JS. | Tokens and cost in session/JSON events (R). | `opencode run --format json` "raw JSON events" (V); `--auto`; `--attach` to a server (V). | `--continue`, `--session <id>`, `--fork` (V). | `--variant` "provider-specific reasoning effort", `--thinking` display (V). | TypeScript/Bun (R). MIT (R, not confirmed by fetched pages). Large, active. | None found. |
| **Crush** (github.com/charmbracelet/crush) | Tools include bash, ls, grep, edit, view, MCP (V). Prompt size unknown. | `openai-compat` type with custom base URL and "Anthropic-compatible APIs" with configurable endpoints (V). | `permissions allow/deny` per tool; "hook support ... preliminary" (V); `--yolo` (V). No documented external gate that returns allow/deny per call. | Metrics/telemetry toggle only; per-turn usage unknown (V). | `crush run` non-interactive exists (R); JSON output not documented (V). | Persistent, resumable sessions (V). | Unknown (V). | Go. **FSL-1.1-MIT** (V): source-available with a competing-use restriction, converts to MIT after 2 years. 28.3k stars (V). | None found. |
| **Goose** (github.com/block/goose) | MCP extensions (70+); built-in developer extension has shell/edit tools (R). Prompt size unknown. | 15+ providers incl. Anthropic, OpenAI, Ollama, OpenRouter (V); custom OpenAI host via `OPENAI_HOST` (R). | Goose mode auto/approve/smart_approve/chat (R). No external per-call hook found. | Unknown. | `goose run -t/-i`, `--no-session`, `--output-format json` (R; CLI docs page 404 this spike). | `goose run -n <name> -r` (R). | Unknown. | Rust, Apache-2.0 (V), 54.6k stars (V). | None found. |
| **OpenHands** (github.com/OpenHands/OpenHands; SDK at OpenHands/software-agent-sdk) | Main repo is now the TypeScript "agent-canvas" front end; the Python agent, tools and server moved to `software-agent-sdk` (V). Tool count unknown. | "any LLM" (V); SDK uses litellm (R). | Confirmation mode / security analyzer exist in the SDK (R); not on fetched page. | litellm cost accounting (R). | Headless via `python -m openhands.core.main -t` historically (R); docs page failed to fetch. | Unknown. | Via litellm (R). | Python SDK + TS canvas. MIT (V). 89.1k stars (V). Heavy (docker runtime optional) (V). | None found. |
| **smolagents** (huggingface.co/docs/smolagents/reference/agents) | Not a coding-agent harness: `CodeAgent` executes Python code, `ToolCallingAgent` does JSON tool calls; no file/shell tools shipped for repo work (V). System prompt is a template (`PromptTemplates.system_prompt`) (V). | `OpenAIServerModel(api_base=...)`, `LiteLLMModel`, `InferenceClientModel` (R); Anthropic via LiteLLM (R). | `step_callbacks` run per step (after), `final_answer_checks` (V). No pre-tool gate; would need a subclass of `execute_tool_call` (V/R). | `RunResult` with token usage when `return_full_result=True` (V/R). | Python API, `run(stream=True)` yields steps (V). No CLI JSON stream. | In-process only (`reset=False`) (V). | Model-class kwargs (R). | Python, Apache-2.0 (R), no litellm unless chosen. | None found. |
| **HF tiny-agents** (huggingface.co/blog/tiny-agents) | ~50 lines JS / ~70 lines Python; tools come only from MCP servers (V). | OpenAI-compatible via `InferenceClient`/`base_url` (V/R). | None (V). | None (V). | Library, no CLI JSON stream. | None. | None. | TS (huggingface.js, MIT) / Python (huggingface_hub, Apache-2.0) (R). | None found. |
| **qwen-code** (github.com/QwenLM/qwen-code) | Gemini CLI fork (v0.8.2), now independent (V). Tool count unknown. | OpenAI, Anthropic, Gemini, Qwen, "any third-party provider or local model (Ollama/vLLM)" (V); `OPENAI_BASE_URL` (R). | "Hooks" listed (V), names not on page; Gemini-CLI-style BeforeTool hooks (R). | Unknown. | `qwen -p` (V); `--output-format stream-json` (R). | "Session Management" (V); `--resume` (R). | Config keys `interleaved_thinking`, `thinking_type`, `reasoning_effort` (V). | TypeScript, Apache-2.0 (V), 28.1k stars (V). | Gemini CLI lineage had a prompt-injection RCE (Tracebit, Aug 2025) fixed in 0.1.14 (R); qwen-code forked from 0.8.2, so post-fix. |
| **nanocoder** (github.com/Nano-Collective/nanocoder) | Tool list not on page. | Ollama, OpenAI-compatible (OpenRouter, Anthropic, Google via compat), llama.cpp (V). | "lifecycle hooks" (V), details unknown. | Unknown. | `nanocoder ... run "prompt"` (V); JSON unknown. | `/resume` (V). | Unknown. | TypeScript, 2.5k stars (V), license not captured (MIT, R). | None found. |
| **kimi-cli** (github.com/MoonshotAI/kimi-cli) | — | — | — | — | — | — | — | Python, Apache-2.0 (V). **Archived 2026-09-23**, "migrate to Kimi Code CLI" (V). Drop. | — |
| **letta-code** (github.com/letta-ai/letta-code) | Skills, subagents, MemFS (V). | OpenAI, Anthropic, Z.ai keys via `/connect` (V); **requires a Letta server** (cloud default, `--backend local`) (V). | "Run custom scripts at key points"; permission modes (V). | Unknown (V). | `letta -p ... --output-format json` (V). | Agents persist server-side (V). | Unknown. | TS/Bun, Apache-2.0, 3.4k stars (V). | None found. |
| **Cline CLI** (docs.cline.bot/cline-cli/overview) | ~10 tools (R). | `cline auth` with "your own provider key" (V); OpenAI-compatible base URL and Anthropic supported in the extension (R). | Hooks exist in Cline (R); not on fetched page. | Unknown. | `--json` NDJSON; headless auto-triggers on piped stdin/redirected stdout (V). | Unknown. | `--thinking none|low|medium|high|xhigh` (V). | TS/Node, Apache-2.0 (R). | None found this spike. |
| **octofriend** (github.com/synthetic-lab/octofriend) | fs read/edit, shell, fetch, web search, LSP, MCP (V). | "any OpenAI-compatible or Anthropic-compatible LLM API" (V). | Not found (V). | Unknown (V). | Not documented (V). | `octo --resume <session-id>` (V). | Unknown (V). | TypeScript, MIT, 1.0k stars (V). | None found. |
| **SWE-agent** (github.com/SWE-agent/SWE-agent) | Not fetched. Tool bundles via YAML config; litellm; MIT (R). Superseded by mini-swe-agent for this purpose. | | | | | | | | |
| **aider** (aider.chat/docs/scripting.html) | No tool calling; edit formats (diff/whole) + `/run` (R). | Via litellm (R); OpenAI-compatible and Anthropic (R). | None; Python API "not officially supported ... could change" (V). | Cost per message (R). | `--message` single-shot (V); no JSON event stream (R). | `--restore-chat-history` (R). | `--reasoning-effort`, `--thinking-tokens` (R). | Python, Apache-2.0, **pulls litellm** (R). | None found. |
| **Amp** (ampcode.com) | Not fetched. Proprietary (Sourcegraph, now Amp Code Inc.) (R). Excluded on license. | | | | | | | | |

## Wire-format library

Question: a lightweight Python library, not litellm, speaking both OpenAI chat-completions
and Anthropic messages with tool calls, streaming and cache-usage fields.

| Library | Verdict |
|---|---|
| **any-llm** (github.com/mozilla-ai/any-llm) | Apache-2.0; "leverages official provider SDKs"; extras per provider (`any-llm-sdk[openai]`); custom OpenAI-compatible endpoints; streaming and tools (all V). Cache-usage fields unknown. 2.2k stars. Dependency weight = openai + anthropic SDKs plus its own layer. |
| **aisuite** (github.com/andrewyng/aisuite) | MIT; extras per provider; streaming and tool calling with `max_turns` loop (V). Usage/cache fields unknown. 16.3k stars, 686 commits; maintenance moderate. Same dependency weight as above. |
| **openai + anthropic official SDKs** | Both expose the cache fields we need: Anthropic `usage.cache_creation_input_tokens` / `cache_read_input_tokens`; OpenAI `usage.prompt_tokens_details.cached_tokens` (R). Each pulls httpx, pydantic, anyio, jiter, sniffio, distro, typing-extensions (~8 packages, mostly shared). No abstraction layer to fight. |
| **llm** (simonw) | Apache-2.0; OpenAI core, Anthropic via `llm-anthropic` plugin; usage logged to SQLite; pulls click, sqlite-utils and more (R). Built for a CLI, not for embedding. |
| **pydantic-ai model layer** | Usable but pulls pydantic-ai-slim + provider SDKs + pydantic; heavier than the two SDKs alone (R). |
| **Hand-rolled** | Both wire formats with SSE streaming, tool calls and usage fit in ~300 lines on `urllib` or `http.client`, zero dependencies. This is the only option that keeps the key-holding process at "depends only on `mcp`". |

Recommendation: hand-roll the two adapters (they are small and the surface we use is
narrow), and keep `openai`/`anthropic` SDKs as a fallback only if streaming edge cases bite.

**litellm `model_prices_and_context_window.json`**: the repository LICENSE is MIT except
"content that resides under the 'enterprise/' directory" (V). The file lives at the
repository root (github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json),
not under `enterprise/`, so it is MIT and freely reusable as a data file with attribution (R
for the path).

## Verdict

### 1. pi coding agent — wrap as driver (recommended)

Reason: it already has every item on the list with citations: 8 built-in tools with an
allowlist (`-t read,bash,edit,write`), a replaceable system prompt, both OpenAI and
Anthropic wire formats with custom base URLs, a `tool_call` extension event that can block,
`--mode json` JSONL on stdout, `--session`/`--continue`, `--thinking` levels, MIT.

- Tokens per turn: the ~16k Claude Code fixed prefix becomes our own prompt (<1k) plus 4 tool
  schemas (~600). Estimated ~14k saved per turn, ~380k per 27-turn task. Verify by
  reading `input_tokens` from the first JSON event.
- Guardability: a ~30-line TS extension that forwards each `tool_call` to our guard
  (over stdin/stdout of the driver, or `--mode rpc`) and blocks on deny. Equivalent to the
  PreToolUse hook we use today. Loss: the gate lives in a JS file we ship, not in-process.
- Telemetry: JSON events carry per-turn usage with cache read/write (R, verify field names
  on first run). Cost tables come from pi's `models.json`, which we would override with our
  own pricing.
- What we lose: Python-only stack (needs Node 22.19+), prompt-prefix stability is pi's, not
  ours (check that pi does not inject per-turn dynamic text before the conversation, or KV
  reuse on llama.cpp breaks). Unverified: `models.json` `api` values and per-provider
  `enable_thinking` passthrough for oMLX/llama.cpp.

### 2. Codex CLI — keep as the Codex-model driver; do not use as the universal worker

Reason: PreToolUse hooks, `exec --json` with `cached_input_tokens`, `exec resume`, all
verified. But the config reference now documents only `wire_api = "responses"`, and there
is no Anthropic messages support. DeepSeek and z.ai do not serve `/v1/responses` (R), so
unless `"chat"` still works this cannot reach half our providers. Its base prompt is also
several thousand tokens we cannot remove. Gain: nothing over pi for non-OpenAI endpoints.

### 3. mini-swe-agent — vendor the idea, not the code

Reason: the loop is ~100 lines and the one-tool design (bash only, no function calling)
works on any chat endpoint including models with weak tool-call parsers. But it pulls
litellm, has no hook, no streaming JSON, no resume, and no cache-token reporting. Vendoring
the code brings litellm; vendoring the design (bash-only fallback mode) is worth one flag in
our own harness for local models that mangle tool-call JSON.

### Build our own — viable, second choice behind pi

Adopt pi as the driver first; build only if the pi extension gate or its prompt prefix turns
out not to be byte-stable for KV reuse.

## What would have to be true for build-our-own

Estimated size: ~1,000–1,300 lines of Python plus ~800 lines of tests.

| Part | Lines | Notes |
|---|---|---|
| Two wire adapters (chat-completions, messages), SSE streaming, tool calls, usage incl. cache fields | ~300 | `urllib`/`http.client`, no deps |
| Loop: system prompt, turn budget, tool dispatch, gate callback, event emitter | ~150 | JSONL events matching our trace schema 3 |
| Tools: read, edit (exact-string replace), write, bash (timeout, cwd, output cap), grep | ~250 | edit robustness is where every harness spends its time |
| Session persistence and resume (messages JSONL + cursor) | ~80 | |
| Provider quirks: DeepSeek `reasoning_content` echo, GLM thinking, `chat_template_kwargs.enable_thinking`, Anthropic `thinking` + `cache_control` placement | ~120 | |
| CLI (`--print`, `--json`, `--resume`, `--tools`) | ~80 | |

Risks, in order:
1. Tool-call parsing on local servers: llama.cpp/vLLM tool parsers differ per chat
   template; models emit tool calls in text. Mitigation: a bash-only text-action mode
   (mini-swe-agent style) behind a flag.
2. Prompt-cache stability: Anthropic needs `cache_control` on a stable prefix; local KV
   reuse needs byte-identical prefix. Our own loop controls this fully; pi's does not.
3. Thinking + tools interplay: DeepSeek V3.2 thinking mode requires echoing
   `reasoning_content` across tool turns; GLM similar. Must be tested per provider.
4. Context overflow: no compaction in v1; rely on round boundaries and `subagent`'s
   checkpoints.
5. Streaming partial tool-argument JSON: buffer until `finish_reason`.
6. Maintenance: we own every provider regression. pi has a large contributor base absorbing
   that.

## Not established this spike

- pi `models.json` schema and per-turn cache fields (R only).
- Codex `wire_api = "chat"` availability in the current release.
- Goose CLI flags (docs page 404), opencode license and star count, nanocoder license.
- System prompt token counts for every candidate: none publish them; measure by sending one
  empty turn to a local llama.cpp with `--verbose` and reading `n_prompt_tokens`.
