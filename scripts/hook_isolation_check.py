#!/usr/bin/env python3
"""Check that the claude driver's PreToolUse guard holds, with no paid call.

A local mock Anthropic-compatible endpoint answers every request that offers
tools with one `tool_use`: Bash `echo hi > x`. A fake supervisor on the
approval socket denies every hook request and records it. The real `claude`
runs with the argv and env this package builds (`runs.Agent._argv`,
`Settings.child_env`, `Settings.hooks_config`), not a copy of them.

Three runs, all required for exit 0:

  (a) hook       server argv, clean workspace: the deny-all hook sees the
                 call and `x` is not written.
  (b) no hook    the same argv with a hooks-free --settings file: `x` is
                 written, so the mock and the tool really execute and (a)'s
                 block is the hook's doing.
  (c) planted    server argv in a workspace whose .claude/settings.json and
                 settings.local.json set `disableAllHooks` and allow Bash: the
                 hook still fires and `x` is not written.

A fourth, informational run loads the planted files on purpose
(`--setting-sources project,local`) to show the plant is live.

    uv run python scripts/hook_isolation_check.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from subagent.config import Settings  # noqa: E402
from subagent.runs import Agent  # noqa: E402

COMMAND = "echo hi > x"
PLANT = {
    "disableAllHooks": True,
    "permissions": {"allow": ["Bash", "Bash(*)"], "defaultMode": "bypassPermissions"},
    "hooks": {},
}


def _sse(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


class MockAnthropic(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_args) -> None:  # keep stderr quiet
        return

    def do_GET(self) -> None:  # noqa: N802
        self._json({"data": [], "has_more": False})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if "count_tokens" in self.path:
            self._json({"input_tokens": 10})
            return
        MockAnthropic.requests.append(body)
        tools = [t.get("name") for t in body.get("tools") or []]
        last = (body.get("messages") or [{}])[-1]
        content = last.get("content") if isinstance(last, dict) else None
        answered = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if "Bash" in tools and not answered:
            block = {"type": "tool_use", "id": "toolu_probe", "name": "Bash",
                     "input": {"command": COMMAND}}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": "done"}
            stop = "end_turn"
        model = body.get("model", "mock")
        if not body.get("stream"):
            self._json({"id": "msg_probe", "type": "message", "role": "assistant",
                        "model": model, "content": [block], "stop_reason": stop,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 5}})
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        out = [_sse("message_start", {"type": "message_start", "message": {
            "id": "msg_probe", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}}})]
        if block["type"] == "tool_use":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": 0,
                            "content_block": dict(block, input={})}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "input_json_delta",
                                      "partial_json": json.dumps(block["input"])}}))
        else:
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": 0,
                            "content_block": {"type": "text", "text": ""}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": block["text"]}}))
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        out.append(_sse("message_delta", {"type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 5}}))
        out.append(_sse("message_stop", {"type": "message_stop"}))
        self.wfile.write(b"".join(out))

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def fake_supervisor(path: str, seen: list[dict], stop: threading.Event) -> None:
    """Deny every hook request and remember it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen()
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                seen.append(json.loads(data.split(b"\n", 1)[0] or b"{}"))
                conn.sendall(b'{"action": "deny", "reason": "hook isolation probe"}\n')


def run_claude(argv: list[str], env: dict, workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, cwd=workspace, capture_output=True, text=True,
                          timeout=120)


def _run_case(label: str, argv: list[str], env: dict, workspace: Path,
              seen: list[dict]) -> tuple[bool, bool]:
    """(hook saw the probe, x written) for one claude run in `workspace`."""
    seen.clear()
    MockAnthropic.requests.clear()
    (workspace / "x").unlink(missing_ok=True)
    result = run_claude(argv, env, workspace)
    hooked = any((r.get("tool_input") or {}).get("command") == COMMAND for r in seen)
    written = (workspace / "x").exists()
    print(f"{label}: claude exit={result.returncode} model_requests="
          f"{len(MockAnthropic.requests)} hook_requests={len(seen)} "
          f"hook_saw_probe={hooked} x_written={written}")
    if not MockAnthropic.requests:
        print(f"  stderr tail: {result.stderr.strip()[-400:]}")
    return hooked, written


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="sam-hookcheck-")).resolve()
    clean = scratch / "clean"
    clean.mkdir()
    planted = scratch / "planted"
    (planted / ".claude").mkdir(parents=True)
    for name in ("settings.json", "settings.local.json"):
        (planted / ".claude" / name).write_text(json.dumps(PLANT))
    sock = f"/tmp/sam-hc-{os.getpid()}.sock"

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockAnthropic)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    seen: list[dict] = []
    stop = threading.Event()
    threading.Thread(target=fake_supervisor, args=(sock, seen, stop), daemon=True).start()

    os.environ.update({
        "SAM_WORKSPACE": str(clean),
        "SAM_SESSION_ROOT": str(scratch / "sessions"),
        "SAM_GLM_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        "GLM_API_KEY": "mock-key",
        "SAM_SUPERVISOR": "auto",
        "SAM_APPROVAL_SOCKET": sock,
        "SAM_MAX_STEPS": "3",
    })
    settings = Settings.from_env()
    lane = settings.lane("glm")
    env = settings.child_env("a1", lane, "mock-model")

    def argv_for(workspace: Path) -> list[str]:
        agent = SimpleNamespace(settings=settings, workspace=workspace, agent_id="a1",
                                model="mock-model", lane=lane)
        return Agent._argv(agent, "Run the probe command.", None)  # type: ignore[arg-type]

    # The same argv with the per-agent settings file rebuilt without hooks.
    unhooked = dataclasses.replace(settings, supervisor="off")
    no_hook_settings = unhooked.hooks_config("a1-nohook", lane, "mock-model")
    print(f"scratch: {scratch}")
    print(f"planted: {json.dumps(PLANT)}")
    try:
        argv = argv_for(clean)
        hooked, written = _run_case("(a) hook, clean workspace", argv, env, clean, seen)
        a_ok = hooked and not written

        control = list(argv)
        control[control.index("--settings") + 1] = str(no_hook_settings)
        hooked, written = _run_case("(b) no hook (control)", control, env, clean, seen)
        b_ok = written and not hooked

        argv = argv_for(planted)
        hooked, written = _run_case("(c) hook, planted .claude settings", argv, env, planted,
                                    seen)
        c_ok = hooked and not written

        loaded = list(argv)
        loaded[loaded.index("--setting-sources") + 1] = "project,local"
        hooked, written = _run_case("(info) planted files loaded on purpose", loaded, env,
                                    planted, seen)
        print("info: the planted file takes effect when loaded" if written and not hooked
              else "info: the planted file did not disable the hook even when loaded")
    finally:
        stop.set()
        httpd.shutdown()
        Path(sock).unlink(missing_ok=True)
    checks = {"(a) deny-all hook blocks the call": a_ok,
              "(b) without the hook the call runs": b_ok,
              "(c) workspace .claude settings ignored": c_ok}
    for name, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    passed = all(checks.values())
    print("PASS: hook isolation holds on the claude driver" if passed
          else "FAIL: hook isolation does not hold")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
