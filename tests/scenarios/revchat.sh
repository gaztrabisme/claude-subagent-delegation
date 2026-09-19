mkdir -p src .delegate
if printf '%s' "$FAKE_PROMPT" | grep -q "You are a code reviewer"; then
  n=$(( $(cat .delegate/review_attempts 2>/dev/null || echo 0) + 1 )); echo $n > .delegate/review_attempts
  [ "$REVIEWER" = chat ] && echo '{"verdict":"concerns","issues":[{"severity":"medium","file":"src/add.js","line":1,"issue":"no validation"}]}' > .delegate/fake_message.txt
  exit 0   # never writes .delegate/review/result.json
fi
echo 'export const add = (a, b) => a + b;' > src/add.js
echo '{"status":"done","summary":"ok"}' > .delegate/result.json
