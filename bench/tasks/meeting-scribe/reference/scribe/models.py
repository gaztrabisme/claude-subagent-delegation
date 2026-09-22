"""Data models shared across the scribe package."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float
    speaker: str | None
    text: str
    confidence: float


@dataclass(frozen=True)
class ActionItem:
    text: str
    assignee: str
    due: str | None
