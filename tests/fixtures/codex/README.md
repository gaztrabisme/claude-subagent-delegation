Real `codex exec --json` streams captured on this machine (codex-cli 0.15x), trimmed.
- usage_limit.jsonl: quota refusal before any work (thread.started, turn.started, error, turn.failed).
- context_full.jsonl: failure after work had started (tail of a long run).
- success.jsonl: first 24 events of a completed run plus its turn.completed usage line.
Usage-limit message variants seen in ~/.codex/sessions: "try again at 1:01 PM", "try again at Sep 18th, 2026 3:22 PM", "try again at Sep 20th, 2026 1:29 PM".
