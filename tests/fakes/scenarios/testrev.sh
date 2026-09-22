mkdir -p src .subagent
if printf '%s' "$FAKE_PROMPT" | grep -q "TEST SUITE"; then
  echo test_review >> .subagent/calls.txt
  [ -s .subagent/review/plan.md ] && grep -q "implement add" .subagent/review/plan.md && echo "plan ok" >> .subagent/calls.txt
  grep -q "test/add.test.js" .subagent/review/test_files.txt && echo "test list ok" >> .subagent/calls.txt
  if [ "$TR" = highmissing ]; then
    echo '{"verdict":"concerns","issues":[{"severity":"high","kind":"missing_test","file":"test/add.test.js","line":1,"issue":"no test for overflow"}]}' > .subagent/review/tests_result.json
  elif [ "$TR" = medium ]; then
    echo '{"verdict":"concerns","issues":[{"severity":"medium","kind":"missing_test","file":"test/add.test.js","line":1,"issue":"no test for add(0, 0)"}]}' > .subagent/review/tests_result.json
  elif grep -q FIXED test/add.test.js; then
    echo '{"verdict":"ok","issues":[]}' > .subagent/review/tests_result.json
  else
    echo '{"verdict":"concerns","issues":[{"severity":"high","kind":"wrong_test","file":"test/add.test.js","line":3,"issue":"expects add(-2,-3) to be -5 but the plan says 6"}]}' > .subagent/review/tests_result.json
  fi
  exit 0
fi
if printf '%s' "$FAKE_PROMPT" | grep -q "You are a code reviewer"; then
  echo code_review >> .subagent/calls.txt
  echo '{"verdict":"ok","issues":[]}' > .subagent/review/result.json; exit 0
fi
echo worker >> .subagent/calls.txt
echo 'export const add = (a, b) => a + b;' > src/add.js
echo '{"status":"done","summary":"ok"}' > .subagent/result.json
