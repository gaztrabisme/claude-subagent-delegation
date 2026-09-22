mkdir -p src && echo 'export const add = (a, b) => a + b;' > src/add.js
mkdir -p .delegate && echo '{"status":"done","summary":"added add"}' > .delegate/result.json
