grep -q "src/add.js" .delegate/review/diff.patch && echo "diff has src/add.js"
grep -q "test/add.test.js" .delegate/review/diff.patch && echo "LEAK: diff contains tests"
echo "// reviewer was here" >> src/add.js     # reviewers must not edit: will be restored
mkdir -p .delegate/review
echo '{"verdict":"concerns","issues":[{"severity":"medium","file":"src/add.js","line":1,"issue":"no input validation"}]}' > .delegate/review/result.json
