Grok Build `--output-format streaming-messages-json`, copied from `wiki/briefs/grok-402-fixture.jsonl`.
- 402.jsonl: an HTTP 402 refusal before any work — `system/init` (session id, model, tools) then a `result` with `is_error` and the balance-exhausted error in `errors`. The refusal text is what `router.classify_refusal("grok", …)` reads as `grok_balance`.
