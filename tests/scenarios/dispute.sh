# Worker that thinks a test is wrong.
mkdir -p .delegate
echo "test 'adds' expects 5 but the spec says 6" > .delegate/test_change_request.md
echo '{"status":"needs_test_change","summary":"disputed"}' > .delegate/result.json
