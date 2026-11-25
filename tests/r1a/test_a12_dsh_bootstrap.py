"""A12 acceptance tests: DSH bootstrap cordis.yml tamper detection and guard_tamper trace."""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path

from subagent.guard.classify import ALLOW, DENY
from subagent.guard.supervisor import Supervisor
from subagent.providers.dsh import NONCE_ENV, READY_MARKER
from subagent.runs import COMPLETED, Agent, Registry
from subagent.telemetry.trace import Trace
from tests.conftest import make_settings

from ._helpers import send_guard_request, wire_sign

_FAKE_DSH_HANDSHAKE_SCRIPT = """\
#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

sys.stdin.read()
nonce = os.environ.get({nonce_env!r}, "")
sys.stderr.write(f"{ready_marker}{{nonce}}\\n")
sys.stderr.flush()

sync = Path(os.environ["A12_SYNC_DIR"])
cordis = Path(os.environ["DSH_HOME"]) / "profiles" / "headless" / "cordis.yml"

def wait_for(name):
    target = sync / name
    deadline = time.monotonic() + 10.0
    while not target.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        time.sleep(0.02)

# Step 1: signal ready with untouched cordis.yml
(sync / "step1_ready").write_text("1", encoding="utf-8")
wait_for("step1_done")

if os.environ.get("TAMPER_MODE") == "1":
    original = cordis.read_text(encoding="utf-8")
    cordis.write_text("- id: tampered\\n", encoding="utf-8")
    (sync / "step2_tampered").write_text("1", encoding="utf-8")
    wait_for("step2_done")

    cordis.write_text(original, encoding="utf-8")
    (sync / "step3_restored").write_text("1", encoding="utf-8")
    wait_for("step3_done")

for ev in (
    {{"type": "session", "sessionId": "s-a12", "cwd": os.getcwd()}},
    {{"type": "status", "phase": "turn_start", "turn": 1}},
    {{"type": "text", "text": "ok"}},
    {{"type": "status", "phase": "turn_end", "turn": 1, "reason": {{"kind": "completed"}}}},
    {{"type": "final", "sessionId": "s-a12", "reason": {{"kind": "completed"}}, "text": "ok"}},
):
    print(json.dumps(ev), flush=True)
sys.exit(0)
"""


def _wait_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.02)


def _signed_read(ws: Path, agent_id: str, key: bytes) -> dict:
    return wire_sign(
        key,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "workspace": str(ws),
            "cwd": str(ws),
            "agent_id": agent_id,
            "nonce": secrets.token_hex(32),
        },
    )


def test_a12_dsh_cordis_tamper_denies_remaining_requests_and_traces_guard_tamper(
    tmp_path: Path, monkeypatch
) -> None:
    """A12: modifying cordis.yml after DSH readiness denies that & later requests and writes guard_tamper."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# ws\n", encoding="utf-8")
    owner_dsh = tmp_path / "owner-dsh"
    owner_dsh.mkdir()
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_dsh))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh-key")

    fake_dsh = tmp_path / "fake_dsh.py"
    fake_dsh.write_text(
        _FAKE_DSH_HANDSHAKE_SCRIPT.format(nonce_env=NONCE_ENV, ready_marker=READY_MARKER),
        encoding="utf-8",
    )
    fake_dsh.chmod(0o755)

    # Sync folders live inside the workspace: the dsh Seatbelt profile only allows
    # writes under the workspace, the agent tmp dir and a few DSH home paths.
    sync1 = ws / ".a12-sync"
    sync1.mkdir()
    monkeypatch.setenv("A12_SYNC_DIR", str(sync1))
    monkeypatch.setenv("TAMPER_MODE", "1")

    trace_path = tmp_path / "trace.jsonl"
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        supervisor="off",
        trace=str(trace_path),
        child_env_passthrough=["A12_SYNC_DIR", "TAMPER_MODE"],
        providers={
            "dsh": {
                "driver": "dsh",
                "vendor": "deepseek",
                "binary": str(fake_dsh),
                "api_key_env": "DEEPSEEK_API_KEY",
                "experimental": True,
            }
        },
    )
    can_nest = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        timeout=5,
    ).returncode == 0
    if not can_nest:
        real_agent_spawn = Agent._spawn

        def strip_nested_sandbox(self, run, prompt, resume):
            proc = real_agent_spawn(self, run, prompt, resume)
            if proc.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]:
                proc.argv = proc.argv[3:]
            return proc

        monkeypatch.setattr(Agent, "_spawn", strip_nested_sandbox)

    trace = Trace(trace_path)
    reg = Registry(settings, start_reaper=False, trace=trace)
    supervisor = Supervisor(settings, registry=reg, trace=trace)

    try:
        agent = reg.create_agent("a12", ws, provider="dsh")
        run1 = agent.submit("run 1", verification="true")

        _wait_file(sync1 / "step1_ready")
        # Give DshProcess.events() a moment to process the stderr readiness line.
        time.sleep(0.1)
        r_before = send_guard_request(supervisor, _signed_read(ws, agent.agent_id, agent.approval_key))
        assert r_before["action"] == ALLOW, f"pre-tamper request must be allowed: {r_before}"
        (sync1 / "step1_done").write_text("1", encoding="utf-8")

        _wait_file(sync1 / "step2_tampered")
        r_tamper = send_guard_request(supervisor, _signed_read(ws, agent.agent_id, agent.approval_key))
        assert r_tamper["action"] == DENY, f"tampered cordis.yml must deny: {r_tamper}"
        (sync1 / "step2_done").write_text("1", encoding="utf-8")

        _wait_file(sync1 / "step3_restored")
        r_restore = send_guard_request(supervisor, _signed_read(ws, agent.agent_id, agent.approval_key))
        assert r_restore["action"] == DENY, f"every later request after tamper must deny: {r_restore}"
        (sync1 / "step3_done").write_text("1", encoding="utf-8")

        assert run1.done.wait(timeout=15)
        assert run1.state == COMPLETED, run1.error

        records = [
            json.loads(ln)
            for ln in trace_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        tamper_recs = [r for r in records if r.get("type") == "guard_tamper"]
        assert len(tamper_recs) == 1, f"expected 1 guard_tamper trace record, got {records}"
        trec = tamper_recs[0]
        assert trec["agent_id"] == agent.agent_id
        assert trec["run_id"] == run1.run_id
        assert str(trec["path"]).endswith("cordis.yml")
        assert isinstance(trec["ts"], (int, float))

        # Next launch rewrites cordis.yml cleanly and allows normal requests again.
        sync2 = ws / ".a12-sync2"
        sync2.mkdir()
        monkeypatch.setenv("A12_SYNC_DIR", str(sync2))
        monkeypatch.setenv("TAMPER_MODE", "0")
        run2 = agent.submit("run 2", verification="true")
        _wait_file(sync2 / "step1_ready")
        time.sleep(0.1)
        r_next = send_guard_request(supervisor, _signed_read(ws, agent.agent_id, agent.approval_key))
        (sync2 / "step1_done").write_text("1", encoding="utf-8")
        assert run2.done.wait(timeout=15)
        assert run2.state == COMPLETED, run2.error
        assert r_next["action"] == ALLOW, f"fresh launch should reset cordis.yml and allow: {r_next}"
    finally:
        for s in (sync1,):
            for name in ("step1_done", "step2_done", "step3_done"):
                (s / name).write_text("1", encoding="utf-8")
        reg.shutdown()
