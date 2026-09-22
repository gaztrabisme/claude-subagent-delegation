#!/usr/bin/env python3
"""Show or clear provider memory: which providers a refusal has closed, and until when.

    uv run python scripts/reopen_lane.py              # list every entry
    uv run python scripts/reopen_lane.py glm          # reopen glm now

The file is <session_root>/lane_state.json; the session root is the loaded
settings' one — --config names a config file, and the usual layers beneath it
($SUBAGENT_CONFIG, the user's own config.toml) apply — else
~/.subagent/sessions, and --session-root overrides all of that. A running
server reads the file on every dispatch, so a reopened provider is tried on
the next delegation without a restart.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_harness  # noqa: E402
from subagent.lane_state import LaneState  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("lane", nargs="*",
                        help="providers to reopen; none lists the entries")
    parser.add_argument("--config", type=Path, default=None,
                        help="TOML whose [core] session_root holds the lane state")
    parser.add_argument("--session-root", type=Path, default=None,
                        help="the session root, instead of the settings' one")
    args = parser.parse_args(argv)
    root = args.session_root or lane_harness.load_settings(args.config).session_root
    state = LaneState(Path(root).expanduser().resolve())
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
