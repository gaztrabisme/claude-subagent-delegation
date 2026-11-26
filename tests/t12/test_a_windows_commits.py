"""T12 spec items: windows, commit selection, records-only, merges, machinery shares."""

from __future__ import annotations

import pytest

from ._helpers import (
    CUR,
    NOW,
    PREV,
    PREV_START,
    START,
    commit,
    entry,
    git,
    make_repo,
    merge_branch,
    out,
    pad,
    repos_file,
    run,
)


def one_repo(tmp_path, records=None):
    repo = make_repo(tmp_path, "a")
    rf = repos_file(tmp_path, [entry("a", repo, records=records)])
    return repo, rf


# ---- windows ------------------------------------------------------------------------

def test_default_window_is_seven_days_with_z_output(tmp_path):
    _, rf = one_repo(tmp_path)
    d = out(tmp_path, repos=rf)
    assert d["window"] == {"start": START, "end": NOW}
    assert d["previous_window"] == {"start": PREV_START, "end": START}


def test_days_option(tmp_path):
    _, rf = one_repo(tmp_path)
    d = out(tmp_path, "--days", "3", repos=rf)
    assert d["window"] == {"start": "2026-11-17T00:00:00Z", "end": NOW}
    assert d["previous_window"] == {"start": "2026-11-14T00:00:00Z", "end": "2026-11-17T00:00:00Z"}


def test_start_option_overrides_days_and_previous_has_same_length(tmp_path):
    _, rf = one_repo(tmp_path)
    d = out(tmp_path, "--start", "2026-11-18T00:00:00Z", "--days", "30", repos=rf)
    assert d["window"] == {"start": "2026-11-18T00:00:00Z", "end": NOW}
    assert d["previous_window"] == {"start": "2026-11-16T00:00:00Z", "end": "2026-11-18T00:00:00Z"}


def test_offset_input_is_output_as_utc_z(tmp_path):
    _, rf = one_repo(tmp_path)
    d = out(tmp_path, repos=rf, now="2026-11-20T07:00:00+07:00")
    assert d["window"] == {"start": START, "end": NOW}


def test_same_now_gives_identical_output(tmp_path):
    repo, rf = one_repo(tmp_path)
    commit(repo, {"src/x.py": "x\n"}, "c", CUR)
    a = run(tmp_path, repos=rf).stdout
    b = run(tmp_path, repos=rf).stdout
    assert a == b


def test_window_is_half_open(tmp_path):
    repo, rf = one_repo(tmp_path)
    commit(repo, {"src/a": "1"}, "at previous start", PREV_START)   # previous window
    commit(repo, {"src/b": "2"}, "at start", START)                  # current window
    commit(repo, {"src/c": "3"}, "last second", "2026-11-19T23:59:59Z")  # current
    commit(repo, {"src/d": "4"}, "at end", NOW)                      # excluded from both
    commit(repo, {"src/e": "5"}, "before previous", "2026-11-05T23:59:59Z")  # excluded
    cur = out(tmp_path, repos=rf)
    assert cur["commits"]["total"] == 2
    # the previous window is observable through the tripwire previous share
    assert cur["tripwire"]["previous"] == 100.0
    assert cur["tripwire"]["current"] == 100.0


def test_previous_window_counts_only_its_own_commits(tmp_path):
    # previous window: 1 machinery commit; current: 1 machinery + 1 outside
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    pad(m, PREV)
    pad(m, CUR)
    pad(o, CUR)
    rf = repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")])
    d = out(tmp_path, repos=rf)
    assert d["tripwire"]["previous"] == 100.0
    assert d["tripwire"]["current"] == 50.0


def test_committer_date_not_author_date_decides(tmp_path):
    repo, rf = one_repo(tmp_path)
    commit(repo, {"src/a": "1"}, "authored in window, committed after", NOW,
           author_date=CUR)
    commit(repo, {"src/b": "2"}, "authored before, committed in window", CUR,
           author_date="2026-01-01T00:00:00Z")
    d = out(tmp_path, repos=rf)
    assert d["commits"]["total"] == 1


# ---- (a) commits and records-only ---------------------------------------------------

def test_records_only_rule(tmp_path):
    repo, rf = one_repo(tmp_path)
    commit(repo, {"wiki/a.md": "a"}, "records only", CUR)
    commit(repo, {"wiki/b.md": "b", "wiki/sub/c.md": "c"}, "records only, two files", CUR)
    commit(repo, {"wiki/d.md": "d", "src/x.py": "x"}, "mixed", CUR)
    commit(repo, {"src/y.py": "y"}, "code", CUR)
    commit(repo, None, "empty commit changes no path", CUR, allow_empty=True)
    d = out(tmp_path, repos=rf)
    assert d["commits"]["total"] == 5
    assert d["commits"]["records_only"] == 2
    assert d["commits"]["records_only_share"] == 40.0
    assert d["commits"]["per_repo"] == {"a": {"total": 5, "records_only": 2}}


def test_records_default_is_wiki_prefix(tmp_path):
    repo, rf = one_repo(tmp_path)  # records omitted
    commit(repo, {"wiki/a.md": "a"}, "in wiki", CUR)
    commit(repo, {"docs/a.md": "a"}, "in docs", CUR)
    d = out(tmp_path, repos=rf)
    assert d["commits"]["records_only"] == 1


def test_custom_records_prefixes(tmp_path):
    repo, rf = one_repo(tmp_path, records=["wiki/", "audit/"])
    commit(repo, {"audit/x.md": "a"}, "audit", CUR)
    commit(repo, {"audit/y.md": "a", "wiki/z.md": "b"}, "both prefixes", CUR)
    commit(repo, {"docs/q.md": "a"}, "docs", CUR)
    d = out(tmp_path, repos=rf)
    assert d["commits"]["records_only"] == 2


def test_records_only_deletion_counts_as_changed_path(tmp_path):
    repo, rf = one_repo(tmp_path)
    commit(repo, {"wiki/a.md": "a"}, "add", PREV)
    commit(repo, {"wiki/a.md": None}, "remove", CUR)
    d = out(tmp_path, repos=rf)
    assert d["commits"]["records_only"] == 1 and d["commits"]["total"] == 1


@pytest.mark.parametrize("n,total,share", [(1, 3, 33.3), (2, 3, 66.7), (0, 3, 0.0), (3, 3, 100.0)])
def test_share_rounding(tmp_path, n, total, share):
    repo, rf = one_repo(tmp_path)
    for i in range(n):
        commit(repo, {f"wiki/{i}.md": "r"}, "rec", CUR)
    for _ in range(total - n):
        pad(repo, CUR)
    assert out(tmp_path, repos=rf)["commits"]["records_only_share"] == share


def test_zero_commits_gives_null_shares(tmp_path):
    _, rf = one_repo(tmp_path)
    d = out(tmp_path, repos=rf)
    assert d["commits"]["total"] == 0 and d["commits"]["records_only_share"] is None
    assert d["machinery"]["machinery_share"] is None and d["machinery"]["outside_share"] is None


def test_only_commits_reachable_from_branch_count(tmp_path):
    repo, rf = one_repo(tmp_path)
    pad(repo, CUR)
    git(repo, "checkout", "-q", "-b", "other")
    pad(repo, CUR)
    git(repo, "checkout", "-q", "main")
    assert out(tmp_path, repos=rf)["commits"]["total"] == 1


# ---- merges -------------------------------------------------------------------------

def test_merges_excluded_from_commit_counts_and_counted_as_merged_changes(tmp_path):
    repo, rf = one_repo(tmp_path)
    pad(repo, PREV)
    merge_branch(repo, "f1", {"src/f1.py": "1"}, CUR)
    merge_branch(repo, "f2", {"wiki/f2.md": "2"}, CUR)
    d = out(tmp_path, repos=rf)
    # base pad is previous window; the two side commits are non-merge commits in window
    assert d["commits"]["total"] == 2
    assert d["commits"]["records_only"] == 1
    assert d["machinery"]["machinery_commits"] == 2
    assert d["coordination"]["merged_changes"] == 2


def test_merged_changes_first_parent_only(tmp_path):
    repo, rf = one_repo(tmp_path)
    pad(repo, PREV)
    # feat contains an inner merge (second-parent history of main), then feat is merged
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, {"src/f.py": "f"}, "feat work", CUR)
    git(repo, "checkout", "-q", "-b", "sub")
    commit(repo, {"src/s.py": "s"}, "sub work", CUR)
    git(repo, "checkout", "-q", "feat")
    commit(repo, {"src/f2.py": "f2"}, "feat more", CUR)
    git(repo, "merge", "-q", "--no-ff", "-m", "inner merge", "sub", date=CUR)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "outer merge", "feat", date=CUR)
    d = out(tmp_path, repos=rf)
    assert d["coordination"]["merged_changes"] == 1
    assert d["commits"]["total"] == 3  # feat work, sub work, feat more


def test_merged_changes_window_by_committer_date_and_branch(tmp_path):
    repo, rf = one_repo(tmp_path)
    pad(repo, PREV)
    merge_branch(repo, "old", {"src/o.py": "o"}, PREV)       # previous window: not counted
    merge_branch(repo, "new", {"src/n.py": "n"}, CUR)         # counted
    git(repo, "checkout", "-q", "-b", "unmerged")
    commit(repo, {"src/u.py": "u"}, "unmerged side", CUR)
    git(repo, "checkout", "-q", "main")
    assert out(tmp_path, repos=rf)["coordination"]["merged_changes"] == 1


def test_merged_changes_summed_over_repos(tmp_path):
    a = make_repo(tmp_path, "a")
    b = make_repo(tmp_path, "b")
    for r in (a, b):
        pad(r, PREV)
    merge_branch(a, "x", {"src/x": "1"}, CUR)
    merge_branch(b, "y", {"src/y": "1"}, CUR)
    merge_branch(b, "z", {"src/z": "1"}, CUR)
    rf = repos_file(tmp_path, [entry("a", a), entry("b", b, "outside")])
    assert out(tmp_path, repos=rf)["coordination"]["merged_changes"] == 3


# ---- (b) machinery vs outside -------------------------------------------------------

def test_machinery_and_outside_shares(tmp_path):
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    pad(m, CUR, 2)
    commit(m, {"wiki/r.md": "r"}, "records commit also counts", CUR)  # counts as machinery
    pad(o, CUR, 1)
    rf = repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")])
    d = out(tmp_path, repos=rf)
    assert d["machinery"] == {"machinery_commits": 3, "outside_commits": 1,
                              "machinery_share": 75.0, "outside_share": 25.0}
    assert "no outside repo listed" not in d["warnings"]


def test_share_rounding_complement(tmp_path):
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    pad(m, CUR, 1)
    pad(o, CUR, 2)
    rf = repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")])
    mc = out(tmp_path, repos=rf)["machinery"]
    assert mc["machinery_share"] == 33.3 and mc["outside_share"] == 66.7


def test_no_outside_repo_warning(tmp_path):
    repo, rf = one_repo(tmp_path)
    pad(repo, CUR)
    d = out(tmp_path, repos=rf)
    assert "no outside repo listed" in d["warnings"]
    assert d["machinery"]["machinery_share"] == 100.0 and d["machinery"]["outside_share"] == 0.0


def test_outside_only_has_zero_machinery_share(tmp_path):
    o = make_repo(tmp_path, "o")
    pad(o, CUR, 2)
    rf = repos_file(tmp_path, [entry("o", o, "outside")])
    d = out(tmp_path, repos=rf)
    assert d["machinery"]["machinery_share"] == 0.0 and d["machinery"]["outside_share"] == 100.0


# ---- repo problems and input errors -------------------------------------------------

def test_non_git_path_warns_and_counts_zero(tmp_path):
    good = make_repo(tmp_path, "good")
    pad(good, CUR)
    notgit = tmp_path / "plain"
    notgit.mkdir()
    rf = repos_file(tmp_path, [entry("good", good), entry("plain", notgit)])
    d = out(tmp_path, repos=rf)
    assert d["commits"]["per_repo"]["plain"] == {"total": 0, "records_only": 0}
    assert d["commits"]["total"] == 1
    assert d["warnings"]


def test_missing_branch_warns_and_counts_zero(tmp_path):
    repo = make_repo(tmp_path, "a")
    pad(repo, CUR)
    rf = repos_file(tmp_path, [entry("a", repo, branch="nope")])
    d = out(tmp_path, repos=rf)
    assert d["commits"]["per_repo"]["a"] == {"total": 0, "records_only": 0}
    assert d["warnings"]


def test_invalid_class_is_exit_2(tmp_path):
    repo = make_repo(tmp_path, "a")
    rf = repos_file(tmp_path, [entry("a", repo, "sideways")])
    cp = run(tmp_path, repos=rf)
    assert cp.returncode == 2
    assert cp.stderr.strip()


def test_missing_repos_file_is_exit_2(tmp_path):
    cp = run(tmp_path, repos=tmp_path / "nope.json")
    assert cp.returncode == 2 and cp.stderr.strip()


def test_invalid_json_repos_file_is_exit_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    cp = run(tmp_path, repos=bad)
    assert cp.returncode == 2 and cp.stderr.strip()


def test_tilde_and_relative_repo_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(home, "tilde")
    pad(repo, CUR)
    rel = make_repo(tmp_path / "cfg", "rel")
    pad(rel, CUR)
    pad(rel, CUR)
    rf = repos_file(tmp_path / "cfg", [
        {"name": "t", "path": "~/tilde", "branch": "main", "class": "machinery"},
        {"name": "r", "path": "rel", "branch": "main", "class": "outside"},
    ])
    # run from a different cwd than the repos file's directory
    d = out(tmp_path, repos=rf, home=home)
    assert d["commits"]["per_repo"] == {"t": {"total": 1, "records_only": 0},
                                        "r": {"total": 2, "records_only": 0}}


def test_out_option_writes_same_json_and_repos_are_untouched(tmp_path):
    repo, rf = one_repo(tmp_path)
    pad(repo, CUR)
    before = git(repo, "status", "--porcelain")
    target = tmp_path / "result.json"
    cp = run(tmp_path, "--out", str(target), repos=rf)
    assert cp.returncode == 0
    import json
    assert json.loads(target.read_text()) == json.loads(cp.stdout)
    assert git(repo, "status", "--porcelain") == before
