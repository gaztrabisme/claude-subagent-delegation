mkdir -p src .subagent
if printf '%s' "$FAKE_PROMPT" | grep -q "You are a code reviewer"; then
  echo '{"verdict":"ok","issues":[]}' > .subagent/review/result.json; exit 0
fi
if printf '%s' "$FAKE_PROMPT" | grep -q "Integration plan"; then
  echo "integration round sees both parts: $(ls src | tr '\n' ' ')"
  echo 'export const b = () => "B";' > src/b.js            # fix the integration bug
elif printf '%s' "$FAKE_PROMPT" | grep -q 'You are worker "a"'; then
  echo 'export const a = () => "A";' > src/a.js
else
  echo 'export const b = () => "b";' > src/b.js            # wrong case: merged suite fails
fi
echo '{"status":"done","summary":"part done"}' > .subagent/result.json
