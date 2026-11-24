#!/usr/bin/env python3
"""Fake DeepSeek Harness (`dsh`) CLI for offline acceptance tests.

Records its invocation (argv, cwd, stdin read to EOF, and selected environment
variables plus every present env key) to ``FAKE_DSH_RECORD`` as one JSON line
per call, replays the synthetic NDJSON stream named by ``FAKE_DSH_FIXTURE`` on
stdout, optionally writes ``FAKE_DSH_STDERR`` to stderr, and exits with
``FAKE_DSH_EXIT`` (default 0).

Reading stdin to EOF mirrors ``dsh --profile headless -``: a driver that fails
to close child stdin after writing the prompt will hang here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RECORDED_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DSH_HOME",
    "SUBAGENT_GUARD_HOOK",
    "SUBAGENT_APPROVAL_SOCKET",
    "SUBAGENT_HOOK_TIMEOUT",
    "PATH",
    "HOME",
    "GLM_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "GH_TOKEN",
    "DB_PASSWORD",
    "CUSTOM_DS_KEY",
)


def main() -> int:
    argv = sys.argv[1:]
    stdin_text = sys.stdin.read()

    record_path = os.environ.get("FAKE_DSH_RECORD")
    if record_path:
        record = {
            "argv": argv,
            "cwd": os.getcwd(),
            "stdin": stdin_text,
            "env": {key: os.environ.get(key) for key in RECORDED_ENV_KEYS},
            "env_keys": sorted(os.environ.keys()),
        }
        with open(record_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    fixture_path = os.environ.get("FAKE_DSH_FIXTURE")
    if fixture_path:
        path = Path(fixture_path)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    sys.stdout.write(line + "\n")
            sys.stdout.flush()

    stderr_text = os.environ.get("FAKE_DSH_STDERR")
    if stderr_text:
        sys.stderr.write(stderr_text if stderr_text.endswith("\n") else stderr_text + "\n")
        sys.stderr.flush()

    return int(os.environ.get("FAKE_DSH_EXIT") or "0")


if __name__ == "__main__":
    raise SystemExit(main())
