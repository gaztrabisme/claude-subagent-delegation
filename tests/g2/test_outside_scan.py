"""G2 offline: scripts/outside_scan.py flags unverified harness lanes and stale fixtures."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "outside_scan.py"
NOW = "2026-11-26T12:00:00Z"


def _env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
    env.update(extra or {})
    return env


def git(repo: Path, *args: str, date: str | None = None, author_date: str | None = None) -> None:
    extra = {}
    if date:
        extra["GIT_COMMITTER_DATE"] = date
        extra["GIT_AUTHOR_DATE"] = author_date or date
    subprocess.run(["git", "-C", str(repo), *args], env=_env(repo.parent, extra), check=True,
                   capture_output=True, text=True)


def make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


def commit_file(repo: Path, rel: str, text: str, date: str, author_date: str | None = None):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", f"add {rel}", date=date, author_date=author_date)


def write_status(base: Path, rows: Any, name: str = "status.json") -> Path:
    p = base / name
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def lane(name: str, version: Any, verified: Any, state: str = "ok") -> dict:
    return {"lane": name, "harness_version": version, "harness_state": state,
            "verified_version": verified}


def run(base: Path, status: Path | None, fixtures: Path | None, *extra: str,
        now: str | None = NOW) -> subprocess.CompletedProcess:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist yet"
    cmd = [sys.executable, "-I", str(SCRIPT)]
    if status is not None:
        cmd += ["--status", str(status)]
    if fixtures is not None:
        cmd += ["--fixtures", str(fixtures)]
    if now is not None:
        cmd += ["--now", now]
    cmd += list(extra)
    return subprocess.run(cmd, cwd=base, env=_env(base), capture_output=True, text=True)


def scan(base: Path, status: Path, fixtures: Path, *extra: str, now: str | None = NOW) -> dict:
    cp = run(base, status, fixtures, *extra, now=now)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def empty_fixtures(base: Path) -> Path:
    d = base / "fx"
    d.mkdir()
    return d


def when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- lanes --------------------------------------------------------------------------

LANE_CASES = [
    (lane("a", None, None), "unknown"),
    (lane("b", None, "1.0"), "unknown"),
    (lane("c", "", None), "unknown"),
    (lane("d", "", "1.0"), "unknown"),
    (lane("e", "1.0", None), "unverified"),
    (lane("f", "1.0", ""), "unverified"),
    (lane("g", "1.0", "1.0"), "match"),
    (lane("h", "1.1", "1.0"), "drift"),
    (lane("i", "1.0", "1.1"), "drift"),
]


@pytest.mark.parametrize("row,state", LANE_CASES, ids=[r[0]["lane"] for r in LANE_CASES])
def test_lane_state(tmp_path, row, state):
    d = scan(tmp_path, write_status(tmp_path, [row]), empty_fixtures(tmp_path))
    assert d["lanes"] == [{"lane": row["lane"], "harness_version": row["harness_version"],
                           "verified_version": row["verified_version"], "state": state}]
    if state == "match":
        assert d["flags"] == []
    else:
        assert d["flags"] == [{"kind": "lane", "id": row["lane"], "reason": state}]


def test_lanes_keep_status_order_and_flags_follow_it(tmp_path):
    rows = [lane("zz", "1", "2"), lane("aa", "1", "1"), lane("mm", None, None),
            lane("bb", "3", None)]
    d = scan(tmp_path, write_status(tmp_path, rows), empty_fixtures(tmp_path))
    assert [x["lane"] for x in d["lanes"]] == ["zz", "aa", "mm", "bb"]
    assert d["flags"] == [
        {"kind": "lane", "id": "zz", "reason": "drift"},
        {"kind": "lane", "id": "mm", "reason": "unknown"},
        {"kind": "lane", "id": "bb", "reason": "unverified"},
    ]
    assert d["summary"] == {"lanes": 4, "lanes_flagged": 3, "fixtures": 0, "fixtures_flagged": 0}


def test_empty_status_list(tmp_path):
    d = scan(tmp_path, write_status(tmp_path, []), empty_fixtures(tmp_path))
    assert d["lanes"] == [] and d["flags"] == []


# ---- fixtures -----------------------------------------------------------------------

def fixture_repo(tmp_path):
    repo = make_repo(tmp_path)
    return repo, repo / "fx"


def test_fixture_dates_ages_and_sort(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/codex/z.jsonl", "{}\n", "2026-11-20T12:00:00Z")
    commit_file(repo, "fx/claude/b.jsonl", "{}\n", "2026-11-26T11:00:00Z")
    commit_file(repo, "fx/claude/a.jsonl", "{}\n", "2026-10-20T12:00:00Z")
    commit_file(repo, "fx/claude/notes.txt", "x\n", "2026-01-01T12:00:00Z")      # not jsonl
    commit_file(repo, "fx/claude/sub/deep.jsonl", "{}\n", "2026-01-01T12:00:00Z")  # too deep
    d = scan(tmp_path, write_status(tmp_path, []), fx)
    got = [(f["driver"], f["file"], f["age_days"], f["stale"]) for f in d["fixtures"]]
    assert got == [("claude", "claude/a.jsonl", 37, True),
                   ("claude", "claude/b.jsonl", 0, False),
                   ("codex", "codex/z.jsonl", 6, False)]
    assert list(d["fixtures"][0]) == ["driver", "file", "last_change", "age_days", "stale"]
    assert when(d["fixtures"][0]["last_change"]) == when("2026-10-20T12:00:00Z")
    assert d["flags"] == [{"kind": "fixture", "id": "claude/a.jsonl", "reason": "stale"}]
    assert d["summary"] == {"lanes": 0, "lanes_flagged": 0, "fixtures": 3, "fixtures_flagged": 1}


def test_last_change_is_last_commit_touching_that_file_by_committer_date(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/claude/a.jsonl", "1\n", "2026-06-01T00:00:00Z")
    commit_file(repo, "fx/claude/a.jsonl", "2\n", "2026-11-25T12:00:00Z",
                author_date="2026-01-01T00:00:00Z")
    commit_file(repo, "fx/claude/other.jsonl", "x\n", "2026-11-26T00:00:00Z")
    d = scan(tmp_path, write_status(tmp_path, []), fx)
    a = [f for f in d["fixtures"] if f["file"] == "claude/a.jsonl"][0]
    assert when(a["last_change"]) == when("2026-11-25T12:00:00Z")
    assert a["age_days"] == 1 and a["stale"] is False


def test_stale_at_max_age_plus_one_but_not_at_max_age(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/d/at30.jsonl", "x", "2026-10-27T12:00:00Z")      # exactly 30 days
    commit_file(repo, "fx/d/at30h.jsonl", "x", "2026-10-27T11:00:00Z")     # 30 d 1 h: floor 30
    commit_file(repo, "fx/d/at31.jsonl", "x", "2026-10-26T12:00:00Z")      # 31 days
    d = scan(tmp_path, write_status(tmp_path, []), fx)
    by = {f["file"]: f for f in d["fixtures"]}
    assert by["d/at30.jsonl"]["age_days"] == 30 and by["d/at30.jsonl"]["stale"] is False
    assert by["d/at30h.jsonl"]["age_days"] == 30 and by["d/at30h.jsonl"]["stale"] is False
    assert by["d/at31.jsonl"]["age_days"] == 31 and by["d/at31.jsonl"]["stale"] is True
    assert [f["id"] for f in d["flags"]] == ["d/at31.jsonl"]


def test_max_age_days_option(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/d/a.jsonl", "x", "2026-11-16T12:00:00Z")   # 10 days
    commit_file(repo, "fx/d/b.jsonl", "x", "2026-11-15T12:00:00Z")   # 11 days
    d = scan(tmp_path, write_status(tmp_path, []), fx, "--max-age-days", "10")
    assert d["max_age_days"] == 10
    by = {f["file"]: f["stale"] for f in d["fixtures"]}
    assert by == {"d/a.jsonl": False, "d/b.jsonl": True}


def test_default_max_age_is_30(tmp_path):
    d = scan(tmp_path, write_status(tmp_path, []), empty_fixtures(tmp_path))
    assert d["max_age_days"] == 30


def test_untracked_fixture_is_undated(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/d/tracked.jsonl", "x", "2026-11-25T12:00:00Z")
    (fx / "d" / "loose.jsonl").write_text("x", encoding="utf-8")   # never added
    d = scan(tmp_path, write_status(tmp_path, []), fx)
    loose = [f for f in d["fixtures"] if f["file"] == "d/loose.jsonl"][0]
    assert loose["last_change"] is None
    assert d["flags"] == [{"kind": "fixture", "id": "d/loose.jsonl", "reason": "undated"}]
    assert d["summary"]["fixtures"] == 2 and d["summary"]["fixtures_flagged"] == 1


def test_fixtures_outside_any_git_repo_are_undated(tmp_path):
    fx = tmp_path / "plain"
    (fx / "d").mkdir(parents=True)
    (fx / "d" / "x.jsonl").write_text("x", encoding="utf-8")
    d = scan(tmp_path, write_status(tmp_path, []), fx)
    assert d["fixtures"][0]["last_change"] is None
    assert d["flags"] == [{"kind": "fixture", "id": "d/x.jsonl", "reason": "undated"}]


def test_flag_order_lanes_first_then_fixtures_in_fixture_order(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/b/old.jsonl", "x", "2026-01-01T00:00:00Z")
    commit_file(repo, "fx/a/old.jsonl", "x", "2026-01-01T00:00:00Z")
    (fx / "a" / "zloose.jsonl").write_text("x", encoding="utf-8")
    rows = [lane("q", "1", None), lane("p", "1", "1")]
    d = scan(tmp_path, write_status(tmp_path, rows), fx)
    assert d["flags"] == [
        {"kind": "lane", "id": "q", "reason": "unverified"},
        {"kind": "fixture", "id": "a/old.jsonl", "reason": "stale"},
        {"kind": "fixture", "id": "a/zloose.jsonl", "reason": "undated"},
        {"kind": "fixture", "id": "b/old.jsonl", "reason": "stale"},
    ]
    assert d["summary"] == {"lanes": 2, "lanes_flagged": 1, "fixtures": 3, "fixtures_flagged": 3}


def test_output_shape_network_false_and_determinism(tmp_path):
    repo, fx = fixture_repo(tmp_path)
    commit_file(repo, "fx/d/a.jsonl", "x", "2026-11-25T12:00:00Z")
    st = write_status(tmp_path, [lane("a", "1", "1")])
    cp1 = run(tmp_path, st, fx)
    cp2 = run(tmp_path, st, fx)
    assert cp1.returncode == 0 and cp1.stdout == cp2.stdout
    d = json.loads(cp1.stdout)
    assert list(d) == ["now", "max_age_days", "lanes", "fixtures", "flags", "summary", "network"]
    assert d["network"] is False
    assert when(d["now"]) == when(NOW)
    assert list(d["summary"]) == ["lanes", "lanes_flagged", "fixtures", "fixtures_flagged"]
    assert list(d["lanes"][0]) == ["lane", "harness_version", "verified_version", "state"]


# ---- exit 2 -------------------------------------------------------------------------

def test_status_missing_is_exit_2(tmp_path):
    cp = run(tmp_path, tmp_path / "nope.json", empty_fixtures(tmp_path))
    assert cp.returncode == 2 and cp.stderr.strip()


def test_status_invalid_json_is_exit_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    cp = run(tmp_path, bad, empty_fixtures(tmp_path))
    assert cp.returncode == 2 and cp.stderr.strip()


@pytest.mark.parametrize("payload", [{"lane": "a"}, "text", 5, None])
def test_status_not_a_list_is_exit_2(tmp_path, payload):
    cp = run(tmp_path, write_status(tmp_path, payload), empty_fixtures(tmp_path))
    assert cp.returncode == 2 and cp.stderr.strip()


def test_fixtures_dir_missing_is_exit_2(tmp_path):
    cp = run(tmp_path, write_status(tmp_path, []), tmp_path / "nope")
    assert cp.returncode == 2 and cp.stderr.strip()


def test_script_does_not_import_network_modules():
    assert SCRIPT.exists(), "scripts/outside_scan.py does not exist yet"
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
    assert not re.search(r"__import__\(\s*['\"](socket|urllib|http|requests)",
                         SCRIPT.read_text(encoding="utf-8"))
