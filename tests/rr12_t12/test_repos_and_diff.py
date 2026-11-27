"""RR-12 T12 re-review: unreadable repos, '++' content lines, duplicate repos."""

from __future__ import annotations

import json

import pytest

from tests.t12._helpers import CUR, PREV, commit, entry, make_repo, out, pad, repos_file

REG = "wiki/status/registers.md"


def two_window_outside(tmp_path):
    o = make_repo(tmp_path, "o")
    pad(o, PREV, 20)
    pad(o, CUR, 20)
    return o


def machinery_with_history(tmp_path):
    m = make_repo(tmp_path, "m")
    pad(m, PREV, 2)
    pad(m, CUR, 2)
    return m


@pytest.mark.parametrize("how", ["wrong_branch", "missing_path"])
def test_unreadable_outside_repo_gives_no_share_and_no_trip(tmp_path, how):
    m = machinery_with_history(tmp_path)
    o = two_window_outside(tmp_path)
    if how == "wrong_branch":
        bad = entry("o", o, "outside", branch="nope")
    else:
        bad = entry("o", tmp_path / "gone", "outside")
    d = out(tmp_path, repos=repos_file(tmp_path, [entry("m", m), bad]))
    assert d["machinery"]["machinery_share"] is None
    assert d["tripwire"]["tripped"] is None
    assert d["tripwire"]["cut_list"] == []
    assert any("o" in w.split() or "repo o" in w for w in d["warnings"])


def test_unreadable_machinery_repo_gives_no_share_and_no_trip(tmp_path):
    o = two_window_outside(tmp_path)
    bad = entry("m", tmp_path / "gone")
    d = out(tmp_path, repos=repos_file(tmp_path, [bad, entry("o", o, "outside")]))
    assert d["machinery"]["machinery_share"] is None
    assert d["tripwire"]["tripped"] is None
    assert any("repo m" in w for w in d["warnings"])


def test_added_line_starting_with_plusplus_keeps_waiting_membership(tmp_path):
    repo = make_repo(tmp_path, "a")
    base = ("# R\n\n## Waiting on the owner\n\n| id | q |\n|---|---|\n| W1 | q |\n")
    commit(repo, {REG: base}, "base", PREV)
    new = base + "++ note one\n++ note two\n## Done\n| D1 | item |\n"
    commit(repo, {REG: new}, "add lines starting with ++", CUR)
    d = out(tmp_path, repos=repos_file(tmp_path, [entry("a", repo)]))
    assert d["owner_questions"]["waiting_rows_added"] == 0


def test_added_row_after_plusplus_line_still_counts_once(tmp_path):
    repo = make_repo(tmp_path, "a")
    base = "# R\n\n## Waiting on the owner\n\n| id | q |\n|---|---|\n| W1 | q |\n\n## Done\n"
    commit(repo, {REG: base}, "base", PREV)
    new = base.replace("| W1 | q |\n", "| W1 | q |\n| W2 | q |\n++ note\n| W3 | q |\n")
    commit(repo, {REG: new}, "rows around a ++ line", CUR)
    d = out(tmp_path, repos=repos_file(tmp_path, [entry("a", repo)]))
    assert d["owner_questions"]["waiting_rows_added"] == 2


def test_same_repo_listed_twice_is_counted_once(tmp_path):
    repo = make_repo(tmp_path, "a")
    pad(repo, CUR, 2)
    commit(repo, {REG: "# R\n\n## Waiting on the owner\n\n| id | q |\n|---|---|\n| W1 | q |\n"},
           "row", CUR)
    once = out(tmp_path, repos=repos_file(tmp_path, [entry("a", repo)], name="one.json"))
    twice = out(tmp_path, repos=repos_file(
        tmp_path, [entry("a", repo), entry("a2", repo)], name="two.json"))
    assert twice["commits"]["total"] == once["commits"]["total"] == 3
    assert twice["machinery"]["machinery_commits"] == 3
    assert twice["owner_questions"]["waiting_rows_added"] == 1


def test_same_name_listed_twice_is_counted_once(tmp_path):
    repo = make_repo(tmp_path, "a")
    pad(repo, CUR, 3)
    f = repos_file(tmp_path, [entry("a", repo), entry("a", repo)])
    cp = out(tmp_path, repos=f)
    assert cp["commits"]["total"] == 3
    assert cp["machinery"]["machinery_commits"] == 3
    assert json.dumps(cp["commits"]["per_repo"])
