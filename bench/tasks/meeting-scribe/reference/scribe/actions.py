"""Action-item and decision extraction."""

import re

from .langs import find_due
from .merge import merge_turns
from .models import ActionItem

PRONOUNS = {"i", "we", "you", "he", "she", "it", "they"}

WILL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*)\s+will\b")
AT_RE = re.compile(r"@([A-Za-z][A-Za-z0-9._-]*)")


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"[.!?;\n]", text)
    return [p.strip() for p in parts if p.strip()]


def collapse(text: str) -> str:
    return " ".join(text.split())


def _first_word(sentence: str, at: re.Match) -> str:
    if at.start() == 0:
        rest = sentence[at.end():].lstrip()
    else:
        rest = sentence
    tokens = rest.split()
    return tokens[0].lower() if tokens else ""


def extract_decisions(segments, *, gap=2.0) -> list[str]:
    merged = merge_turns(segments, gap=gap)
    decisions = []
    for seg in merged:
        for sentence in split_sentences(seg.text):
            normalized = collapse(sentence)
            lower = normalized.lower()
            if lower.startswith("we agreed") or lower.startswith("decision:"):
                decisions.append(normalized)
    return decisions


def extract_action_items(segments, *, meeting_date, lang="en", gap=2.0) -> list[ActionItem]:
    merged = merge_turns(segments, gap=gap)
    items = []
    for seg in merged:
        for sentence in split_sentences(seg.text):
            item = _action_from_sentence(sentence, meeting_date, lang)
            if item is not None:
                items.append(item)
    return items


def _action_from_sentence(sentence: str, meeting_date, lang: str) -> ActionItem | None:
    will = WILL_RE.match(sentence)
    if will and will.group(1).lower() not in PRONOUNS:
        assignee = will.group(1)
        text = sentence[will.end():].lstrip()
    else:
        at = AT_RE.search(sentence)
        if at is None:
            return None
        if _first_word(sentence, at) in PRONOUNS:
            return None
        assignee = at.group(1)
        text = sentence[:at.start()] + " " + sentence[at.end():]

    due, span = find_due(text, meeting_date, lang)
    if span is not None:
        text = text[:span[0]] + " " + text[span[1]:]
    text = collapse(text)
    return ActionItem(text=text, assignee=assignee, due=due)
