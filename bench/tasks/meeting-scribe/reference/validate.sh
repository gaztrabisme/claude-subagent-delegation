#!/usr/bin/env bash
# Validate the meeting-scribe reference implementation against the hidden tests.
#
# 1. Copies a fresh copy of the repo skeleton, lays the reference `scribe/` package
#    over it, copies the hidden tests in as `_hidden_tests/` (exactly like
#    bench/run.py does after an agent finishes), and runs them with unittest.
# 2. Repeats the run against the untouched skeleton; that run must FAIL, proving
#    the hidden tests actually test something.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # .../meeting-scribe/reference
TASK="$(dirname "$HERE")"               # .../meeting-scribe
WORK="$(mktemp -d "$TASK/.validate.XXXXXX")"
SKEL="$(mktemp -d "$TASK/.skeleton.XXXXXX")"
trap 'rm -rf "$WORK" "$SKEL"' EXIT

install_hidden() {  # $1 = repo dir that receives _hidden_tests/
    mkdir -p "$1/_hidden_tests"
    cp "$TASK"/hidden/*.py "$1/_hidden_tests/"
    touch "$1/_hidden_tests/__init__.py"
}

# 1. Reference must pass.
cp -R "$TASK/repo" "$WORK/repo"
rm -rf "$WORK/repo/scribe"
cp -R "$HERE/scribe" "$WORK/repo/scribe"
install_hidden "$WORK/repo"
(cd "$WORK/repo" && python3 -m unittest discover -s _hidden_tests -t . -v)

# 2. Bare skeleton must fail.
cp -R "$TASK/repo" "$SKEL/repo"
install_hidden "$SKEL/repo"
if (cd "$SKEL/repo" && python3 -m unittest discover -s _hidden_tests -t . >/dev/null 2>&1); then
    echo "FAIL: the bare skeleton passed the hidden tests" >&2
    exit 1
fi

echo "OK: reference passes and the bare skeleton fails"
