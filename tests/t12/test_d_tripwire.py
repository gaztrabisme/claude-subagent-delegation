"""T12 spec: tripwire threshold logic, cut_list ranking, key order, no network."""

from __future__ import annotations

import ast
import re

from ._helpers import (
    CUR,
    PREV,
    SCRIPT,
    commit,
    entry,
    make_repo,
    out,
    pad,
    repos_file,
)


def scenario(tmp_path, cur, prev, rulebook=None):
    """cur / prev are (machinery commits, outside commits) in each window."""
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    pad(m, PREV, prev[0])
    pad(o, PREV, prev[1])
    pad(m, CUR, cur[0])
    pad(o, CUR, cur[1])
    return repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")], rulebook=rulebook)


def tw(tmp_path, *args, **kw):
    d = out(tmp_path, *args, repos=kw["rf"])
    return d["tripwire"]


def test_tripped_when_both_windows_strictly_above(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (9, 1), (5, 0)))
    assert list(t) == ["threshold", "current", "previous", "tripped", "cut_list"]
    assert t["threshold"] == 80.0
    assert t["current"] == 90.0 and t["previous"] == 100.0
    assert t["tripped"] is True


def test_exactly_at_threshold_is_not_tripped(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (4, 1), (4, 1)))
    assert t["current"] == 80.0 and t["previous"] == 80.0
    assert t["tripped"] is False and t["cut_list"] == []


def test_only_current_above_is_not_tripped(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (9, 1), (4, 1)))
    assert t["tripped"] is False and t["cut_list"] == []


def test_only_previous_above_is_not_tripped(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (4, 1), (9, 1)))
    assert t["tripped"] is False


def test_null_previous_is_not_tripped(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (9, 1), (0, 0)))
    assert t["previous"] is None and t["current"] == 90.0
    assert t["tripped"] is False and t["cut_list"] == []


def test_null_current_is_not_tripped(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (0, 0), (9, 1)))
    assert t["current"] is None and t["tripped"] is False


def test_threshold_option(tmp_path):
    rf = scenario(tmp_path, (4, 1), (4, 1))
    t = out(tmp_path, "--threshold", "79.9", repos=rf)["tripwire"]
    assert t["threshold"] == 79.9 and t["tripped"] is True
    t = out(tmp_path, "--threshold", "50", repos=rf)["tripwire"]
    assert t["threshold"] == 50.0 and t["tripped"] is True
    t = out(tmp_path, "--threshold", "80.1", repos=rf)["tripwire"]
    assert t["tripped"] is False


def test_tripped_with_nothing_to_cut_gives_empty_list(tmp_path):
    t = tw(tmp_path, rf=scenario(tmp_path, (9, 1), (9, 1)))
    assert t["tripped"] is True and t["cut_list"] == []


# ---- cut list -----------------------------------------------------------------------

def section(name: str, words: int) -> str:
    return f"## Method: {name}\n" + " ".join(f"w{i}" for i in range(words - 4)) + "\n"


def cut_scenario(tmp_path, book: str, setup):
    """Machinery repo m (wiki records) and outside repo o, tripped in both windows."""
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    pad(m, PREV, 5)
    pad(o, PREV, 1)
    pad(m, CUR, 5)
    pad(o, CUR, 1)
    bookfile = tmp_path / "book.md"
    bookfile.write_text(book, encoding="utf-8")
    setup(m, o)
    rf = repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")],
                    rulebook=[str(bookfile)])
    d = out(tmp_path, repos=rf)
    assert d["tripwire"]["tripped"] is True
    return d["tripwire"]["cut_list"], bookfile


def test_cut_list_ranking_cap_cost_and_record_candidates(tmp_path):
    alpha = section("Alpha rule", 20) + "## Other\nnot counted words here\n"
    delta = section("Delta rule", 20)
    beta = section("Beta rule", 40)
    gamma = section("Gamma rule", 30)
    book = "# Book\n\n" + alpha + delta + beta + gamma
    expected_alpha = len((section("Alpha rule", 20)).split())

    def setup(m, o):
        # records-only commits in the window: notes.md gets 3 + 3 added words
        commit(m, {"wiki/notes.md": "a b\nc\n"}, "wiki notes", CUR)
        commit(m, {"wiki/notes.md": "a b\nc\nd e f\n"}, "wiki notes more", CUR)
        # a record touched only by a mixed commit is not a candidate (large cost, effect 0)
        commit(m, {"wiki/mixed.md": " ".join(["m"] * 100) + "\n", "src/code.py": "c"},
               "mixed commit", CUR)
        # beta is mentioned in a non-records commit (effect 1), gamma twice (effect 2)
        commit(m, {"src/b.py": "b"}, "touch things\n\nbody mentions BETA RULE here", CUR)
        commit(m, {"src/g1.py": "g"}, "gamma rule fix", CUR)
        commit(o, {"src/g2.py": "g"}, "work\n\nalso the Gamma Rule", CUR)
        # a records-only commit that names Alpha does not count as effect
        commit(m, {"wiki/other.md": "o"}, "records mention alpha rule", CUR)

    cut, bookfile = cut_scenario(tmp_path, book, setup)
    assert len(cut) == 3
    assert [list(i) for i in cut] == [["kind", "name", "file", "cost", "effect"]] * 3
    # effect 0: Alpha (cost 20), Delta (cost 20, name ascending), notes (cost 6),
    # other.md (cost 1) -- cap of 3 drops the cheapest, and mixed.md is not a candidate
    assert cut[0] == {"kind": "method", "name": "Alpha rule", "file": str(bookfile),
                      "cost": expected_alpha, "effect": 0}
    assert cut[1]["name"] == "Delta rule" and cut[1]["kind"] == "method"
    assert cut[1]["cost"] == expected_alpha and cut[1]["effect"] == 0
    assert cut[2] == {"kind": "record", "name": "m:wiki/notes.md", "file": "wiki/notes.md",
                      "cost": 6, "effect": 0}


def test_cut_list_effect_is_global_case_insensitive_and_body_aware(tmp_path):
    book = ("# Book\n\n" + section("Pee rule", 100) + section("Tee rule", 200)
            + section("Qu rule", 10) + section("Ar rule", 9) + section("Ess rule", 8))

    def setup(m, o):
        # only an outside-repo commit, in the body and in other case, names Pee rule
        commit(o, {"src/p.py": "p"}, "outside work\n\nPEE RULE applied", CUR)
        # a records-only commit names Tee rule: not counted as effect
        commit(m, {"wiki/tee.md": "t"}, "records mention tee rule", CUR)

    cut, _ = cut_scenario(tmp_path, book, setup)
    names = [i["name"] for i in cut]
    # Tee has effect 0 and the biggest cost; Pee has effect 1 so it falls behind the cap
    assert names[0] == "Tee rule"
    assert "Pee rule" not in names
    assert all(i["effect"] == 0 for i in cut)


def test_cut_list_effect_value_reported(tmp_path):
    book = "# B\n\n" + section("Solo rule", 12)

    def setup(m, o):
        commit(m, {"src/a.py": "a"}, "uses solo rule", CUR)
        commit(m, {"src/b.py": "b"}, "SOLO RULE again", CUR)
        commit(o, {"src/c.py": "c"}, "unrelated", CUR)

    cut, bookfile = cut_scenario(tmp_path, book, setup)
    assert cut == [{"kind": "method", "name": "Solo rule", "file": str(bookfile),
                    "cost": 12, "effect": 2}]


def test_record_cost_counts_added_words_only_and_not_diff_headers(tmp_path):
    book = "# B\n"

    def setup(m, o):
        commit(m, {"wiki/long.md": "one two three four five\nsix\n"}, "wiki long", CUR)
        commit(m, {"wiki/long.md": "one two three four five\nseven eight\n"}, "wiki long 2", CUR)

    cut, _ = cut_scenario(tmp_path, book, setup)
    rec = [i for i in cut if i["name"] == "m:wiki/long.md"]
    # added lines: "one..five"(5) + "six"(1), then "seven eight"(2) = 8; removed lines ignored
    assert rec == [{"kind": "record", "name": "m:wiki/long.md", "file": "wiki/long.md",
                    "cost": 8, "effect": 0}]


def test_pre_window_record_commits_are_not_candidates(tmp_path):
    book = "# B\n"

    def setup(m, o):
        commit(m, {"wiki/old.md": "old words here"}, "old wiki", PREV)

    cut, _ = cut_scenario(tmp_path, book, setup)
    assert cut == []


def test_cut_list_empty_when_not_tripped_even_with_candidates(tmp_path):
    book = tmp_path / "book.md"
    book.write_text("# B\n\n" + section("Some rule", 10), encoding="utf-8")
    m = make_repo(tmp_path, "m")
    o = make_repo(tmp_path, "o")
    pad(m, CUR, 4)
    pad(o, CUR, 1)
    pad(m, PREV, 4)
    pad(o, PREV, 1)
    rf = repos_file(tmp_path, [entry("m", m), entry("o", o, "outside")], rulebook=[str(book)])
    t = out(tmp_path, repos=rf)["tripwire"]
    assert t["tripped"] is False and t["cut_list"] == []


# ---- shape and offline --------------------------------------------------------------

def test_top_level_and_nested_key_order(tmp_path):
    d = out(tmp_path, repos=scenario(tmp_path, (4, 1), (4, 1)))
    assert list(d) == ["window", "previous_window", "commits", "machinery", "rulebook",
                       "coordination", "owner_questions", "tripwire", "warnings"]
    assert list(d["window"]) == ["start", "end"]
    assert list(d["commits"]) == ["total", "records_only", "records_only_share", "per_repo"]
    assert list(d["machinery"]) == ["machinery_commits", "outside_commits", "machinery_share",
                                    "outside_share"]
    assert isinstance(d["warnings"], list)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", d["window"]["start"])


def test_script_does_not_import_network_modules():
    assert SCRIPT.exists(), "scripts/overhead_meter.py does not exist yet"
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    banned = {"socket", "urllib", "http", "requests", "httpx", "ssl", "ftplib", "smtplib",
              "aiohttp", "websocket", "websockets", "xmlrpc"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            seen.add(node.module.split(".")[0])
    assert not (seen & banned), seen & banned
    # no dynamic import of those either
    text = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"__import__\(\s*['\"](socket|urllib|http|requests)", text)
