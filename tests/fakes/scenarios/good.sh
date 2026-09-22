mkdir -p src && echo 'export const add = (a, b) => a + b;' > src/add.js
mkdir -p .subagent && echo '{"status":"done","summary":"added add"}' > .subagent/result.json
