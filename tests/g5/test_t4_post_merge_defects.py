"""T4: scripts/post_merge_defects.py (SPEC section 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._helpers import (
    assert_out_contract,
    commit,
    git,
    make_repo,
    run,
    run_json,
    short,
)

S = "post_merge_defects.py"
HDR = "| id | package | found | by | fixed in | note |\n|---|---|---|---|---|---|\n"


def reg(tmp_path: Path, rows: list[str], header: str = HDR, name: str = "reg.md") -> Path:
    p = tmp_path / name
    p.write_text("# Register\n\n" + header + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def row(i: str, pkg: str, found: str = "2026-11-12") -> str:
    return f"| {i} | {pkg} | {found} | qa | abc1234 | note |"


def go(repo: Path, register: Path | None = None, *extra: str, base: str = "fuse") -> dict:
    args = ["--repo", repo, "--base", base,
            "--register", register or repo.parent / "none.md", *extra]
    return run_json(S, args)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = make_repo(tmp_path)
    commit(r, "init")
    return r


def test_trailer_without_defect_id_is_own_defect(repo: Path, tmp_path: Path) -> None:
    sha = commit(repo, "fix a", ["Fixes-pkg: R1a"])
    out = go(repo)
    assert out["packages"] == {"R1a": {"defects": 1, "ids": [f"commit:{short(repo, sha)}"]}}
    assert out["total"] == 1 and out["from_commits"] == 1
    assert out["from_register"] == 0 and out["both"] == 0
    assert out["base"] == "fuse"
    assert out["window"] == {"since": None, "until": None}


def test_commit_without_trailer_ignored(repo: Path) -> None:
    commit(repo, "plain")
    out = go(repo)
    assert out["packages"] == {} and out["total"] == 0


def test_defect_id_names_defect_and_dedups(repo: Path) -> None:
    commit(repo, "fix 1", ["Fixes-pkg: R1a", "Defect-id: D-1"])
    commit(repo, "fix 2", ["Fixes-pkg: R1a", "Defect-id: D-1"])
    commit(repo, "fix 3", ["Fixes-pkg: R1a", "Defect-id: D-2"])
    out = go(repo)
    assert out["packages"]["R1a"] == {"defects": 2, "ids": ["D-1", "D-2"]}
    assert out["total"] == 2 and out["from_commits"] == 2


def test_two_fixes_pkg_count_once_each(repo: Path) -> None:
    commit(repo, "fix", ["Fixes-pkg: R1a", "Fixes-pkg: T12", "Defect-id: D-9"])
    out = go(repo)
    assert out["packages"]["R1a"] == {"defects": 1, "ids": ["D-9"]}
    assert out["packages"]["T12"] == {"defects": 1, "ids": ["D-9"]}


def test_packages_and_ids_sorted(repo: Path) -> None:
    for pkg, i in [("T12", "D-3"), ("R1a", "D-2"), ("R1a", "D-1")]:
        commit(repo, "fix", [f"Fixes-pkg: {pkg}", f"Defect-id: {i}"])
    out = go(repo)
    assert list(out["packages"]) == ["R1a", "T12"]
    assert out["packages"]["R1a"]["ids"] == ["D-1", "D-2"]


def test_only_commits_reachable_from_base(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "side fix", ["Fixes-pkg: R9", "Defect-id: D-side"])
    git(repo, "checkout", "-q", "fuse")
    commit(repo, "main fix", ["Fixes-pkg: R1a", "Defect-id: D-main"])
    out = go(repo)
    assert list(out["packages"]) == ["R1a"]


def test_base_option(tmp_path: Path) -> None:
    r = make_repo(tmp_path, branch="main")
    commit(r, "init")
    commit(r, "fix", ["Fixes-pkg: R1a", "Defect-id: D-1"])
    out = go(r, base="main")
    assert out["base"] == "main" and out["total"] == 1


def test_register_only(repo: Path, tmp_path: Path) -> None:
    p = reg(tmp_path, [row("D-5", "R2"), row("D-6", "R2"), row("D-7", "R1a")])
    out = go(repo, p)
    assert out["packages"]["R2"] == {"defects": 2, "ids": ["D-5", "D-6"]}
    assert out["packages"]["R1a"] == {"defects": 1, "ids": ["D-7"]}
    assert (out["from_register"], out["from_commits"], out["both"], out["total"]) == (3, 0, 0, 3)


def test_register_and_commit_same_id_counted_once_register_package_wins(repo: Path, tmp_path: Path) -> None:
    commit(repo, "fix", ["Fixes-pkg: WRONG", "Defect-id: D-1"])
    commit(repo, "fix", ["Fixes-pkg: R1a", "Defect-id: D-2"])
    p = reg(tmp_path, [row("D-1", "R1a"), row("D-3", "R2")])
    out = go(repo, p)
    assert "WRONG" not in out["packages"]
    assert out["packages"]["R1a"] == {"defects": 2, "ids": ["D-1", "D-2"]}
    assert out["packages"]["R2"]["ids"] == ["D-3"]
    assert (out["both"], out["from_commits"], out["from_register"], out["total"]) == (1, 1, 1, 3)


def test_missing_register_is_warning_not_error(repo: Path) -> None:
    commit(repo, "fix", ["Fixes-pkg: R1a", "Defect-id: D-1"])
    out = go(repo, repo.parent / "nope.md")
    assert any("no register file" in w for w in out["warnings"])
    assert out["from_register"] == 0 and out["total"] == 1


def test_bad_row_skipped_with_warning(repo: Path, tmp_path: Path) -> None:
    p = reg(tmp_path, [row("D-1", "R1a"), "| D-2 | R1a | 2026-11-12 | qa |", row("D-3", "R1a")])
    out = go(repo, p)
    assert out["packages"]["R1a"]["ids"] == ["D-1", "D-3"]
    assert out["warnings"], "a skipped row must be reported"


def test_register_table_selected_by_exact_header(repo: Path, tmp_path: Path) -> None:
    other = "| id | package | found | by |\n|---|---|---|---|\n| X-1 | Z | 2026-11-12 | q |\n\n"
    p = tmp_path / "reg.md"
    p.write_text("# R\n\n" + other + HDR + row("D-1", "R1a") + "\n\n" + HDR + row("D-2", "R1a") + "\n")
    out = go(repo, p)
    assert out["packages"] == {"R1a": {"defects": 1, "ids": ["D-1"]}}


def test_since_until_filter_commits_inclusive(repo: Path) -> None:
    for i, d in [("D-1", "2026-11-09T23:00:00Z"), ("D-2", "2026-11-10T00:30:00Z"),
                 ("D-3", "2026-11-12T20:00:00Z"), ("D-4", "2026-11-13T01:00:00Z")]:
        commit(repo, "fix", ["Fixes-pkg: R1a", f"Defect-id: {i}"], date=d)
    out = go(repo, None, "--since", "2026-11-10", "--until", "2026-11-12")
    assert out["packages"]["R1a"]["ids"] == ["D-2", "D-3"]
    assert out["window"] == {"since": "2026-11-10", "until": "2026-11-12"}


def test_since_until_filter_register_rows(repo: Path, tmp_path: Path) -> None:
    p = reg(tmp_path, [row("D-1", "R1a", "2026-11-09"), row("D-2", "R1a", "2026-11-10"),
                       row("D-3", "R1a", "2026-11-12"), row("D-4", "R1a", "2026-11-13")])
    out = go(repo, p, "--since", "2026-11-10", "--until", "2026-11-12")
    assert out["packages"]["R1a"]["ids"] == ["D-2", "D-3"]
    out = go(repo, p, "--since", "2026-11-13")
    assert out["packages"]["R1a"]["ids"] == ["D-4"]
    out = go(repo, p, "--until", "2026-11-09")
    assert out["packages"]["R1a"]["ids"] == ["D-1"]


@pytest.mark.parametrize("bad", [
    ["--base", "no-such-branch"],
    ["--since", "not-a-date"],
    ["--until", "2026-13-45"],
])
def test_bad_input_exit_2(repo: Path, bad: list[str]) -> None:
    r = run(S, ["--repo", repo, "--register", repo.parent / "n.md", *bad])
    assert r.returncode == 2 and r.stderr.strip()


def test_not_a_repo_exit_2(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    r = run(S, ["--repo", d, "--register", tmp_path / "n.md"])
    assert r.returncode == 2 and r.stderr.strip()


def test_out_contract(repo: Path, tmp_path: Path) -> None:
    commit(repo, "fix", ["Fixes-pkg: R1a", "Defect-id: D-1"])
    p = reg(tmp_path, [row("D-2", "R2")])
    outdir = tmp_path / "o"
    outdir.mkdir()
    assert_out_contract(S, ["--repo", repo, "--register", p], outdir / "r.json", [outdir, repo])
    d = json.loads((outdir / "r.json").read_text())
    assert d["total"] == 2
