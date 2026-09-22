"""Markdown minutes renderer."""

import re

from .actions import extract_action_items, extract_decisions, split_sentences
from .langs import format_date
from .merge import is_unknown, merge_turns
from .timefmt import format_duration, format_timestamp

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "is", "are", "was", "were", "be", "been", "being", "we", "our", "you",
    "your", "he", "she", "it", "they", "them", "this", "that", "these",
    "those", "with", "at", "by", "will", "not", "have", "has", "had", "do",
    "does", "did", "i", "me", "my", "us",
}

TOKEN_RE = re.compile(r"[^\W_]{2,}")


def _word_freq(text: str) -> dict[str, int]:
    freq = {}
    for token in TOKEN_RE.findall(text.lower()):
        if token not in STOPWORDS:
            freq[token] = freq.get(token, 0) + 1
    return freq


def _summarize(text: str, n: int) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return ""
    freq = _word_freq(text)
    scored = []
    for index, sentence in enumerate(sentences):
        tokens = [t for t in TOKEN_RE.findall(sentence.lower()) if t not in STOPWORDS]
        score = sum(freq.get(t, 0) for t in tokens)
        scored.append((score, index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = sorted(scored[:n], key=lambda item: item[1])
    return ". ".join(sentence for _, _, sentence in chosen) + ("." if chosen else "")


def render_minutes(segments, *, meeting_date, lang="en", title="Meeting Minutes",
                   gap=2.0, summary_sentences=2) -> str:
    merged = merge_turns(segments, gap=gap)
    full_text = " ".join(s.text for s in merged)

    attendees = []
    for seg in merged:
        if not is_unknown(seg.speaker) and seg.speaker not in attendees:
            attendees.append(seg.speaker)

    decisions = extract_decisions(merged, gap=gap)
    actions = extract_action_items(merged, meeting_date=meeting_date, lang=lang, gap=gap)
    summary = _summarize(full_text, summary_sentences)

    lines = [
        f"# {title}",
        "",
        f"Date: {format_date(meeting_date, lang)}",
        f"Attendees: {', '.join(attendees)}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Decisions",
        "",
    ]
    for decision in decisions:
        lines.append(f"- {decision}")
    lines += [
        "",
        "## Action items",
        "",
        "| Assignee | Action | Due |",
        "| --- | --- | --- |",
    ]
    for item in actions:
        lines.append(f"| {item.assignee} | {item.text} | {item.due or ''} |")
    lines += ["", "## Transcript", ""]

    speaker_number = 0
    prev_end = None
    for seg in merged:
        if prev_end is not None and seg.start_s - prev_end > 30.0:
            lines.append(f"[{format_timestamp(prev_end)}] [silence {format_duration(seg.start_s - prev_end)}]")
        speaker = seg.speaker
        if is_unknown(speaker):
            speaker_number += 1
            speaker = f"Speaker {speaker_number}"
        lines.append(f"[{format_timestamp(seg.start_s)}] {speaker}: {seg.text}")
        prev_end = seg.end_s

    return "\n".join(lines) + "\n"
