# Fake worker + reviewer. Behaviour chosen by $AUTO_SCENARIO; the prompt tells us which role/round we are.
mkdir -p src .delegate
echo "$FAKE_MODEL" >> .delegate/models_seen.txt
if printf '%s' "$FAKE_PROMPT" | grep -q "You are a code reviewer"; then
  n=$(( $(cat .delegate/review_count 2>/dev/null || echo 0) + 1 )); echo $n > .delegate/review_count
  if [ "$AUTO_SCENARIO" = concerns ] || { [ "$AUTO_SCENARIO" = fixloop ] && [ $n = 1 ]; }; then
    echo '{"verdict":"concerns","issues":[{"severity":"high","file":"src/add.js","line":1,"issue":"uses eval"}]}' > .delegate/review/result.json
  else
    echo '{"verdict":"ok","issues":[]}' > .delegate/review/result.json
  fi
  exit 0
fi
case "$AUTO_SCENARIO" in
  dispute)
    echo "test 'adds' expects 5 but spec says 6" > .delegate/test_change_request.md
    echo '{"status":"needs_test_change","summary":"disputed"}' > .delegate/result.json; exit 0 ;;
  escalate)
    echo 'export const add = (a, b) => a - b;' > src/add.js ;;
  *)
    if printf '%s' "$FAKE_PROMPT" | grep -q "high-severity problems"; then
      echo 'export const add = (a, b) => a + b; // reviewed' > src/add.js
    elif printf '%s' "$FAKE_PROMPT" | grep -q "The test suite fails" || [ "$AUTO_SCENARIO" != fixloop ]; then
      sleep "${FAKE_SLEEP:-0}"; echo 'export const add = (a, b) => eval("a + b");' > src/add.js
    else
      echo 'export const add = (a, b) => a - b;' > src/add.js     # first round: buggy
    fi ;;
esac
echo '{"status":"done","summary":"worker finished"}' > .delegate/result.json
