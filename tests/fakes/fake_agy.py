#!/usr/bin/env python3
"""Replay one captured agy stream-json fixture for offline tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

record = os.environ.get("FAKE_AGY_RECORD")
if record:
    Path(record).write_text(json.dumps(sys.argv[1:]) + "\n", encoding="utf-8")

stdout_fixture = Path(os.environ["FAKE_AGY_STDOUT"])
sys.stdout.write(stdout_fixture.read_text(encoding="utf-8"))
sys.stdout.flush()

stderr_fixture = os.environ.get("FAKE_AGY_STDERR")
if stderr_fixture:
    path = Path(stderr_fixture)
    if path.exists():
        sys.stderr.write(path.read_text(encoding="utf-8"))
        sys.stderr.flush()

sys.exit(int(os.environ.get("FAKE_AGY_EXIT_CODE", "0")))
