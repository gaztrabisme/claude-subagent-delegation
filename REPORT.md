# P1a report: the `omp` driver core

## Status

DONE. `src/subagent/providers/omp.py` lands with `OMP_PROVIDER`
(`guard`/`boot`/`argv`/`env`/`translator`/`refusal` + `spawn`), registered in
`base.py` (`DRIVER_OMP`, `DRIVERS`, `VENDOR_BY_DRIVER`) and in the
`providers/__init__.py` lookup. `examples/config.omp.toml` and the README row
are in.

Checks run:

- `uv run python -c "import subagent.providers.omp"` → ok
- `uv run pytest -q tests/test_providers_copilot.py` → 11 passed
- extra sanity: the example config loads through `config.load(extra=...)`
  (driver/vendor `omp`, llamacpp health, local pricing, `allow_unguarded`),
  `for_driver("omp")` resolves, and a Translator/argv smoke test produced the
  shapes below.

## Event mapping (omp → claude stream-json)

omp prints one JSON object per line. Verified against
`@oh-my-pi/pi-agent-core/src/types.ts` (`AgentEvent`, lines 860–890): every
field name in the brief matches the source exactly — no corrections needed.

| omp line (`type`) | emitted claude event | notes |
|---|---|---|
| no `type` (session header) | — | ignored |
| `agent_start` | `system/init` (first event only) | the init rides on the first feed, as in copilot |
| `turn_start` | — | ignored |
| `message_update` + `assistantMessageEvent.type == "text_delta"` | `assistant` / text block | deltas accumulate into `last_message` |
| `message_update` + `thinking_delta` | `assistant` / thinking block | `_assistant_text` reads text blocks only, so this is display-only |
| `message_update` + `done` | — | message already streamed; nothing to emit |
| `message_update` + `error` | — | reason folded into the result's `error`, sets `saw_error` |
| `tool_execution_start` | `assistant` / `tool_use` block | `toolCallId`→`id`, `toolName`→`name`, `args`→`input`; counts as one step (via `_tool_uses` in `_ingest`) |
| `tool_execution_end` | `user` / `tool_result` block | `isError`→`is_error`; non-str result JSON-dumped |
| `turn_end` | — | repeats message + tool results already streamed |
| `agent_end` | — | usage captured (event-level `usage`, else scanned off `messages`), folded into the synthetic result |
| process exit | synthetic `result` | usage totals (`input`→`input_tokens`, `cacheRead`→`cache_read_input_tokens`, `cacheWrite`→`cache_creation_input_tokens`, `output`→`output_tokens`), `num_turns: 1`, `is_error` from exit code or `saw_error` |

## argv

- First turn: `[omp, -p, --mode, json, --no-session, --cwd, <ws>, (--model M)?, *extra_args?, <prompt>]`.
- Resume turn: the prompt is prepended with
  `"Continue the previous task in this directory."` plus the previous turn's
  final assistant text as context when the `Session` holds it.
- omp's own `--resume` needs a saved session; `--no-session` disables that,
  so there is nothing to reopen — the continue line is the whole mechanism.

## Resume approach

omp runs `--no-session`, so nothing persists between turns. The driver keeps
the thread in the `Session` object instead: `OmpProcess.events()` writes the
turn's accumulated `last_message` into `session.data["last_message"]` when the
turn ends; the next turn's `argv` sees it (same `Session` instance — runs.py
keys sessions per driver name and reuses them) and prepends the continue line
plus that text. `argv` detects a resume turn by `session.data` holding a
`last_message`; on the very first turn it is empty, so the prompt goes out
verbatim. The session id on events is a minted uuid used only to tag records —
omp never sees it.

## Refusal

`refusal()` scans the synthetic result's `error` (which carries stderr + raw
stdout lines + `message_update` error reasons). Patterns: `connection
refused`, `ECONNREFUSED`, `no model`, `model not found`, `provider <x> not
configured`, `401`, `429`. All map to `COPILOT_MODEL_UNAVAILABLE` — the
closest code the router already defines ("a model this account cannot use;
never closes the lane"). No new router codes were invented.

## Uncertainties / notes

1. **`agent_end` usage location**: the brief says `messages` *may* carry
   `usage`; the types.ts excerpt doesn't pin whether it sits on the event or
   on individual messages. The Translator checks the event-level `usage`
   first and also scans `messages`, summing per key — if both levels ever
   carry the same totals, counts would double. Part 2's recorded fixture
   should pin this down.
2. **`num_turns: 1`**: one omp one-shot run = one turn, matching what
   `_ingest` adds. If omp ever streams multiple turns in a single run this
   undercounts.
3. **thinking blocks**: emitted in the native claude shape; the current
   `_ingest`/`_assistant_text` path ignores them, so they are harmless. Kept
   because the brief lists `thinking_delta` as mapped.
4. **Resume detection** relies on `runs.py`'s flow (`_spawn` overwrites
   `session.session_id` with `resume` before `argv` runs, and `session.data`
   starts empty). A caller that invokes `argv` directly on a booted `Session`
   (id already minted by `boot`, no `last_message` yet) still gets a
   first-turn prompt — correct for the only flow in this build.
5. **`--mode json` / `--no-session` flags**: taken from the brief, not
   verified against a live omp binary (none installed here, and no network);
   part 2's fake binary exercises the real argv shape.
6. The unused `log` import mirrors copilot.py's existing pattern.
