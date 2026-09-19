mkdir -p src .delegate
if printf '%s' "$FAKE_PROMPT" | grep -q "TEST SUITE"; then
  echo test_review >> .delegate/calls.txt
  [ -s .delegate/review/plan.md ] && grep -q "implement add" .delegate/review/plan.md && echo "plan ok" >> .delegate/calls.txt
  grep -q "test/add.test.js" .delegate/review/test_files.txt && echo "test list ok" >> .delegate/calls.txt
  if [ "$TR" = highmissing ]; then
    echo '{"verdict":"concerns","issues":[{"severity":"high","kind":"missing_test","file":"test/add.test.js","line":1,"issue":"no test for overflow"}]}' > .delegate/review/tests_result.json
  elif [ "$TR" = medium ]; then
    echo '{"verdict":"concerns","issues":[{"severity":"medium","kind":"missing_test","file":"test/add.test.js","line":1,"issue":"no test for add(0, 0)"}]}' > .delegate/review/tests_result.json
  elif grep -q FIXED test/add.test.js; then
    echo '{"verdict":"ok","issues":[]}' > .delegate/review/tests_result.json
  else
    echo '{"verdict":"concerns","issues":[{"severity":"high","kind":"wrong_test","file":"test/add.test.js","line":3,"issue":"expects add(-2,-3) to be -5 but the plan says 6"}]}' > .delegate/review/tests_result.json
  fi
  exit 0
fi
if printf '%s' "$FAKE_PROMPT" | grep -q "You are a code reviewer"; then
  echo code_review >> .delegate/calls.txt
  echo '{"verdict":"ok","issues":[]}' > .delegate/review/result.json; exit 0
fi
echo worker >> .delegate/calls.txt
echo 'export const add = (a, b) => a + b;' > src/add.js
echo '{"status":"done","summary":"ok"}' > .delegate/result.json
