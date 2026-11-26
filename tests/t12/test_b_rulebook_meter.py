"""T12 spec items (c) rulebook and (d) coordination."""

from __future__ import annotations

import pytest

from ._helpers import (
    CUR,
    NOW,
    PREV,
    START,
    entry,
    make_repo,
    merge_branch,
    meter_file,
    out,
    pad,
    repos_file,
    role,
)

BOOK_A = """# Rules

## Method: First thing
one two three

## Method status table
| a | b |

## Method record
text

### Method: nested heading
 ## Method: indented
## Methods overview
## Method: Second thing
x y
"""
BOOK_B = "plain text with seven words in it\n"


def wc(text: str) -> int:
    return len(text.split())


def setup_rulebook(tmp_path, files):
    repo = make_repo(tmp_path, "a")
    pad(repo, CUR)
    rf = repos_file(tmp_path, [entry("a", repo)], rulebook=[str(f) for f in files])
    return rf


def test_words_methods_and_per_file_entries(tmp_path):
    a = tmp_path / "A.md"
    b = tmp_path / "B.md"
    a.write_text(BOOK_A, encoding="utf-8")
    b.write_text(BOOK_B, encoding="utf-8")
    d = out(tmp_path, repos=setup_rulebook(tmp_path, [a, b]))
    rb = d["rulebook"]
    assert list(rb) == ["words", "methods", "files"]
    assert rb["words"] == wc(BOOK_A) + wc(BOOK_B)
    assert rb["methods"] == 2          # only lines starting with "## Method:"
    assert rb["files"] == [
        {"path": str(a), "words": wc(BOOK_A), "methods": 2},
        {"path": str(b), "words": wc(BOOK_B), "methods": 0},
    ]


def test_missing_rulebook_file(tmp_path):
    a = tmp_path / "A.md"
    a.write_text(BOOK_A, encoding="utf-8")
    gone = tmp_path / "gone-file.md"
    d = out(tmp_path, repos=setup_rulebook(tmp_path, [a, gone]))
    rb = d["rulebook"]
    assert rb["words"] == wc(BOOK_A) and rb["methods"] == 2
    assert rb["files"][1] == {"path": str(gone), "words": None, "methods": None}
    assert any("gone-file.md" in w for w in d["warnings"])


def test_empty_or_absent_rulebook_gives_zero(tmp_path):
    repo = make_repo(tmp_path, "a")
    rf = repos_file(tmp_path, [entry("a", repo)])  # no rulebook key
    rb = out(tmp_path, repos=rf)["rulebook"]
    assert rb == {"words": 0, "methods": 0, "files": []}


def test_relative_rulebook_path_resolves_against_repos_file_dir(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "rel.md").write_text(BOOK_B, encoding="utf-8")
    repo = make_repo(tmp_path, "a")
    rf = repos_file(cfg, [entry("a", repo)], rulebook=["rel.md"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rb = out(elsewhere, repos=rf)["rulebook"]
    assert rb["words"] == wc(BOOK_B)
    assert rb["files"][0]["words"] == wc(BOOK_B)


def test_tilde_rulebook_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "book.md").write_text(BOOK_B, encoding="utf-8")
    repo = make_repo(tmp_path, "a")
    rf = repos_file(tmp_path, [entry("a", repo)], rulebook=["~/book.md"])
    rb = out(tmp_path, repos=rf, home=home)["rulebook"]
    assert rb["words"] == wc(BOOK_B)


# ---- (d) coordination ---------------------------------------------------------------

KEYS = ["meter_window", "coordinator_io_tokens", "claude_io_tokens", "coordinator_io_share",
        "merged_changes", "coordinator_io_tokens_per_merged_change"]


def merged_scenario(tmp_path, merges=3):
    repo = make_repo(tmp_path, "a")
    pad(repo, PREV)
    for i in range(merges):
        merge_branch(repo, f"f{i}", {f"src/f{i}.py": "x"}, CUR)
    return repos_file(tmp_path, [entry("a", repo)])


@pytest.mark.parametrize("coord,others,share,per", [
    ((100, 900), (1000, 2000), 25.0, 333),     # 1000 / 4000; 1000 / 3
    ((500, 1500), (1500, 500), 50.0, 667),     # 2000 / 4000; 2000 / 3
])
def test_coordinator_tokens_share_and_per_merged_change(tmp_path, coord, others, share, per):
    rf = merged_scenario(tmp_path, 3)
    m = meter_file(tmp_path, {"coordinator": role(*coord), "m-s": role(*others)})
    d = out(tmp_path, "--meter", str(m), repos=rf)
    co = d["coordination"]
    assert list(co) == KEYS
    io = sum(coord)
    assert co["meter_window"] == {"start": START, "end": NOW}
    assert co["coordinator_io_tokens"] == io
    assert co["claude_io_tokens"] == io + sum(others)
    assert co["coordinator_io_share"] == share
    assert co["merged_changes"] == 3
    assert co["coordinator_io_tokens_per_merged_change"] == per
    assert isinstance(per, int) and isinstance(co["coordinator_io_tokens_per_merged_change"], int)
    assert "meter window differs from the T12 window" not in d["warnings"]


def test_cache_tokens_are_ignored(tmp_path):
    rf = merged_scenario(tmp_path, 1)
    r = {"tokens": {"input": 10, "output": 20, "cache_read": 999999, "cache_write": 5, "total": 1000034}}
    m = meter_file(tmp_path, {"coordinator": r})
    co = out(tmp_path, "--meter", str(m), repos=rf)["coordination"]
    assert co["coordinator_io_tokens"] == 30 and co["claude_io_tokens"] == 30
    assert co["coordinator_io_share"] == 100.0


def test_zero_merged_changes_gives_null_per_change_but_tokens_present(tmp_path):
    rf = merged_scenario(tmp_path, 0)
    m = meter_file(tmp_path, {"coordinator": role(10, 20), "x": role(5, 5)})
    co = out(tmp_path, "--meter", str(m), repos=rf)["coordination"]
    assert co["merged_changes"] == 0
    assert co["coordinator_io_tokens"] == 30
    assert co["coordinator_io_tokens_per_merged_change"] is None


def test_meter_window_mismatch_warns(tmp_path):
    rf = merged_scenario(tmp_path, 2)
    m = meter_file(tmp_path, {"coordinator": role(10, 20)},
                   start="2026-11-25T07:35:00Z", end="2026-11-25T08:45:18Z")
    d = out(tmp_path, "--meter", str(m), repos=rf)
    assert "meter window differs from the T12 window" in d["warnings"]
    assert d["coordination"]["meter_window"] == {"start": "2026-11-25T07:35:00Z",
                                                 "end": "2026-11-25T08:45:18Z"}
    assert d["coordination"]["coordinator_io_tokens"] == 30


def test_no_meter(tmp_path):
    rf = merged_scenario(tmp_path, 3)
    d = out(tmp_path, repos=rf)
    co = d["coordination"]
    assert co == {"meter_window": None, "coordinator_io_tokens": None, "claude_io_tokens": None,
                  "coordinator_io_share": None, "merged_changes": 3,
                  "coordinator_io_tokens_per_merged_change": None}
    assert d["warnings"]


def test_unreadable_meter_behaves_like_no_meter(tmp_path):
    rf = merged_scenario(tmp_path, 2)
    d = out(tmp_path, "--meter", str(tmp_path / "missing.json"), repos=rf)
    co = d["coordination"]
    assert co["coordinator_io_tokens"] is None and co["claude_io_tokens"] is None
    assert co["coordinator_io_share"] is None
    assert co["coordinator_io_tokens_per_merged_change"] is None
    assert co["merged_changes"] == 2
    assert d["warnings"]


def test_invalid_json_meter_behaves_like_no_meter(tmp_path):
    rf = merged_scenario(tmp_path, 2)
    bad = tmp_path / "bad.json"
    bad.write_text("[[", encoding="utf-8")
    d = out(tmp_path, "--meter", str(bad), repos=rf)
    assert d["coordination"]["coordinator_io_tokens"] is None
    assert d["warnings"]


@pytest.mark.parametrize("bad", [True, "5", None, [1]])
def test_non_number_coordinator_token_is_unusable(tmp_path, bad):
    rf = merged_scenario(tmp_path, 3)
    r = {"tokens": {"input": bad, "output": 20}}
    m = meter_file(tmp_path, {"coordinator": r, "m-s": role(1, 1)})
    d = out(tmp_path, "--meter", str(m), repos=rf)
    co = d["coordination"]
    assert co["coordinator_io_tokens"] is None
    assert co["coordinator_io_share"] is None
    assert co["coordinator_io_tokens_per_merged_change"] is None
    assert co["merged_changes"] == 3
    assert d["warnings"]
