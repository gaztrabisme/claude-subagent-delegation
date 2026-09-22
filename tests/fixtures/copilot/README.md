Copilot CLI JSON events, the shapes `tests/fakes/fake_copilot.py` emits (and the loop's `LiveLog._feed` reads).
- success.jsonl: one turn — turn start, a bash tool call with partial output and completion, a final message, and a usage checkpoint (`totalNanoAiu` plus the token fields a checkpoint may carry).
The driver folds these into claude stream-json and synthesises the terminal `result` on process exit; there is no native `result` event.
