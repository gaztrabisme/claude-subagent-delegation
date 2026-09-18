#!/usr/bin/env python3
"""Show or clear lane memory: which lanes a refusal has closed, and until when.

    uv run python scripts/reopen_lane.py              # list every entry
    uv run python scripts/reopen_lane.py glm          # reopen glm now

The file is <session_root>/lane_state.json; the session root is --session-root,
else SAM_SESSION_ROOT, else ~/.subagent-mcp/sessions (the server's default).
A running server reads the file on every dispatch, so a reopened lane is
tried on the next delegation without a restart.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from subagent_mcp.lane_state import LaneState  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("lane", nargs="*", help="lanes to reopen; none lists the entries")
    parser.add_argument("--session-root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.session_root or Path(
        os.environ.get("SAM_SESSION_ROOT") or Path.home() / ".subagent-mcp" / "sessions")
    state = LaneState(root.expanduser().resolve())
    if not args.lane:
        entries = state.entries()
        if not entries:
            print(f"no lane closed ({state.path})")
        for name in sorted(entries):
            live = state.closed(name)
            status = f"closed until {live['closed_until'].isoformat()}" if live else "expired"
            print(f"{name}: {status} ({entries[name].get('code')})")
        return 0
    for name in args.lane:
        removed = state.reopen(name)
        print(f"{name}: reopened" if removed else f"{name}: was not closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
