Oh My Pi (`omp`) `-p --mode json --no-session` streams, recorded live from omp 18.0.11 on
2026-09-22 against a local oMLX server (`api: openai-completions`, model
`Qwen3.8-Flash-Next-REAP-384-oQ4e-BF16-MTP-PLE`), with `PI_CODING_AGENT_DIR` pointed at a
throwaway agent config containing `models.yml` (provider `omlx`) and `config.yml`
(`modelRoles.default`). The driver now writes the same two files under
`<session_root>/agents/<agent-id>/omp-agent/` for every run. The API key is never written:
`models.yml` names its environment variable and the child receives that value in its
allowlisted environment. Each `.jsonl` is stdout verbatim; the `.stderr` beside it is stderr.

- simple.jsonl: `Reply with exactly: OK` — session header, one turn: thinking deltas, a text
  delta, `message_end`/`turn_end`/`agent_end`. Usage sits on the assistant message
  (`agent_end.messages[].usage`, repeated on `message_end`/`turn_end`), never on the event.
- tool_call.jsonl: `Create a file named hello.txt containing the word hi, using your write
  tool, then stop.` — two turns: a `write` tool call (`tool_execution_start` / `_update` /
  `_end`, the result a `{content: [{type: "text", text}], details}` object), then the closing
  text. Two assistant messages, each with its own usage.
- refused.jsonl: the model's base URL pointing at a closed port. The first attempt only: the
  assistant message stops with `stopReason: "error"` and `errorMessage: "Unable to connect. Is
  the computer able to access the url?"`, then `auto_retry_start`. The real run retried ten
  times, 15 s each, before the 120 s alarm killed it (exit 142); stderr stayed empty.
- model_not_found.stderr: `--model omlx/does-not-exist` — stdout empty, exit 1, this on stderr.

The driver folds these into claude stream-json and synthesises the terminal `result` on
process exit; there is no native `result` event. `--hook` installs the packaged
`guard/omp_hook.ts`, which forwards every native `tool_call` to `approval_hook.py` and blocks
unless the server returns an explicit allow. `tests/fakes/fake_omp.py` replays the fixtures.

The 2026-09-28 request-shape check used omp 18.0.11 through `scripts/lane_harness.py`'s
`SamplingProxy` to the configured oMLX endpoint. The chat-completions request body was logged,
but the run ended with an error and zero output tokens because `OMLX_API_KEY` was unset; a
separate `/api/status` check returned HTTP 401 (`API key required`). This confirms the
serialized request body but not a successful model turn. With `send_sampling = false`, no temperature,
top-p/top-k/min-p, penalties, seed, max-token, or reasoning-effort fields were present. The
Qwen body still contained `preserve_thinking: true` and
`chat_template_kwargs: {preserve_thinking: true}`. omp 18.0.11's `models.yml` schema does not
expose a `qwenPreserveThinking` override; `--thinking off`, `reasoning: false`, and
`omitMaxOutputTokens: true` did not remove it. This remains an upstream/configuration limit.
