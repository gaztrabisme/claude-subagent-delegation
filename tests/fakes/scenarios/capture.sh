mkdir -p src .delegate && echo 'export const add = (a, b) => a + b;' > src/add.js
printf '%s' "$FAKE_PROMPT" > .delegate/prompt_seen.txt
echo '{"status":"done","summary":"ok"}' > .delegate/result.json
