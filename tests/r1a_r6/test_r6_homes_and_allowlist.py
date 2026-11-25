"""R1a round 6 acceptance tests: per-agent homes and a write allowlist (H1-H5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from subagent import sandbox
from tests.r1a._helpers import sbpl_allows

from ._lanes import (
    HOME_ENV,
    LANE_IDS,
    capture_lane,
    run_probe,
    skip_unless_seatbelt,
    write_allow_roots,
)

STD_DEVICES = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper", "/dev/fd")


def _under(path: str | Path, root: str | Path) -> bool:
    path, root = os.path.abspath(path), os.path.abspath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _owner_files(owner: Path) -> list[Path]:
    """Every fake owner-home file, plus new names in the owner CLI dirs and the home root."""
    existing = [p for p in owner.rglob("*") if p.is_file()]
    new = [owner / ".claude" / "new-hook.json", owner / ".claude" / "projects" / "p" / "new.md",
           owner / ".codex" / "new.toml", owner / ".omp" / "agent" / "new.yml",
           owner / ".dsh" / "new.yaml", owner / "fresh-file.txt", owner / ".bashrc"]
    return existing + new


@pytest.mark.parametrize("lane_id", LANE_IDS)
def test_h1_live_seatbelt_write_allowlist(tmp_path: Path, monkeypatch, lane_id: str) -> None:
    """H1: live sandbox-exec; child writes ws and agent dir, never the owner home, <ws>/.codex or other /dev nodes."""
    lane = capture_lane(tmp_path, monkeypatch, lane_id)
    skip_unless_seatbelt()
    profile = lane.profile
    must_write = [lane.ws / "h1-probe.txt", lane.agent_dir / "h1-probe.txt", Path("/dev/null")]
    if lane.child_home is not None:
        must_write.append(lane.child_home / "h1-scratch.txt")
    must_fail = [*_owner_files(lane.owner), lane.ws / ".codex" / "config.toml",
                 lane.ws / ".codex" / "hooks.json", lane.ws / ".codex" / "new.toml",
                 Path("/dev/random")]
    result = run_probe(profile, [*must_write, *must_fail])
    wrongly_denied = [str(p) for p in must_write if result[str(p)] != "ok"]
    wrongly_allowed = [str(p) for p in must_fail if result[str(p)] == "ok"]
    problems = []
    if wrongly_denied:
        problems.append(f"expected writable but denied: {wrongly_denied}")
    if wrongly_allowed:
        problems.append(f"expected denied but writable: {wrongly_allowed}")
    if '(subpath "/dev")' in profile:
        problems.append("profile grants all of /dev")
    assert not problems, f"{lane_id}: {problems}"


def test_h1_profile_write_roots_are_exactly_ws_agent_dir_and_devices(
    tmp_path: Path, monkeypatch,
) -> None:
    """H1: in every A9 profile each write root is the workspace, the agent dir or a standard device."""
    problems: list[str] = []
    for lane_id in LANE_IDS:
        sub = tmp_path / lane_id
        sub.mkdir()
        lane = capture_lane(sub, monkeypatch, lane_id)
        for root in write_allow_roots(lane.profile):
            ok = (_under(root, lane.ws) or _under(root, lane.agent_dir)
                  or root in STD_DEVICES)
            if not ok:
                problems.append(f"{lane_id}: write root {root}")
            if _under(root, lane.owner) or (_under(lane.owner, root) and root != "/"):
                problems.append(f"{lane_id}: write root {root} touches the owner home")
    assert not problems, problems


def test_h2_child_homes_live_under_the_agent_directory(tmp_path: Path, monkeypatch) -> None:
    """H2: CLAUDE_CONFIG_DIR (both auth modes), CODEX_HOME, the omp and dsh homes are under agents/<id>/."""
    problems: list[str] = []
    for lane_id in LANE_IDS:
        sub = tmp_path / lane_id
        sub.mkdir()
        lane = capture_lane(sub, monkeypatch, lane_id)
        value = lane.env.get(HOME_ENV[lane_id])
        if not value:
            problems.append(f"{lane_id}: {HOME_ENV[lane_id]} not set in the child env")
        elif not _under(value, lane.agent_dir):
            problems.append(f"{lane_id}: {HOME_ENV[lane_id]}={value} is not under {lane.agent_dir}")
        elif not Path(value).is_dir():
            problems.append(f"{lane_id}: {value} was not created")
    assert not problems, problems


def test_h3_claude_login_home_holds_only_the_credential_brought_in(
    tmp_path: Path, monkeypatch,
) -> None:
    """H3: a claude login lane's CLAUDE_CONFIG_DIR carries the owner's .credentials.json and no other owner state."""
    lane = capture_lane(tmp_path, monkeypatch, "claude-login")
    home = lane.child_home
    assert home is not None and _under(home, lane.agent_dir), (
        f"login child must run on a per-agent home, got {lane.env.get('CLAUDE_CONFIG_DIR')!r}")
    names = {p.name for p in home.iterdir()}
    assert ".credentials.json" in names, f"credential not brought in: {sorted(names)}"
    allowed = {".credentials.json", "settings.json"}  # settings.json is the server's guard file
    extra = sorted(names - allowed)
    assert not extra, f"login home holds more than the credential: {extra}"
    guard = (home / "settings.json").read_text(encoding="utf-8")
    assert "owner" not in guard, "the owner's settings must not be copied"
    cred = home / ".credentials.json"
    assert cred.read_text(encoding="utf-8") == (lane.owner / ".claude/.credentials.json").read_text(
        encoding="utf-8")


def test_h3_login_lanes_bring_in_no_owner_content_beyond_credentials(
    tmp_path: Path, monkeypatch,
) -> None:
    """H3: codex, claude-login and dsh-login homes hold no copy of owner settings, memory, sessions or storages."""
    forbidden = {"CLAUDE.md", "MEMORY.md", "SKILL.md", "snap.sh", "gate", "old.json", "s.json",
                 "t.json", "d.log", "AGENTS.md", ".env", ".zshrc"}
    problems: list[str] = []
    for lane_id in ("claude-login", "codex", "dsh-login"):
        sub = tmp_path / lane_id
        sub.mkdir()
        lane = capture_lane(sub, monkeypatch, lane_id)
        home = lane.child_home
        if home is None or not _under(home, lane.agent_dir):
            problems.append(f"{lane_id}: home {home} is not a per-agent home")
            continue
        for path in home.rglob("*"):
            if path.name in forbidden:
                problems.append(f"{lane_id}: owner content {path.relative_to(home)}")
            if (path.is_symlink() and _under(os.path.realpath(path), lane.owner)
                    and path.name not in (".credentials.json", ".credentials.yaml", "auth.json")):
                problems.append(f"{lane_id}: link to owner file {path.name}")
    assert not problems, problems


def test_h4_owner_home_exceptions_is_a_dict_of_path_reason_lists() -> None:
    """H4: sandbox.OWNER_HOME_EXCEPTIONS exists as {driver: [(path, reason)]} with nonempty text."""
    table = getattr(sandbox, "OWNER_HOME_EXCEPTIONS", None)
    assert isinstance(table, dict), "sandbox.OWNER_HOME_EXCEPTIONS must be a dict"
    for driver, entries in table.items():
        assert isinstance(driver, str) and isinstance(entries, list), (driver, entries)
        for entry in entries:
            assert isinstance(entry, tuple) and len(entry) == 2, (driver, entry)
            path, reason = entry
            assert isinstance(path, str) and path, (driver, entry)
            assert isinstance(reason, str) and reason.strip(), (driver, entry)


def test_h4_every_owner_home_write_root_is_a_listed_exception(
    tmp_path: Path, monkeypatch,
) -> None:
    """H4: any write root under the owner home in any profile appears in OWNER_HOME_EXCEPTIONS for its driver."""
    table = getattr(sandbox, "OWNER_HOME_EXCEPTIONS", None)
    assert isinstance(table, dict), "sandbox.OWNER_HOME_EXCEPTIONS must be a dict"
    problems: list[str] = []
    for lane_id in LANE_IDS:
        sub = tmp_path / lane_id
        sub.mkdir()
        lane = capture_lane(sub, monkeypatch, lane_id)
        driver = lane_id.split("-")[0]
        listed = [p for p, _ in table.get(driver, [])]
        for root in write_allow_roots(lane.profile):
            if _under(root, lane.owner) and not any(_under(root, p) for p in listed):
                problems.append(f"{lane_id}: {root} is writable in the owner home but not listed")
    assert not problems, problems


def test_h5_autoload_paths_and_guard_files_are_write_denied(
    tmp_path: Path, monkeypatch,
) -> None:
    """H5: profiles deny the owner's CLI homes, <ws>/.codex and each driver's guard-config files in its own home."""
    problems: list[str] = []
    for lane_id in LANE_IDS:
        sub = tmp_path / lane_id
        sub.mkdir()
        lane = capture_lane(sub, monkeypatch, lane_id)
        profile, ad, owner = lane.profile, lane.agent_dir, lane.owner
        guard = {
            "claude-api": [ad / "claude-home/settings.json"],
            "claude-login": [ad / "claude-home/settings.json"],
            "codex": [ad / "codex-home/config.toml", ad / "codex-home/hooks.json"],
            "omp": [ad / "omp-agent/config.yml", ad / "omp-agent/models.yml"],
            "dsh-api": [ad / "dsh-home/dsh-guard/cordis.patch.yml"],
            "dsh-login": [ad / "dsh-home/dsh-guard/cordis.patch.yml"],
        }[lane_id]
        reach = [owner / ".claude/settings.json", owner / ".claude/CLAUDE.md",
                 owner / ".claude/projects/p/memory/MEMORY.md", owner / ".claude.json",
                 owner / ".codex/config.toml", owner / ".codex/AGENTS.md",
                 owner / ".codex/skills/s/SKILL.md", owner / ".omp/agent/config.yml",
                 owner / ".dsh/.env", owner / ".dsh/profiles/p/cordis.yml",
                 lane.ws / ".codex/config.toml", lane.ws / ".codex/hooks.json",
                 lane.ws / ".codex/new-file.toml"]
        for path in [*guard, *reach]:
            if sbpl_allows(profile, "file-write*", path):
                problems.append(f"{lane_id}: {path} is writable")
    assert not problems, problems


def test_h5_claude_login_child_reads_the_guard_hook_from_its_own_home(
    tmp_path: Path, monkeypatch,
) -> None:
    """H5: the claude login child's CLAUDE_CONFIG_DIR holds the guard settings.json, write-denied."""
    lane = capture_lane(tmp_path, monkeypatch, "claude-login")
    home = lane.child_home
    assert home is not None, "login child env has no CLAUDE_CONFIG_DIR, so the hook is read from the owner's home"
    settings_file = home / "settings.json"
    assert _under(home, lane.agent_dir) and settings_file.is_file()
    assert not sbpl_allows(lane.profile, "file-write*", settings_file)
    assert not sbpl_allows(lane.profile, "file-write*", lane.owner / ".claude" / "settings.json")
