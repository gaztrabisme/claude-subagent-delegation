# Worker that thinks a test is wrong.
mkdir -p .subagent
echo "test 'adds' expects 5 but the spec says 6" > .subagent/test_change_request.md
echo '{"status":"needs_test_change","summary":"disputed"}' > .subagent/result.json
