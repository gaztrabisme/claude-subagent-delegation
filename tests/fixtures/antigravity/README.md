# Antigravity CLI captures

Captured from Google Antigravity CLI `agy` v1.2.12 on 2026-09-28. Each `.stdout.jsonl` contains the CLI's stream-json output; the adjacent `.stderr.txt` contains stderr. Absolute scratch workspace paths were replaced with `<WORKSPACE>` for portability. The supplied probe did not include a saved stderr stream, so `probe.stderr.txt` is empty.

All new calls ran from a scratch directory inside the worktree with `--output-format stream-json --dangerously-skip-permissions --sandbox` and model `gemini-3.8-flash-low`, except `bad-model`, which used model `agy-model-that-does-not-exist`. The command forms were:

- `probe`: supplied capture; original command line was not retained. Its stream shows a view, edit, command and `OK` response.
- `failing`: `agy -p "Use run_command with CommandLine exactly \`python3 -c 'from pathlib import Path; Path(\"nonzero-ran\").write_text(\"yes\"); raise SystemExit(7)'\`, then reply with the single word DONE." --output-format stream-json --model gemini-3.8-flash-low --dangerously-skip-permissions --sandbox`. The scratch marker was present after the tool call; the requested command exits 7, while agy itself exited 0 and reported `SUCCESS`.
- `bad-model`: `agy -p "Reply with the single word OK." --output-format stream-json --model agy-model-that-does-not-exist --dangerously-skip-permissions --sandbox`. agy exited 1 and emitted a `result.status` of `ERROR`; the same invalid-model message appeared on stderr.
- `resumed`: `agy -p "Continue this conversation and reply with the single word OK. Do not use tools." --output-format stream-json --model gemini-3.8-flash-low --conversation 013c3dfb-618f-44cf-8000-d9a9dd0182fc --dangerously-skip-permissions --sandbox`. agy exited 0 and emitted `SUCCESS` for the original conversation.

The supplied probe result reports `input_tokens=41916`, `cache_read_tokens=24406`, `output_tokens=430`, `thinking_tokens=0`, and `total_tokens=42346`. Since `input_tokens + output_tokens = total_tokens`, `input_tokens` includes cache reads; uncached input is `input_tokens - cache_read_tokens`. The resumed result reports cumulative conversation usage (`input_tokens=61562`, `output_tokens=431`, `cache_read_tokens=24406`, `num_turns=2`), while the resumed agent-response step adds 19646 input and 1 output token. The driver stores the prior cumulative counters and reports per-turn deltas.

`failing` demonstrates that a non-zero tool command does not make the overall turn fail: the tool step is `DONE`, the final status is `SUCCESS`, and agy exits 0. A successful top-level status therefore means the agent turn completed, not that every command it ran returned zero. `bad-model` is a process-level failure: final status `ERROR`, exit code 1.
