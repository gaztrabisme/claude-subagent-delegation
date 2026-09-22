mkdir -p src .subagent
P="$FAKE_PROMPT"
if printf '%s' "$P" | grep -q "writing a TEST SUITE from an outline"; then
  echo writer >> .subagent/calls.txt
  f=$(grep -m1 '^## ' .subagent/TESTS.md | cut -c4- | awk '{print $1}')
  mkdir -p "$(dirname "$f")"
  if [ "$OUT" = wrong_always ] || { [ "$OUT" = wrong_once ] && ! printf '%s' "$P" | grep -q "Fix pass"; }; then
    exp=-5    # transcription error
  else
    exp=5; printf '%s' "$P" | grep -q "Fix pass" && echo fixpass >> .subagent/calls.txt
  fi
  cat > "$f" <<EOT
import { test } from 'node:test'; import assert from 'node:assert/strict'; import { add } from '../src/add.js';
test('adds', () => assert.equal(add(2, 3), $exp));
EOT
  [ "$OUT" = sneaky ] && echo 'export const add = (a, b) => a + b; // written by the test writer!' > src/add.js
  echo '{"status":"done","files":["'"$f"'"],"cases":1,"notes":"ok"}' > .subagent/test_writer_result.json
  exit 0
fi
if printf '%s' "$P" | grep -q "TEST SUITE before the code"; then   # test review
  echo test_review >> .subagent/calls.txt
  if grep -q ", -5)" $(cat .subagent/review/test_files.txt | head -1); then
    echo '{"verdict":"concerns","issues":[{"severity":"high","kind":"wrong_test","file":"x","line":2,"issue":"add(2,3) is 5, not -5"}]}' > .subagent/review/tests_result.json
  else
    echo '{"verdict":"ok","issues":[]}' > .subagent/review/tests_result.json
  fi
  exit 0
fi
if printf '%s' "$P" | grep -q "You are a code reviewer"; then
  echo code_review >> .subagent/calls.txt; echo '{"verdict":"ok","issues":[]}' > .subagent/review/result.json; exit 0
fi
echo worker >> .subagent/calls.txt
[ "$OUT" = odd_path ] && echo "// worker tampering" >> checks/add_check.js
echo 'export const add = (a, b) => a + b;' > src/add.js
echo '{"status":"done","summary":"ok"}' > .subagent/result.json
