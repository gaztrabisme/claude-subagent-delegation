"""T12 spec 1(e): waiting rows count only added diff lines inside the new section."""

from __future__ import annotations

from tests.t12._helpers import CUR, PREV, commit, entry, make_repo, out, repos_file

REG = "wiki/status/registers.md"
HDR = "| id | question | since |\n|---|---|---|\n"
ROWS = "| W0 | q0 | d |\n| W1 | q1 | d |\n"


def doc(heading: str, rows: str = ROWS, holds: str = "", done: str = "") -> str:
    return (f"# Registers\n\n## Holds\n\n| id | hold |\n|---|---|\n{holds}\n"
            f"## {heading}\n\n{HDR}{rows}\n"
            f"## Done\n\n| id | item |\n|---|---|\n{done}")


def count(tmp_path, repo) -> int:
    d = out(tmp_path, repos=repos_file(tmp_path, [entry("a", repo)]))
    return d["owner_questions"]["waiting_rows_added"]


def test_heading_added_over_unchanged_rows_counts_zero(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: doc("Pending")}, "table under another heading", PREV)
    commit(repo, {REG: doc("Waiting on the owner")}, "rename heading", CUR)
    assert count(tmp_path, repo) == 0


def test_heading_added_where_none_existed_counts_zero_for_unchanged_rows(tmp_path):
    repo = make_repo(tmp_path, "a")
    old = doc("Pending").replace("## Pending", "## Notes")
    commit(repo, {REG: old}, "table under notes", PREV)
    commit(repo, {REG: doc("Waiting on the owner")}, "retitle section", CUR)
    assert count(tmp_path, repo) == 0


def test_new_row_in_same_commit_as_heading_rename_counts_one(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: doc("Pending")}, "table under another heading", PREV)
    commit(repo, {REG: doc("Waiting on the owner", ROWS + "| W2 | q2 | d |\n")},
           "rename heading and add W2", CUR)
    assert count(tmp_path, repo) == 1


def test_new_row_in_existing_section_counts_one(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: doc("Waiting on the owner")}, "base", PREV)
    commit(repo, {REG: doc("Waiting on the owner", ROWS + "| W2 | q2 | d |\n")}, "add W2", CUR)
    assert count(tmp_path, repo) == 1


def test_row_added_outside_section_counts_zero(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: doc("Waiting on the owner")}, "base", PREV)
    commit(repo, {REG: doc("Waiting on the owner", holds="| H1 | hold |\n")}, "hold added", CUR)
    assert count(tmp_path, repo) == 0


def test_id_moved_into_section_from_elsewhere_counts_one(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {REG: doc("Waiting on the owner", holds="| M1 | moving |\n")}, "base", PREV)
    commit(repo, {REG: doc("Waiting on the owner", ROWS + "| M1 | moving |\n")}, "move M1", CUR)
    assert count(tmp_path, repo) == 1
