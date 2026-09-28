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
| `session` (the header) | — | ignored |
| `agent_start` | `system/init` (first event only) | the init rides on the first feed, as in copilot |
| `turn_start` | — | counts a turn; resets `last_message` so the result carries the final turn's text |
| `message_update` + `assistantMessageEvent.type == "text_delta"` | `assistant` / text block | deltas accumulate into `last_message` |
| `message_update` + `thinking_delta` | `assistant` / thinking block | `_assistant_text` reads text blocks only, so this is display-only |
| `message_update` + `done` | — | message already streamed; nothing to emit |
| `message_update` + `error` | — | reason folded into the result's `error`, sets `saw_error` |
| `tool_execution_start` | `assistant` / `tool_use` block | `toolCallId`→`id`, `toolName`→`name`, `args`→`input`; counts as one step (via `_tool_uses` in `_ingest`) |
| `tool_execution_update` | — | ignored |
| `tool_execution_end` | `user` / `tool_result` block | `isError`→`is_error`; `result.content[].text` blocks joined as the content (a str result passes through, anything else is JSON-dumped) |
| `message_end` | — | an assistant message with `stopReason: "error"` sets `saw_error` and folds `errorMessage` into the result's `error` |
| `auto_retry_start` | — | ignored (its `errorMessage` already arrived on `message_end`) |
| `turn_end` | — | repeats message + tool results already streamed |
| `agent_end` | — | usage summed over the assistant messages in `messages` (the only place it is read), folded into the synthetic result |
| process exit | synthetic `result` | usage totals (`input`→`input_tokens`, `cacheRead`→`cache_read_input_tokens`, `cacheWrite`→`cache_creation_input_tokens`, `output`→`output_tokens`), `num_turns` = count of `turn_start`, `result` = the last turn's text (stripped), `is_error` from exit code or `saw_error` |

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

# P1b report: fixtures, fake and tests

## Status

DONE. `tests/fakes/fake_omp.py`, `tests/fixtures/omp/`, `tests/test_providers_omp.py`;
driver fixes below.

- `uv run pytest -q tests/test_providers_omp.py tests/test_providers_copilot.py` → 32 passed
  (21 omp, 11 copilot).
- `uv run pytest -q tests/` → `4 failed, 741 passed, 1 skipped, 18 subtests passed in 217.71s`.
  The 4 are `tests/loop/test_loop_registry.py::ContinueSession_{copilot,claude}::test_pre_verify_restores_tests_before_the_suite_runs`
  and `tests/loop/test_loop_rounds.py::Rounds_{copilot,claude}::test_good_round`; all 4 fail
  identically at c9698e6 with this work stashed (KeyError in test_loop_rounds.py:17), so they
  predate it.

## Fixture provenance: live

Recorded 2026-09-22 with omp 18.0.11 (`/Users/GaryT/.bun/bin/omp`) against oMLX at
`http://127.0.0.1:8000/v1`, model `Qwen3.8-Flash-Next-REAP-384-oQ4e-BF16-MTP-PLE`, HOME set to
a throwaway `.capture-home` in the workspace holding only `agent/models.yml` (provider `omlx`,
`api: openai-completions`, contextWindow 32768, maxTokens 4096) and `agent/config.yml`
(`modelRoles.default`). Command: `omp -p --mode json --no-session --cwd <dir> "<prompt>"` under
a 300 s alarm, stdin from /dev/null. Both runs exited 0 with empty stderr; the tool run wrote
`hello.txt` = `hi`.

- `simple.jsonl` (16 lines): 41 s, 17,933 input / 16 output tokens (the system prompt is the bulk).
- `tool_call.jsonl` (50 lines): two turns, one `write` call, 1,564+1,678 input / 80+40 output,
  16,384 cache-read per call.
- `refused.jsonl`: extra recording, base URL on a closed port, trimmed to the first attempt
  (9 lines). omp does not print ECONNREFUSED: the assistant message stops with
  `errorMessage: "Unable to connect. Is the computer able to access the url?"`, then
  `auto_retry_start` and up to 10 retries at 15 s each (the 120 s alarm killed it, exit 142).
- `model_not_found.stderr`: `--model omlx/does-not-exist` → stdout empty, exit 1,
  `Model "omlx/does-not-exist" not found` on stderr.

Two facts from the capture. The first attempt hung for the whole 300 s: omp reads a piped
stdin to EOF before starting (`Reading prompt from piped stdin (waiting for EOF)`), and the
capture shell's stdin was a pipe. And omp with no `~/.omp` config at all refuses; the
throwaway HOME needs both yml files.

The capture directories (`.capture-home/`, `.capture-tmp/`) are gitignored and still on disk;
recursive deletes are blocked for agents on this machine, so remove them by hand.

## Driver fixes (things the recording contradicted)

1. **stdin** (real bug): `Process._start` inherited the parent's stdin, which under the MCP
   server is the transport pipe; omp reads it to EOF and never starts. `base.Process._start`
   grew a `closed_stdin` keyword (hands the child /dev/null; default behaviour unchanged for
   every other driver) and `OmpProcess.events` uses it. `fake_omp.py` reads its stdin to EOF
   too, so the end-to-end test hangs (then times out) if this regresses.
2. **Usage double-count** (the P1a open point): usage lives on each assistant message in
   `agent_end.messages` and is repeated on `message_end`/`turn_end`; there is no event-level
   `usage`. `_collect_usage` now sums only the assistant messages in `agent_end`.
3. **Tool result shape**: `tool_execution_end.result` is `{content: [{type: "text", text}],
   details}`, not a string. The text blocks become the `tool_result` content instead of a
   JSON dump of the whole object.
4. **Final text across turns**: `last_message` accumulated every turn's text, so a tool run's
   result was `"\n\n" + "Created ..."`. It now resets on `turn_start` (claude's `result` is
   the last assistant message) and is stripped; `num_turns` counts `turn_start` events (2 for
   the tool fixture) instead of the constant 1.
5. **Error reporting**: an assistant `message_end` with `stopReason: "error"` folds its
   `errorMessage` into the result and marks it an error (that is where a down server is
   reported; `message_update` with `type: "error"` was already handled).
6. **Refusal patterns**: `unable to connect` added; `model not found` widened to
   `model\b[^\n]*\bnot found` so `Model "x" not found` matches.
7. Session header is `{"type": "session", ...}`, not a type-less object; docstrings fixed.
   Unused `log` import removed (ruff F401).

## Test counts

`tests/test_providers_omp.py`: 21 tests. argv (4: first-turn shape with `-p`, `--mode json`,
`--no-session`, `--cwd`, `--model`, extras then prompt; no model; resume prefix + previous text
+ no `--resume`/`--continue`; binary from config), boot (4), translator on the recordings (6:
simple reply, tool-call stream, every recorded event type handled, tool-result shapes,
error exit + raw lines, `message_update` error), refusals (5, three parametrised patterns),
end to end through `Registry` with the fake (2: COMPLETED with usage/steps/resume prompt/closed
stdin/stripped keys; refused stream fails the run without closing the lane).

## Uncertainties

- `refused.jsonl` is the first attempt only; the driver sees the full retry stream in real use
  (same `message_end` error repeated, deduped into one `error` line). Not exercised end to end
  against a live down server through the Registry.
- `Model "x" not found` arrives on stderr with an empty stdout and exit 1; the driver's
  `finish()` folds stderr into `error`, and the pattern test covers the text, but no fixture
  replays that exact stderr through the fake (FAKE_OMP_STDERR exists for it).
- omp's thinking output is on by default for this model (`thinking_delta` events in both
  recordings); `--thinking off` via `extra_args` would trim tokens but was not recorded.
