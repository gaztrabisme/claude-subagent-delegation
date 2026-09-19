# Worker that takes a few seconds (for background/wait tests), then writes a "v2" implementation.
sleep "${FAKE_SLEEP:-6}"
mkdir -p src .delegate && echo 'export const add = (a, b) => a + b; // v2' > src/add.js
echo '{"status":"done","summary":"v2"}' > .delegate/result.json
