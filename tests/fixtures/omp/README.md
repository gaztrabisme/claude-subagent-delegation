Oh My Pi (`omp`) `-p --mode json --no-session` streams, recorded live from omp 18.0.11 on
2026-09-22 against a local oMLX server (`api: openai-completions`, model
`Qwen3.8-Flash-Next-REAP-384-oQ4e-BF16-MTP-PLE`), with HOME pointed at a throwaway
`~/.omp` holding only `agent/models.yml` (one provider `omlx`) and `agent/config.yml`
(`modelRoles.default`). Each `.jsonl` is stdout verbatim; the `.stderr` beside it is stderr.

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
process exit; there is no native `result` event. `tests/fakes/fake_omp.py` replays them.
