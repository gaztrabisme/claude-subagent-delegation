mkdir -p src .subagent && echo 'export const add = (a, b) => a + b;' > src/add.js
printf '%s' "$FAKE_PROMPT" > .subagent/prompt_seen.txt
echo '{"status":"done","summary":"ok"}' > .subagent/result.json
