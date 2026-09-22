# Task: meeting scribe (no speech model)

Create a Python package `scribe/` (standard library only, Python 3.10+) that turns a list of
meeting transcript segments into Markdown minutes and extracts action items and decisions.
There is no real speech-to-text model: transcription reads a JSON fixture next to the audio path.

Everything the hidden tests check is stated as a rule below; each rule has at least one example.
Do not add dependencies, do not read the network, do not touch files outside this repository.

## Public API (`scribe/__init__.py` exports)

- `Segment` — a `@dataclass(frozen=True)` with these fields in this exact order:
  `start_s: float`, `end_s: float`, `speaker: str | None`, `text: str`, `confidence: float`.
  `speaker` is `None` (or empty/whitespace) for an unknown speaker.
- `Transcriber` — a `typing.Protocol` with one method
  `transcribe(self, audio_path) -> list[Segment]`.
- `FakeTranscriber` — a `Transcriber` that reads a JSON fixture (below).
- `ActionItem` — a `@dataclass(frozen=True)` with fields in this exact order:
  `text: str`, `assignee: str`, `due: str | None`. `due` is an ISO date string `YYYY-MM-DD`
  or `None`.
- `merge_turns(segments, *, gap=2.0) -> list[Segment]`
- `format_timestamp(seconds) -> str`
- `format_duration(seconds) -> str`
- `extract_action_items(segments, *, meeting_date, lang="en", gap=2.0) -> list[ActionItem]`
- `extract_decisions(segments, *, gap=2.0) -> list[str]`
- `render_minutes(segments, *, meeting_date, lang="en", title="Meeting Minutes", gap=2.0,
  summary_sentences=2) -> str`

`meeting_date` is a `datetime.date`. `lang` is one of `"en"`, `"vi"`, `"ja"`.

## CLI

Two subcommands, both using `FakeTranscriber` to read the audio's JSON fixture, and both
runnable as `python3 -m scribe ...` (so `scribe/__main__.py` must exist):

```
python3 -m scribe minutes IN.wav --date YYYY-MM-DD --out OUT.md [--lang L] [--gap F]
python3 -m scribe actions IN.wav --json [--date YYYY-MM-DD] [--lang L] [--gap F]
```

- `minutes` requires `--date` and `--out`; it renders the minutes and writes them to `--out`.
- `actions` prints a JSON array of action items (one object per item with keys `text`,
  `assignee`, `due`) to stdout. The `--json` flag is accepted; the output is always JSON.
  `--date` defaults to today and only matters for relative due dates (below).
- `--lang` defaults to `"en"`; `--gap` defaults to `2.0`.
- An unknown language raises `ValueError`; an unparseable `--date` makes the CLI exit non-zero.

## JSON fixture (`FakeTranscriber`)

`FakeTranscriber().transcribe(audio_path)` reads the file `"<audio_path>.json"` (the audio path
plus the literal suffix `.json`). That file is a JSON array; each element is an object with
`"start_s"`, `"end_s"`, `"text"`, and optionally `"speaker"` and `"confidence"`. Missing
`"speaker"` (or JSON `null`) becomes `None`; missing `"confidence"` becomes `1.0`.
A missing fixture file raises `FileNotFoundError`.

Example: `transcribe("fixtures/meeting1.wav")` reads `fixtures/meeting1.wav.json`.

## Unknown speakers

A segment is **unknown** if `speaker is None` or `speaker.strip() == ""`. For merging, two
unknown segments count as the same speaker. In the rendered transcript, unknown speakers are
relabelled `Speaker 1`, `Speaker 2`, … in order of first appearance (each unknown segment after
merging gets the next number). Unknown speakers never appear in the attendee list.

## Merging (`merge_turns`)

1. Sort the segments by `start_s` ascending (stable sort; ties keep their input order).
2. Walk the sorted list, comparing each segment to the last segment currently in the result:

   - **Overlap** — if `seg.start_s < last.end_s`: the earlier-started segment wins. The result
     segment keeps `last.start_s`, `last.speaker`, and `last.confidence`; its end becomes
     `max(last.end_s, seg.end_s)`; and the later segment's text is appended in square brackets:
     `last.text + " [" + seg.text + "]"`. (This happens even for the same speaker.)
   - **Same speaker within gap** — else if the two segments have the same speaker and
     `seg.start_s - last.end_s <= gap`: merge into one segment with start `last.start_s`,
     end `seg.end_s`, speaker `last.speaker`, text `last.text + " " + seg.text`, and
     confidence `min(last.confidence, seg.confidence)`.
   - Otherwise the segment is appended unchanged.

Whenever segments are combined for any reason, the combined `confidence` is the minimum of the
parts. Two segments with `gap == threshold` merge (the comparison is `<=`).

Examples:

- `Segment(0,5,"Alice","hello",0.9)` then `Segment(3,7,"Bob","world",0.8)` overlap, so they
  become one segment `Segment(0,7,"Alice","hello [world]",0.8)`.
- `Segment(0,2,"Alice","hi",0.9)` then `Segment(2,4,"Alice","there",0.7)` (gap 0 <= 2) become
  `Segment(0,4,"Alice","hi there",0.7)`.
- With `gap=0.0`, `Segment(0,2,"Alice","hi",0.9)` then `Segment(2,4,"Alice","there",0.7)` still
  merge (the gap `0` satisfies `<= 0.0`).
- `Segment(0,2,"Alice","hi",0.9)` then `Segment(5,7,"Alice","again",0.7)` (gap 3 > 2) stay
  separate.

## Timestamps (`format_timestamp`)

`format_timestamp(seconds)` truncates `seconds` to whole seconds (`int(seconds)`) and returns
`MM:SS` (minutes and seconds, both zero-padded to two digits) when the time is under one hour,
otherwise `H:MM:SS` (the hour is not padded).

Examples: `format_timestamp(0) == "00:00"`, `format_timestamp(59.9) == "00:59"`,
`format_timestamp(60) == "01:00"`, `format_timestamp(3599) == "59:59"`,
`format_timestamp(3600) == "1:00:00"`, `format_timestamp(3661) == "1:01:01"`.

## Durations (`format_duration`)

`format_duration(seconds)` truncates to whole seconds and formats a duration compactly:

- under 60 s: `"45s"`
- 60 s to under 1 hour: `"1m12s"` (minutes, then remaining seconds, no padding)
- 1 hour or more: `"1h2m5s"`

Examples: `format_duration(45) == "45s"`, `format_duration(72) == "1m12s"`,
`format_duration(3725) == "1h2m5s"`.

## Silence

In the transcript, a gap between two consecutive merged segments is a **silence** when
`next.start_s - prev.end_s > 30.0` seconds. Each silence is rendered on its own line
`[<format_timestamp(prev.end_s)>] [silence <format_duration(gap)>]` before the next segment.

Example: `Segment(0,28,"Bob","ok",1.0)` then `Segment(73,77,"Alice","hi",1.0)` (gap 45) renders
`[00:28] [silence 45s]` between the two lines. A gap of exactly 30.0 (or less) is not silence.

## Sentence splitting

Wherever sentences are needed (summary, decisions, action items), the text is split on any of
`. ! ? ;` or a newline; each part is stripped and empty parts are dropped. Abbreviations are not
treated specially (fixtures avoid them).

Example: `"Ship it. Do it now"` splits into `["Ship it", "Do it now"]`.

## Decisions (`extract_decisions`)

`extract_decisions(segments, *, gap=2.0)` merges the segments and then, for each merged segment
in order, splits **that segment's** text into sentences. It returns — in order of appearance —
each sentence whose whitespace-collapsed text starts (case-insensitively) with `"we agreed"` or
with `"decision:"`. The returned string is the whole sentence with runs of whitespace collapsed
to single spaces and trimmed.

Example: `"We agreed to use Postgres."` and `"Decision: deploy Friday."` are both decisions,
returned verbatim as `"We agreed to use Postgres"` and `"Decision: deploy Friday"`.

## Action items (`extract_action_items`)

Merges the segments and then, for each merged segment in order, splits **that segment's** text
into sentences and extracts one action item per sentence that matches, in this order:

1. **"Name will …" form** — the sentence matches `^([A-Za-z][A-Za-z0-9._-]*)\s+will\b` and the
   captured name is NOT one of `i, we, you, he, she, it, they` (case-insensitive). The assignee
   is that name; the action text is the rest of the sentence after `will` (leading whitespace
   trimmed). Example: `"Alice will write the release notes by Friday."` → assignee `"Alice"`,
   text `"write the release notes"`, due `"2026-09-25"` (see dates below, meeting date
   2026-09-22).
2. **`@name` form** — the sentence contains a token `@([A-Za-z][A-Za-z0-9._-]*)` AND is
   imperative. A sentence is imperative when its first word — after removing a leading `@name`
   token if the sentence starts with one — is not one of `i, we, you, he, she, it, they`
   (case-insensitive). The assignee is the name after `@`; the action text is the sentence with
   the `@name` token removed. Example: `"@bob send the slides to the client by 2026-09-30."` →
   assignee `"bob"`, text `"send the slides to the client"`, due `"2026-09-30"`. Example:
   `"We agreed @bob owns the report."` is NOT an action item (first word `we`).

After the assignee is removed, a **due date** is looked for in the remaining text (first match
wins):

- **ISO date** — `YYYY-MM-DD`, optionally preceded by the word `by` (case-insensitive), e.g.
  `2026-09-30` or `by 2026-09-30`. Invalid calendar dates (e.g. `2026-02-30`) are skipped (they
  are not a due date and are left in the text). The `due` is the date string, and the date (plus
  an optional preceding `by`) is removed from the text.
- **Relative date** — a phrase meaning "by <weekday>", resolved against `meeting_date` to the
  first occurrence of that weekday on or after the meeting date. The weekday names and the
  marker word depend on `lang`:
  - `en`: `by <weekday>`, weekday names `monday`…`sunday` (case-insensitive).
  - `vi`: `đến <weekday>`, weekday names `thứ hai` (Monday), `thứ ba`, `thứ tư`, `thứ năm`,
    `thứ sáu` (Friday), `thứ bảy`, `chủ nhật` (Sunday).
  - `ja`: `<weekday>まで`, weekday names `月曜日` (Monday), `火曜日`, `水曜日`, `木曜日`,
    `金曜日` (Friday), `土曜日`, `日曜日` (Sunday).
  Example: meeting date `2026-09-22` (a Tuesday) → `"by Friday"` is `"2026-09-25"`.

If there is no due date, `due` is `None`. Finally, the action text has runs of whitespace
collapsed to single spaces and is trimmed.

Examples (meeting date `2026-09-22`, `lang="en"`):

- `"Alice will write the release notes by Friday."` → `ActionItem("write the release notes",
  "Alice", "2026-09-25")`.
- `"@bob send the slides to the client by 2026-09-30."` → `ActionItem("send the slides to the
  client", "bob", "2026-09-30")`.
- `"Send the report."` → no action item (no assignee).
- `"I will review the slides."` → no action item (`i` is a pronoun).

## Summary

The summary selects up to `summary_sentences` sentences by word frequency:

1. Tokenize the whole merged transcript text (lowercased) into words with the regex
   `[^\W_]{2,}` (runs of 2+ Unicode letters/digits, no underscore).
2. Drop these stopwords: `the a an and or but of to in on for is are was were be been being we
   our you your he she it they them this that these those with at by will not have has had do
   does did i me my us`.
3. Count the remaining words across the whole text.
4. Score each sentence as the sum of the frequencies of its non-stopword words.
5. Take the top `summary_sentences` sentences, ties broken by earlier position in the text, and
   output them in transcript order (not score order), joined with `". "` and terminated with a
   final `"."` (omitted if there are no sentences).

Example: for the text `"Ship the report. Ship the report. Ship it now."` with
`summary_sentences=2`, `ship` has the highest frequency so the first two sentences are chosen and
the summary is `"Ship the report. Ship the report."`.

## Minutes layout (`render_minutes`)

`render_minutes` merges the segments and renders Markdown with this exact section order:

```markdown
# <title>

Date: <formatted date>
Attendees: <a>, <b>

## Summary

<summary>

## Decisions

- <decision>

## Action items

| Assignee | Action | Due |
| --- | --- | --- |
| <assignee> | <text> | <due or empty> |

## Transcript

[00:00] Alice: Good morning.
[00:28] [silence 45s]
[01:13] Alice: Any other topics?
```

- Title is the `title` argument.
- **Date** is `meeting_date` formatted for the language:
  - `en`: `"September 22, 2026"` (full month name, day without leading zero, year)
  - `vi`: `"ngày 22 tháng 9 năm 2026"`
  - `ja`: `"2026年9月22日"`
- **Attendees** are the distinct non-unknown speakers in order of first appearance, joined
  with `", "` (empty if there are none).
- **Summary** is the summary text (may be empty).
- **Decisions** are one `- ` bullet per decision, in order (the section appears even when empty).
- **Action items** is a Markdown table with the header above; the Due cell is the ISO date or
  empty for `None`. The table always has its two header rows even when there are no items.
- **Transcript** is one line per merged segment: `[<format_timestamp(start_s)>] <speaker>: <text>`
  with unknown speakers relabelled `Speaker 1`…, and a silence line inserted before any segment
  whose gap from the previous one exceeds 30 s (see Silence).

The output ends with a single trailing newline.

## Empty transcript

An empty segment list renders all sections with empty content (empty summary, no decisions, the
empty action-items table header, and no transcript lines) and does not raise.
