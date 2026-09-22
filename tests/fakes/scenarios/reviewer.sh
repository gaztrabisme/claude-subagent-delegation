grep -q "src/add.js" .subagent/review/diff.patch && echo "diff has src/add.js"
grep -q "test/add.test.js" .subagent/review/diff.patch && echo "LEAK: diff contains tests"
echo "// reviewer was here" >> src/add.js     # reviewers must not edit: will be restored
mkdir -p .subagent/review
echo '{"verdict":"concerns","issues":[{"severity":"medium","file":"src/add.js","line":1,"issue":"no input validation"}]}' > .subagent/review/result.json
