# Worker whose implementation is wrong: the runner's own test run must catch it.
mkdir -p src .subagent && echo 'export const add = (a, b) => a - b;' > src/add.js
echo '{"status":"done","summary":"looks done to me"}' > .subagent/result.json
