#!/usr/bin/env python3
"""Fake Oh My Pi (`omp`) CLI for the test suite.

Installed under the omp provider's `binary` name. Records its argv and the
environment keys the driver is expected to strip to FAKE_OMP_RECORD (one JSON
line per call), then replays the recorded event stream named by
FAKE_OMP_FIXTURE on stdout, line by line, the way `omp -p --mode json` prints
it. Options:
  FAKE_OMP_STDERR=<text>   write this to stderr (a refused connection, say)
  FAKE_OMP_EXIT=<code>     exit with this code (default 0)
"""

import json
import os
import sys

argv = sys.argv[1:]
# The real omp reads a piped stdin to EOF before it starts (it takes the
# prompt from there when one is piped in), so a driver that leaves the
# child's stdin open hangs it. Reading here reproduces that: only a driver
# that hands the child a closed stdin gets past this line promptly.
record = {
    "argv": argv,
    "cwd": os.getcwd(),
    "env": {k: os.environ.get(k) for k in ("ANTHROPIC_AUTH_TOKEN", "GLM_API_KEY")},
    "stdin": sys.stdin.read() if not sys.stdin.isatty() else "",
}
with open(os.environ["FAKE_OMP_RECORD"], "a") as fh:
    fh.write(json.dumps(record) + "\n")

with open(os.environ["FAKE_OMP_FIXTURE"]) as fh:
    for line in fh:
        if line.strip():
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
sys.stdout.flush()

stderr = os.environ.get("FAKE_OMP_STDERR")
if stderr:
    sys.stderr.write(stderr + "\n")
    sys.stderr.flush()
sys.exit(int(os.environ.get("FAKE_OMP_EXIT") or 0))
