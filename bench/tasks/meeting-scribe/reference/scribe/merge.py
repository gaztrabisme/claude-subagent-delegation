"""Speaker-turn merging: overlaps and adjacent same-speaker turns."""

from .models import Segment


def is_unknown(speaker) -> bool:
    return speaker is None or not str(speaker).strip()


def same_speaker(a, b) -> bool:
    if is_unknown(a) or is_unknown(b):
        return is_unknown(a) and is_unknown(b)
    return a == b


def merge_turns(segments, *, gap=2.0) -> list[Segment]:
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: (s.start_s, s.end_s))
    result = [ordered[0]]
    for seg in ordered[1:]:
        cur = result[-1]
        if seg.start_s < cur.end_s:  # overlap: earlier start wins, later text in brackets
            result[-1] = Segment(
                cur.start_s,
                max(cur.end_s, seg.end_s),
                cur.speaker,
                f"{cur.text} [{seg.text}]",
                min(cur.confidence, seg.confidence),
            )
        elif same_speaker(seg.speaker, cur.speaker) and seg.start_s - cur.end_s <= gap:
            result[-1] = Segment(
                cur.start_s,
                seg.end_s,
                cur.speaker,
                f"{cur.text} {seg.text}",
                min(cur.confidence, seg.confidence),
            )
        else:
            result.append(seg)
    return result
