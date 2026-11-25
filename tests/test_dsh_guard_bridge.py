"""Acceptance tests for the DSH guard bridge plugin bundle (W2b, section B).

Pins contract points 11-15 offline using Node (via ``SUBAGENT_TEST_NODE``,
DSH's bundled Node at ``/Applications/DeepSeek Harness.app/Contents/Resources/runtime/bin/node``,
or ``node`` on PATH) and a stub hook script plus an end-to-end check with the
real ``src/subagent/guard/approval_hook.py``.

Bridge Harness Interface Contract
---------------------------------
Derived directly from DSH's own ``@deepseek-ai/dsh-experimental-auto-review``
and ``@deepseek-ai/dsh-tools`` inside ``app.asar``:
  - ``dsh-experimental-auto-review/src/index.ts:151-153, 732-782``:
    Cordis plugin exports ``name``, ``inject``, and ``apply(ctx)``, registering
    a prepended ``tools/pre-execute`` waterfall listener via
    ``ctx.on('tools/pre-execute', async (exec, next) => ..., { prepend: true })``
    (directly or inside ``ctx.effect(...)``).
  - ``dsh-tools/src/events.ts:18`` and ``dsh-tools/src/contracts.ts:175-225, 375-379``:
    The listener receives ``(exec, next)`` where ``exec`` is a ``ToolExecution``
    (``{ callId, rootCallId, name, arguments, agent?: { id, session: { id, header?: { cwd } } }, signal }``)
    and returns a ``PreToolDecision``: delegates to ``await next()`` on allow,
    or returns ``{ kind: 'deny', reason: '<message>', info?: ... }`` without
    calling ``next()`` on deny.
  - ``dsh-experimental-auto-review/package.json:11-18``:
    ESM package (``"type": "module"``, ``"main"`` entry point) paired with
    ``cordis.patch.yml``.

Explicit exports expected from ``integrations/dsh-guard/`` (ESM entry resolved
via ``package.json`` ``main``, else ``index.mjs``, else ``index.js``):
  1. ``export function apply(ctx)`` — wires the ``tools/pre-execute`` waterfall
     listener (matching ``dsh-experimental-auto-review/src/index.ts:732``).
  2. ``export function decide(call, env)`` (sync or async) — evaluates a single
     call ``{ name, arguments, callId?, cwd?, sessionId?, agent? }`` against
     ``env`` (reading ``env.SUBAGENT_GUARD_HOOK`` and optional
     ``env.SUBAGENT_HOOK_TIMEOUT`` in seconds) and returns
     ``{ allow: boolean, reason: string }``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from subagent.config import APPROVAL_HOOK
from subagent.guard.classify import classify

from .signed_supervisor import private_socket_path, signed_supervisor_socket, write_key_file

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = REPO_ROOT / "integrations" / "dsh-guard"
BRIDGE_PATCH = BRIDGE_DIR / "cordis.patch.yml"

DSH_BUNDLED_NODE = Path(
    "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/bin/node"
)
DSH_ELECTRON_BINARY = Path(
    "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness"
)


def _find_node() -> tuple[str, dict[str, str]]:
    """Locate a working Node executable and any wrapper env it requires."""
    extra_env: dict[str, str] = {}
    override = os.environ.get("SUBAGENT_TEST_NODE")
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), extra_env
        pytest.skip(f"SUBAGENT_TEST_NODE is not an executable file: {override}")

    if (
        DSH_BUNDLED_NODE.is_file()
        and os.access(DSH_BUNDLED_NODE, os.X_OK)
        and DSH_ELECTRON_BINARY.is_file()
        and os.access(DSH_ELECTRON_BINARY, os.X_OK)
    ):
        # DSH's bundled runtime/bin/node wrapper execs $DSH_DESKTOP_NODE_EXECUTABLE.
        extra_env["DSH_DESKTOP_NODE_EXECUTABLE"] = str(DSH_ELECTRON_BINARY)
        return str(DSH_BUNDLED_NODE), extra_env

    system_node = shutil.which("node")
    if system_node:
        return system_node, extra_env

    pytest.skip(
        "No Node runtime available (set SUBAGENT_TEST_NODE, install DeepSeek Harness, "
        "or put `node` on PATH)"
    )


def _bridge_entry() -> Path | None:
    """Resolve the ESM entry point of `integrations/dsh-guard/`."""
    pkg_json = BRIDGE_DIR / "package.json"
    if pkg_json.is_file():
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        main = data.get("main")
        if isinstance(main, str) and main.strip():
            target = (BRIDGE_DIR / main).resolve()
            if target.is_file():
                return target
    for name in ("index.mjs", "index.js", "src/index.mjs", "src/index.js", "lib/index.js"):
        candidate = BRIDGE_DIR / name
        if candidate.is_file():
            return candidate
    return None


def _require_bridge_entry() -> Path:
    entry = _bridge_entry()
    assert entry is not None, (
        f"DSH guard bridge entry point missing under {BRIDGE_DIR} "
        "(expected package.json `main` or index.mjs / index.js)"
    )
    return entry


def _run_node(
    script: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float = 15.0,
) -> dict:
    """Execute a small ESM driver snippet in Node and return its last stdout JSON object."""
    node_bin, node_extra_env = _find_node()
    full_env = {**os.environ, **node_extra_env, **env}
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, (
        f"Node harness exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"Node harness produced no JSON output; stderr:\n{proc.stderr}"
    return json.loads(lines[-1])


def _write_stub_hook(tmp_path: Path) -> tuple[Path, Path]:
    """Write a configurable Python stub mimicking `approval_hook.py`."""
    record_file = tmp_path / "stub-hook-payloads.jsonl"
    script = tmp_path / "stub_hook.py"
    script.write_text(
        """\
#!/usr/bin/env python3
import json, os, sys, time

raw = sys.stdin.read()
record = os.environ.get("STUB_HOOK_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"raw": raw, "parsed": json.loads(raw) if raw.strip() else None}) + "\\n")

mode = sys.argv[1] if len(sys.argv) > 1 else "allow"
if mode == "allow":
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "stub allowed",
        }
    }))
    sys.exit(0)
if mode == "deny":
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked by policy: protected file",
        }
    }))
    sys.exit(2)
if mode == "nonzero_with_allow":
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "should not trust nonzero exit",
        }
    }))
    sys.exit(1)
if mode == "timeout":
    time.sleep(5.0)
    sys.exit(0)
if mode == "garbage":
    print("not valid json {{{")
    sys.exit(0)
if mode == "empty":
    sys.exit(0)
if mode == "missing_decision":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse"}}))
    sys.exit(0)
sys.exit(3)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, record_file


def _stub_hook_cmd(script: Path, mode: str) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(mode)}"


APPLY_HARNESS_JS = r"""
import { pathToFileURL } from 'node:url';

const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
if (typeof mod.apply !== 'function') {
  throw new Error('bridge must export apply(ctx)');
}

const listeners = [];
const ctx = {
  on(event, handler, options) {
    listeners.push({ event, handler, options: options ?? null });
    return () => {};
  },
  effect(fn) {
    const out = fn();
    if (out && typeof out[Symbol.iterator] === 'function') {
      for (const _item of out) { /* collect disposers */ }
    }
    return () => {};
  },
  workingDirectory: {
    async ensure() {
      return process.env.TEST_CWD;
    },
  },
  get(name) {
    if (name === 'workingDirectory') return ctx.workingDirectory;
    return undefined;
  },
};

mod.apply(ctx);
const preExec = listeners.filter((l) => l.event === 'tools/pre-execute');
if (preExec.length === 0) {
  throw new Error('apply(ctx) did not register a tools/pre-execute listener');
}

const call = JSON.parse(process.env.TEST_CALL_JSON);
const controller = new AbortController();
const exec = {
  callId: call.callId ?? 'call-1',
  rootCallId: call.callId ?? 'call-1',
  name: call.name,
  arguments: call.arguments,
  cwd: process.env.TEST_CWD,
  sessionId: call.sessionId ?? 'session-dsh-bridge-1',
  agent: {
    id: call.sessionId ?? 'session-dsh-bridge-1',
    session: {
      id: call.sessionId ?? 'session-dsh-bridge-1',
      header: { cwd: process.env.TEST_CWD },
    },
  },
  signal: controller.signal,
};

let nextCalled = false;
const next = async () => {
  nextCalled = true;
  return { kind: 'allow' };
};

const decision = await preExec[0].handler(exec, next);
process.stdout.write(JSON.stringify({
  listenerCount: preExec.length,
  listenerOptions: preExec[0].options,
  nextCalled,
  decision,
}) + '\n');
"""

DECIDE_HARNESS_JS = r"""
import { pathToFileURL } from 'node:url';

const mod = await import(pathToFileURL(process.env.TEST_BRIDGE_ENTRY).href);
if (typeof mod.decide !== 'function') {
  throw new Error('bridge must export decide(call, env)');
}

const call = JSON.parse(process.env.TEST_CALL_JSON);
const env = JSON.parse(process.env.TEST_DECIDE_ENV_JSON);
const result = await mod.decide(call, env);
process.stdout.write(JSON.stringify(result) + '\n');
"""


# --- Bundle structure & PTC disabled (points 11, 14) -------------------------


def test_guard_bridge_bundle_and_patch_exist():
    assert BRIDGE_DIR.is_dir(), f"missing guard bridge directory {BRIDGE_DIR}"
    assert BRIDGE_PATCH.is_file(), f"missing guard bridge patch {BRIDGE_PATCH}"
    entry = _require_bridge_entry()
    assert entry.is_file()

    patch_text = BRIDGE_PATCH.read_text(encoding="utf-8")
    assert "insert:" in patch_text, "cordis.patch.yml must insert the guard bridge plugin"


def test_guard_bridge_patch_leaves_ptc_executor_disabled():
    assert BRIDGE_PATCH.is_file(), f"missing guard bridge patch {BRIDGE_PATCH}"
    patch_text = BRIDGE_PATCH.read_text(encoding="utf-8")

    # Parse simple YAML list entries for any reference to PTC / direct executor.
    # It must NEVER enable tool-executor-ptc (disabled: false), and if it
    # mentions tool-executor-ptc it must keep disabled: true.
    assert not re.search(
        r"tool-executor-ptc[\s\S]{0,200}?disabled\s*:\s*false\b",
        patch_text,
        re.IGNORECASE,
    ), f"bridge patch must not enable tool-executor-ptc:\n{patch_text}"

    # If the patch adds a row referencing dsh-tool-executor-ptc without disabling
    # it, fail closed.
    if "dsh-tool-executor-ptc" in patch_text or "tool-executor-ptc" in patch_text:
        assert re.search(
            r"tool-executor-ptc[\s\S]{0,200}?disabled\s*:\s*true\b",
            patch_text,
            re.IGNORECASE,
        ), f"tool-executor-ptc entry in bridge patch must set disabled: true:\n{patch_text}"


# --- Registration & tool name mapping (points 11, 12, 15) --------------------


def test_apply_registers_prepended_tools_pre_execute_listener(tmp_path: Path):
    _find_node()
    entry = _require_bridge_entry()
    stub, _ = _write_stub_hook(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    out = _run_node(
        APPLY_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CWD": str(ws),
            "TEST_CALL_JSON": json.dumps({"name": "read", "arguments": {"file_path": "a.py"}}),
            "SUBAGENT_GUARD_HOOK": _stub_hook_cmd(stub, "allow"),
        },
    )
    assert out["listenerCount"] == 1
    assert out["nextCalled"] is True
    assert out["decision"] == {"kind": "allow"}


@pytest.mark.parametrize(
    ("dsh_tool", "dsh_args", "expected_claude_tool", "expected_key", "expected_val"),
    [
        ("bash", {"command": "pytest -q", "description": "Run tests"}, "Bash", "command", "pytest -q"),
        ("read", {"file_path": "src/app.py"}, "Read", "file_path", "src/app.py"),
        ("write", {"file_path": "src/out.py", "content": "x = 1\n"}, "Write", "file_path", "src/out.py"),
        (
            "edit",
            {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
            "Edit",
            "file_path",
            "src/app.py",
        ),
        ("grep", {"pattern": "TODO", "path": "src"}, "Grep", "pattern", "TODO"),
        ("glob", {"pattern": "**/*.py", "path": "src"}, "Glob", "pattern", "**/*.py"),
    ],
    ids=["bash", "read", "write", "edit", "grep", "glob"],
)
def test_bridge_maps_all_dsh_tools_and_sends_pretooluse_payload(
    tmp_path: Path,
    dsh_tool: str,
    dsh_args: dict,
    expected_claude_tool: str,
    expected_key: str,
    expected_val: str,
):
    _find_node()
    entry = _require_bridge_entry()
    stub, record_file = _write_stub_hook(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    out = _run_node(
        APPLY_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CWD": str(ws),
            "TEST_CALL_JSON": json.dumps({
                "name": dsh_tool,
                "arguments": dsh_args,
                "sessionId": "session-dsh-map-1",
            }),
            "SUBAGENT_GUARD_HOOK": _stub_hook_cmd(stub, "allow"),
            "STUB_HOOK_RECORD": str(record_file),
        },
    )

    assert out["nextCalled"] is True
    assert out["decision"]["kind"] == "allow"

    recorded = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 1
    payload = recorded[0]["parsed"]
    assert payload["hook_event_name"] == "PreToolUse"
    assert payload["tool_name"] == expected_claude_tool
    assert payload["tool_input"][expected_key] == expected_val
    assert Path(payload["cwd"]).resolve() == ws.resolve()
    assert payload["session_id"] == "session-dsh-map-1"


# --- Allow / deny / fail-closed rules (points 13, 15) ------------------------


def test_bridge_denies_on_explicit_deny_without_running_tool_and_returns_reason(tmp_path: Path):
    _find_node()
    entry = _require_bridge_entry()
    stub, _ = _write_stub_hook(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    out = _run_node(
        APPLY_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CWD": str(ws),
            "TEST_CALL_JSON": json.dumps({
                "name": "write",
                "arguments": {"file_path": "tests/test_secret.py", "content": "bad"},
            }),
            "SUBAGENT_GUARD_HOOK": _stub_hook_cmd(stub, "deny"),
        },
    )

    assert out["nextCalled"] is False, "denied call must NOT execute tool body"
    assert out["decision"]["kind"] == "deny"
    assert "protected file" in (out["decision"].get("reason") or "")


@pytest.mark.parametrize(
    ("case", "hook_mode", "extra_env"),
    [
        ("nonzero_exit_even_with_allow_json", "nonzero_with_allow", {}),
        ("timeout", "timeout", {"SUBAGENT_HOOK_TIMEOUT": "0.3"}),
        ("unparseable_output", "garbage", {}),
        ("empty_output", "empty", {}),
        ("missing_permission_decision", "missing_decision", {}),
        ("missing_hook_env", None, {}),
        ("empty_hook_env", "", {}),
        ("nonexistent_hook_binary", "/nonexistent/subagent-guard-hook-binary", {}),
    ],
    ids=[
        "nonzero-exit",
        "timeout",
        "unparseable-output",
        "empty-output",
        "missing-decision",
        "missing-env",
        "empty-env",
        "nonexistent-binary",
    ],
)
def test_bridge_fails_closed_on_hook_errors(
    tmp_path: Path,
    case: str,
    hook_mode: str | None,
    extra_env: dict[str, str],
):
    _find_node()
    entry = _require_bridge_entry()
    stub, _ = _write_stub_hook(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    env: dict[str, str] = {
        "TEST_BRIDGE_ENTRY": str(entry),
        "TEST_CWD": str(ws),
        "TEST_CALL_JSON": json.dumps({"name": "bash", "arguments": {"command": "echo hi"}}),
        **extra_env,
    }
    if hook_mode is None:
        # Ensure SUBAGENT_GUARD_HOOK is absent in the Node process.
        env.pop("SUBAGENT_GUARD_HOOK", None)
        script = "delete process.env.SUBAGENT_GUARD_HOOK;\n" + APPLY_HARNESS_JS
    elif hook_mode == "" or hook_mode.startswith("/nonexistent/"):
        env["SUBAGENT_GUARD_HOOK"] = hook_mode
        script = APPLY_HARNESS_JS
    else:
        env["SUBAGENT_GUARD_HOOK"] = _stub_hook_cmd(stub, hook_mode)
        script = APPLY_HARNESS_JS

    out = _run_node(script, cwd=ws, env=env, timeout=10.0)

    assert out["nextCalled"] is False, f"{case}: tool must not run on fail-closed error"
    assert out["decision"]["kind"] == "deny", f"{case}: expected deny, got {out['decision']}"
    assert isinstance(out["decision"].get("reason"), str) and out["decision"]["reason"].strip(), (
        f"{case}: denial must carry a model-facing reason"
    )


@pytest.mark.parametrize(
    "unmapped_tool",
    ["unknown_custom_tool", "run_code", "web_fetch", "subagent"],
)
def test_bridge_denies_unmapped_tool_names_without_invoking_next(tmp_path: Path, unmapped_tool: str):
    _find_node()
    entry = _require_bridge_entry()
    stub, record_file = _write_stub_hook(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    out = _run_node(
        APPLY_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CWD": str(ws),
            "TEST_CALL_JSON": json.dumps({"name": unmapped_tool, "arguments": {"x": 1}}),
            "SUBAGENT_GUARD_HOOK": _stub_hook_cmd(stub, "allow"),
            "STUB_HOOK_RECORD": str(record_file),
        },
    )

    assert out["nextCalled"] is False, f"unmapped tool {unmapped_tool!r} must be denied"
    assert out["decision"]["kind"] == "deny"
    assert out["decision"].get("reason")
    assert not record_file.exists(), "unmapped tool should be denied without calling hook as allowed"


# --- Direct `decide(call, env)` harness contract (point 15) ------------------


def test_decide_returns_allow_and_deny_shape_directly(tmp_path: Path):
    _find_node()
    entry = _require_bridge_entry()
    stub, _ = _write_stub_hook(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    allow_res = _run_node(
        DECIDE_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CALL_JSON": json.dumps({
                "name": "bash",
                "arguments": {"command": "ls"},
                "cwd": str(ws),
                "sessionId": "session-decide-1",
            }),
            "TEST_DECIDE_ENV_JSON": json.dumps({
                "SUBAGENT_GUARD_HOOK": _stub_hook_cmd(stub, "allow"),
            }),
        },
    )
    assert allow_res["allow"] is True
    assert isinstance(allow_res.get("reason"), str)

    deny_res = _run_node(
        DECIDE_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CALL_JSON": json.dumps({
                "name": "bash",
                "arguments": {"command": "rm -rf /"},
                "cwd": str(ws),
                "sessionId": "session-decide-1",
            }),
            "TEST_DECIDE_ENV_JSON": json.dumps({
                "SUBAGENT_GUARD_HOOK": _stub_hook_cmd(stub, "deny"),
            }),
        },
    )
    assert deny_res["allow"] is False
    assert "protected file" in deny_res["reason"]

    missing_env_res = _run_node(
        DECIDE_HARNESS_JS,
        cwd=ws,
        env={
            "TEST_BRIDGE_ENTRY": str(entry),
            "TEST_CALL_JSON": json.dumps({
                "name": "read",
                "arguments": {"file_path": "README.md"},
                "cwd": str(ws),
            }),
            "TEST_DECIDE_ENV_JSON": json.dumps({}),
        },
    )
    assert missing_env_res["allow"] is False
    assert missing_env_res.get("reason")


# --- End-to-end bridge -> real approval_hook.py -> real classify() -----------


@contextmanager
def _classification_socket(path: Path, workspace: Path, context: dict, *, key: bytes):
    """Local signed Unix approval socket answering each request via the real classifier."""
    def responder(req: dict) -> dict:
        verdict = classify(
            req["tool_name"],
            req["tool_input"],
            workspace,
            cwd=req.get("cwd"),
            context=context,
        )
        return {"action": verdict.action, "reason": verdict.reason}

    with signed_supervisor_socket(path, key, responder, backlog=4) as received:
        yield received


def test_bridge_blocks_protected_test_write_and_allows_workspace_read_through_real_approval_hook(
    tmp_path: Path,
):
    _find_node()
    entry = _require_bridge_entry()
    ws = tmp_path / "ws"
    protected = ws / "tests" / "test_protected.py"
    protected.parent.mkdir(parents=True)
    protected.write_text("# protected\n", encoding="utf-8")
    readme = ws / "README.md"
    readme.write_text("# hello\n", encoding="utf-8")

    key, _key_path, key_args = write_key_file(tmp_path / "approval.key")
    hook_cmd = " ".join(
        shlex.quote(p)
        for p in (sys.executable, str(APPROVAL_HOOK), "--agent", "dsh-agent-1", *key_args)
    )
    context = {"protected": [str(protected)], "state_allow": []}

    with (
        private_socket_path("dsh-guard.sock") as sock_path,
        _classification_socket(sock_path, ws, context, key=key) as requests,
    ):
        denied = _run_node(
            APPLY_HARNESS_JS,
            cwd=ws,
            env={
                "TEST_BRIDGE_ENTRY": str(entry),
                "TEST_CWD": str(ws),
                "TEST_CALL_JSON": json.dumps({
                    "name": "write",
                    "arguments": {"file_path": "tests/test_protected.py", "content": "tampered"},
                    "sessionId": "session-dsh-real-hook",
                }),
                "SUBAGENT_GUARD_HOOK": hook_cmd,
                "SUBAGENT_APPROVAL_SOCKET": str(sock_path),
            },
        )
        allowed = _run_node(
            APPLY_HARNESS_JS,
            cwd=ws,
            env={
                "TEST_BRIDGE_ENTRY": str(entry),
                "TEST_CWD": str(ws),
                "TEST_CALL_JSON": json.dumps({
                    "name": "read",
                    "arguments": {"file_path": "README.md"},
                    "sessionId": "session-dsh-real-hook",
                }),
                "SUBAGENT_GUARD_HOOK": hook_cmd,
                "SUBAGENT_APPROVAL_SOCKET": str(sock_path),
            },
        )

    assert denied["nextCalled"] is False
    assert denied["decision"]["kind"] == "deny"
    assert "test" in denied["decision"]["reason"].lower() or "read-only" in denied["decision"]["reason"].lower()

    assert allowed["nextCalled"] is True
    assert allowed["decision"]["kind"] == "allow"

    assert len(requests) == 2
    assert requests[0]["agent_id"] == "dsh-agent-1"
    assert requests[0]["tool_name"] == "Write"
    assert requests[0]["tool_input"]["file_path"] == "tests/test_protected.py"
    assert requests[1]["tool_name"] == "Read"
    assert requests[1]["tool_input"]["file_path"] == "README.md"
