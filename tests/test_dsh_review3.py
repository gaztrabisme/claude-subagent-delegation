"""Regression tests for DSH review round 3 & decision S31 (offline, synthetic data)."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from subagent.config import Settings
from subagent.guard import classify as guard_classify
from subagent.guard.classify import (
    ALLOW,
    DENY,
    classify,
    protect_provider_homes,
    provider_secret_files,
)
from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.providers.dsh import DshProvider, _bridge_bundle

from .conftest import TEST_CHILD_ENV_PASSTHROUGH, make_settings
from .test_dsh_guard_bridge import _find_node, _require_bridge_entry, _write_stub_hook

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_FIXTURES = Path(__file__).parent / "fixtures" / "dsh_review"

R3_ENV_PASSTHROUGH = (
    *TEST_CHILD_ENV_PASSTHROUGH,
    "FAKE_DSH_RECORD",
    "FAKE_DSH_FIXTURE",
    "FAKE_DSH_STDERR",
    "FAKE_DSH_EXIT",
    "FAKE_DSH_NO_READY",
    "FAKE_DSH_READY_LINE",
    "FAKE_DSH_PAUSE_SUBSTR",
    "FAKE_DSH_PAUSE_SECONDS",
    "FAKE_DSH_AFTER_PAUSE_MARKER",
    "FAKE_DSH_LATE_READY_LINE",
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


def _driver() -> DshProvider:
    return for_driver("dsh")


def _dsh_cfg(*, login: bool = False, **overrides: Any) -> ProviderConfig:
    values: dict[str, Any] = {
        "name": "deepseek-login" if login else "deepseek",
        "driver": "dsh",
        "vendor": "deepseek",
        "binary": "/fake/bin/dsh",
        "api_key_envs": () if login else ("DEEPSEEK_API_KEY",),
        "max_agents": 1 if login else 4,
        "experimental": True,
        "extra": {"auth": "login"} if login else {},
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _dsh_settings(tmp_path: Path, providers: dict | None = None, **overrides: Any) -> Settings:
    overrides.setdefault("child_env_passthrough", list(R3_ENV_PASSTHROUGH))
    return make_settings(tmp_path, providers=providers, **overrides)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _make_owner_home(base: Path, *, name: str = "owner-dsh") -> tuple[Path, Path, Path, Path]:
    owner_home = base / name
    storages = owner_home / "storages"
    sessions = owner_home / "sessions"
    storages.mkdir(parents=True, exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    cred_file = owner_home / ".credentials.yaml"
    cred_file.write_text(
        "version: 1\nrecords:\n  deepseek-account-platform/default:\n"
        "    kind: grant\n    payload:\n      token: synthetic-r3-token\n",
        encoding="utf-8",
    )
    cred_file.chmod(0o600)
    storage_file = storages / "x"
    storage_file.write_text('{"synthetic": true}\n', encoding="utf-8")
    session_file = sessions / "s1.json"
    session_file.write_text('{"id": "s1"}\n', encoding="utf-8")
    (owner_home / ".env").write_text("DEEPSEEK_API_KEY=synthetic-owner-env\n", encoding="utf-8")
    (owner_home / "extra.txt").write_text("synthetic-extra\n", encoding="utf-8")
    return owner_home, cred_file, storage_file, session_file


def _sbpl_allows(profile: str, operation: str, target: Path) -> bool:
    """Evaluate macOS Seatbelt `(allow|deny) <operation> ...` rules in order."""
    target_strs = {
        str(target),
        os.path.abspath(target),
        str(target.resolve()),
    }
    allowed = "(allow default)" in profile
    rule_re = re.compile(rf"^\((allow|deny)\s+{re.escape(operation)}\s+(.+)\)$")
    filter_re = re.compile(r'\((subpath|literal)\s+"((?:[^"\\]|\\.)*)"\)')

    def _unescape(raw: str) -> str:
        return (
            raw.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )

    def _matches(kind: str, raw_path: str) -> bool:
        rule_path = _unescape(raw_path).rstrip("/") or "/"
        for candidate in target_strs:
            cand = candidate.rstrip("/") or "/"
            if kind == "literal" and cand == rule_path:
                return True
            if kind == "subpath" and (
                rule_path == "/" or cand == rule_path or cand.startswith(rule_path + "/")
            ):
                return True
        return False

    for raw_line in profile.splitlines():
        match = rule_re.match(raw_line.strip())
        if not match:
            continue
        decision, body = match.group(1), match.group(2)
        for kind, raw_path in filter_re.findall(body):
            if _matches(kind, raw_path):
                allowed = decision == "allow"
    return allowed


def _sbpl_allows_write(profile: str, target: Path) -> bool:
    return _sbpl_allows(profile, "file-write*", target)


def _sbpl_allows_read(profile: str, target: Path) -> bool:
    return _sbpl_allows(profile, "file-read*", target)


def _strip_sandbox_prefix(process) -> None:
    """Strip outer sandbox-exec wrapper before executing fake_dsh inside sandboxed test runners."""
    if process.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]:
        process.argv = process.argv[3:]


def _install_fake_dsh(tmp_path: Path) -> tuple[Path, Path]:
    """Install a configurable streaming fake `dsh` CLI for offline N2/N5 checks."""
    bindir = tmp_path / "fake-dsh-bin"
    bindir.mkdir(exist_ok=True)
    record = tmp_path / "fake-dsh-calls.jsonl"
    binary = bindir / "dsh"
    binary.write_text(
        f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path

stdin_text = sys.stdin.read()
rec = os.environ.get("FAKE_DSH_RECORD")
if rec:
    with open(rec, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "stdin": stdin_text,
            "env": dict(os.environ),
        }}) + "\\n")

ready_line = os.environ.get("FAKE_DSH_READY_LINE")
if ready_line is not None and ready_line != "":
    sys.stderr.write(ready_line if ready_line.endswith("\\n") else ready_line + "\\n")
    sys.stderr.flush()
elif os.environ.get("FAKE_DSH_NO_READY") != "1":
    sys.stderr.write("__SUBAGENT_DSH_GUARD_READY__\\n")
    sys.stderr.flush()

stderr_text = os.environ.get("FAKE_DSH_STDERR", "")
if stderr_text:
    sys.stderr.write(stderr_text if stderr_text.endswith("\\n") else stderr_text + "\\n")
    sys.stderr.flush()

fixture = os.environ.get("FAKE_DSH_FIXTURE", "")
pause_sub = os.environ.get("FAKE_DSH_PAUSE_SUBSTR", "")
pause_sec = float(os.environ.get("FAKE_DSH_PAUSE_SECONDS") or "0")
after_marker = os.environ.get("FAKE_DSH_AFTER_PAUSE_MARKER", "")
late_ready = os.environ.get("FAKE_DSH_LATE_READY_LINE", "")
paused = False

if fixture and Path(fixture).exists():
    for line in Path(fixture).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sys.stdout.write(line + "\\n")
        sys.stdout.flush()
        if not paused and pause_sub and pause_sub in line and pause_sec > 0:
            paused = True
            time.sleep(pause_sec)
            if late_ready:
                sys.stderr.write(late_ready if late_ready.endswith("\\n") else late_ready + "\\n")
                sys.stderr.flush()
            if after_marker:
                Path(after_marker).write_text("reached\\n", encoding="utf-8")

raise SystemExit(int(os.environ.get("FAKE_DSH_EXIT") or "0"))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, record


def _discover_nonce_env_key(
    driver: DshProvider,
    cfg: ProviderConfig,
    settings: Settings,
    ws: Path,
) -> tuple[str, str, str]:
    """Compare two spawn environments for the same agent_id to locate the per-run nonce var."""
    p1 = driver.spawn(cfg, settings, "nonce-probe", "task", ws, Session(provider=cfg.name), None)
    p2 = driver.spawn(cfg, settings, "nonce-probe", "task", ws, Session(provider=cfg.name), None)
    diff_keys = [
        k for k in sorted(set(p1.env) | set(p2.env)) if p1.env.get(k) != p2.env.get(k)
    ]
    assert diff_keys, (
        "DshProvider.spawn() did not inject a per-run random nonce env var into the child environment"
    )
    key = diff_keys[0]
    v1 = p1.env.get(key, "")
    v2 = p2.env.get(key, "")
    assert isinstance(v1, str) and len(v1) >= 16, f"expected random nonce token for {key!r}, got {v1!r}"
    assert isinstance(v2, str) and len(v2) >= 16 and v1 != v2
    return key, v1, v2


def _capture_bridge_stderr(ws: Path, env: dict[str, str]) -> str:
    """Run real `integrations/dsh-guard/index.mjs` `apply(ctx)` and return its stderr."""
    node_bin, node_extra_env = _find_node()
    entry = _require_bridge_entry()
    script = r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const ctx = {
  on() { return () => {}; },
  effect(fn) {
    for (const _ of fn() ?? []) {}
    return () => {};
  },
};
mod.apply(ctx);
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        cwd=str(ws),
        env={**os.environ, **node_extra_env, **env, "TEST_BRIDGE_ENTRY": str(entry)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stderr


# --- S31a: Driver refuses to spawn when outer sandbox-exec is inactive --------


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_r3_s31a_driver_refuses_spawn_without_outer_sandbox_in_both_auth_modes(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """S31a: dsh refuses to spawn (error names sandbox) when outer sandbox-exec is inactive."""
    if login:
        owner_home, _, _, _ = _make_owner_home(tmp_path)
        monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-s31a")

    # Keep session_root outside tempfile.gettempdir() so S31a is exercised directly.
    alt_temp = tmp_path / "sys-tmp"
    alt_temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(alt_temp))

    spawned: list[tuple] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    private = tmp_path / "private"
    private.mkdir()

    # 1. Non-macOS platform cannot apply outer sandbox-exec.
    monkeypatch.setattr(sys, "platform", "linux")
    cfg_no_deny = _dsh_cfg(login=login, deny_read=())
    with pytest.raises(Exception) as exc_info:
        driver.spawn(cfg_no_deny, settings, "s31a-linux", "task", ws, Session(provider=cfg_no_deny.name), None)
    assert "sandbox" in str(exc_info.value).lower(), (
        f"refusal error must name the sandbox, got: {exc_info.value!r}"
    )
    assert not spawned

    # 2. On macOS, if wrap_argv fails to wrap argv with sandbox-exec, spawn refuses.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("subagent.providers.dsh.wrap_argv", lambda argv, *a, **kw: list(argv))
    cfg_with_deny = _dsh_cfg(login=login, deny_read=(str(private),))
    with pytest.raises(Exception) as exc_info2:
        driver.spawn(cfg_with_deny, settings, "s31a-nowrap", "task", ws, Session(provider=cfg_with_deny.name), None)
    assert "sandbox" in str(exc_info2.value).lower(), (
        f"refusal error must name the sandbox, got: {exc_info2.value!r}"
    )
    assert not spawned


# --- S31b: Outer Seatbelt profile write & read denials ------------------------


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_r3_s31b_outer_seatbelt_profile_denies_config_writes_and_owner_home_reads(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """S31b: outer Seatbelt profile denies config/env/profile/bridge writes and owner-home reads
    (allowing only `.credentials.yaml` in login mode, denying all owner-home reads in API-key mode)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    owner_home, cred_file, storage_file, session_file = _make_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    if not login:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-s31b")

    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)
    (ws / ".env").write_text("DEEPSEEK_API_KEY=synthetic-ws-env\n", encoding="utf-8")

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(login=login, model="deepseek-flash", deny_read=(str(private),))
    session = Session(provider=cfg.name)

    argv = driver.argv(cfg, settings, "r3-s31b", "task", ws, session, "deepseek-flash")
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]

    dsh_home = Path(driver.env(settings, "r3-s31b", cfg, session)["DSH_HOME"]).resolve()
    agent_dir = (settings.session_root / "agents" / "r3-s31b").resolve()
    bridge_root, bridge_entry, bridge_patch = _bridge_bundle()

    write_denied_targets = (
        ws / ".env",
        dsh_home / ".env",
        dsh_home / "cordis.patch.yml",
        dsh_home / "dsh-guard" / "cordis.patch.yml",
        agent_dir / "dsh-login.patch.yml",
        agent_dir / "dsh-model.patch.yml",
        dsh_home / "profiles",
        dsh_home / "profiles" / "headless",
        dsh_home / "profiles" / "headless" / "cordis.patch.yml",
        dsh_home / "profiles" / "custom.yml",
        bridge_root,
        bridge_root / "new_hook.mjs",
        bridge_entry,
        bridge_patch,
    )
    for target in write_denied_targets:
        assert not _sbpl_allows_write(profile, target), (
            f"Seatbelt profile must deny writes to {target}"
        )

    # Even when cwd is the directory containing the bridge bundle, the bridge bundle stays write-denied.
    repo_argv = driver.argv(cfg, settings, "r3-s31b-repo", "task", bridge_root.parent, session, None)
    assert repo_argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    repo_profile = repo_argv[2]
    assert not _sbpl_allows_write(repo_profile, bridge_entry)
    assert not _sbpl_allows_write(repo_profile, bridge_root / "new_hook.mjs")

    # Runtime directories remain writable.
    assert _sbpl_allows_write(profile, dsh_home / "sessions" / "s1.json")
    assert _sbpl_allows_write(profile, dsh_home / "storages" / "state.json")

    # Owner DSH home read policy.
    if login:
        assert _sbpl_allows_read(profile, cred_file), (
            "login mode must still allow in-process read of owner .credentials.yaml"
        )
        for denied_read in (storage_file, session_file, owner_home / ".env", owner_home / "extra.txt"):
            assert not _sbpl_allows_read(profile, denied_read), (
                f"login mode Seatbelt must deny reads of {denied_read}"
            )
    else:
        for denied_read in (
            owner_home,
            cred_file,
            storage_file,
            session_file,
            owner_home / ".env",
            owner_home / "extra.txt",
        ):
            assert not _sbpl_allows_read(profile, denied_read), (
                f"API-key mode Seatbelt must deny all owner DSH home reads including {denied_read}"
            )


# --- N1: Fresh agent DSH home pre-creates profiles/headless before spawn ------


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_r3_n1_fresh_agent_dsh_home_precreates_profiles_headless_before_spawn(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """N1: preparing a fresh agent DSH home creates `profiles/headless` before child spawn."""
    monkeypatch.setattr(sys, "platform", "darwin")
    if login:
        owner_home, _, _, _ = _make_owner_home(tmp_path)
        monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-n1")

    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(login=login, deny_read=(str(private),))

    # Popen must not be needed; profiles/headless must exist right after spawn() prepares the home.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected Popen during prepare")),
    )
    process = driver.spawn(cfg, settings, "r3-n1-fresh", "task", ws, Session(provider=cfg.name), None)
    dsh_home = Path(process.env["DSH_HOME"]).resolve()
    headless_dir = dsh_home / "profiles" / "headless"
    assert headless_dir.is_dir(), f"expected {headless_dir} to be pre-created before spawn"


# --- N2: Per-run nonce readiness, multiline activation failure, no grace window ---


def test_r3_n2_bridge_uses_per_run_nonce_deleted_on_load_and_stripped_from_hooks(
    tmp_path: Path, monkeypatch
) -> None:
    """N2: driver injects a per-run random nonce env var; bridge deletes it from process.env
    on load, never passes it to hook children, and includes it in readiness stderr."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-n2")
    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(deny_read=(str(private),))
    nonce_key, nonce_1, _nonce_2 = _discover_nonce_env_key(driver, cfg, settings, ws)

    node_bin, node_extra_env = _find_node()
    entry = _require_bridge_entry()
    stub, _hook_record = _write_stub_hook(tmp_path)
    hook_env_dump = tmp_path / "hook-env-dump.json"

    hook_wrapper = tmp_path / "hook_env_probe.py"
    hook_wrapper.write_text(
        f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path

Path({str(hook_env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding="utf-8")
proc = subprocess.run(
    [{sys.executable!r}, {str(stub)!r}, "allow"],
    input=sys.stdin.read(),
    text=True,
    capture_output=True,
)
sys.stdout.write(proc.stdout)
sys.exit(proc.returncode)
""",
        encoding="utf-8",
    )
    hook_wrapper.chmod(0o755)

    script = r"""
import { pathToFileURL } from 'node:url';
const nonceKey = process.env.TEST_NONCE_KEY;
const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const hasNonceAfterImport = Object.hasOwn(process.env, nonceKey);
const listeners = [];
const ctx = {
  on(event, handler, options) { listeners.push({ event, handler, options }); return () => {}; },
  effect(fn) { for (const _ of fn() ?? []) {} return () => {}; },
};
mod.apply(ctx);
const hasNonceAfterApply = Object.hasOwn(process.env, nonceKey);
const preExec = listeners.find((l) => l.event === 'tools/pre-execute');
const decision = await preExec.handler(
  { name: 'bash', arguments: { command: 'echo ok' }, cwd: process.env.TEST_CWD },
  async () => ({ kind: 'allow' }),
);
process.stdout.write(JSON.stringify({ hasNonceAfterImport, hasNonceAfterApply, decision }) + '\n');
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        cwd=str(ws),
        env={
            **os.environ,
            **node_extra_env,
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_NONCE_KEY": nonce_key,
            "TEST_CWD": str(ws),
            nonce_key: nonce_1,
            "SUBAGENT_GUARD_HOOK": f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_wrapper))}",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["hasNonceAfterImport"] is False, (
        f"bridge did not delete {nonce_key!r} from process.env on module load"
    )
    assert out["hasNonceAfterApply"] is False
    assert out["decision"] == {"allow": True, "reason": "stub allowed"} or out["decision"] == {
        "kind": "allow"
    }

    hook_env = json.loads(hook_env_dump.read_text(encoding="utf-8"))
    assert nonce_key not in hook_env, f"bridge leaked {nonce_key!r} to hook child env"
    assert all(nonce_1 not in str(v) for v in hook_env.values()), (
        "bridge leaked nonce value into hook child env"
    )
    assert nonce_1 in proc.stderr, (
        f"bridge readiness stderr did not carry the per-run nonce {nonce_1!r}: {proc.stderr!r}"
    )


def test_r3_n2_driver_rejects_static_or_wrong_nonce_and_accepts_run_nonce(
    tmp_path: Path, monkeypatch
) -> None:
    """N2: static readiness marker, diagnostic path containing marker, and wrong nonce are
    rejected; only the bridge readiness line for the current run's nonce is accepted."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-n2")
    binary, _record = _install_fake_dsh(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(binary=str(binary), deny_read=(str(private),))
    nonce_key, probe_nonce, _ = _discover_nonce_env_key(driver, cfg, settings, ws)
    sample_ready_line = _capture_bridge_stderr(ws, {nonce_key: probe_nonce}).strip()
    assert probe_nonce in sample_ready_line

    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "session_turn_final.jsonl"))
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    # 1. Static marker only -> must fail closed.
    monkeypatch.setenv("FAKE_DSH_READY_LINE", "__SUBAGENT_DSH_GUARD_READY__")
    p_static = driver.spawn(cfg, settings, "n2-static", "task", ws, Session(provider="deepseek"), None)
    _strip_sandbox_prefix(p_static)
    res_static = [e for e in p_static.events() if e.get("type") == "result"][-1]
    assert res_static["is_error"] is True, f"static READY_MARKER was accepted: {res_static}"

    # 2. Diagnostic line containing static marker -> must fail closed.
    monkeypatch.setenv(
        "FAKE_DSH_READY_LINE",
        "dsh: warning: /tmp/__SUBAGENT_DSH_GUARD_READY__/bad/.env: EACCES",
    )
    p_forged = driver.spawn(cfg, settings, "n2-forged", "task", ws, Session(provider="deepseek"), None)
    _strip_sandbox_prefix(p_forged)
    res_forged = [e for e in p_forged.events() if e.get("type") == "result"][-1]
    assert res_forged["is_error"] is True, f"forged diagnostic marker was accepted: {res_forged}"

    # 3. Wrong nonce -> must fail closed.
    wrong_line = sample_ready_line.replace(probe_nonce, "wrong-nonce-00000000")
    monkeypatch.setenv("FAKE_DSH_READY_LINE", wrong_line)
    p_wrong = driver.spawn(cfg, settings, "n2-wrong", "task", ws, Session(provider="deepseek"), None)
    _strip_sandbox_prefix(p_wrong)
    res_wrong = [e for e in p_wrong.events() if e.get("type") == "result"][-1]
    assert res_wrong["is_error"] is True, f"wrong nonce was accepted: {res_wrong}"

    # 4. Exact bridge readiness line for this run's nonce -> accepted.
    import secrets

    expected_nonce = secrets.token_urlsafe(32)
    good_line = _capture_bridge_stderr(ws, {nonce_key: expected_nonce}).strip()
    monkeypatch.setenv("FAKE_DSH_READY_LINE", good_line)
    monkeypatch.setattr("subagent.providers.dsh.secrets.token_urlsafe", lambda _n=32: expected_nonce)
    p_good = driver.spawn(cfg, settings, "n2-good", "task", ws, Session(provider="deepseek"), None)
    assert p_good.env[nonce_key] == expected_nonce
    _strip_sandbox_prefix(p_good)
    res_good = [e for e in p_good.events() if e.get("type") == "result"][-1]
    assert res_good["is_error"] is False, f"valid run nonce was rejected: {res_good}"


def test_r3_n2_driver_fails_closed_on_multiline_guard_did_not_activate_report(
    tmp_path: Path, monkeypatch
) -> None:
    """N2: two-line DSH `did not activate` + `subagent-dsh-guard` stderr report must
    immediately kill the child and fail the run closed naming guard activation."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-n2")
    binary, _record = _install_fake_dsh(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)

    after_marker = tmp_path / "reached-after-multiline-error"
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "session_turn_final.jsonl"))
    monkeypatch.setenv("FAKE_DSH_PAUSE_SUBSTR", '"turn_start"')
    monkeypatch.setenv("FAKE_DSH_PAUSE_SECONDS", "0.25")
    monkeypatch.setenv("FAKE_DSH_AFTER_PAUSE_MARKER", str(after_marker))
    monkeypatch.setenv("FAKE_DSH_NO_READY", "1")
    monkeypatch.setenv(
        "FAKE_DSH_STDERR",
        "dsh: warning: 1 entry did not activate\n"
        "subagent-dsh-guard (file:///tmp/integrations/dsh-guard/index.mjs): Error: boom in apply",
    )
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(binary=str(binary), deny_read=(str(private),))
    process = driver.spawn(cfg, settings, "n2-multiline", "task", ws, Session(provider="deepseek"), None)
    _strip_sandbox_prefix(process)

    events = list(process.events())
    res = [e for e in events if e.get("type") == "result"][-1]
    assert res["is_error"] is True
    err = (res.get("error") or "").lower()
    assert "did not activate" in err and "subagent-dsh-guard" in err, (
        f"expected multi-line 'did not activate' guard error, got: {res.get('error')!r}"
    )
    assert "readiness proof" not in err, (
        f"multi-line activation warning was missed by stderr detector: {res.get('error')!r}"
    )
    assert not after_marker.exists(), (
        "child was not killed promptly on multi-line activation error (survived 0.25s)"
    )


def test_r3_n2_driver_kills_child_immediately_when_tool_event_precedes_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    """N2: no tool event is accepted before readiness; the driver kills the child with no
    500ms grace window if a tool event arrives before the readiness proof."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-n2")
    binary, _record = _install_fake_dsh(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(binary=str(binary), deny_read=(str(private),))

    # Case 1: Child emits session + tool_call, pauses 0.25s (inside old 500ms window), then
    # writes after_marker and completes. With no grace window, child must be killed before 0.25s.
    after_marker = tmp_path / "executed-within-500ms-window"
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "session_turn_final.jsonl"))
    monkeypatch.setenv("FAKE_DSH_NO_READY", "1")
    monkeypatch.setenv("FAKE_DSH_PAUSE_SUBSTR", '"tool_call"')
    monkeypatch.setenv("FAKE_DSH_PAUSE_SECONDS", "0.25")
    monkeypatch.setenv("FAKE_DSH_AFTER_PAUSE_MARKER", str(after_marker))
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    p1 = driver.spawn(cfg, settings, "n2-nograce", "task", ws, Session(provider="deepseek"), None)
    _strip_sandbox_prefix(p1)
    events1 = list(p1.events())
    res1 = [e for e in events1 if e.get("type") == "result"][-1]
    assert res1["is_error"] is True
    assert not after_marker.exists(), (
        "child survived into the old 500ms grace window after emitting tool_call without readiness"
    )
    assert not any(
        b.get("type") == "tool_use"
        for ev in events1
        if ev.get("type") == "assistant"
        for b in (ev.get("message") or {}).get("content", [])
        if isinstance(b, dict)
    ), "tool_call before readiness must never be translated/accepted"

    # Case 2: Readiness arrives 150ms AFTER tool_call; tool_call preceded proof so run must fail.
    nonce_key, _, _ = _discover_nonce_env_key(driver, cfg, settings, ws)
    p2 = driver.spawn(cfg, settings, "n2-late-ready", "task", ws, Session(provider="deepseek"), None)
    late_ready = _capture_bridge_stderr(ws, {nonce_key: p2.env[nonce_key]}).strip()
    monkeypatch.setenv("FAKE_DSH_PAUSE_SECONDS", "0.15")
    monkeypatch.delenv("FAKE_DSH_AFTER_PAUSE_MARKER", raising=False)
    monkeypatch.setenv("FAKE_DSH_LATE_READY_LINE", late_ready)

    _strip_sandbox_prefix(p2)
    events2 = list(p2.events())
    res2 = [e for e in events2 if e.get("type") == "result"][-1]
    assert res2["is_error"] is True, (
        f"tool_call preceding late readiness proof must be rejected, got: {res2}"
    )


# --- N3: Temp session_root refusal without outer sandbox; bridge bundle write denial ---


@pytest.mark.parametrize("login", [False, True], ids=["api-key", "login"])
def test_r3_n3_driver_refuses_temp_session_root_without_outer_sandbox(
    tmp_path: Path, monkeypatch, login: bool
) -> None:
    """N3: runs whose session_root is under the system temp dir are refused unless outer sandbox is active."""
    if login:
        owner_home, _, _, _ = _make_owner_home(tmp_path)
        monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-r3-n3")

    spawned: list[tuple] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    assert settings.session_root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    ws = _workspace(tmp_path)

    # Outer sandbox inactive via non-macOS platform and empty deny_read.
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = _dsh_cfg(login=login, deny_read=())
    with pytest.raises((RuntimeError, ValueError)) as exc_info:
        driver.spawn(cfg, settings, "n3-tmp-linux", "task", ws, Session(provider=cfg.name), None)
    msg = str(exc_info.value).lower()
    assert any(tok in msg for tok in ("sandbox", "temp", "tmp", "session")), (
        f"unexpected refusal message for temp session_root without outer sandbox: {exc_info.value!r}"
    )
    assert not spawned

    # Outer sandbox inactive via bypassed wrap_argv on macOS.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("subagent.providers.dsh.wrap_argv", lambda argv, *a, **kw: list(argv))
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    cfg_sb = _dsh_cfg(login=login, deny_read=(str(private),))
    with pytest.raises((RuntimeError, ValueError)) as exc_info2:
        driver.spawn(cfg_sb, settings, "n3-tmp-nowrap", "task", ws, Session(provider=cfg_sb.name), None)
    msg2 = str(exc_info2.value).lower()
    assert any(tok in msg2 for tok in ("sandbox", "temp", "tmp", "session")), (
        f"unexpected refusal message for temp session_root without outer sandbox: {exc_info2.value!r}"
    )
    assert not spawned


def test_r3_n3_classifier_denies_writes_under_bridge_bundle(tmp_path: Path) -> None:
    """N3: `classify()` must deny Write/Edit/Bash writes targeting `integrations/dsh-guard/`
    even when `cwd` is inside that tree."""
    bridge_root, bridge_entry, bridge_patch = _bridge_bundle()
    integrations_ws = bridge_root.parent

    checks: list[tuple[str, dict[str, Any]]] = [
        ("Write", {"file_path": str(bridge_entry), "content": "// tampered\n"}),
        ("Write", {"file_path": str(bridge_patch), "content": "[]\n"}),
        ("Write", {"file_path": str(bridge_root / "new_evil.mjs"), "content": "// new\n"}),
        ("Edit", {"file_path": str(bridge_entry), "old_string": "apply", "new_string": "noop"}),
        ("Bash", {"command": "echo hacked > dsh-guard/index.mjs"}),
        ("Bash", {"command": "cp evil.mjs dsh-guard/index.mjs"}),
    ]
    for tool_name, tool_input in checks:
        verdict = classify(tool_name, tool_input, integrations_ws)
        assert verdict.action == DENY, (
            f"expected DENY for {tool_name}({tool_input!r}) under bridge bundle, "
            f"got {verdict.action}: {verdict.reason}"
        )

    # From an unrelated workspace, writing to bridge_entry is still DENY (not ESCALATE).
    other_ws = _workspace(tmp_path)
    ext_verdict = classify("Write", {"file_path": str(bridge_entry), "content": "x"}, other_ws)
    assert ext_verdict.action == DENY, (
        f"expected DENY from outside workspace for bridge write, got {ext_verdict.action}: {ext_verdict.reason}"
    )


# --- N4: Multi-root find, ls -R/--recursive, ~/.d*/sessions/*, custom DSH_OWNER_HOME ---


def _assert_n4_cases_denied(
    *,
    ws: Path,
    custom_owner: Path,
    context: dict[str, Any] | None,
) -> None:
    checks: list[tuple[str, dict[str, Any]]] = [
        ("Bash", {"command": "find /tmp ~ -name '*'"}),
        ("Bash", {"command": "ls -R ~"}),
        ("Bash", {"command": "ls --recursive ~"}),
        ("Bash", {"command": "cat ~/.d*/sessions/*"}),
        ("Bash", {"command": "cat ~/.dsh/storages/x"}),
        ("Read", {"file_path": str(custom_owner / ".credentials.yaml")}),
        ("Read", {"file_path": str(custom_owner / "storages" / "x")}),
        ("Read", {"file_path": str(custom_owner / "sessions" / "s1.json")}),
        ("Bash", {"command": f"cat {custom_owner}/storages/x"}),
        ("Bash", {"command": f"cat {custom_owner}/sessions/*"}),
        ("Bash", {"command": f"find /tmp {custom_owner} -name '*'"}),
        ("Bash", {"command": f"ls -R {custom_owner}"}),
        ("Bash", {"command": f"ls --recursive {custom_owner}"}),
    ]
    for tool_name, tool_input in checks:
        verdict = classify(tool_name, tool_input, ws, context=context)
        assert verdict.action == DENY, (
            f"expected DENY for {tool_name}({tool_input!r}), got {verdict.action}: {verdict.reason}"
        )
    assert classify("Read", {"file_path": str(ws / "README.md")}, ws, context=context).action == ALLOW
    assert classify("Bash", {"command": "ls -R ."}, ws, context=context).action == ALLOW
    assert classify("Bash", {"command": "find . -name '*'"}, ws, context=context).action == ALLOW


def test_r3_n4_classifier_denies_multi_root_find_recursive_ls_globs_and_custom_owner_home(
    tmp_path: Path, monkeypatch
) -> None:
    """N4: `classify()` denies multi-root find, recursive ls, `~/.d*/sessions/*`, `~/.dsh/storages/x`,
    and reads under a custom `DSH_OWNER_HOME` both during a sign-in run and with no run active."""
    fake_home = tmp_path / "home"
    _make_owner_home(fake_home, name=".dsh")
    custom_owner, _, _, _ = _make_owner_home(tmp_path, name="custom-dsh")

    ws = _workspace(tmp_path)
    (ws / "README.md").write_text("# ws\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("DSH_OWNER_HOME", str(custom_owner))

    # 1. With no run active (DSH provider configured with custom DSH_OWNER_HOME).
    settings = _dsh_settings(
        tmp_path,
        providers={
            "deepseek-login": {
                "driver": "dsh",
                "auth": "login",
                "vendor": "deepseek",
                "binary": "/fake/bin/dsh",
                "max_agents": 1,
                "experimental": True,
            }
        },
    )
    protect_provider_homes(settings)
    context = {"secret_files": [str(p) for p in provider_secret_files(settings)]}
    _assert_n4_cases_denied(ws=ws, custom_owner=custom_owner, context=context)

    # 2. During an active sign-in run (even if DSH_OWNER_HOME changed after startup).
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)
    protect_provider_homes(settings)
    startup_context = {"secret_files": [str(p) for p in provider_secret_files(settings)]}

    monkeypatch.setenv("DSH_OWNER_HOME", str(custom_owner))
    private = tmp_path / "private"
    private.mkdir()
    driver = _driver()
    cfg = _dsh_cfg(login=True, deny_read=(str(private),))
    process = driver.spawn(cfg, settings, "r3-n4-login", "task", ws, Session(provider=cfg.name), None)

    checked = False

    def inspect_during_run(*_args, **_kwargs):
        nonlocal checked
        _assert_n4_cases_denied(ws=ws, custom_owner=custom_owner, context=startup_context)
        checked = True
        raise RuntimeError("stop after N4 classify check")

    monkeypatch.setattr(process, "_start", inspect_during_run)
    with pytest.raises(RuntimeError, match="stop after N4 classify check"):
        next(process.events())
    assert checked


# --- N5: Login mode exports empty DEEPSEEK_API_KEY in child env --------------


def test_r3_n5_login_mode_sets_empty_deepseek_api_key_in_child_env(
    tmp_path: Path, monkeypatch
) -> None:
    """N5: in login mode, child env has `DEEPSEEK_API_KEY == ''` (to shadow `<cwd>/.env`),
    both when parent `DEEPSEEK_API_KEY` is set and when it is unset."""
    monkeypatch.setattr(sys, "platform", "darwin")
    owner_home, _, _, _ = _make_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    binary, record = _install_fake_dsh(tmp_path)
    cfg = _dsh_cfg(login=True, binary=str(binary), deny_read=(str(private),))
    ws = _workspace(tmp_path)

    # Case 1: parent has a real/ambient key; child env must set DEEPSEEK_API_KEY="".
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-parent-ambient-n5")
    env_with_parent = driver.env(settings, "n5-parent-set", cfg, Session(provider=cfg.name))
    assert "DEEPSEEK_API_KEY" in env_with_parent
    assert env_with_parent["DEEPSEEK_API_KEY"] == ""

    # Case 2: parent has DEEPSEEK_API_KEY unset; child env must STILL set DEEPSEEK_API_KEY="".
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    env_without_parent = driver.env(settings, "n5-parent-unset", cfg, Session(provider=cfg.name))
    assert "DEEPSEEK_API_KEY" in env_without_parent
    assert env_without_parent["DEEPSEEK_API_KEY"] == ""

    # Case 3: end-to-end spawn delivers DEEPSEEK_API_KEY="" to the child process.
    monkeypatch.setenv("FAKE_DSH_RECORD", str(record))
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "session_turn_final.jsonl"))
    process = driver.spawn(cfg, settings, "n5-spawn", "task", ws, Session(provider=cfg.name), None)
    assert process.env.get("DEEPSEEK_API_KEY") == ""
    _strip_sandbox_prefix(process)
    list(process.events())
    calls = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(calls) == 1
    assert calls[0]["env"].get("DEEPSEEK_API_KEY") == ""
