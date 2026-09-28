"""The omp driver, against recorded `omp -p --mode json` streams and a fake
`omp` binary that replays them.

The fixtures under tests/fixtures/omp were recorded live from omp 18.0.11 on
a local oMLX model (see the README there). The fake (tests/fakes/fake_omp.py,
installed by the `fake_omp` fixture below under the provider's `binary` name)
prints the fixture named by FAKE_OMP_FIXTURE and records its argv, cwd, env
and what it read from stdin to FAKE_OMP_RECORD (one JSON line per call).
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from subagent import router
from subagent.guard.classify import classify
from subagent.providers import omp as omp_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import COMPLETED, Registry

from .conftest import default_providers, make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "omp"
FAKE_OMP = Path(__file__).parent / "fakes" / "fake_omp.py"

MODEL = "Qwen3.8-Flash-Next-REAP-384-oQ4e-BF16-MTP-PLE"


def _omp_cfg(**extra) -> ProviderConfig:
    return ProviderConfig(name="local", driver="omp", vendor="omp", extra=extra)


def _configured_omp_cfg(**patch) -> ProviderConfig:
    values = {
        "name": "omlx",
        "driver": "omp",
        "vendor": "omlx",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "Qwen3.6-35B-A3B-OptiQ-4bit-REAP-19B",
        "api_key_envs": ("OMLX_API_KEY",),
        "local": True,
        "send_sampling": False,
    }
    values.update(patch)
    return ProviderConfig(**values)


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _translate(name: str, exit_code: int = 0, extra_text: str = "") -> list[dict]:
    translator = omp_driver.Translator("omp-session-1")
    out: list[dict] = []
    for event in _load(name):
        out.extend(translator.feed(event))
    out.append(translator.finish(exit_code, extra_text))
    return out


def _blocks(events: list[dict], kind: str, block_type: str) -> list[dict]:
    return [
        b for e in events if e["type"] == kind
        for b in e["message"]["content"] if b["type"] == block_type
    ]


# --- argv ---------------------------------------------------------------------


def test_argv_first_turn_shape(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = _omp_cfg(extra_args=["--thinking", "low"])
    ws = _workspace(tmp_path)
    argv = omp_driver.OMP_PROVIDER.argv(
        cfg, settings, "a1", "do it", ws, Session(provider="local"), f"omlx/{MODEL}"
    )
    assert argv[0] == "omp"
    assert "-p" in argv
    assert argv[argv.index("--mode") + 1] == "json"
    assert "--no-session" in argv
    assert argv[argv.index("--cwd") + 1] == str(ws)
    assert argv[argv.index("--model") + 1] == f"local/omlx/{MODEL}"
    assert argv[-3:] == ["--thinking", "low", "do it"]  # extras, then the prompt verbatim
    assert "--resume" not in argv and "--continue" not in argv


def test_argv_without_a_model_leaves_the_choice_to_omp(tmp_path: Path):
    settings = make_settings(tmp_path)
    argv = omp_driver.OMP_PROVIDER.argv(
        _omp_cfg(), settings, "a1", "do", _workspace(tmp_path), Session(provider="local"), None
    )
    assert "--model" not in argv
    assert argv[-1] == "do"


def test_argv_resume_turn_prepends_the_continue_line_and_previous_text(tmp_path: Path):
    settings = make_settings(tmp_path)
    session = Session(provider="local", session_id="s1", data={"last_message": "wrote hello.txt"})
    argv = omp_driver.OMP_PROVIDER.argv(
        _omp_cfg(), settings, "a1", "now delete it", _workspace(tmp_path), session, None
    )
    prompt = argv[-1]
    assert prompt.startswith(omp_driver.RESUME_PREFIX)
    assert "wrote hello.txt" in prompt
    assert prompt.endswith("now delete it")
    # omp runs --no-session: the continue line is the whole mechanism.
    assert "--resume" not in argv and "--continue" not in argv
    assert "--no-session" in argv


def test_argv_binary_comes_from_config(tmp_path: Path):
    settings = make_settings(tmp_path)
    cfg = ProviderConfig(name="local", driver="omp", vendor="omp", binary="/opt/bin/omp")
    argv = omp_driver.OMP_PROVIDER.argv(
        cfg, settings, "a1", "do", _workspace(tmp_path), Session(provider="local"), None
    )
    assert argv[0] == "/opt/bin/omp"


def test_spawn_writes_agent_scoped_config_and_passes_the_key_only_in_env(
    tmp_path: Path, monkeypatch
):
    settings = make_settings(tmp_path)
    cfg = _configured_omp_cfg(binary="/fake/omp")
    secret = "test-omp-secret-never-on-disk"
    monkeypatch.setenv("OMLX_API_KEY", secret)
    workspace = _workspace(tmp_path)

    first = omp_driver.OMP_PROVIDER.spawn(
        cfg, settings, "agent-one", "reply OK", workspace,
        Session(provider="omlx"), cfg.model,
    )
    agent_home = settings.session_root / "agents" / "agent-one" / "omp-agent"
    config_path = Path(first.argv[first.argv.index("--config") + 1])
    assert config_path == agent_home / "config.yml"
    assert config_path.is_file()
    assert Path(first.env["PI_CODING_AGENT_DIR"]) == agent_home
    assert agent_home.is_relative_to(settings.session_root / "agents")
    assert not config_path.is_relative_to(workspace)

    models_path = agent_home / "models.yml"
    assert models_path.is_file()
    generated = "\n".join(path.read_text() for path in agent_home.rglob("*") if path.is_file())
    assert cfg.base_url in generated
    assert cfg.model in generated
    assert "OMLX_API_KEY" in generated
    assert secret not in generated
    assert first.env.get("OMLX_API_KEY") == secret
    assert secret not in " ".join(first.argv)

    second = omp_driver.OMP_PROVIDER.spawn(
        cfg, settings, "agent-two", "reply OK", workspace,
        Session(provider="omlx"), cfg.model,
    )
    second_home = Path(second.env["PI_CODING_AGENT_DIR"])
    assert second_home == settings.session_root / "agents" / "agent-two" / "omp-agent"
    assert second_home != agent_home


@contextmanager
def _classification_socket(path: Path, workspace: Path, context: dict):
    """One-shot approval socket that answers with the real deterministic classifier."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(25)
    received: list[dict] = []

    def serve_once():
        connection, _ = listener.accept()
        with connection:
            chunks = []
            while b"\n" not in b"".join(chunks):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            request = json.loads(b"".join(chunks).split(b"\n", 1)[0])
            received.append(request)
            verdict = classify(
                request["tool_name"], request["tool_input"], workspace,
                cwd=request.get("cwd"), context=context,
            )
            response = {"action": verdict.action, "reason": verdict.reason}
            connection.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        yield received
    finally:
        listener.close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)


@contextmanager
def _openai_tool_stub():
    """Local streaming OpenAI stub: request one write, then return a final answer."""
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            call = len(requests) == 1
            if call:
                delta = {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_protected_write",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps({
                                "path": "tests/test_protected.py", "content": "tampered",
                            }),
                        },
                    }],
                }
                finish = "tool_calls"
            else:
                delta = {"content": "The protected write was blocked."}
                finish = "stop"
            chunks = [
                {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "qwen-stub", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "qwen-stub", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_hook_blocks_omp_write_to_a_protected_test_path(
    fake_omp, tmp_path: Path, monkeypatch
):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun is needed to execute the packaged omp TypeScript hook")

    workspace = _workspace(tmp_path)
    protected = workspace / "tests" / "test_guard_classifier.py"
    protected.parent.mkdir()
    protected.write_text("# protected test\n")
    settings = make_settings(
        tmp_path, approval_socket=f"/tmp/omp-{uuid.uuid4().hex[:8]}.sock"
    )
    cfg = _configured_omp_cfg(binary=fake_omp.path)
    agent_id = "hook-agent"
    context = {"protected": [str(protected)], "state_allow": []}
    socket_path = Path(settings.approval_socket)
    hook_record = tmp_path / "hook-result.json"
    process = omp_driver.OMP_PROVIDER.spawn(
        cfg, settings, agent_id, "write a test", workspace,
        Session(provider="omlx"), cfg.model,
    )
    # The fake CLI invokes the real extension, which starts the actual Python
    # PreToolUse hook. That hook reaches this socket and gets a classifier verdict.
    process.env["FAKE_OMP_TOOL_EVENT"] = json.dumps({
        "toolName": "write",
        "input": {"path": "tests/test_guard_classifier.py", "content": "tampered"},
    })
    process.env["FAKE_OMP_BUN"] = bun
    process.env["FAKE_OMP_HOOK_RECORD"] = str(hook_record)
    assert process.env["SUBAGENT_GUARD_PYTHON"] == sys.executable
    assert Path(process.env["SUBAGENT_GUARD_HOOK"]).name == "approval_hook.py"
    assert process.env["SUBAGENT_GUARD_AGENT_ID"] == agent_id
    assert Path(process.env["SUBAGENT_APPROVAL_SOCKET"]) == socket_path
    with _classification_socket(socket_path, workspace, context) as requests:
        events = list(process.events())

    assert "--hook" in process.argv
    assert Path(process.argv[process.argv.index("--hook") + 1]).name == "omp_hook.ts"
    assert hook_record.is_file()
    response = json.loads(hook_record.read_text())
    assert response["block"] is True
    assert "protected" in response["reason"].lower() or "test" in response["reason"].lower()
    assert len(requests) == 1
    assert requests[0]["agent_id"] == agent_id
    assert requests[0]["tool_name"] == "Write"
    assert requests[0]["tool_input"]["file_path"] == "tests/test_guard_classifier.py"
    assert any(event.get("type") == "result" for event in events)


def test_hook_maps_ast_edit_paths_to_multi_edit_before_classification(
    fake_omp, tmp_path: Path
):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun is needed to execute the packaged omp TypeScript hook")

    workspace = _workspace(tmp_path)
    protected = workspace / "tests" / "test_ast_edit.py"
    protected.parent.mkdir()
    protected.write_text("# protected\n")
    settings = make_settings(
        tmp_path, approval_socket=f"/tmp/omp-{uuid.uuid4().hex[:8]}.sock"
    )
    context = {"protected": [str(protected)], "state_allow": []}
    hook_record = tmp_path / "hook-result.json"
    process = omp_driver.OMP_PROVIDER.spawn(
        _configured_omp_cfg(binary=fake_omp.path), settings, "ast-agent", "edit",
        workspace, Session(provider="omlx"), None,
    )
    process.env["FAKE_OMP_TOOL_EVENT"] = json.dumps({
        "toolName": "ast_edit",
        "input": {"paths": ["tests/test_ast_edit.py"], "ops": []},
    })
    process.env["FAKE_OMP_BUN"] = bun
    process.env["FAKE_OMP_HOOK_RECORD"] = str(hook_record)

    with _classification_socket(Path(settings.approval_socket), workspace, context) as requests:
        events = list(process.events())

    assert json.loads(hook_record.read_text())["block"] is True
    assert len(requests) == 1
    assert requests[0]["tool_name"] == "MultiEdit"
    assert requests[0]["tool_input"] == {
        "edits": [{"file_path": "tests/test_ast_edit.py"}],
    }
    assert any(event.get("type") == "result" for event in events)


def test_hook_denies_unmapped_omp_tool_without_asking_supervisor(
    fake_omp, tmp_path: Path
):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun is needed to execute the packaged omp TypeScript hook")

    workspace = _workspace(tmp_path)
    settings = make_settings(
        tmp_path, approval_socket=f"/tmp/omp-{uuid.uuid4().hex[:8]}.sock"
    )
    hook_record = tmp_path / "hook-result.json"
    process = omp_driver.OMP_PROVIDER.spawn(
        _configured_omp_cfg(binary=fake_omp.path), settings, "unknown-agent", "edit",
        workspace, Session(provider="omlx"), None,
    )
    process.env["FAKE_OMP_TOOL_EVENT"] = json.dumps({
        "toolName": "future_write_tool",
        "input": {"path": "tests/test_guard_classifier.py"},
    })
    process.env["FAKE_OMP_BUN"] = bun
    process.env["FAKE_OMP_HOOK_RECORD"] = str(hook_record)

    events = list(process.events())

    decision = json.loads(hook_record.read_text())
    assert decision["block"] is True
    assert "unmapped" in decision["reason"].lower()
    assert any(event.get("type") == "result" for event in events)


@pytest.mark.parametrize(("event", "expected_tool"), [
    pytest.param({
        "toolName": "lsp",
        "input": {
            "action": "rename_file", "file": "tests/test_lsp.py", "new_name": "src/new.py",
        },
    }, "MultiEdit", id="lsp-rename-file"),
    pytest.param({
        "toolName": "eval",
        "input": {
            "language": "javascript",
            "code": "writeFileSync('tests/test_eval.py', 'tampered')",
        },
    }, "Bash", id="eval-inline-code"),
])
def test_hook_maps_other_omp_write_tools_into_classifier_shapes(
    fake_omp, tmp_path: Path, event: dict, expected_tool: str
):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("Bun is needed to execute the packaged omp TypeScript hook")

    workspace = _workspace(tmp_path)
    protected_name = "test_lsp.py" if event["toolName"] == "lsp" else "test_eval.py"
    protected = workspace / "tests" / protected_name
    protected.parent.mkdir()
    protected.write_text("# protected\n")
    settings = make_settings(
        tmp_path, approval_socket=f"/tmp/omp-{uuid.uuid4().hex[:8]}.sock"
    )
    context = {"protected": [str(protected)], "state_allow": []}
    hook_record = tmp_path / "hook-result.json"
    process = omp_driver.OMP_PROVIDER.spawn(
        _configured_omp_cfg(binary=fake_omp.path), settings, "mapped-agent", "edit",
        workspace, Session(provider="omlx"), None,
    )
    process.env["FAKE_OMP_TOOL_EVENT"] = json.dumps(event)
    process.env["FAKE_OMP_BUN"] = bun
    process.env["FAKE_OMP_HOOK_RECORD"] = str(hook_record)

    with _classification_socket(Path(settings.approval_socket), workspace, context) as requests:
        events = list(process.events())

    assert json.loads(hook_record.read_text())["block"] is True
    assert len(requests) == 1
    assert requests[0]["tool_name"] == expected_tool
    if event["toolName"] == "eval":
        assert "node -e" in requests[0]["tool_input"]["command"]
    else:
        assert requests[0]["tool_input"]["edits"][0]["file_path"] == "tests/test_lsp.py"
    assert any(event.get("type") == "result" for event in events)


def test_real_omp_hook_blocks_protected_write_against_local_stub(
    tmp_path: Path, monkeypatch
):
    omp = shutil.which("omp")
    if omp is None:
        pytest.skip("omp is not installed")
    if shutil.which("bun") is None:
        pytest.skip("Bun is needed to load the omp TypeScript hook")

    workspace = _workspace(tmp_path)
    protected = workspace / "tests" / "test_protected.py"
    protected.parent.mkdir()
    protected.write_text("# protected\n")
    settings = make_settings(
        tmp_path, approval_socket=f"/tmp/omp-{uuid.uuid4().hex[:8]}.sock"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    context = {"protected": [str(protected)], "state_allow": []}
    socket_path = Path(settings.approval_socket)

    with _openai_tool_stub() as (base_url, requests):
        cfg = _configured_omp_cfg(binary=omp, base_url=base_url, model="qwen-stub")
        process = omp_driver.OMP_PROVIDER.spawn(
            cfg, settings, "real-hook-agent", "write the protected test file", workspace,
            Session(provider="omlx"), cfg.model,
        )
        with _classification_socket(socket_path, workspace, context) as decisions:
            events = list(process.events())

    assert protected.read_text() == "# protected\n"
    assert len(decisions) == 1
    assert decisions[0]["agent_id"] == "real-hook-agent"
    assert decisions[0]["tool_name"] == "Write"
    assert decisions[0]["tool_input"]["file_path"] == "tests/test_protected.py"
    assert len(requests) >= 2
    prohibited = {
        "temperature", "top_p", "top_k", "min_p", "presence_penalty",
        "frequency_penalty", "repetition_penalty", "repeat_penalty", "seed",
        "max_tokens", "max_completion_tokens", "reasoning", "reasoning_effort",
        "thinking", "thinking_budget", "reasoning_budget", "enable_thinking",
    }
    assert not prohibited.intersection(requests[0])
    # omp's Qwen compatibility layer still sends this undocumented control;
    # we have not found a supported models.yml switch for qwenPreserveThinking.
    assert requests[0].get("preserve_thinking") is True
    assert requests[0].get("chat_template_kwargs") == {"preserve_thinking": True}
    assert any(event.get("type") == "result" for event in events)


# --- boot: guard gate and the minted session ----------------------------------


def test_guard_label_is_hook(tmp_path: Path):
    assert omp_driver.OMP_PROVIDER.guard(make_settings(tmp_path), _omp_cfg()) == "hook"


def test_boot_mints_a_session_id(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/omp")
    settings = make_settings(tmp_path)
    error, session = omp_driver.OMP_PROVIDER.boot(settings, "a1", _configured_omp_cfg())
    assert error is None
    uuid.UUID(session.session_id)  # tags the records; omp never sees it


def test_boot_does_not_require_allow_unguarded_when_hook_is_installed(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/omp")
    settings = make_settings(tmp_path)  # allow_unguarded defaults off
    error, _ = omp_driver.OMP_PROVIDER.boot(settings, "a1", _configured_omp_cfg())
    assert error is None


def test_boot_checks_the_binary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    settings = make_settings(tmp_path)
    error, _ = omp_driver.OMP_PROVIDER.boot(settings, "a1", _configured_omp_cfg())
    assert error and "not on PATH" in error


def test_boot_rejects_sampling_cli_overrides_when_sampling_is_disabled(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(shutil, "which", lambda b: "/fake/omp")
    cfg = _configured_omp_cfg(extra={"extra_args": ["--temperature=0.5"]})
    error, _ = omp_driver.OMP_PROVIDER.boot(make_settings(tmp_path), "a1", cfg)
    assert error and "send_sampling = false" in error


# --- the translator on the recorded streams -----------------------------------


def test_translator_simple_reply():
    out = _translate("simple.jsonl")
    assert out[0] == {"type": "system", "subtype": "init", "session_id": "omp-session-1"}
    assert [e["type"] for e in out] == ["system", "assistant", "assistant", "assistant", "result"]
    # Two thinking deltas, then the text delta, in stream order.
    thinking = _blocks(out, "assistant", "thinking")
    assert [b["thinking"] for b in thinking] == [
        "\nThe user is asking me to reply", ' with exactly "OK".\n',
    ]
    assert [b["text"] for b in _blocks(out, "assistant", "text")] == ["\n\nOK"]
    assert not _blocks(out, "assistant", "tool_use")

    result = out[-1]
    assert result["type"] == "result" and result["subtype"] == "success"
    assert result["is_error"] is False
    assert result["result"] == "OK"
    assert result["session_id"] == "omp-session-1"
    assert result["num_turns"] == 1
    assert "error" not in result
    # Usage is read from the assistant message agent_end carries, once.
    assert result["usage"] == {
        "input_tokens": 17933,
        "output_tokens": 16,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_translator_tool_call_stream():
    out = _translate("tool_call.jsonl")
    kinds = [e["type"] for e in out]
    assert kinds[0] == "system" and kinds[-1] == "result"
    assert "user" in kinds

    tool_use = _blocks(out, "assistant", "tool_use")
    assert tool_use == [{
        "type": "tool_use",
        "id": "call_2b66c830",
        "name": "write",
        "input": {"path": "hello.txt", "content": "hi"},
    }]
    tool_result = _blocks(out, "user", "tool_result")
    assert tool_result == [{
        "type": "tool_result",
        "tool_use_id": "call_2b66c830",
        "content": "[hello.txt#A564]\nSuccessfully wrote 2 bytes to hello.txt",
        "is_error": False,
    }]
    # The tool_use precedes its result, which precedes the second turn's text.
    assert kinds.index("user") > next(
        i for i, e in enumerate(out)
        if e["type"] == "assistant"
        and any(b["type"] == "tool_use" for b in e["message"]["content"])
    )

    result = out[-1]
    assert result["is_error"] is False
    # The final turn's text, not the first turn's "\n\n" prefixed to it.
    assert result["result"] == "Created `hello.txt` containing `hi`."
    assert result["num_turns"] == 2
    # Two assistant messages, two model calls: their usage is summed.
    assert result["usage"] == {
        "input_tokens": 1564 + 1678,
        "output_tokens": 80 + 40,
        "cache_read_input_tokens": 16384 * 2,
        "cache_creation_input_tokens": 0,
    }


def test_translator_every_event_type_is_handled():
    """Each type the recorded streams carry maps to a known shape or is
    dropped on purpose; nothing leaks through as an unknown claude event."""
    seen: set[str] = set()
    translator = omp_driver.Translator("s")
    for name in ("simple.jsonl", "tool_call.jsonl", "refused.jsonl"):
        for event in _load(name):
            seen.add(event.get("type"))
            for emitted in translator.feed(event):
                assert emitted["type"] in ("system", "assistant", "user")
    assert seen >= {
        "session", "agent_start", "turn_start", "message_start", "message_update",
        "message_end", "tool_execution_start", "tool_execution_update",
        "tool_execution_end", "turn_end", "agent_end", "auto_retry_start",
    }


def test_translator_tool_result_shapes():
    translator = omp_driver.Translator("s")
    string_result = translator.feed({
        "type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
        "result": "plain text", "isError": True,
    })[-1]
    assert string_result["message"]["content"][0] == {
        "type": "tool_result", "tool_use_id": "c1", "content": "plain text", "is_error": True,
    }
    other = translator.feed({
        "type": "tool_execution_end", "toolCallId": "c2", "toolName": "x",
        "result": {"rows": [1, 2]},
    })[-1]
    assert json.loads(other["message"]["content"][0]["content"]) == {"rows": [1, 2]}
    assert other["message"]["content"][0]["is_error"] is False


def test_translator_reports_error_exit_and_raw_lines():
    translator = omp_driver.Translator("s")
    for event in _load("simple.jsonl"):
        translator.feed(event)
    result = translator.finish(1, "Error: model not found: omlx/nope")
    assert result["is_error"] is True
    assert result["subtype"] == "error"
    assert "model not found" in result["error"]


def test_translator_message_update_error_marks_the_result():
    translator = omp_driver.Translator("s")
    translator.feed({"type": "agent_start"})
    translator.feed({
        "type": "message_update",
        "assistantMessageEvent": {"type": "error", "reason": "provider omlx not configured"},
    })
    result = translator.finish(0)
    assert result["is_error"] is True
    assert "not configured" in result["error"]


# --- refusals: a server or model that cannot be reached ----------------------


def test_refused_connection_stream_is_a_refusal_that_does_not_close():
    """What omp actually prints when the server is down: an assistant message
    that stopped on `error` with `errorMessage` "Unable to connect...", then
    auto retries. Not ECONNREFUSED."""
    out = _translate("refused.jsonl")
    result = out[-1]
    assert result["is_error"] is True
    assert "Unable to connect" in result["error"]
    # No agent_end, so no usage: only the two defaults.
    assert result["usage"] == {"input_tokens": 0, "output_tokens": 0}

    refusal = omp_driver.OMP_PROVIDER.refusal(_omp_cfg(), out)
    assert refusal is not None
    assert refusal.code == router.COPILOT_MODEL_UNAVAILABLE
    # Driver-local: the lane is never closed for it.
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is None


@pytest.mark.parametrize("text", [
    "connect ECONNREFUSED 127.0.0.1:8000",
    "Error: model not found: omlx/nope",
    "Unable to connect. Is the computer able to access the url?",
])
def test_refusal_patterns(text: str):
    events = [{"type": "result", "subtype": "error", "is_error": True, "result": "",
               "error": text}]
    refusal = omp_driver.OMP_PROVIDER.refusal(_omp_cfg(), events)
    assert refusal is not None and refusal.code == router.COPILOT_MODEL_UNAVAILABLE


def test_no_refusal_on_success_or_unrelated_error():
    cfg = _omp_cfg()
    assert omp_driver.OMP_PROVIDER.refusal(cfg, [{"type": "result", "is_error": False}]) is None
    assert omp_driver.OMP_PROVIDER.refusal(
        cfg, [{"type": "result", "is_error": True, "error": "tests failed"}]
    ) is None


# --- end to end: a fake `omp` binary ------------------------------------------


@pytest.fixture
def fake_omp(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "omp"
    binary.write_text(f"#!{sys.executable}\n{FAKE_OMP.read_text()}")
    binary.chmod(0o755)
    record = tmp_path / "omp-calls.jsonl"
    monkeypatch.setenv("FAKE_OMP_RECORD", str(record))
    monkeypatch.setenv("FAKE_OMP_FIXTURE", str(FIXTURES / "tool_call.jsonl"))

    class Fake:
        path = str(binary)

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


def test_run_completes_and_resumes_through_the_prompt(fake_omp, tmp_path: Path):
    # The stock providers stay declared so their keys count as leaked.
    providers = {**default_providers(), "local": {"driver": "omp", "binary": fake_omp.path}}
    providers["local"].update({"base_url": "http://127.0.0.1:8000/v1", "model": "fixture-model"})
    settings = make_settings(tmp_path, providers=providers, allow_unguarded=True, run_timeout=30)
    reg = Registry(settings, start_reaper=False)
    try:
        ws = _workspace(tmp_path)
        agent = reg.create_agent("o", ws, provider="local")
        assert agent.wait_ready(5) is None
        run = agent.submit("make hello.txt", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        uuid.UUID(run.session_id)  # minted, never omp's own session id
        assert run.session_id == agent.session_id
        assert run.usage.input == 1564 + 1678
        assert run.usage.output == 120
        assert run.usage.cache_read == 32768
        assert run.usage.steps == 1  # the one write call
        assert agent.info()["usage"]["input"] == 1564 + 1678

        second = agent.follow_up("now remove it", verification="true")
        assert second.done.wait(10)
        assert second.state == COMPLETED, second.error
        first, then = fake_omp.calls()[:2]
        assert first["argv"][-1] == "make hello.txt"
        assert first["argv"][first["argv"].index("--cwd") + 1] == str(ws)
        assert first["argv"][first["argv"].index("--model") + 1] == "local/fixture-model"
        # The resume turn carries the previous turn's final text in the prompt.
        assert then["argv"][-1].startswith(omp_driver.RESUME_PREFIX)
        assert "Created `hello.txt` containing `hi`." in then["argv"][-1]
        assert then["argv"][-1].endswith("now remove it")
        # The child's stdin is closed, not the server's: omp would otherwise
        # sit reading it to EOF.
        assert first["stdin"] == "" and then["stdin"] == ""
        # The server's own keys never reach the child.
        assert first["env"]["GLM_API_KEY"] is None
    finally:
        reg.shutdown()


def test_run_refused_connection_fails_without_closing_the_lane(fake_omp, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OMP_FIXTURE", str(FIXTURES / "refused.jsonl"))
    monkeypatch.setenv("FAKE_OMP_EXIT", "1")
    providers = {"local": {
        "driver": "omp", "binary": fake_omp.path,
        "base_url": "http://127.0.0.1:8000/v1", "model": "fixture-model",
    }}
    settings = make_settings(tmp_path, providers=providers, allow_unguarded=True, run_timeout=30)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("o", _workspace(tmp_path), provider="local")
        assert agent.wait_ready(5) is None
        run = agent.submit("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state != COMPLETED
        assert run.error and "Unable to connect" in run.error
    finally:
        reg.shutdown()
