"""RR-12 T12 re-review: owner_answers counts only decision headings new in the window."""

from __future__ import annotations

import pytest

from tests.t12._helpers import CUR, PREV, commit, entry, make_repo, out, repos_file

DEC = "wiki/decisions.md"
Q = 'the owner, 2026-11-01 said "yes"\n'


def answers(tmp_path, repo) -> int:
    d = out(tmp_path, repos=repos_file(tmp_path, [entry("a", repo)]))
    return d["owner_questions"]["owner_answers"]


def test_retitled_existing_decision_heading_is_not_counted_again(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {DEC: f"# D\n\n## S90 first\n\n{Q}"}, "old answer", PREV)
    commit(repo, {DEC: f"# D\n\n## S90 first (fixed title)\n\n{Q}"}, "retitle", CUR)
    assert answers(tmp_path, repo) == 0


def test_reordered_existing_decision_sections_are_not_counted_again(tmp_path):
    repo = make_repo(tmp_path, "a")
    a, b = f"## S90 first\n\n{Q}\n", f"## S91 second\n\n{Q}\n"
    commit(repo, {DEC: f"# D\n\n{a}{b}"}, "old answers", PREV)
    commit(repo, {DEC: f"# D\n\n{b}{a}"}, "reorder", CUR)
    assert answers(tmp_path, repo) == 0


def test_new_decision_heading_alongside_a_retitle_counts_only_the_new_one(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {DEC: f"# D\n\n## S90 first\n\n{Q}"}, "old answer", PREV)
    new = f"# D\n\n## S90 first (retitled)\n\n{Q}\n## S91 new\n\n{Q}"
    commit(repo, {DEC: new}, "retitle and add", CUR)
    assert answers(tmp_path, repo) == 1


@pytest.mark.parametrize("heading", ["## Summary", "## Scope notes", "## Status"])
def test_non_decision_heading_is_not_a_decision(tmp_path, heading):
    repo = make_repo(tmp_path, "a")
    commit(repo, {DEC: f"# D\n\n{heading}\n\n{Q}"}, "section quoting the owner", CUR)
    assert answers(tmp_path, repo) == 0


def test_s_digit_heading_still_counts(tmp_path):
    repo = make_repo(tmp_path, "a")
    commit(repo, {DEC: f"# D\n\n## S7 decision\n\n{Q}"}, "answer", CUR)
    assert answers(tmp_path, repo) == 1
