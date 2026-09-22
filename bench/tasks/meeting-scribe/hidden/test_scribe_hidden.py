import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scribe import (
    ActionItem,
    FakeTranscriber,
    Segment,
    extract_action_items,
    extract_decisions,
    format_duration,
    format_timestamp,
    merge_turns,
    render_minutes,
)

HERE = Path(__file__).resolve().parent


def _fixtures():
    for base in (Path.cwd(), HERE.parent):
        cand = base / "fixtures"
        if (cand / "meeting1.wav.json").exists():
            return cand
    raise RuntimeError("fixtures directory not found")


FIXTURES = _fixtures()
MEETING1 = FIXTURES / "meeting1.wav"
MEETING2 = FIXTURES / "meeting2.wav"
MEETING_DATE = date(2026, 9, 22)  # a Tuesday


def _seg(start, end, speaker, text, confidence=1.0):
    return Segment(start, end, speaker, text, confidence)


def _run_cli(*args):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "scribe", *args],
        cwd=str(HERE.parent),
        capture_output=True,
        text=True,
        env=env,
    )


class SegmentModel(unittest.TestCase):
    def test_fields_in_order(self):
        s = Segment(0.0, 2.5, "Alice", "hello", 0.9)
        self.assertEqual(s.start_s, 0.0)
        self.assertEqual(s.end_s, 2.5)
        self.assertEqual(s.speaker, "Alice")
        self.assertEqual(s.text, "hello")
        self.assertEqual(s.confidence, 0.9)

    def test_speaker_can_be_none(self):
        self.assertIsNone(Segment(0.0, 1.0, None, "", 1.0).speaker)


class FakeTranscriberTests(unittest.TestCase):
    def test_reads_fixture_next_to_audio(self):
        segments = FakeTranscriber().transcribe(str(MEETING1))
        self.assertEqual(len(segments), 12)
        self.assertEqual(segments[0], Segment(0.0, 2.0, "Alice", "Good morning everyone.", 0.98))
        self.assertEqual(segments[-1], Segment(98.0, 100.0, "Alice", "Great, meeting adjourned.", 0.92))

    def test_missing_fixture_raises(self):
        with self.assertRaises(FileNotFoundError):
            FakeTranscriber().transcribe(str(FIXTURES / "nope.wav"))

    def test_defaults_for_speaker_and_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            (Path(tmp) / "a.wav.json").write_text(
                json.dumps([{"start_s": 0.0, "end_s": 1.0, "text": "hi", "speaker": None}])
            )
            [segment] = FakeTranscriber().transcribe(str(audio))
            self.assertIsNone(segment.speaker)
            self.assertEqual(segment.confidence, 1.0)


class MergeTurnsTests(unittest.TestCase):
    def test_sorts_by_start(self):
        merged = merge_turns([
            _seg(5, 8, "B", "b"), _seg(0, 2, "A", "a"), _seg(2, 4, "C", "c"),
        ], gap=1.0)
        self.assertEqual([s.start_s for s in merged], [0.0, 2.0, 5.0])

    def test_overlap_earlier_start_wins(self):
        merged = merge_turns([_seg(0, 5, "Alice", "hello", 0.9), _seg(3, 7, "Bob", "world", 0.8)])
        self.assertEqual(merged, [Segment(0.0, 7.0, "Alice", "hello [world]", 0.8)])

    def test_same_speaker_within_gap_merges(self):
        merged = merge_turns([_seg(0, 2, "Alice", "hi", 0.9), _seg(2, 4, "Alice", "there", 0.7)])
        self.assertEqual(merged, [Segment(0.0, 4.0, "Alice", "hi there", 0.7)])

    def test_gap_boundary_is_inclusive(self):
        merged = merge_turns([_seg(0, 2, "Alice", "hi", 0.9), _seg(3, 4, "Alice", "there", 0.7)],
                             gap=1.0)
        self.assertEqual(len(merged), 1)
        separate = merge_turns([_seg(0, 2, "Alice", "hi", 0.9), _seg(3, 4, "Alice", "there", 0.7)],
                               gap=0.9)
        self.assertEqual(len(separate), 2)

    def test_different_speakers_do_not_merge(self):
        merged = merge_turns([_seg(0, 2, "Alice", "hi"), _seg(2, 4, "Bob", "yo")])
        self.assertEqual(len(merged), 2)

    def test_two_unknown_speakers_count_as_same(self):
        merged = merge_turns([_seg(0, 2, None, "hi", 0.9), _seg(2, 4, "", "there", 0.7)])
        self.assertEqual(len(merged), 1)
        self.assertIsNone(merged[0].speaker)
        self.assertEqual(merged[0].text, "hi there")


class FormatTimestampTests(unittest.TestCase):
    def test_under_an_hour(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(59.9), "00:59")
        self.assertEqual(format_timestamp(3599), "59:59")

    def test_hour_and_up(self):
        self.assertEqual(format_timestamp(3600), "1:00:00")
        self.assertEqual(format_timestamp(3661), "1:01:01")
        self.assertEqual(format_timestamp(7325), "2:02:05")


class FormatDurationTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(60), "1m0s")
        self.assertEqual(format_duration(72), "1m12s")
        self.assertEqual(format_duration(3599), "59m59s")
        self.assertEqual(format_duration(3600), "1h0m0s")
        self.assertEqual(format_duration(3725), "1h2m5s")


class SilenceTests(unittest.TestCase):
    def test_gap_over_30_renders_silence(self):
        md = render_minutes([_seg(0, 28, "Bob", "ok"), _seg(73, 77, "Alice", "hi")],
                            meeting_date=MEETING_DATE)
        self.assertIn("[00:28] [silence 45s]", md)

    def test_gap_of_exactly_30_is_not_silence(self):
        md = render_minutes([_seg(0, 30, "Bob", "ok"), _seg(60, 63, "Alice", "hi")],
                            meeting_date=MEETING_DATE)
        self.assertNotIn("[silence", md)


class UnknownSpeakerTests(unittest.TestCase):
    def test_labelled_in_order_and_absent_from_attendees(self):
        md = render_minutes([
            _seg(0, 2, None, "hi"),
            _seg(5, 7, "Alice", "hello"),
            _seg(10, 12, "", "yo"),
        ], meeting_date=MEETING_DATE)
        self.assertIn("[00:00] Speaker 1: hi", md)
        self.assertIn("[00:05] Alice: hello", md)
        self.assertIn("[00:10] Speaker 2: yo", md)
        self.assertIn("Attendees: Alice", md)
        self.assertNotIn("Attendees: Alice, Speaker", md)


class DecisionTests(unittest.TestCase):
    def test_we_agreed(self):
        self.assertEqual(
            extract_decisions([_seg(0, 3, "Alice", "We agreed to use Postgres.")]),
            ["We agreed to use Postgres"],
        )

    def test_decision_colon_case_insensitive(self):
        self.assertEqual(
            extract_decisions([_seg(0, 3, "Alice", "Decision: deploy Friday.")]),
            ["Decision: deploy Friday"],
        )

    def test_non_decision(self):
        self.assertEqual(extract_decisions([_seg(0, 3, "Alice", "We should ship it.")]), [])


class ActionItemTests(unittest.TestCase):
    def test_at_assignee_with_iso_due(self):
        self.assertEqual(
            extract_action_items(
                [_seg(0, 5, "A", "@bob send the report by 2026-09-30.")],
                meeting_date=MEETING_DATE,
            ),
            [ActionItem("send the report", "bob", "2026-09-30")],
        )

    def test_name_will_with_relative_due(self):
        self.assertEqual(
            extract_action_items(
                [_seg(0, 5, "A", "Alice will write the release notes by Friday.")],
                meeting_date=MEETING_DATE,
            ),
            [ActionItem("write the release notes", "Alice", "2026-09-25")],
        )

    def test_pronoun_will_is_not_an_action(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "I will review the slides.")],
                                 meeting_date=MEETING_DATE),
            [],
        )

    def test_no_assignee_is_not_an_action(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "Send the report.")],
                                 meeting_date=MEETING_DATE),
            [],
        )

    def test_at_mention_in_decision_is_not_an_action(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "We agreed @bob owns the report.")],
                                 meeting_date=MEETING_DATE),
            [],
        )

    def test_relative_sunday(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "@bob fix it by Sunday.")],
                                 meeting_date=MEETING_DATE),
            [ActionItem("fix it", "bob", "2026-09-27")],
        )

    def test_relative_vi(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "@minh gửi báo cáo đến thứ sáu.")],
                                 meeting_date=MEETING_DATE, lang="vi"),
            [ActionItem("gửi báo cáo", "minh", "2026-09-25")],
        )

    def test_relative_ja(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "@tanaka レポートを送る 金曜日まで")],
                                 meeting_date=MEETING_DATE, lang="ja"),
            [ActionItem("レポートを送る", "tanaka", "2026-09-25")],
        )

    def test_invalid_iso_date_is_skipped(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "@bob send it by 2026-02-30.")],
                                 meeting_date=MEETING_DATE),
            [ActionItem("send it by 2026-02-30", "bob", None)],
        )

    def test_no_due_date(self):
        self.assertEqual(
            extract_action_items([_seg(0, 5, "A", "@bob send the report.")],
                                 meeting_date=MEETING_DATE),
            [ActionItem("send the report", "bob", None)],
        )


class SummaryTests(unittest.TestCase):
    def test_top_sentences_by_word_frequency(self):
        md = render_minutes([_seg(0, 6, "A", "Ship the report. Ship the report. Ship it now.")],
                            meeting_date=MEETING_DATE, summary_sentences=2)
        self.assertIn("## Summary\n\nShip the report. Ship the report.\n", md)


class RenderMinutesTests(unittest.TestCase):
    def test_en_date(self):
        md = render_minutes([], meeting_date=MEETING_DATE)
        self.assertIn("Date: September 22, 2026", md)

    def test_vi_date(self):
        md = render_minutes([], meeting_date=MEETING_DATE, lang="vi")
        self.assertIn("Date: ngày 22 tháng 9 năm 2026", md)

    def test_ja_date(self):
        md = render_minutes([], meeting_date=MEETING_DATE, lang="ja")
        self.assertIn("Date: 2026年9月22日", md)

    def test_custom_title_and_attendee_order(self):
        md = render_minutes([_seg(0, 2, "Bob", "a"), _seg(4, 6, "Alice", "b")],
                            meeting_date=MEETING_DATE, title="Standup")
        self.assertTrue(md.startswith("# Standup\n"))
        self.assertIn("Attendees: Bob, Alice", md)

    def test_unknown_language_raises(self):
        with self.assertRaises(ValueError):
            render_minutes([], meeting_date=MEETING_DATE, lang="xx")


class EmptyTranscriptTests(unittest.TestCase):
    def test_renders_all_sections(self):
        md = render_minutes([], meeting_date=MEETING_DATE)
        for marker in ["# Meeting Minutes", "Date: September 22, 2026", "Attendees: ",
                       "## Summary", "## Decisions", "## Action items", "## Transcript",
                       "| Assignee | Action | Due |"]:
            self.assertIn(marker, md)


class Meeting1FixtureTests(unittest.TestCase):
    def test_end_to_end(self):
        segments = FakeTranscriber().transcribe(str(MEETING1))
        md = render_minutes(segments, meeting_date=MEETING_DATE)
        self.assertIn("Date: September 22, 2026", md)
        self.assertIn("Attendees: Alice, Bob, Carol", md)
        self.assertIn("- We agreed to launch the beta in October", md)
        self.assertIn("| Alice | write the release notes | 2026-09-25 |", md)
        self.assertIn("| bob | send the slides to the client | 2026-09-30 |", md)
        self.assertIn(
            "[00:15] Bob: Alice will write the release notes by Friday. "
            "[And I will prepare the demo.]", md,
        )
        self.assertIn("[00:37] [silence 45s]", md)
        self.assertIn("[01:22] Alice: Any other topics?", md)


class Meeting2FixtureTests(unittest.TestCase):
    def test_vi_fixture(self):
        segments = FakeTranscriber().transcribe(str(MEETING2))
        items = extract_action_items(segments, meeting_date=MEETING_DATE, lang="vi")
        self.assertEqual(items, [ActionItem("gửi báo cáo", "minh", "2026-09-25")])
        md = render_minutes(segments, meeting_date=MEETING_DATE, lang="vi")
        self.assertIn("Date: ngày 22 tháng 9 năm 2026", md)
        self.assertIn("Attendees: Minh, Lan", md)
        self.assertIn("| minh | gửi báo cáo | 2026-09-25 |", md)


class CliTests(unittest.TestCase):
    def test_minutes_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.md"
            proc = _run_cli("minutes", str(MEETING1), "--date", "2026-09-22", "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            md = out.read_text()
            self.assertIn("Date: September 22, 2026", md)
            self.assertIn("- We agreed to launch the beta in October", md)
            self.assertIn("| Alice | write the release notes | 2026-09-25 |", md)
            self.assertIn("[00:37] [silence 45s]", md)

    def test_actions_prints_json(self):
        proc = _run_cli("actions", str(MEETING1), "--json", "--date", "2026-09-22")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        items = json.loads(proc.stdout)
        self.assertEqual(items, [
            {"text": "write the release notes", "assignee": "Alice", "due": "2026-09-25"},
            {"text": "send the slides to the client", "assignee": "bob", "due": "2026-09-30"},
        ])


if __name__ == "__main__":
    unittest.main()
