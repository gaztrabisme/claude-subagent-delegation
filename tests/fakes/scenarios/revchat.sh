mkdir -p src .subagent
if printf '%s' "$FAKE_PROMPT" | grep -q "You are a code reviewer"; then
  n=$(( $(cat .subagent/review_attempts 2>/dev/null || echo 0) + 1 )); echo $n > .subagent/review_attempts
  [ "$REVIEWER" = chat ] && echo '{"verdict":"concerns","issues":[{"severity":"medium","file":"src/add.js","line":1,"issue":"no validation"}]}' > .subagent/fake_message.txt
  exit 0   # never writes .subagent/review/result.json
fi
echo 'export const add = (a, b) => a + b;' > src/add.js
echo '{"status":"done","summary":"ok"}' > .subagent/result.json
