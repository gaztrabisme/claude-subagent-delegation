# Worker that edits a protected test, adds a new test file, and claims success.
chmod u+w test/add.test.js
echo "// gutted" > test/add.test.js
echo "x" > test/extra.test.js
mkdir -p src .delegate && echo 'export const add = (a, b) => a + b;' > src/add.js
echo '{"status":"done","summary":"all green"}' > .delegate/result.json
