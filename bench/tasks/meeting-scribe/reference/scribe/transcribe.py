"""Transcriber protocol and the JSON-fixture fake transcriber."""

import json
from pathlib import Path
from typing import Protocol

from .models import Segment


class Transcriber(Protocol):
    def transcribe(self, audio_path) -> list[Segment]:
        """Return the segments for the given audio path."""


class FakeTranscriber:
    """Reads ``<audio_path>.json`` and returns its segments."""

    def transcribe(self, audio_path) -> list[Segment]:
        fixture = Path(str(audio_path) + ".json")
        data = json.loads(fixture.read_text())
        return [
            Segment(
                start_s=float(item["start_s"]),
                end_s=float(item["end_s"]),
                speaker=item.get("speaker"),
                text=item["text"],
                confidence=float(item.get("confidence", 1.0)),
            )
            for item in data
        ]
