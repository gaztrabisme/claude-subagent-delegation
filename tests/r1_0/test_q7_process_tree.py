"""Q7 acceptance tests: whole process tree killed on normal exit and cancel."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from subagent.providers.base import Session
from subagent.providers.dsh import NONCE_ENV, READY_MARKER, DshProcess, DshTranslator
from subagent.runs import CANCELLED, COMPLETED, Registry
from tests.conftest import make_settings

_FAKE_CLI_TEMPLATE = """\
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = {mode!r}
pid_a_file = Path({pid_a_file!r})
pid_b_file = Path({pid_b_file!r})
ready_file = Path({ready_file!r})

if mode == "dsh":
    sys.stdin.read()
    nonce = os.environ.get({nonce_env!r}, "")
    sys.stderr.write(f"{ready_marker}{{nonce}}\\n")
    sys.stderr.flush()

# (a) child in a new session sleeping 60s
child_a = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    start_new_session=True,
)
pid_a_file.write_text(str(child_a.pid), encoding="utf-8")

# (b) double fork whose intermediate parent exits immediately
pid1 = os.fork()
if pid1 == 0:
    os.setsid()
    pid2 = os.fork()
    if pid2 == 0:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
        tmp = pid_b_file.with_suffix(".tmp")
        tmp.write_text(str(os.getpid()), encoding="utf-8")
        tmp.rename(pid_b_file)
        time.sleep(60)
        os._exit(0)
    os._exit(0)
os.waitpid(pid1, 0)

deadline = time.monotonic() + 3.0
while not pid_b_file.exists() and time.monotonic() < deadline:
    time.sleep(0.02)

if mode == "cancel":
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s-q7"}}), flush=True)
    ready_file.write_text("ready\\n", encoding="utf-8")
    time.sleep(60)
    sys.exit(0)

if mode == "dsh":
    events = [
        {{"type": "session", "sessionId": "s-q7-dsh", "cwd": os.getcwd()}},
        {{"type": "status", "phase": "turn_start", "turn": 1}},
        {{"type": "text", "text": "ok"}},
        {{"type": "status", "phase": "turn_end", "turn": 1, "reason": {{"kind": "completed"}}}},
        {{"type": "final", "sessionId": "s-q7-dsh", "reason": {{"kind": "completed"}}, "text": "ok"}},
    ]
    for ev in events:
        print(json.dumps(ev), flush=True)
    sys.exit(0)

for ev in (
    {{"type": "system", "subtype": "init", "session_id": "s-q7"}},
    {{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "done"}}]}}}},
    {{
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "session_id": "s-q7",
        "usage": {{"input_tokens": 1, "output_tokens": 1}},
        "num_turns": 1,
    }},
):
    print(json.dumps(ev), flush=True)
sys.exit(0)
"""


def _write_fake_cli(
    path: Path, *, mode: str, pid_a: Path, pid_b: Path, ready: Path
) -> Path:
    path.write_text(
        _FAKE_CLI_TEMPLATE.format(
            mode=mode,
            pid_a_file=str(pid_a),
            pid_b_file=str(pid_b),
            ready_file=str(ready),
            nonce_env=NONCE_ENV,
            ready_marker=READY_MARKER,
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_all_dead(pids: list[int], timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [p for p in pids if _is_alive(p)]
        if not alive:
            return []
        time.sleep(0.05)
    return [p for p in pids if _is_alive(p)]


def _cleanup_pids(pids: list[int], outsider: subprocess.Popen[str] | None) -> None:
    for pid in pids:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    if outsider is not None:
        with suppress(OSError):
            outsider.kill()
        with suppress(OSError, subprocess.SubprocessError):
            outsider.wait(timeout=2)


def _read_pids(*files: Path) -> list[int]:
    return [int(f.read_text(encoding="utf-8").strip()) for f in files]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX os.fork")
def test_q7_process_normal_exit_kills_setsid_and_double_fork_descendants(
    tmp_path: Path,
) -> None:
    """Q7: normal exit of a Process-backed CLI kills setsid and double-forked descendants."""
    pid_a_file = tmp_path / "pid_a"
    pid_b_file = tmp_path / "pid_b"
    ready_file = tmp_path / "ready"
    cli = _write_fake_cli(
        tmp_path / "fake_claude",
        mode="normal",
        pid_a=pid_a_file,
        pid_b=pid_b_file,
        ready=ready_file,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers={
            "claude_q7": {
                "driver": "claude",
                "binary": str(cli),
                "base_url": "http://127.0.0.1:1",
                "model": "m",
                "api_key": "local",
            }
        },
        run_timeout=30,
    )
    outsider = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    recorded_pids: list[int] = []
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("q7", ws, provider="claude_q7")
        run = agent.submit("spawn children", verification="true")
        assert run.done.wait(15), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        recorded_pids = _read_pids(pid_a_file, pid_b_file)
        survivors = _wait_all_dead(recorded_pids, timeout=10.0)
        assert not survivors, f"descendants survived normal exit: {survivors}"
        assert outsider.poll() is None and _is_alive(outsider.pid)
    finally:
        reg.shutdown()
        _cleanup_pids(recorded_pids, outsider)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX os.fork")
def test_q7_cancelled_run_kills_setsid_and_double_fork_descendants(
    tmp_path: Path,
) -> None:
    """Q7: cancelling a run kills setsid and double-forked descendants without harming outsiders."""
    pid_a_file = tmp_path / "pid_a"
    pid_b_file = tmp_path / "pid_b"
    ready_file = tmp_path / "ready"
    cli = _write_fake_cli(
        tmp_path / "fake_claude",
        mode="cancel",
        pid_a=pid_a_file,
        pid_b=pid_b_file,
        ready=ready_file,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers={
            "claude_q7": {
                "driver": "claude",
                "binary": str(cli),
                "base_url": "http://127.0.0.1:1",
                "model": "m",
                "api_key": "local",
            }
        },
        run_timeout=30,
    )
    outsider = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    recorded_pids: list[int] = []
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("q7-cancel", ws, provider="claude_q7")
        run = agent.submit("hang", verification="true")
        deadline = time.monotonic() + 10.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "fake CLI did not reach ready state"
        recorded_pids = _read_pids(pid_a_file, pid_b_file)
        assert all(_is_alive(p) for p in recorded_pids)

        agent.close("cancel")
        assert run.done.wait(10), f"cancelled run did not finish: {run.state}"
        assert run.state == CANCELLED
        survivors = _wait_all_dead(recorded_pids, timeout=10.0)
        assert not survivors, f"descendants survived cancel: {survivors}"
        assert outsider.poll() is None and _is_alive(outsider.pid)
    finally:
        reg.shutdown()
        _cleanup_pids(recorded_pids, outsider)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX os.fork")
def test_q7_dsh_process_normal_exit_kills_setsid_and_double_fork_descendants(
    tmp_path: Path,
) -> None:
    """Q7: DshProcess normal exit kills setsid and double-forked descendants within 10s."""
    pid_a_file = tmp_path / "pid_a"
    pid_b_file = tmp_path / "pid_b"
    ready_file = tmp_path / "ready"
    cli = _write_fake_cli(
        tmp_path / "fake_dsh",
        mode="dsh",
        pid_a=pid_a_file,
        pid_b=pid_b_file,
        ready=ready_file,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    outsider = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    recorded_pids: list[int] = []
    try:
        env = {
            "PATH": os.environ.get("PATH", ""),
            NONCE_ENV: "q7-dsh-nonce",
        }
        proc = DshProcess(
            [sys.executable, str(cli)],
            env,
            str(ws),
            "spawn children",
            DshTranslator(Session(provider="deepseek")),
        )
        events = list(proc.events())
        results = [e for e in events if e.get("type") == "result"]
        assert len(results) == 1 and results[0]["is_error"] is False
        recorded_pids = _read_pids(pid_a_file, pid_b_file)
        survivors = _wait_all_dead(recorded_pids, timeout=10.0)
        assert not survivors, f"DshProcess descendants survived normal exit: {survivors}"
        assert outsider.poll() is None and _is_alive(outsider.pid)
    finally:
        _cleanup_pids(recorded_pids, outsider)
