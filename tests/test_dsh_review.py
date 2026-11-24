"""Regression tests for DSH review findings 1-9 (offline, synthetic data)."""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from subagent import config, router
from subagent.config import Settings
from subagent.guard import classify as guard_classify
from subagent.guard.classify import (
    ALLOW,
    DENY,
    _redact_private_paths,
    classify,
    protect,
    protect_provider_homes,
    provider_secret_files,
    release,
)
from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.providers.dsh import DshProvider, _bridge_bundle
from subagent.runs import COMPLETED, FAILED, KILL_BUDGET, KILL_LOOP, Registry, Run
from subagent.telemetry.trace import Trace

from .conftest import TEST_CHILD_ENV_PASSTHROUGH, make_settings
from .test_dsh_guard_bridge import _find_node, _require_bridge_entry, _run_node, _write_stub_hook

REPO_ROOT = Path(__file__).resolve().parents[1]
DSH_FIXTURES = Path(__file__).parent / "fixtures" / "dsh"
REVIEW_FIXTURES = Path(__file__).parent / "fixtures" / "dsh_review"
FAKE_DSH = Path(__file__).parent / "fakes" / "fake_dsh.py"

DSH_REVIEW_ENV_PASSTHROUGH = (
    *TEST_CHILD_ENV_PASSTHROUGH,
    "FAKE_DSH_RECORD",
    "FAKE_DSH_FIXTURE",
    "FAKE_DSH_STDERR",
    "FAKE_DSH_EXIT",
    "FAKE_DSH_NO_READY",
    "FAKE_DSH_PAUSE_SUBSTR",
    "FAKE_DSH_PAUSE_SECONDS",
    "FAKE_DSH_AFTER_PAUSE_MARKER",
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
    overrides.setdefault("child_env_passthrough", list(DSH_REVIEW_ENV_PASSTHROUGH))
    return make_settings(tmp_path, providers=providers, **overrides)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _make_owner_home(base: Path, *, name: str = "owner-dsh") -> tuple[Path, Path, Path]:
    owner_home = base / name
    storages = owner_home / "storages"
    storages.mkdir(parents=True, exist_ok=True)
    cred_file = owner_home / ".credentials.yaml"
    cred_file.write_text(
        "version: 1\nrecords:\n  deepseek-account-platform/default:\n"
        "    kind: grant\n    payload:\n      token: synthetic-review-token\n",
        encoding="utf-8",
    )
    cred_file.chmod(0o600)
    storage_file = storages / "state.json"
    storage_file.write_text('{"synthetic": true}\n', encoding="utf-8")
    return owner_home, cred_file, storage_file


def _patch_rows(text: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        match = re.match(r"^\s*-\s+id:\s*['\"]?([^\s'\"]+)", line)
        if match:
            current = rows.setdefault(match.group(1), {})
            continue
        if current is None:
            continue
        disabled = re.match(r"^\s+disabled:\s*(true|false)\s*$", line, re.IGNORECASE)
        if disabled:
            current["disabled"] = disabled.group(1).lower()
    return rows


@pytest.fixture
def fake_dsh(tmp_path: Path, monkeypatch):
    """Install `tests/fakes/fake_dsh.py` as an executable `dsh` binary."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "dsh"
    binary.write_text(f"#!{sys.executable}\n{FAKE_DSH.read_text(encoding='utf-8')}", encoding="utf-8")
    binary.chmod(0o755)

    record = tmp_path / "dsh-calls.jsonl"
    monkeypatch.setenv("FAKE_DSH_RECORD", str(record))
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "session_turn_final.jsonl"))
    monkeypatch.delenv("FAKE_DSH_STDERR", raising=False)
    monkeypatch.delenv("FAKE_DSH_NO_READY", raising=False)
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    class FakeDsh:
        path = str(binary)

        def use(
            self,
            fixture: Path | str | None,
            *,
            exit_code: int = 0,
            stderr: str = "",
            guard_ready: bool = True,
        ) -> None:
            if not fixture:
                monkeypatch.setenv("FAKE_DSH_FIXTURE", "")
            else:
                path = Path(fixture)
                if not path.is_absolute():
                    path = REVIEW_FIXTURES / path
                monkeypatch.setenv("FAKE_DSH_FIXTURE", str(path))
            monkeypatch.setenv("FAKE_DSH_EXIT", str(exit_code))
            if stderr:
                monkeypatch.setenv("FAKE_DSH_STDERR", stderr)
            else:
                monkeypatch.delenv("FAKE_DSH_STDERR", raising=False)
            if guard_ready:
                monkeypatch.delenv("FAKE_DSH_NO_READY", raising=False)
            else:
                monkeypatch.setenv("FAKE_DSH_NO_READY", "1")

        def calls(self) -> list[dict[str, Any]]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]

    return FakeDsh()


def _install_streaming_fake_dsh(tmp_path: Path) -> Path:
    """Install a streaming fake `dsh` binary that can pause mid-stream before EOF."""
    bindir = tmp_path / "streaming-bin"
    bindir.mkdir(exist_ok=True)
    binary = bindir / "dsh"
    binary.write_text(
        f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path

stdin_text = sys.stdin.read()
rec = os.environ.get("FAKE_DSH_RECORD")
if rec:
    with open(rec, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin_text}}) + "\\n")

fixture = os.environ.get("FAKE_DSH_FIXTURE", "")
pause_sub = os.environ.get("FAKE_DSH_PAUSE_SUBSTR", "")
pause_sec = float(os.environ.get("FAKE_DSH_PAUSE_SECONDS") or "0")
after_marker = os.environ.get("FAKE_DSH_AFTER_PAUSE_MARKER", "")
paused = False

if os.environ.get("FAKE_DSH_NO_READY") != "1":
    sys.stderr.write("__SUBAGENT_DSH_GUARD_READY__\\n")
    sys.stderr.flush()

if fixture and Path(fixture).exists():
    for line in Path(fixture).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sys.stdout.write(line + "\\n")
        sys.stdout.flush()
        if not paused and pause_sub and pause_sub in line and pause_sec > 0:
            paused = True
            time.sleep(pause_sec)
            if after_marker:
                Path(after_marker).write_text("resumed\\n", encoding="utf-8")

stderr_text = os.environ.get("FAKE_DSH_STDERR", "")
if stderr_text:
    sys.stderr.write(stderr_text if stderr_text.endswith("\\n") else stderr_text + "\\n")
    sys.stderr.flush()
raise SystemExit(int(os.environ.get("FAKE_DSH_EXIT") or "0"))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _registry_with_dsh(
    tmp_path: Path,
    binary_path: str,
    *,
    login: bool = False,
    trace_path: Path | None = None,
    extra_providers: dict[str, dict[str, Any]] | None = None,
    **settings_overrides: Any,
) -> Registry:
    name = "deepseek-login" if login else "deepseek"
    provider: dict[str, Any] = {
        "driver": "dsh",
        "vendor": "deepseek",
        "binary": binary_path,
        "experimental": True,
    }
    if login:
        provider["auth"] = "login"
        provider["max_agents"] = 1
    else:
        provider["api_key_env"] = "DEEPSEEK_API_KEY"
    providers: dict[str, Any] = {name: provider}
    if extra_providers:
        providers.update(extra_providers)
    settings = _dsh_settings(tmp_path, providers=providers, run_timeout=30, **settings_overrides)
    trace = Trace(trace_path) if trace_path is not None else None
    return Registry(settings, start_reaper=False, trace=trace)


@contextmanager
def _recording_approval_socket(path: Path):
    """Accept any connection on the approval socket and record raw messages."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    listener.settimeout(0.1)
    messages: list[str] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(0.5)
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
                messages.append(raw)
                conn.sendall(b'{"action":"allow","reason":"ready","ok":true}\n')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield messages
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)


def _snapshot_files(root: Path) -> set[Path]:
    return {p.resolve() for p in root.rglob("*") if p.is_file()}


def _sbpl_allows_write(profile: str, target: Path) -> bool:
    """Evaluate macOS Seatbelt `(allow|deny) file-write*` rules in order (last match wins)."""
    target_strs = {str(target), str(target.resolve())}
    allowed = "(allow default)" in profile
    rule_re = re.compile(r'^\((allow|deny)\s+file-write\*\s+(.+)\)$')
    filter_re = re.compile(r'\((subpath|literal)\s+"((?:[^"\\]|\\.)*)"\)')

    def _unescape(raw: str) -> str:
        return raw.replace('\\"', '"').replace("\\\\", "\\")

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
        line = raw_line.strip()
        match = rule_re.match(line)
        if not match:
            continue
        decision, body = match.group(1), match.group(2)
        for kind, raw_path in filter_re.findall(body):
            if _matches(kind, raw_path):
                allowed = decision == "allow"
    return allowed


# --- Finding 1: Fail closed when subagent-dsh-guard fails to load/activate ---


def test_review_1_bridge_emits_readiness_proof_after_listener_registration(tmp_path: Path) -> None:
    """Finding 1: `apply(ctx)` must emit a readiness proof after `tools/pre-execute` registers,
    and must NOT emit it if listener registration throws."""
    node_bin, node_extra_env = _find_node()
    entry = _require_bridge_entry()
    ws = _workspace(tmp_path)
    stub, hook_record = _write_stub_hook(tmp_path)
    sock_path = Path(f"/tmp/dsh-ready-{uuid.uuid4().hex[:8]}.sock")

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg()
    env = driver.env(settings, "bridge-ready", cfg, Session(provider="deepseek"))
    dsh_home = Path(env["DSH_HOME"]).resolve()
    env["SUBAGENT_GUARD_HOOK"] = f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))} allow"
    env["SUBAGENT_APPROVAL_SOCKET"] = str(sock_path)
    env["STUB_HOOK_RECORD"] = str(hook_record)

    script = r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const shouldFail = process.env.TEST_FAIL_REGISTER === '1';
const ctx = {
  on(event, _handler, _options) {
    if (shouldFail) throw new Error('simulated registration failure');
    return () => {};
  },
  effect(fn) {
    const out = fn();
    if (out && typeof out[Symbol.iterator] === 'function') {
      for (const _item of out) {}
    }
    return () => {};
  },
};
mod.apply(ctx);
await new Promise((r) => setTimeout(r, 150));
"""

    before_files = _snapshot_files(tmp_path)
    with _recording_approval_socket(sock_path) as sock_msgs:
        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", script],
            cwd=str(ws),
            env={**os.environ, **node_extra_env, **env, "TEST_BRIDGE_ENTRY": str(entry)},
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert proc.returncode == 0, proc.stderr
    created_files = _snapshot_files(tmp_path) - before_files
    signals = {
        "files": sorted(str(p) for p in created_files if p.is_relative_to(dsh_home) or p.is_relative_to(tmp_path)),
        "socket_messages": sock_msgs,
        "hook_called": hook_record.exists(),
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }
    assert any(signals.values()), (
        "apply(ctx) emitted no readiness proof (no sentinel file, socket/hook handshake, or output)"
    )

    # Negative check: throwing inside ctx.on must not emit readiness proof.
    hook_record.unlink(missing_ok=True)
    fail_dsh_home = tmp_path / "fail-dsh-home"
    fail_dsh_home.mkdir()
    before_fail = _snapshot_files(tmp_path)
    with _recording_approval_socket(sock_path) as fail_sock_msgs:
        fail_proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", script],
            cwd=str(ws),
            env={
                **os.environ,
                **node_extra_env,
                **env,
                "DSH_HOME": str(fail_dsh_home),
                "TEST_BRIDGE_ENTRY": str(entry),
                "TEST_FAIL_REGISTER": "1",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert fail_proc.returncode != 0
    assert not (_snapshot_files(tmp_path) - before_fail)
    assert not fail_sock_msgs
    assert not hook_record.exists()


def test_review_1_driver_fails_closed_without_readiness_proof(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 1: when fake dsh emits session/turn events without bridge readiness proof,
    DshProcess must fail closed instead of succeeding."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-1")
    fake_dsh.use("session_turn_final.jsonl", exit_code=0, guard_ready=False)
    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(binary=fake_dsh.path)

    process = driver.spawn(cfg, settings, "r1-no-ready", "task", ws, Session(provider="deepseek"), None)
    events = list(process.events())
    results = [e for e in events if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0]["is_error"] is True, f"expected fail-closed error without readiness proof, got {results[0]}"


def test_review_1_driver_kills_child_when_turn_starts_without_readiness_proof(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 1: when fake dsh emits `session`/`turn_start` without readiness proof,
    the driver kills the child before subsequent tool calls/final complete."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-1")
    binary = _install_streaming_fake_dsh(tmp_path)
    after_marker = tmp_path / "child-reached-after-turn-start"
    monkeypatch.setenv("FAKE_DSH_RECORD", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "session_turn_final.jsonl"))
    monkeypatch.setenv("FAKE_DSH_PAUSE_SUBSTR", '"turn_start"')
    monkeypatch.setenv("FAKE_DSH_PAUSE_SECONDS", "2.5")
    monkeypatch.setenv("FAKE_DSH_AFTER_PAUSE_MARKER", str(after_marker))
    monkeypatch.setenv("FAKE_DSH_NO_READY", "1")
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(binary=str(binary))
    process = driver.spawn(cfg, settings, "r1-kill", "task", ws, Session(provider="deepseek"), None)

    started = time.monotonic()
    events = list(process.events())
    elapsed = time.monotonic() - started
    results = [e for e in events if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0]["is_error"] is True, f"expected fail-closed result, got {results[0]}"
    assert not after_marker.exists(), "driver did not kill DSH child when turn started without readiness proof"
    assert elapsed < 2.0, f"driver waited {elapsed:.2f}s instead of killing unguarded child promptly"


def test_review_1_driver_fails_closed_when_stderr_reports_guard_inactive(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 1: if stderr reports DSH failed to activate an entry (`subagent-dsh-guard`),
    the run must fail closed even if stdout contains a completed final event and exit is 0."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-1")
    fake_dsh.use(
        "session_turn_final.jsonl",
        exit_code=0,
        stderr="dsh: warning: 1 entry did not activate (subagent-dsh-guard)",
        guard_ready=False,
    )
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh.path)
    try:
        agent = reg.create_agent("d-inactive", ws, provider="deepseek", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED, f"expected FAILED when guard did not activate, got {run.state}"
        assert "activate" in (run.error or "").lower() or "guard" in (run.error or "").lower()
    finally:
        reg.shutdown()


def test_review_1_trace_dsh_run_with_steps_and_no_verdicts_is_hook_silent(tmp_path: Path) -> None:
    """Finding 1: `Trace.run()` must flag `hook_silent` for `driver == 'dsh'` just like `'codex'`."""
    trace_path = tmp_path / "trace.jsonl"
    trace = Trace(trace_path)
    run = Run(
        run_id="r-dsh-1",
        agent_id="a-dsh-silent",
        prompt="p",
        lane="deepseek",
        driver="dsh",
        guard="hook",
    )
    run.usage.steps = 2
    trace.run(run, str(tmp_path), "deepseek-flash")
    record = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record.get("hook_silent") is True, f"expected hook_silent=True for silent DSH run: {record}"
    assert "hook silent" in str(record.get("guard") or "")

    trace.verdict(
        agent_id="a-dsh-guarded",
        tool="Bash",
        action="allow",
        tier="policy",
        reason="ok",
        facts={},
        latency_ms=1.0,
    )
    guarded = Run(
        run_id="r-dsh-2",
        agent_id="a-dsh-guarded",
        prompt="p",
        lane="deepseek",
        driver="dsh",
        guard="hook",
    )
    guarded.usage.steps = 1
    trace.run(guarded, str(tmp_path), "deepseek-flash")
    guarded_rec = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])
    assert "hook_silent" not in guarded_rec


# --- Finding 2: Outer Seatbelt allows writes to <dsh_home>/cordis.patch.yml, .env, profiles, bridge ---


def test_review_2_outer_seatbelt_denies_dsh_home_patch_dotenv_profiles_and_bridge_writes(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 2: under `deny_read`, Seatbelt must deny writes to `<dsh_home>/cordis.patch.yml`,
    `<dsh_home>/.env`, `<dsh_home>/profiles`, and the packaged bridge entry."""
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(deny_read=(str(private),))

    argv = driver.argv(cfg, settings, "a-sb-1", "task", ws, Session(provider="deepseek"), None)
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]
    dsh_home = Path(driver.env(settings, "a-sb-1", cfg, Session(provider="deepseek"))["DSH_HOME"]).resolve()

    assert not _sbpl_allows_write(profile, dsh_home / "cordis.patch.yml"), (
        "Seatbelt must deny writes to <dsh_home>/cordis.patch.yml"
    )
    assert not _sbpl_allows_write(profile, dsh_home / ".env"), (
        "Seatbelt must deny writes to <dsh_home>/.env"
    )
    assert not _sbpl_allows_write(profile, dsh_home / "profiles" / "custom.yml"), (
        "Seatbelt must deny writes to <dsh_home>/profiles"
    )
    assert _sbpl_allows_write(profile, dsh_home / "sessions" / "s1.json"), (
        "Seatbelt should still allow DSH runtime session writes"
    )

    # Even when workspace is the subagent repo itself, the bridge entry must be write-denied.
    _root, bridge_entry, _patch = _bridge_bundle()
    repo_argv = driver.argv(cfg, settings, "a-sb-2", "task", REPO_ROOT, Session(provider="deepseek"), None)
    repo_profile = repo_argv[2]
    assert not _sbpl_allows_write(repo_profile, bridge_entry), (
        f"Seatbelt must deny writes to bridge entry {bridge_entry}"
    )


# --- Finding 3: Owner DSH home secret_files(), PROVIDER_HOMES, Glob/Grep/Bash scans ---


def _assert_dsh_home_scans_denied(
    *,
    fake_home: Path,
    cred_file: Path,
    storage_file: Path,
    readme: Path,
    ws: Path,
    context: dict[str, Any] | None,
) -> None:
    checks: list[tuple[str, dict[str, Any]]] = [
        ("Read", {"file_path": str(cred_file)}),
        ("Read", {"file_path": str(storage_file)}),
        ("Glob", {"path": str(fake_home), "pattern": ".dsh/**"}),
        ("Grep", {"path": str(fake_home), "pattern": ".", "include": ".dsh/**"}),
        ("Bash", {"command": "cat ~/.d*/.cred*"}),
        ("Bash", {"command": "cat ~/.d*/storages/*"}),
        ("Bash", {"command": "grep -r . ~"}),
        ("Bash", {"command": "rg . ~"}),
        ("Bash", {"command": "find ~ -name '*'"}),
    ]
    for tool_name, tool_input in checks:
        verdict = classify(tool_name, tool_input, ws, context=context)
        assert verdict.action == DENY, (
            f"expected DENY for {tool_name}({tool_input!r}), got {verdict.action}: {verdict.reason}"
        )
    assert classify("Read", {"file_path": str(readme)}, ws, context=context).action == ALLOW


def test_review_3_dsh_provider_declares_secret_files(tmp_path: Path, monkeypatch) -> None:
    """Finding 3: `DshProvider.secret_files(cfg)` must declare `.credentials.yaml` and `storages`."""
    driver = _driver()
    assert hasattr(driver, "secret_files"), "DshProvider must implement secret_files(cfg)"

    fake_home = tmp_path / "home"
    default_owner = (fake_home / ".dsh").resolve()
    default_owner.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)

    declared_default = {Path(p).resolve() for p in driver.secret_files(_dsh_cfg(login=True))}
    assert (default_owner / ".credentials.yaml").resolve() in declared_default
    assert (default_owner / "storages").resolve() in declared_default

    custom_owner = (tmp_path / "custom-dsh").resolve()
    custom_owner.mkdir(parents=True)
    monkeypatch.setenv("DSH_OWNER_HOME", str(custom_owner))
    declared_custom = {Path(p).resolve() for p in driver.secret_files(_dsh_cfg(login=True))}
    assert (custom_owner / ".credentials.yaml").resolve() in declared_custom
    assert (custom_owner / "storages").resolve() in declared_custom


def test_review_3_classify_denies_dsh_home_scans_during_login_run(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 3: during an active login run, `classify()` must deny Glob/Grep/Bash globs
    and recursive readers targeting owner DSH home (`.credentials.yaml` and `storages/`)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)
    owner_home, cred_file, storage_file = _make_owner_home(fake_home, name=".dsh")

    ws = _workspace(tmp_path)
    readme = ws / "README.md"
    readme.write_text("# ws\n", encoding="utf-8")

    cfg = _dsh_cfg(login=True)
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

    driver = _driver()
    process = driver.spawn(cfg, settings, "r3-login", "task", ws, Session(provider="deepseek-login"), None)

    checked = False

    def inspect_during_run(*_args, **_kwargs):
        nonlocal checked
        _assert_dsh_home_scans_denied(
            fake_home=fake_home,
            cred_file=cred_file,
            storage_file=storage_file,
            readme=readme,
            ws=ws,
            context=context,
        )
        checked = True
        raise RuntimeError("stop after classify check")

    monkeypatch.setattr(process, "_start", inspect_during_run)
    with pytest.raises(RuntimeError, match="stop after classify check"):
        next(process.events())
    assert checked


def test_review_3_classify_denies_dsh_home_when_no_login_run_active(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 3: even with no login run active, configuring a DSH/deepseek provider must
    protect `~/.dsh/` (`storages/` and `.credentials.yaml`) against Read/Glob/Grep/Bash scans."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)
    _owner_home, cred_file, storage_file = _make_owner_home(fake_home, name=".dsh")

    ws = _workspace(tmp_path)
    readme = ws / "README.md"
    readme.write_text("# ws\n", encoding="utf-8")

    settings = _dsh_settings(
        tmp_path,
        providers={
            "deepseek": {
                "driver": "dsh",
                "vendor": "deepseek",
                "binary": "/fake/bin/dsh",
                "api_key_env": "DEEPSEEK_API_KEY",
                "experimental": True,
            }
        },
    )
    protect_provider_homes(settings)
    context = {"secret_files": [str(p) for p in provider_secret_files(settings)]}

    _assert_dsh_home_scans_denied(
        fake_home=fake_home,
        cred_file=cred_file,
        storage_file=storage_file,
        readme=readme,
        ws=ws,
        context=context,
    )


def test_review_3_login_outer_seatbelt_denies_owner_storages_reads(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 3: in login mode under outer Seatbelt, `<owner_home>/storages` must be read-denied
    (while `.credentials.yaml` stays readable in-process by DSH)."""
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")
    owner_home, _cred_file, _storage_file = _make_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    other = tmp_path / "other-secret"
    other.mkdir()

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(login=True, deny_read=(str(other),))

    argv = driver.argv(cfg, settings, "r3-sb", "task", ws, Session(provider="deepseek-login"), None)
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]
    storages_resolved = (owner_home / "storages").resolve()
    assert f'(deny file-read* (subpath "{storages_resolved}"))' in profile, (
        f"expected owner storages read denial in Seatbelt profile:\n{profile}"
    )


# --- Finding 4: Credential path persistence and redaction ---


def test_review_4_login_patch_removed_after_process_events_finish(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 4: `dsh-login.patch.yml` holding `<owner_home>/.credentials.yaml` must be cleaned up
    when `DshProcess.events()` exits."""
    owner_home, _cred_file, _ = _make_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(login=True, binary=fake_dsh.path)

    process = driver.spawn(cfg, settings, "r4-cleanup", "task", ws, Session(provider="deepseek-login"), None)
    patch_file = (settings.session_root / "agents" / "r4-cleanup" / "dsh-login.patch.yml").resolve()
    assert patch_file.is_file()

    list(process.events())
    assert not patch_file.exists(), f"login patch {patch_file} still exists after process finished"


def test_review_4_login_stderr_owner_path_redacted_from_result_and_trace(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 4: owner credential path in DSH stderr must be redacted before reaching `result['error']`,
    `run.error`, or `trace.jsonl` (including symlinked owner-home spellings)."""
    real_owner, _cred, _ = _make_owner_home(tmp_path, name="real-owner-dsh")
    link_owner = tmp_path / "symlink-owner-dsh"
    link_owner.symlink_to(real_owner)
    link_cred = link_owner / ".credentials.yaml"
    monkeypatch.setenv("DSH_OWNER_HOME", str(link_owner))

    fake_dsh.use("", exit_code=1, stderr=f"dsh: credentials parse error in {link_cred}")
    ws = _workspace(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    reg = _registry_with_dsh(tmp_path, fake_dsh.path, login=True, trace_path=trace_path)
    try:
        agent = reg.create_agent("d-leak", ws, provider="deepseek-login", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.submit("run", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert str(link_owner) not in (run.error or ""), f"symlink owner path leaked into run.error: {run.error}"
        assert str(real_owner) not in (run.error or ""), f"resolved owner path leaked into run.error: {run.error}"
    finally:
        reg.shutdown()

    trace_text = trace_path.read_text(encoding="utf-8")
    assert str(link_owner) not in trace_text, "symlink owner path leaked into trace.jsonl"
    assert str(real_owner) not in trace_text, "resolved owner path leaked into trace.jsonl"


def test_review_4_redact_private_paths_handles_symlink_spelling_and_non_claude_label(
    tmp_path: Path,
) -> None:
    """Finding 4: `_redact_private_paths` must redact unresolved symlink spellings embedded in strings
    and must not label DSH/generic owner roots as Claude config."""
    real_owner, _, _ = _make_owner_home(tmp_path, name="real-dsh")
    link_owner = tmp_path / "alias-dsh"
    link_owner.symlink_to(real_owner)
    protect(link_owner, temporary=True)
    try:
        embedded = _redact_private_paths(f"cat {link_owner / '.credentials.yaml'}")
        direct = _redact_private_paths(str(link_owner / ".credentials.yaml"))
        resolved_embedded = _redact_private_paths(f"cat {real_owner / '.credentials.yaml'}")
    finally:
        release(link_owner)

    for text in (embedded, direct, resolved_embedded):
        assert str(link_owner) not in text, f"unresolved symlink path leaked: {text!r}"
        assert str(real_owner) not in text, f"resolved path leaked: {text!r}"
        assert "Claude" not in text, f"placeholder should not hardcode 'Claude': {text!r}"


# --- Finding 5: Streaming step usage, live budget, no phantom turns, unique step IDs ---


def test_review_5_killed_run_preserves_completed_step_usage(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 5: killing a DSH run mid-stream (e.g. loop trip) must preserve completed step usage."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-5")
    fake_dsh.use("loop_after_step1_usage.jsonl")
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh.path, loop_strikes=3)
    try:
        agent = reg.create_agent("d-loop", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert run.finish_reason == KILL_LOOP
        assert run.usage.input == 120, f"lost completed step 1 input usage on killed run: {run.usage.as_dict()}"
        assert run.usage.output == 24
        assert run.usage.cache_read == 16
        assert run.usage.reasoning == 4
    finally:
        reg.shutdown()


def test_review_5_turn_token_budget_trips_mid_run(tmp_path: Path, monkeypatch) -> None:
    """Finding 5: `[core].turn_token_budget` must be enforced live as `step_end` arrives,
    killing the child before step 2 runs."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-5")
    binary = _install_streaming_fake_dsh(tmp_path)
    after_marker = tmp_path / "reached-step-2"
    monkeypatch.setenv("FAKE_DSH_RECORD", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(REVIEW_FIXTURES / "step1_high_usage_then_step2.jsonl"))
    monkeypatch.setenv("FAKE_DSH_PAUSE_SUBSTR", '"step_end"')
    monkeypatch.setenv("FAKE_DSH_PAUSE_SECONDS", "2.5")
    monkeypatch.setenv("FAKE_DSH_AFTER_PAUSE_MARKER", str(after_marker))
    monkeypatch.delenv("FAKE_DSH_NO_READY", raising=False)
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, str(binary), turn_token_budget=100)
    try:
        agent = reg.create_agent("d-budget", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        started = time.monotonic()
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        elapsed = time.monotonic() - started
        assert run.state == FAILED
        assert run.finish_reason == KILL_BUDGET
        assert not after_marker.exists(), "budget trip waited until exit instead of killing before step 2"
        assert elapsed < 2.0, f"budget trip took {elapsed:.2f}s instead of killing mid-run"
    finally:
        reg.shutdown()


def test_review_5_trace_turns_not_split_into_phantom_zero_token_turns(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 5: a 3-step DSH run must produce 3 `turn` trace records (not 4 with a 0-token phantom turn),
    with each step's `tool_calls` attached to its usage turn."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-5")
    fake_dsh.use(DSH_FIXTURES / "success_tools_usage.jsonl")
    ws = _workspace(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    reg = _registry_with_dsh(tmp_path, fake_dsh.path, trace_path=trace_path)
    try:
        agent = reg.create_agent("d-turns", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
    finally:
        reg.shutdown()

    turn_records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("kind") == "turn"
    ]
    assert len(turn_records) == 3, f"expected 3 turn records for 3-step run, got {len(turn_records)}: {turn_records}"
    assert [t["input"] for t in turn_records] == [120, 200, 80]
    assert "Bash" in turn_records[0]["tool_calls"]
    assert "Read" in turn_records[1]["tool_calls"]


def test_review_5_step_end_without_turn_or_step_numbers_do_not_collide(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 5: two `step_end` events omitting `turn`/`step` must use distinct message IDs in `_Meter`."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-5")
    fake_dsh.use("step_end_missing_ids.jsonl")
    ws = _workspace(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    reg = _registry_with_dsh(tmp_path, fake_dsh.path, trace_path=trace_path)
    try:
        agent = reg.create_agent("d-ids", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        assert run.usage.input == 300
        assert run.usage.output == 60
    finally:
        reg.shutdown()

    turn_records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("kind") == "turn"
    ]
    assert len(turn_records) == 2, f"expected 2 distinct step turns, got {turn_records}"
    assert [t["input"] for t in turn_records] == [100, 200], (
        f"step_end events collided on message_id in _Meter: {turn_records}"
    )
    assert [t["output"] for t in turn_records] == [20, 40]


# --- Finding 6: DeepSeek 402 Insufficient Balance and ACCOUNT_QUOTA_EXCEEDED refusals ---


def test_review_6_refusal_handles_deepseek_402_balance_and_closes_lane(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 6: `402 Insufficient Balance` before work must be classified as a DeepSeek balance
    refusal, close the lane, and allow fallback."""
    driver = _driver()
    cfg = _dsh_cfg()
    refusal = driver.refusal(
        cfg,
        [{"type": "result", "is_error": True, "subtype": "error", "error": "402 Insufficient Balance"}],
    )
    assert refusal is not None, "DshProvider.refusal() ignored 402 Insufficient Balance"
    assert refusal.code == router.DEEPSEEK_BALANCE
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is not None

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-6")
    fake_dsh.use("balance_402_error.jsonl", exit_code=1, stderr="dsh: 402 Insufficient Balance")
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh.path)
    try:
        agent = reg.create_agent("d-402", ws, provider="deepseek", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert run.finish_reason == "refused", f"expected refused, got {run.finish_reason}"
        assert reg.lane_state.closed("deepseek") is not None
    finally:
        reg.shutdown()


def test_review_6_refusal_handles_account_quota_exceeded_and_closes_lane(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 6: `ACCOUNT_QUOTA_EXCEEDED` in login mode must be a refusal that closes the lane."""
    owner_home, _, _ = _make_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    cfg = _dsh_cfg(login=True)
    refusal = driver.refusal(
        cfg,
        [{
            "type": "result",
            "is_error": True,
            "subtype": "error",
            "error": "ACCOUNT_QUOTA_EXCEEDED: DeepSeek account quota exceeded",
        }],
    )
    assert refusal is not None, "DshProvider.refusal() ignored ACCOUNT_QUOTA_EXCEEDED"
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is not None

    fake_dsh.use(
        "account_quota_exceeded_error.jsonl",
        exit_code=1,
        stderr="dsh: ACCOUNT_QUOTA_EXCEEDED: DeepSeek account quota exceeded",
    )
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh.path, login=True)
    try:
        agent = reg.create_agent("d-quota", ws, provider="deepseek-login", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert run.finish_reason == "refused", f"expected refused, got {run.finish_reason}"
        assert reg.lane_state.closed("deepseek-login") is not None
    finally:
        reg.shutdown()


# --- Finding 7: Login mode DEEPSEEK_API_KEY / <cwd>/.env fallback ---


def test_review_7_login_mode_blocks_workspace_dotenv_api_key_or_web_search(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 7: login mode must either shadow `<cwd>/.env` with `DEEPSEEK_API_KEY=''`
    or disable `web-search-deepseek` in the generated guard patch."""
    owner_home, _, _ = _make_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(login=True)
    session = Session(provider="deepseek-login")

    env = driver.env(settings, "r7-login", cfg, session)
    argv = driver.argv(cfg, settings, "r7-login", "task", ws, session, None)
    guard_patch = next(
        Path(argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "--patch" and "dsh-guard" in argv[i + 1]
    )
    patch_rows = _patch_rows(guard_patch.read_text(encoding="utf-8"))

    shadows_dotenv = env.get("DEEPSEEK_API_KEY") == ""
    disables_web_search = patch_rows.get("web-search-deepseek", {}).get("disabled") == "true"
    assert shadows_dotenv or disables_web_search, (
        "login mode leaves DEEPSEEK_API_KEY unset (allowing <cwd>/.env fallback) "
        "without disabling web-search-deepseek"
    )


# --- Finding 8: Bridge hardening (Object.prototype, exec.signal, pre-spawn JSON.stringify) ---


@pytest.mark.parametrize("proto_tool", ["constructor", "__proto__"])
def test_review_8_bridge_denies_object_prototype_keys_without_calling_hook(
    tmp_path: Path, proto_tool: str
) -> None:
    """Finding 8a: `Object.prototype` keys on `TOOL_NAMES` must be denied as unsupported without calling the hook."""
    _find_node()
    entry = _require_bridge_entry()
    stub, record_file = _write_stub_hook(tmp_path)
    ws = _workspace(tmp_path)

    out = _run_node(
        r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const res = await mod.decide(
  { name: process.env.TEST_PROTO_TOOL, arguments: {} },
  { SUBAGENT_GUARD_HOOK: process.env.SUBAGENT_GUARD_HOOK }
);
process.stdout.write(JSON.stringify(res) + '\n');
""",
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_PROTO_TOOL": proto_tool,
            "SUBAGENT_GUARD_HOOK": f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))} allow",
            "STUB_HOOK_RECORD": str(record_file),
        },
    )
    assert out["allow"] is False, f"prototype key {proto_tool!r} was allowed: {out}"
    assert "unsupported" in out.get("reason", "").lower()
    assert not record_file.exists(), f"hook should not be called for prototype key {proto_tool!r}"


def test_review_8_bridge_aborts_promptly_on_exec_signal(tmp_path: Path) -> None:
    """Finding 8b: `apply(ctx)` handler must observe `exec.signal` and settle promptly when aborted."""
    _find_node()
    entry = _require_bridge_entry()
    ws = _workspace(tmp_path)
    slow_hook = tmp_path / "slow_hook.py"
    slow_hook.write_text(
        f"""#!{sys.executable}
import json, sys, time
sys.stdin.read()
time.sleep(5.0)
print(json.dumps({{"hookSpecificOutput": {{"hookEventName": "PreToolUse", "permissionDecision": "allow"}}}}))
""",
        encoding="utf-8",
    )
    slow_hook.chmod(0o755)

    out = _run_node(
        r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const listeners = [];
const ctx = {
  on(event, handler, options) { listeners.push({ event, handler, options }); return () => {}; },
  effect(fn) { for (const _ of fn() ?? []) {} return () => {}; },
};
mod.apply(ctx);
const preExec = listeners.find((l) => l.event === 'tools/pre-execute');
const controller = new AbortController();
let nextCalled = false;
const t0 = Date.now();
setTimeout(() => controller.abort(), 100);
const decision = await preExec.handler(
  { name: 'bash', arguments: { command: 'echo hi' }, cwd: process.env.TEST_CWD, signal: controller.signal },
  async () => { nextCalled = true; return { kind: 'allow' }; }
);
process.stdout.write(JSON.stringify({ elapsedMs: Date.now() - t0, nextCalled, decision }) + '\n');
""",
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CWD": str(ws),
            "SUBAGENT_GUARD_HOOK": f"{shlex.quote(sys.executable)} {shlex.quote(str(slow_hook))}",
            "SUBAGENT_HOOK_TIMEOUT": "10",
        },
        timeout=15.0,
    )
    assert out["nextCalled"] is False
    assert out["decision"]["kind"] in ("cancel", "deny")
    assert out["elapsedMs"] < 2000, f"aborted handler took {out['elapsedMs']}ms instead of observing exec.signal"


def test_review_8_bridge_rejects_unserializable_payload_before_spawning_hook(tmp_path: Path) -> None:
    """Finding 8c: unserializable arguments (circular/BigInt) must fail before `spawn()` and `setTimeout()`
    so neither a child process nor a timer leaks."""
    node_bin, node_extra_env = _find_node()
    entry = _require_bridge_entry()
    stub, record_file = _write_stub_hook(tmp_path)
    ws = _workspace(tmp_path)

    script = r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
const circular = {};
circular.self = circular;
const res = await mod.decide(
  { name: 'bash', arguments: circular },
  { SUBAGENT_GUARD_HOOK: process.env.SUBAGENT_GUARD_HOOK, SUBAGENT_HOOK_TIMEOUT: '3' }
);
process.stdout.write(JSON.stringify(res) + '\n');
// Intentionally do NOT call process.exit(): a leaked timer or child keeps Node alive for 3s.
"""
    t0 = time.monotonic()
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        cwd=str(ws),
        env={
            **os.environ,
            **node_extra_env,
            "TEST_BRIDGE_ENTRY": str(entry),
            "SUBAGENT_GUARD_HOOK": f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))} allow",
            "STUB_HOOK_RECORD": str(record_file),
        },
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert res["allow"] is False
    assert not record_file.exists(), "hook process was spawned before JSON.stringify(payload) succeeded"
    assert elapsed < 1.5, f"Node waited {elapsed:.2f}s due to leaked post-spawn timer/child"


# --- Finding 9: Run.summary()/detail() usage_unreported and config.py auth error ---


def test_review_9_run_summary_and_detail_include_usage_unreported_for_dsh(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """Finding 9: `Run.summary()` and `Run.detail()` must surface `usage_unreported` for DSH runs."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-review-9")
    fake_dsh.use(DSH_FIXTURES / "step_without_usage.jsonl")
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh.path)
    try:
        agent = reg.create_agent("d-unrep", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        summary = run.summary()
        detail = run.detail()
        assert "usage_unreported" in summary, f"Run.summary() omitted usage_unreported: {summary}"
        assert {"input", "output"} <= set(summary["usage_unreported"])
        assert "usage_unreported" in detail, f"Run.detail() omitted usage_unreported: {detail}"
        assert {"input", "output"} <= set(detail["usage_unreported"])
    finally:
        reg.shutdown()


def test_review_9_config_auth_login_error_mentions_dsh(tmp_path: Path) -> None:
    """Finding 9: configuring `auth = 'login'` on another driver must mention `'dsh'` alongside `'claude'`."""
    cfg_path = tmp_path / "bad-login.toml"
    cfg_path.write_text('[providers.test]\ndriver = "codex"\nauth = "login"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError) as exc_info:
        config.load(extra=cfg_path)
    msg = str(exc_info.value)
    assert "claude" in msg and "dsh" in msg, f"ConfigError message should mention both 'claude' and 'dsh': {msg!r}"
