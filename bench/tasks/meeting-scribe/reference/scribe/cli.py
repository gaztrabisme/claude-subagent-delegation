"""Command-line interface (``python3 -m scribe``)."""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .actions import extract_action_items
from .render import render_minutes
from .transcribe import FakeTranscriber


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scribe")
    sub = parser.add_subparsers(dest="command", required=True)

    minutes = sub.add_parser("minutes", help="render minutes to a Markdown file")
    minutes.add_argument("audio", help="audio file (its .json fixture is read)")
    minutes.add_argument("--date", required=True, help="meeting date, YYYY-MM-DD")
    minutes.add_argument("--out", required=True, help="output Markdown path")
    minutes.add_argument("--lang", default="en", help="en, vi, or ja")
    minutes.add_argument("--gap", type=float, default=2.0, help="same-speaker merge gap")

    actions = sub.add_parser("actions", help="print action items as JSON")
    actions.add_argument("audio", help="audio file (its .json fixture is read)")
    actions.add_argument("--json", dest="as_json", action="store_true",
                         help="output JSON (always JSON)")
    actions.add_argument("--date", default=None, help="meeting date, YYYY-MM-DD")
    actions.add_argument("--lang", default="en", help="en, vi, or ja")
    actions.add_argument("--gap", type=float, default=2.0, help="same-speaker merge gap")

    args = parser.parse_args(argv)
    segments = FakeTranscriber().transcribe(args.audio)

    if args.command == "minutes":
        markdown = render_minutes(
            segments,
            meeting_date=date.fromisoformat(args.date),
            lang=args.lang,
            gap=args.gap,
        )
        Path(args.out).write_text(markdown)
        return 0

    meeting_date = date.fromisoformat(args.date) if args.date else date.today()
    items = extract_action_items(
        segments,
        meeting_date=meeting_date,
        lang=args.lang,
        gap=args.gap,
    )
    print(json.dumps([asdict(item) for item in items], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
