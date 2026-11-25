"""Locked acceptance tests for DSH review round 4 (offline, synthetic data)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from subagent.guard import classify as guard_classify
from subagent.guard.classify import (
    ALLOW,
    DENY,
    classify,
)
from subagent.guard.supervisor import Supervisor
from subagent.providers.base import Session
from subagent.runs import Registry
from subagent.verify import run_verification

from .signed_supervisor import sign_request, verify
from .test_dsh_review3 import (
    _driver,
    _dsh_cfg,
    _dsh_settings,
    _make_owner_home,
    _sbpl_allows_write,
    _workspace,
)


@pytest.fixture(autouse=True)
def _isolate_home_and_provider_protection(tmp_path: Path, monkeypatch):
    """Never read ~/.dsh or leak global provider protection across tests."""
    synthetic_home = tmp_path / "isolated-home"
    synthetic_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(synthetic_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)
    monkeypatch.setattr(guard_classify, "_provider_homes", ())
    monkeypatch.setattr(guard_classify, "_provider_secret_files", ())


def _dsh_provider_table(*, login: bool = False) -> dict[str, Any]:
    if login:
        return {
            "deepseek": {
                "driver": "dsh",
                "auth": "login",
                "vendor": "deepseek",
                "binary": "/fake/bin/dsh",
                "max_agents": 1,
                "experimental": True,
            }
        }
    return {
        "deepseek": {
            "driver": "dsh",
            "vendor": "deepseek",
            "binary": "/fake/bin/dsh",
            "api_key_env": "DEEPSEEK_API_KEY",
            "max_agents": 4,
            "experimental": True,
        }
    }


def _unescape_sbpl(raw: str) -> str:
    return (
        raw.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _sbpl_rules(profile: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Parse `(allow|deny) <operation> (<kind> "<path>")...` rules from an SBPL profile."""
    rule_re = re.compile(r"^\((allow|deny)\s+(\S+)\s+(.+)\)$")
    filter_re = re.compile(r'\((subpath|literal)\s+"((?:[^"\\]|\\.)*)"\)')
    parsed: list[tuple[str, str, list[tuple[str, str]]]] = []
    for raw_line in profile.splitlines():
        match = rule_re.match(raw_line.strip())
        if not match:
            continue
        decision, operation, body = match.group(1), match.group(2), match.group(3)
        filters = [(kind, _unescape_sbpl(raw_path)) for kind, raw_path in filter_re.findall(body)]
        if filters:
            parsed.append((decision, operation, filters))
    return parsed


def _sbpl_matches(kind: str, rule_path: str, target: Path) -> bool:
    candidates = {
        str(target).rstrip("/") or "/",
        os.path.abspath(target).rstrip("/") or "/",
        str(target.resolve()).rstrip("/") or "/",
    }
    normalized_rule = rule_path.rstrip("/") or "/"
    for cand in candidates:
        if kind == "literal" and cand == normalized_rule:
            return True
        if kind == "subpath" and (
            normalized_rule == "/" or cand == normalized_rule or cand.startswith(normalized_rule + "/")
        ):
            return True
    return False


def _sbpl_allows_read_op(profile: str, operation: str, target: Path) -> bool:
    """Evaluate `file-read*`, `file-read-data`, or `file-read-metadata` in SBPL order."""
    covered_by: dict[str, set[str]] = {
        "file-read*": {"file-read*"},
        "file-read-data": {"file-read*", "file-read-data"},
        "file-read-metadata": {"file-read*", "file-read-metadata"},
    }
    matching_ops = covered_by[operation]
    allowed = "(allow default)" in profile
    for decision, rule_op, filters in _sbpl_rules(profile):
        if rule_op not in matching_ops:
            continue
        for kind, rule_path in filters:
            if _sbpl_matches(kind, rule_path, target):
                allowed = decision == "allow"
    return allowed


class _MemoryStream:
    """Minimal async stream for exercising `Supervisor._handle` without a socket."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._chunks = [(json.dumps(payload) + "\n").encode("utf-8"), b""]
        self.sent = bytearray()

    async def __aenter__(self) -> _MemoryStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def receive(self, _max_bytes: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def reply(self) -> dict[str, Any]:
        return json.loads(self.sent.decode("utf-8"))


def _send_guard_request(supervisor: Supervisor, payload: dict[str, Any]) -> dict[str, Any]:
    stream = _MemoryStream(payload)
    asyncio.run(supervisor._handle(stream))
    return stream.reply()


# --- 1. Profile root config is reset by the parent before every launch -------


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_r4_1_parent_resets_headless_cordis_yml_before_every_launch(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """Requirement 1: before every launch the parent resets `profiles/headless/cordis.yml`
    to its known empty-entries content so stale worker writes cannot survive across runs."""
    monkeypatch.setattr(sys, "platform", "darwin")
    if login:
        owner_home, _, _, _ = _make_owner_home(tmp_path)
        monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r4-1")

    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(login=login, deny_read=(str(private),))

    p1 = driver.spawn(cfg, settings, "r4-1-reset", "task 1", ws, Session(provider=cfg.name), None)
    dsh_home = Path(p1.env["DSH_HOME"]).resolve()
    cordis_yml = dsh_home / "profiles" / "headless" / "cordis.yml"
    assert cordis_yml.is_file(), f"expected {cordis_yml} to exist after first spawn()"
    initial_content = cordis_yml.read_text(encoding="utf-8")

    stale_content = (
        "entries:\n"
        "  - id: worker-injected-plugin\n"
        "    disabled: false\n"
        "# arbitrary-stale-previous-run-text\n"
    )
    cordis_yml.write_text(stale_content, encoding="utf-8")

    # DSH itself has not started when spawn() returns; the reset must happen in the parent.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected Popen during spawn()")),
    )
    p2 = driver.spawn(cfg, settings, "r4-1-reset", "task 2", ws, Session(provider=cfg.name), None)
    assert Path(p2.env["DSH_HOME"]).resolve() == dsh_home

    after_content = cordis_yml.read_text(encoding="utf-8")
    assert after_content != stale_content, (
        "profiles/headless/cordis.yml still holds previous run's arbitrary text before DSH starts"
    )
    assert "worker-injected-plugin" not in after_content
    assert "arbitrary-stale-previous-run-text" not in after_content
    assert after_content == initial_content


# --- 2. Only one writable file under profiles/ --------------------------------


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_r4_2_outer_profile_allows_writes_only_to_cordis_yml_under_profiles(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """Requirement 2: the only write allowance under `<dsh_home>/profiles/` is the exact
    `profiles/headless/cordis.yml` path (no subpath or sibling/other file)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    if login:
        owner_home, _, _, _ = _make_owner_home(tmp_path)
        monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r4-2")

    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(login=login, deny_read=(str(private),))
    session = Session(provider=cfg.name)

    argv = driver.argv(cfg, settings, "r4-2-profiles", "task", ws, session, "deepseek-flash")
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]

    dsh_home = Path(driver.env(settings, "r4-2-profiles", cfg, session)["DSH_HOME"]).resolve()
    profiles_dir = (dsh_home / "profiles").resolve()
    cordis_yml = (profiles_dir / "headless" / "cordis.yml").resolve()

    assert _sbpl_allows_write(profile, cordis_yml), (
        f"Seatbelt profile must allow exact literal write to {cordis_yml}"
    )

    denied_targets = (
        profiles_dir,
        profiles_dir / "headless",
        profiles_dir / "headless" / "package.json",
        profiles_dir / "headless" / "pnpm-workspace.yaml",
        profiles_dir / "headless" / "cordis.patch.yml",
        profiles_dir / "headless" / "other.yml",
        cordis_yml / "nested",
        profiles_dir / "other" / "cordis.yml",
    )
    for target in denied_targets:
        assert not _sbpl_allows_write(profile, target), (
            f"Seatbelt profile must deny writes to {target} under profiles/"
        )

    # Also verify no subpath or non-cordis.yml write-allow rule mentions profiles/.
    profiles_prefix = str(profiles_dir).rstrip("/")
    profile_write_allows: list[tuple[str, str]] = []
    for decision, operation, filters in _sbpl_rules(profile):
        if decision != "allow" or operation != "file-write*":
            continue
        for kind, raw_path in filters:
            norm = raw_path.rstrip("/")
            if norm == profiles_prefix or norm.startswith(profiles_prefix + "/"):
                profile_write_allows.append((kind, norm))

    assert profile_write_allows == [("literal", str(cordis_yml))], (
        f"unexpected write allowances under profiles/: {profile_write_allows!r}"
    )


# --- 3. Sign-in metadata rule -------------------------------------------------


def test_r4_3_sign_in_metadata_rule_on_owner_dsh_home(tmp_path: Path, monkeypatch) -> None:
    """Requirement 3: sign-in mode emits literal `file-read-metadata` on owner DSH home
    and no `file-read*` / `file-read-data` on it; api-key mode emits neither."""
    monkeypatch.setattr(sys, "platform", "darwin")
    owner_home, _, _, _ = _make_owner_home(tmp_path)
    owner_resolved = owner_home.resolve()
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r4-3")

    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)

    def _owner_home_allow_rules(profile: str) -> list[tuple[str, str, str]]:
        target = str(owner_resolved).rstrip("/")
        found: list[tuple[str, str, str]] = []
        for decision, operation, filters in _sbpl_rules(profile):
            if decision != "allow":
                continue
            for kind, raw_path in filters:
                if raw_path.rstrip("/") == target:
                    found.append((operation, kind, raw_path.rstrip("/")))
        return found

    # Sign-in (login) mode: literal file-read-metadata on owner_home, no file-read* / file-read-data.
    login_cfg = _dsh_cfg(login=True, deny_read=(str(private),))
    login_argv = driver.argv(
        login_cfg, settings, "r4-3-login", "task", ws, Session(provider=login_cfg.name), None
    )
    assert login_argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    login_profile = login_argv[2]

    login_owner_allows = _owner_home_allow_rules(login_profile)
    assert ("file-read-metadata", "literal", str(owner_resolved)) in login_owner_allows, (
        f"login profile missing literal file-read-metadata on owner home: {login_owner_allows!r}"
    )
    assert not any(
        op in {"file-read*", "file-read-data"} or kind == "subpath"
        for op, kind, _ in login_owner_allows
    ), f"login profile must not allow file-read* or file-read-data on owner home: {login_owner_allows!r}"

    assert _sbpl_allows_read_op(login_profile, "file-read-metadata", owner_resolved) is True
    assert _sbpl_allows_read_op(login_profile, "file-read-data", owner_resolved) is False
    assert _sbpl_allows_read_op(login_profile, "file-read*", owner_resolved) is False
    # Literal-only: children under owner_home do not inherit file-read-metadata.
    assert _sbpl_allows_read_op(login_profile, "file-read-metadata", owner_resolved / "extra.txt") is False
    assert _sbpl_allows_read_op(login_profile, "file-read-metadata", owner_resolved / "sessions") is False

    # API-key mode: neither file-read-metadata nor file-read* / file-read-data on owner_home.
    apikey_cfg = _dsh_cfg(login=False, deny_read=(str(private),))
    apikey_argv = driver.argv(
        apikey_cfg, settings, "r4-3-apikey", "task", ws, Session(provider=apikey_cfg.name), None
    )
    assert apikey_argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    apikey_profile = apikey_argv[2]

    assert _owner_home_allow_rules(apikey_profile) == []
    assert _sbpl_allows_read_op(apikey_profile, "file-read-metadata", owner_resolved) is False
    assert _sbpl_allows_read_op(apikey_profile, "file-read-data", owner_resolved) is False
    assert _sbpl_allows_read_op(apikey_profile, "file-read*", owner_resolved) is False


# --- 4. Guard context carries provider homes ----------------------------------


def test_r4_4_registry_agent_guard_context_carries_provider_homes_and_denies_reads(
    tmp_path: Path, monkeypatch
) -> None:
    """Requirement 4: an agent created by Registry carries `provider_homes` and `secret_files`
    in its guard context, and the classifier uses them to deny `Read` under a provider home."""
    fake_home = tmp_path / "home"
    _make_owner_home(fake_home, name=".dsh")
    custom_owner, _, _, _ = _make_owner_home(tmp_path, name="custom-owner")
    ws = _workspace(tmp_path)
    (ws / "README.md").write_text("# ws\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("DSH_OWNER_HOME", str(custom_owner))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r4-4")

    settings = _dsh_settings(
        tmp_path,
        providers=_dsh_provider_table(login=False),
        workspace=str(ws),
    )
    registry = Registry(settings, start_reaper=False)
    try:
        agent = registry.create_agent("r4-4-worker", ws, provider="deepseek")
        assert "provider_homes" in agent.guard_context
        assert ".dsh" in agent.guard_context["provider_homes"]
        assert "secret_files" in agent.guard_context
        assert str(custom_owner) in agent.guard_context["secret_files"]

        dsh_home_file = fake_home / ".dsh" / "sessions" / "s1.json"
        owner_file = custom_owner / "storages" / "x"

        # Process globals remain empty; denial comes strictly from agent.guard_context.
        assert classify("Read", {"file_path": str(dsh_home_file)}, ws, context=agent.guard_context).action == DENY
        assert classify("Read", {"file_path": str(owner_file)}, ws, context=agent.guard_context).action == DENY
        assert classify("Read", {"file_path": str(ws / "README.md")}, ws, context=agent.guard_context).action == ALLOW

        supervisor = Supervisor(settings, registry=registry)
        req_home = sign_request(
            agent.approval_key,
            {
                "agent_id": agent.agent_id,
                "tool_name": "Read",
                "tool_input": {"file_path": str(dsh_home_file)},
                "workspace": str(ws),
            },
        )
        reply_home = _send_guard_request(supervisor, req_home)
        assert reply_home["action"] == DENY
        assert reply_home.get("nonce") == req_home["nonce"]
        assert verify(agent.approval_key, reply_home)
        assert "invalid approval request" not in reply_home["reason"]

        req_owner = sign_request(
            agent.approval_key,
            {
                "agent_id": agent.agent_id,
                "tool_name": "Read",
                "tool_input": {"file_path": str(owner_file)},
                "workspace": str(ws),
            },
        )
        reply_owner = _send_guard_request(supervisor, req_owner)
        assert reply_owner["action"] == DENY
        assert reply_owner.get("nonce") == req_owner["nonce"]
        assert verify(agent.approval_key, reply_owner)
        assert "invalid approval request" not in reply_owner["reason"]
    finally:
        registry.shutdown()


# --- 5. Verification keeps provider-home protection ---------------------------


def test_r4_5_verification_denies_reading_provider_home_with_no_signin_run_active(
    tmp_path: Path, monkeypatch
) -> None:
    """Requirement 5: verification classification denies reading a file under a provider home
    even when no sign-in run is active."""
    fake_home = tmp_path / "home"
    _, _, home_storage, _ = _make_owner_home(fake_home, name=".dsh")
    home_storage.write_text("SYNTHETIC-DSH-HOME-CANARY\n", encoding="utf-8")

    custom_owner, _, custom_storage, _ = _make_owner_home(tmp_path, name="custom-owner")
    custom_storage.write_text("SYNTHETIC-OWNER-HOME-CANARY\n", encoding="utf-8")

    ws = _workspace(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("DSH_OWNER_HOME", str(custom_owner))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r4-5")

    settings = _dsh_settings(
        tmp_path,
        providers=_dsh_provider_table(login=False),
        workspace=str(ws),
    )

    for cmd, canary in (
        (f"cat {home_storage}", "SYNTHETIC-DSH-HOME-CANARY"),
        ("cat ~/.dsh/storages/x", "SYNTHETIC-DSH-HOME-CANARY"),
        (f"cat {custom_storage}", "SYNTHETIC-OWNER-HOME-CANARY"),
    ):
        result = run_verification(cmd, ws, settings)
        assert result.passed is False, f"verification should block {cmd!r}, got {result!r}"
        assert result.exit_code is None, f"blocked verification must not execute {cmd!r}"
        assert "blocked before execution" in result.reason
        assert "refused:" in result.reason, (
            f"expected policy DENY ('refused:') for {cmd!r}, got: {result.reason!r}"
        )
        assert canary not in result.output

    assert run_verification("true", ws, settings).passed is True


# --- 6. Missing or unknown agent fails closed ---------------------------------


def test_r4_6_guard_request_missing_or_unknown_agent_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """Requirement 6: a guard request with an empty/missing agent_id or an agent_id no longer
    in the registry must fail closed (DENY) rather than falling back to an empty context
    that allows reading a provider home."""
    fake_home = tmp_path / "home"
    _, _, home_storage, _ = _make_owner_home(fake_home, name=".dsh")
    custom_owner, cred_file, _, _ = _make_owner_home(tmp_path, name="custom-owner")
    ws = _workspace(tmp_path)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("DSH_OWNER_HOME", str(custom_owner))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r4-6")

    settings = _dsh_settings(
        tmp_path,
        providers=_dsh_provider_table(login=False),
        workspace=str(ws),
    )
    registry = Registry(settings, start_reaper=False)
    try:
        evicted = registry.create_agent("r4-6-evicted", ws, provider="deepseek")
        evicted_id = evicted.agent_id
        evicted.close("finished")
        registry._evict_closed()
        assert registry.find_agent(evicted_id) is None

        supervisor = Supervisor(settings, registry=registry)
        for bad_agent_id in ("", None, evicted_id, "unknown-agent-id"):
            for target in (home_storage, cred_file):
                payload: dict[str, Any] = {
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(target)},
                    "workspace": str(ws),
                }
                if bad_agent_id is not None:
                    payload["agent_id"] = bad_agent_id
                reply = _send_guard_request(supervisor, payload)
                assert reply["action"] == DENY, (
                    f"expected DENY for agent_id={bad_agent_id!r} reading {target}, got {reply!r}"
                )
    finally:
        registry.shutdown()
