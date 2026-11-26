"""T5: scripts/harm3_coverage.py (SPEC section 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._helpers import assert_out_contract, commit, git, make_repo, run, run_json

S = "harm3_coverage.py"
HDR = "| Rank | Rule (source) | Breaks | Evidence | Harm | Score | Cheapest mechanism |\n|---|---|---|---|---|---|---|\n"


def audit(tmp_path: Path, rows: list[tuple[str, str, str]], pre: str = "") -> Path:
    body = "".join(f"| {r} | {rule} | b | e | {h} | 9 | m |\n" for r, rule, h in rows)
    p = tmp_path / "audit.md"
    p.write_text("# Audit\n\n" + pre + HDR + body + "\nafter\n", encoding="utf-8")
    return p


def mapfile(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "map.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def go(repo: Path, aud: Path, mp: Path, base: str = "fuse") -> dict:
    return run_json(S, ["--repo", repo, "--audit", aud, "--map", mp, "--base", base])


def mech(name: str, covers: str, paths: list[str], merge: str) -> dict:
    return {"mechanism": name, "covers": covers, "paths": paths, "merge": merge}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = make_repo(tmp_path)
    commit(r, "init", files={"a.py": "1", "dir/b.py": "2"})
    return r


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def test_covered_part_none_and_counts(repo: Path, tmp_path: Path) -> None:
    sha = head(repo)
    a = audit(tmp_path, [("2", "Rule two", "3"), ("7", "Rule seven", "3"), ("9", "Rule nine", "3"),
                         ("4", "Rule four", "2")])
    m = mapfile(tmp_path, {
        "2": [mech("m1", "part", ["a.py"], sha), mech("m2", "full", ["dir/b.py"], sha)],
        "7": [mech("m3", "part", ["a.py"], sha)],
        "9": [mech("m4", "full", ["a.py"], "0" * 40)],
    })
    out = go(repo, a, m)
    assert out["base"] == "fuse"
    assert (out["harm3_rows"], out["covered"], out["part"], out["none"]) == (3, 1, 1, 1)
    assert [(r["rank"], r["rule"], r["status"]) for r in out["rows"]] == [
        ("2", "Rule two", "covered"), ("7", "Rule seven", "part"), ("9", "Rule nine", "none")]
    assert out["rows"][0]["mechanisms"] == [
        {"mechanism": "m1", "covers": "part", "merged": True, "failed": []},
        {"mechanism": "m2", "covers": "full", "merged": True, "failed": []},
    ]


def test_row_without_map_entry_is_none(repo: Path, tmp_path: Path) -> None:
    a = audit(tmp_path, [("2", "R", "3")])
    out = go(repo, a, mapfile(tmp_path, {}))
    assert out["rows"][0]["status"] == "none" and out["rows"][0]["mechanisms"] == []
    assert out["none"] == 1


def test_unknown_merge(repo: Path, tmp_path: Path) -> None:
    a = audit(tmp_path, [("2", "R", "3")])
    m = mapfile(tmp_path, {"2": [mech("m", "full", ["a.py"], "deadbeef" * 5)]})
    row = go(repo, a, m)["rows"][0]
    assert row["status"] == "none"
    assert row["mechanisms"][0]["merged"] is False
    assert row["mechanisms"][0]["failed"] == [f"merge {'deadbeef' * 5} unknown"]


def test_merge_not_ancestor_of_base(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, "side", files={"s.py": "x"})
    git(repo, "checkout", "-q", "fuse")
    a = audit(tmp_path, [("2", "R", "3")])
    m = mapfile(tmp_path, {"2": [mech("m", "full", ["a.py"], side)]})
    row = go(repo, a, m)["rows"][0]
    assert row["status"] == "none"
    assert row["mechanisms"][0]["failed"] == [f"merge {side} not an ancestor of fuse"]


def test_missing_path_in_base_tree(repo: Path, tmp_path: Path) -> None:
    sha = head(repo)
    (repo / "untracked.py").write_text("u")           # on disk but not in base tree
    a = audit(tmp_path, [("2", "R", "3")])
    m = mapfile(tmp_path, {"2": [mech("m", "full", ["a.py", "untracked.py", "gone/x.py"], sha)]})
    row = go(repo, a, m)["rows"][0]
    assert row["status"] == "none"
    assert row["mechanisms"][0]["merged"] is False
    assert row["mechanisms"][0]["failed"] == ["missing path untracked.py", "missing path gone/x.py"]


def test_path_checked_in_base_not_worktree(repo: Path, tmp_path: Path) -> None:
    sha = head(repo)
    (repo / "a.py").unlink()                          # deleted on disk, still in fuse
    a = audit(tmp_path, [("2", "R", "3")])
    m = mapfile(tmp_path, {"2": [mech("m", "full", ["a.py"], sha)]})
    assert go(repo, a, m)["rows"][0]["status"] == "covered"


def test_unmerged_full_with_merged_part_is_part(repo: Path, tmp_path: Path) -> None:
    sha = head(repo)
    a = audit(tmp_path, [("2", "R", "3")])
    m = mapfile(tmp_path, {"2": [mech("f", "full", ["nope.py"], sha), mech("p", "part", ["a.py"], sha)]})
    assert go(repo, a, m)["rows"][0]["status"] == "part"


def test_non_harm3_map_entry_warns_and_is_ignored(repo: Path, tmp_path: Path) -> None:
    sha = head(repo)
    a = audit(tmp_path, [("2", "R", "3"), ("4", "Low", "2")])
    m = mapfile(tmp_path, {"4": [mech("m", "full", ["a.py"], sha)], "88": []})
    out = go(repo, a, m)
    assert out["harm3_rows"] == 1 and [r["rank"] for r in out["rows"]] == ["2"]
    assert out["covered"] == 0
    assert any("4" in w for w in out["warnings"])


def test_missing_map_warns_all_none(repo: Path, tmp_path: Path) -> None:
    a = audit(tmp_path, [("2", "R", "3"), ("3", "S", "3")])
    out = run_json(S, ["--repo", repo, "--audit", a, "--map", tmp_path / "absent.json", "--base", "fuse"])
    assert any("no mechanism map" in w for w in out["warnings"])
    assert (out["harm3_rows"], out["none"], out["covered"], out["part"]) == (2, 2, 0, 0)


def test_header_prefix_and_harm_cell_exact(repo: Path, tmp_path: Path) -> None:
    sha = head(repo)
    decoy = "| Rank | Rule | Harm |\n|---|---|---|\n| 1 | decoy | 3 |\n\n"
    a = audit(tmp_path, [("2", "R", " 3 "), ("5", "T", "30"), ("6", "U", "3.5"), ("8", "V", "2")], pre=decoy)
    m = mapfile(tmp_path, {"2": [mech("m", "full", ["a.py"], sha)]})
    out = go(repo, a, m)
    assert [r["rank"] for r in out["rows"]] == ["2"]
    assert out["covered"] == 1


def test_rows_in_table_order(repo: Path, tmp_path: Path) -> None:
    a = audit(tmp_path, [("10", "A", "3"), ("2", "B", "3"), ("33", "C", "3")])
    out = go(repo, a, mapfile(tmp_path, {}))
    assert [r["rank"] for r in out["rows"]] == ["10", "2", "33"]


@pytest.mark.parametrize("which", ["audit", "badmap", "base"])
def test_bad_input_exit_2(repo: Path, tmp_path: Path, which: str) -> None:
    a = audit(tmp_path, [("2", "R", "3")])
    mp = mapfile(tmp_path, {})
    args: dict = {"--repo": repo, "--audit": a, "--map": mp, "--base": "fuse"}
    if which == "audit":
        args["--audit"] = tmp_path / "missing.md"
    elif which == "badmap":
        mp.write_text("{not json", encoding="utf-8")
    else:
        args["--base"] = "nobranch"
    r = run(S, [x for kv in args.items() for x in kv])
    assert r.returncode == 2 and r.stderr.strip()


def test_out_contract(repo: Path, tmp_path: Path) -> None:
    a = audit(tmp_path, [("2", "R", "3")])
    m = mapfile(tmp_path, {})
    o = tmp_path / "o"
    o.mkdir()
    assert_out_contract(S, ["--repo", repo, "--audit", a, "--map", m, "--base", "fuse"], o / "r.json", [o, repo])
