"""T12 spec item (e): owner_questions counted from registers.md and decisions.md diffs."""

from __future__ import annotations

from ._helpers import CUR, PREV, commit, entry, make_repo, out, repos_file

HEADER = "| id | question | since |\n|---|---|---|\n"


def regs(waiting: list[str], done: list[str] = (), other: list[str] = ("| R1 | risk |",),
         note: str = "") -> str:
    return (
        "# Registers\n\n## Open risks\n\n| id | risk |\n|---|---|\n"
        + "".join(r + "\n" for r in other)
        + "\n## Waiting on the owner\n\n" + HEADER
        + "".join(r + "\n" for r in waiting) + note
        + "\n## Done\n\n| id | item |\n|---|---|\n"
        + "".join(r + "\n" for r in done)
    )


REG = "wiki/status/registers.md"
DEC = "wiki/decisions.md"


def oq(tmp_path, repos):
    rf = repos_file(tmp_path, repos)
    d = out(tmp_path, repos=rf)
    keys = list(d["owner_questions"])
    assert keys == ["waiting_rows_added", "owner_answers", "total"]
    return d["owner_questions"]


def test_waiting_rows_counted_once_and_in_section_only(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: regs(["| W0 | old question | d |"])}, "previous window rows", PREV)
    # in window: add W1 and W2 (plus a prose line in the section and rows elsewhere)
    commit(repo, {REG: regs(["| W0 | old question | d |", "| W1 | q1 | d |", "| W2 | q2 | d |"],
                            note="Some prose line\n", other=["| R1 | risk |", "| R2 | risk2 |"])},
           "add two", CUR)
    # in window: W3 added; W1 edited in place; W0 edited in place; row added under Done
    commit(repo, {REG: regs(["| W0 | old question, reworded | d |", "| W1 | q1 reworded | d |",
                             "| W2 | q2 | d |", "| W3 | q3 | d |"],
                            done=["| D1 | finished |"],
                            note="Some prose line\n", other=["| R1 | risk |", "| R2 | risk2 |"])},
           "add one, edit two", CUR)
    q = oq(tmp_path, [entry("a", repo)])
    assert q == {"waiting_rows_added": 3, "owner_answers": 0, "total": 3}


def test_header_and_separator_rows_do_not_count(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: regs([])}, "file with empty waiting table", CUR)
    assert oq(tmp_path, [entry("a", repo)])["waiting_rows_added"] == 0


def test_rows_added_before_the_window_do_not_count(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: regs(["| W1 | q | d |"])}, "old", PREV)
    assert oq(tmp_path, [entry("a", repo)])["waiting_rows_added"] == 0


def test_rows_removed_do_not_count(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: regs(["| W1 | q | d |", "| W2 | q | d |"])}, "old", PREV)
    commit(repo, {REG: regs(["| W2 | q | d |"])}, "resolve W1", CUR)
    assert oq(tmp_path, [entry("a", repo)])["waiting_rows_added"] == 0


def test_repo_without_the_files_counts_zero(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {"src/x.py": "x"}, "code", CUR)
    assert oq(tmp_path, [entry("a", repo)]) == {"waiting_rows_added": 0, "owner_answers": 0,
                                               "total": 0}


DECISIONS = (
    "# Decisions\n\n"
    "## S90 first\n\nthe owner, 2026-11-25 answered \"go ahead\"\n\n"
    "## S91 second\n\nnothing quoted here\n\n"
    "## S92 third\n\n\"quoted\" before, then the owner, 2026-11-25 and no quote after\n\n"
    "## S93 fourth\n\n"
    "## S94 fifth\n\nthe owner, 2026-11-26 said “yes”\n\n"
    "## Notes\n\nthe owner, 2026-11-26 \"not an S heading\"\n"
)


def test_owner_answers_need_s_heading_and_quoted_owner_line(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {DEC: DECISIONS}, "decisions", CUR)
    q = oq(tmp_path, [entry("a", repo)])
    # S90 (straight quote) and S94 (curly quote) count; S91, S92, S93 and Notes do not
    assert q == {"waiting_rows_added": 0, "owner_answers": 2, "total": 2}


def test_heading_added_before_window_or_edited_later_does_not_count(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {DEC: "# D\n\n## S80 old\n\nthe owner, 2026-11-01 said \"yes\"\n\n## S81 bare\n\ntext\n"},
           "old", PREV)
    # in window: S81 gets a the owner line but its heading was not added in the window
    commit(repo, {DEC: "# D\n\n## S80 old\n\nthe owner, 2026-11-01 said \"yes\"\n\n"
                       "## S81 bare\n\ntext\nthe owner, 2026-11-26 said \"ok\"\n"}, "edit", CUR)
    assert oq(tmp_path, [entry("a", repo)])["owner_answers"] == 0


def test_answers_counted_per_commit_section_as_of_that_commit(tmp_path):
    repo = make_repo(tmp_path, "a")
    # heading added with no the owner line first, then a later in-window commit adds another heading
    commit(repo, {DEC: "# D\n\n## S90 a\n\ntext\n"}, "heading only", CUR)
    commit(repo, {DEC: "# D\n\n## S90 a\n\ntext\n\n## S91 b\n\nthe owner, 2026-11-26 \"yes\"\n"},
           "second heading", CUR)
    assert oq(tmp_path, [entry("a", repo)])["owner_answers"] == 1


def test_counts_both_classes_and_totals(tmp_path):
    a = make_repo(tmp_path, "a")
    b = make_repo(tmp_path, "b")
    commit(a, {REG: regs(["| W1 | q | d |", "| W2 | q | d |"])}, "rows", CUR)
    commit(b, {DEC: DECISIONS}, "decisions", CUR)
    commit(b, {REG: regs(["| X1 | q | d |"])}, "row in outside repo", CUR)
    q = oq(tmp_path, [entry("a", a, "machinery"), entry("b", b, "outside")])
    assert q == {"waiting_rows_added": 3, "owner_answers": 2, "total": 5}
