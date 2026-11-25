"""Shared offline helpers for the R1a-agy acceptance suite (antigravity under the server boundary)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from subagent.runs import Agent, Registry
from subagent.telemetry.trace import Trace
from tests.conftest import make_settings

PROVIDER = "gemini"
SEATBELT = "/usr/bin/sandbox-exec"


def can_nest_sandbox() -> bool:
    """True when this process may start a sandbox-exec child (not already inside Seatbelt)."""
    try:
        return subprocess.run(
            [SEATBELT, "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except OSError:
        return False


def make_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway owner home with credential folders; HOME and Path.home() point at it."""
    home = tmp_path / "home"
    for rel in (".ssh", ".dsh", ".codex", ".claude", ".aws", ".config/gh",
                ".gemini/antigravity-cli"):
        (home / rel).mkdir(parents=True, exist_ok=True)
        (home / rel / "token").write_text("owner-secret\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def fake_binary(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "agy"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def make_agy_settings(tmp_path: Path, binary: Path, trace: str = "off", **provider_extra: Any):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "README.md").write_text("# ws\n", encoding="utf-8")
    sock_dir = (tmp_path / "sockdir").resolve()
    sock_dir.mkdir(mode=0o700, exist_ok=True)
    providers = {PROVIDER: {
        "driver": "antigravity", "vendor": "gemini", "binary": str(binary),
        "experimental": True, **provider_extra,
    }}
    settings = make_settings(
        tmp_path, workspace=str(ws), providers=providers,
        approval_socket=str(sock_dir / "approval.sock"), supervisor="off", trace=trace,
    )
    return ws, sock_dir, settings


def capture_spawn(monkeypatch, settings, ws: Path, agent_id: str) -> tuple[list[str], dict[str, str]]:
    """Run one turn with Process._start stubbed; return the argv and env the child would get."""
    captured: list[tuple[list[str], dict[str, str]]] = []

    def fake_start(self, *, stdin=False, closed_stdin=False):
        captured.append((list(self.argv), dict(self.env)))
        raise RuntimeError("captured-spawn")

    monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent(agent_id, ws, provider=PROVIDER, agent_id=agent_id)
        run = agent.submit("do work", verification="true")
        run.done.wait(timeout=5)
    finally:
        reg.shutdown()
    assert captured, "antigravity did not reach Process._start"
    return captured[0]


# ---- fake agy (stream-json) -------------------------------------------------

def init_event() -> dict:
    return {"event": "init", "conversation_id": "c-1", "init": {"model": "m"}}


def tool_step(index: int, state: str, params: dict, name: str = "run_command") -> dict:
    return {"event": "step_update", "step_update": {
        "conversation_id": "c-1", "step_index": index, "state": state,
        "step_type": "tool", "tool_name": name,
        "tool_info": {"name": name, "parameters": params},
    }}


def text_step(index: int, text: str) -> dict:
    return {"event": "step_update", "step_update": {
        "conversation_id": "c-1", "step_index": index, "state": "DONE",
        "step_type": "agent_response", "text_delta": text,
    }}


def result_event(response: str = "all done") -> dict:
    return {"event": "result", "result": {
        "conversation_id": "c-1", "status": "SUCCESS", "response": response, "num_turns": 1,
        "usage": {"input_tokens": 5, "output_tokens": 1, "thinking_tokens": 0,
                  "cache_read_tokens": 0, "total_tokens": 6},
    }}


def write_fake_agy(path: Path, events: list, prelude: str = "") -> Path:
    """A fake `agy` that prints `events` (dicts, or the string "SLEEP" for a 30 s pause)."""
    body = textwrap.dedent('''\
        import json, os, sys, time
        if "--version" in sys.argv[1:]:
            print("1.2.12"); sys.exit(0)
        open("pid.txt", "w").write(str(os.getpid()))
        EVENTS = json.loads(%r)
        %s
        for ev in EVENTS:
            if ev == "SLEEP":
                time.sleep(30); continue
            print(json.dumps(ev), flush=True)
        ''') % (json.dumps(events), prelude)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def run_fake(tmp_path: Path, monkeypatch, events: list, *, prelude: str = "",
             wait: float = 20, **provider_extra: Any):
    """One real server turn against a fake agy. Returns (agent, run, trace_path, spawned_argvs)."""
    make_home(tmp_path, monkeypatch)
    spawned: list[list[str]] = []
    nest = can_nest_sandbox()
    real_spawn = Agent._spawn

    def record(self, run, prompt, resume):
        proc = real_spawn(self, run, prompt, resume)
        spawned.append(list(proc.argv))
        if not nest and proc.argv[:2] == [SEATBELT, "-p"]:  # already sandboxed: run unwrapped
            proc.argv = proc.argv[3:]
        return proc

    monkeypatch.setattr(Agent, "_spawn", record)
    binary = write_fake_agy(tmp_path / "bin" / "agy", events, prelude)
    trace_path = tmp_path / "trace.jsonl"
    ws, _, settings = make_agy_settings(tmp_path, binary, trace=str(trace_path), **provider_extra)
    reg = Registry(settings, start_reaper=False, trace=Trace(trace_path))
    try:
        agent = reg.create_agent("e2e", ws, provider=PROVIDER)
        run = agent.submit("go", verification="true")
        assert run.done.wait(wait), "run did not finish"
    finally:
        reg.shutdown()
    return agent, run, trace_path, spawned


def trace_of(trace_path: Path, name: str) -> list[dict]:
    if not trace_path.exists():
        return []
    out = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if name in (rec.get("type"), rec.get("kind")):
                out.append(rec)
    return out
