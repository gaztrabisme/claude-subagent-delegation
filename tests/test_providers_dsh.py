"""Acceptance tests for the DeepSeek Harness (`dsh`) provider driver (W2b).

Pins section A of the W2b contract (points 1-10) plus the driver half of point 12
against offline synthetic fixtures (`tests/fixtures/dsh/`) and `tests/fakes/fake_dsh.py`.
No real DSH binary, model endpoint, `.env` file, or credential store is touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from subagent import config, router
from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import COMPLETED, FAILED, Agent, Registry
from subagent.telemetry.trace import Trace

from .conftest import TEST_CHILD_ENV_PASSTHROUGH, make_settings
from .test_trace_schema import USAGE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "dsh"
FAKE_DSH = Path(__file__).parent / "fakes" / "fake_dsh.py"
DSH_DRIVER_FILE = REPO_ROOT / "src" / "subagent" / "providers" / "dsh.py"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "config.dsh.toml"

DSH_TEST_ENV_PASSTHROUGH = (
    *TEST_CHILD_ENV_PASSTHROUGH,
    "FAKE_DSH_RECORD",
    "FAKE_DSH_FIXTURE",
    "FAKE_DSH_STDERR",
    "FAKE_DSH_EXIT",
)


def _driver():
    """Resolve the `dsh` driver via the public `for_driver` registry."""
    return for_driver("dsh")


def _dsh_cfg(**overrides) -> ProviderConfig:
    values = {
        "name": "deepseek",
        "driver": "dsh",
        "vendor": "deepseek",
        "binary": "/fake/bin/dsh",
        "api_key_envs": ("DEEPSEEK_API_KEY",),
        "experimental": True,
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _dsh_settings(tmp_path: Path, providers: dict | None = None, **overrides):
    overrides.setdefault("child_env_passthrough", list(DSH_TEST_ENV_PASSTHROUGH))
    return make_settings(tmp_path, providers=providers, **overrides)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _load_fixture(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _flag_value(argv: list[str], flag: str) -> str:
    idx = argv.index(flag)
    assert idx + 1 < len(argv), f"{flag} missing value in {argv}"
    return argv[idx + 1]


def _patch_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--patch"]


def _blocks(events: list[dict], event_type: str, block_type: str) -> list[dict]:
    return [
        block
        for event in events
        if event.get("type") == event_type
        for block in (event.get("message") or {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == block_type
    ]


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
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(FIXTURES / "success_tools_usage.jsonl"))
    monkeypatch.delenv("FAKE_DSH_STDERR", raising=False)
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    class FakeDsh:
        path = str(binary)

        def use(self, fixture: str, *, exit_code: int = 0, stderr: str = "") -> None:
            monkeypatch.setenv("FAKE_DSH_FIXTURE", str(FIXTURES / fixture) if fixture else "")
            monkeypatch.setenv("FAKE_DSH_EXIT", str(exit_code))
            if stderr:
                monkeypatch.setenv("FAKE_DSH_STDERR", stderr)
            else:
                monkeypatch.delenv("FAKE_DSH_STDERR", raising=False)

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [
                json.loads(line)
                for line in record.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    return FakeDsh()


def _registry_with_dsh(
    tmp_path: Path,
    fake_dsh,
    *,
    provider_patch: dict | None = None,
    trace_path: Path | None = None,
    **settings_overrides,
) -> Registry:
    provider = {
        "driver": "dsh",
        "vendor": "deepseek",
        "binary": fake_dsh.path,
        "api_key_env": "DEEPSEEK_API_KEY",
        "experimental": True,
    }
    if provider_patch:
        provider.update(provider_patch)
    settings = _dsh_settings(
        tmp_path,
        providers={"deepseek": provider},
        run_timeout=30,
        **settings_overrides,
    )
    trace = Trace(trace_path) if trace_path is not None else None
    return Registry(settings, start_reaper=False, trace=trace)


# --- Registration & no machine-specific paths (point 1) ----------------------


def test_dsh_registered_in_for_driver():
    driver = _driver()
    assert driver.name == "dsh"
    assert driver.prompt_on_stdin is True


def test_no_machine_specific_dsh_path_in_src(tmp_path: Path):
    assert DSH_DRIVER_FILE.is_file(), f"expected driver source at {DSH_DRIVER_FILE}"
    source = DSH_DRIVER_FILE.read_text(encoding="utf-8")
    for forbidden in (
        "/Applications/DeepSeek Harness.app",
        "198.51.100",
        "192.0.2",
        "Tailscale.app",
        ".omlx/settings",
    ):
        assert forbidden not in source, f"machine-specific value {forbidden!r} found in {DSH_DRIVER_FILE}"

    # Without ProviderConfig.binary, no hardcoded absolute binary path may appear.
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(binary=None)
    try:
        argv = driver.argv(cfg, settings, "a1", "prompt", _workspace(tmp_path), Session(provider="deepseek"), None)
    except Exception:
        return  # refusing missing binary is also valid
    assert not Path(argv[0]).is_absolute(), f"unexpected default absolute binary path: {argv[0]!r}"


# --- argv, stdin, permission-mode & outer sandbox (points 1, 2) --------------


def test_argv_shape_uses_config_binary_headless_json_never_guard_patch_and_stdin_dash(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(binary="/custom/path/dsh")

    argv = driver.argv(cfg, settings, "a1", "do the task", ws, Session(provider="deepseek"), None)

    assert argv[0] == "/custom/path/dsh"
    assert _flag_value(argv, "--profile") == "headless"
    assert "--json" in argv
    assert _flag_value(argv, "--approval-policy") == "never"
    assert _flag_value(argv, "--permission-mode") == "workspace-write"
    assert argv[-1] == "-"
    assert "do the task" not in argv  # prompt goes on stdin, not argv
    assert "--session-id" not in argv

    patches = _patch_values(argv)
    assert len(patches) == 1
    guard_patch = Path(patches[0])
    assert guard_patch.is_file(), f"guard patch does not exist: {guard_patch}"
    assert "dsh-guard" in str(guard_patch)


def test_prompt_is_delivered_on_stdin_and_closed(fake_dsh, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(binary=fake_dsh.path)

    prompt = "Inspect the repo\nand report status."
    process = driver.spawn(cfg, settings, "a1", prompt, ws, Session(provider="deepseek"), None)
    events = list(process.events())

    assert any(e.get("type") == "result" and not e.get("is_error") for e in events)
    calls = fake_dsh.calls()
    assert len(calls) == 1
    assert calls[0]["stdin"] == prompt
    assert calls[0]["argv"][-1] == "-"
    assert prompt not in calls[0]["argv"]


def test_permission_mode_workspace_write_without_outer_sandbox(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(deny_read=())

    process = driver.spawn(cfg, settings, "a1", "hello", ws, Session(provider="deepseek"), None)

    assert "/usr/bin/sandbox-exec" not in process.argv
    assert process.argv[0] == cfg.binary
    assert _flag_value(process.argv, "--permission-mode") == "workspace-write"


def test_permission_mode_danger_full_access_when_outer_sandbox_active(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    private = tmp_path / "private-secrets"
    private.mkdir()
    cfg = _dsh_cfg(deny_read=(str(private),))

    process = driver.spawn(cfg, settings, "a1", "hello", ws, Session(provider="deepseek"), None)

    assert process.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert process.argv.count("/usr/bin/sandbox-exec") == 1
    assert _flag_value(process.argv, "--permission-mode") == "danger-full-access"

    profile = process.argv[2]
    assert f'(deny file-read* (subpath "{private.resolve()}"))' in profile
    assert '(deny file-write* (subpath "/"))' in profile
    assert f'(subpath "{ws.resolve()}")' in profile
    dsh_home = Path(process.env["DSH_HOME"]).resolve()
    assert f'(subpath "{dsh_home}")' in profile


def test_registry_deny_read_uses_outer_sandbox_without_double_wrapping(
    fake_dsh, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("subagent.sandbox.sys.platform", "darwin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    private = tmp_path / "call-private"
    private.mkdir()
    ws = _workspace(tmp_path)

    captured_argvs: list[list[str]] = []
    real_agent_spawn = Agent._spawn

    def spy_agent_spawn(self, run, prompt, resume):
        proc = real_agent_spawn(self, run, prompt, resume)
        captured_argvs.append(list(proc.argv))
        # Strip sandbox-exec before executing fake_dsh so nested sandboxed test
        # environments still run the fake CLI cleanly.
        if proc.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]:
            proc.argv = proc.argv[3:]
        return proc

    monkeypatch.setattr(Agent, "_spawn", spy_agent_spawn)

    reg = _registry_with_dsh(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek", deny_read=[str(private)])
        assert agent.wait_ready(5) is None
        run = agent.submit("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
    finally:
        reg.shutdown()

    assert len(captured_argvs) == 1
    final_argv = captured_argvs[0]
    assert final_argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert final_argv.count("/usr/bin/sandbox-exec") == 1
    assert _flag_value(final_argv, "--permission-mode") == "danger-full-access"
    assert f'(deny file-read* (subpath "{private.resolve()}"))' in final_argv[2]


# --- Guard patch always present & fail closed (point 3) ----------------------


def test_guard_patch_always_present_even_when_supervisor_off(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path, supervisor="off")
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg()

    argv = driver.argv(cfg, settings, "a1", "task", ws, Session(provider="deepseek"), None)
    patches = _patch_values(argv)
    assert any("dsh-guard" in p for p in patches), f"guard patch missing from {argv}"


def test_guard_label_reports_hook(tmp_path: Path):
    driver = _driver()
    label = driver.guard(_dsh_settings(tmp_path), _dsh_cfg())
    assert "hook" in label, f"expected guard label to include 'hook', got {label!r}"


def test_spawn_fails_closed_when_guard_bridge_or_patch_missing(tmp_path: Path, monkeypatch):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg()

    real_path_exists = Path.exists
    real_path_is_file = Path.is_file
    real_path_is_dir = Path.is_dir
    real_os_exists = os.path.exists
    real_os_isfile = os.path.isfile
    real_os_isdir = os.path.isdir

    def is_bridge(path) -> bool:
        return "dsh-guard" in os.fspath(path)

    monkeypatch.setattr(Path, "exists", lambda self: False if is_bridge(self) else real_path_exists(self))
    monkeypatch.setattr(Path, "is_file", lambda self: False if is_bridge(self) else real_path_is_file(self))
    monkeypatch.setattr(Path, "is_dir", lambda self: False if is_bridge(self) else real_path_is_dir(self))
    monkeypatch.setattr(os.path, "exists", lambda p: False if is_bridge(p) else real_os_exists(p))
    monkeypatch.setattr(os.path, "isfile", lambda p: False if is_bridge(p) else real_os_isfile(p))
    monkeypatch.setattr(os.path, "isdir", lambda p: False if is_bridge(p) else real_os_isdir(p))

    spawned: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    with pytest.raises(Exception) as exc_info:
        driver.spawn(cfg, settings, "a1", "task", ws, Session(provider="deepseek"), None)

    assert not spawned, "driver started a subprocess despite missing guard bridge"
    msg = str(exc_info.value).lower()
    assert "bridge" in msg or "dsh-guard" in msg, f"error should name the bridge, got: {exc_info.value!r}"


# --- Model patch generation (point 4) ----------------------------------------


def test_no_model_means_no_model_patch_and_no_model_flag(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(model=None)

    process = driver.spawn(cfg, settings, "a1", "task", ws, Session(provider="deepseek"), None)
    assert "--model" not in process.argv
    patches = _patch_values(process.argv)
    assert len(patches) == 1, f"expected only guard patch when no model is set, got {patches}"


def test_config_model_generates_patch_in_agent_dir(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(model="deepseek-v4-pro")

    process = driver.spawn(cfg, settings, "agent-m1", "task", ws, Session(provider="deepseek"), None)
    assert "--model" not in process.argv

    patches = _patch_values(process.argv)
    assert len(patches) == 2, f"expected guard patch + model patch, got {patches}"
    model_patches = [Path(p) for p in patches if "dsh-guard" not in p]
    assert len(model_patches) == 1
    model_patch = model_patches[0].resolve()
    agent_dir = (settings.session_root / "agents" / "agent-m1").resolve()
    assert model_patch.is_file()
    assert model_patch.is_relative_to(agent_dir), f"model patch {model_patch} must live in {agent_dir}"
    assert not model_patch.is_relative_to(ws.resolve())

    text = model_patch.read_text(encoding="utf-8")
    assert "agent-default-model" in text
    assert "deepseek-v4-pro" in text


def test_per_call_model_overrides_config_model_in_patch(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg(model="deepseek-flash")

    process = driver.spawn(
        cfg, settings, "agent-m2", "task", ws, Session(provider="deepseek"), "deepseek-v4-pro"
    )
    assert "--model" not in process.argv
    model_patches = [Path(p) for p in _patch_values(process.argv) if "dsh-guard" not in p]
    assert len(model_patches) == 1
    text = model_patches[0].read_text(encoding="utf-8")
    assert "deepseek-v4-pro" in text
    assert "deepseek-flash" not in text


# --- Environment allowlist, key mapping, base URL, DSH_HOME (points 5, 12) ---


def test_env_maps_api_key_from_provider_api_key_env_and_strips_parent_secrets(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CUSTOM_DS_KEY", "sk-custom-deepseek")
    monkeypatch.setenv("GLM_API_KEY", "leaked-glm-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "leaked-anthropic-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked-aws-secret")
    monkeypatch.setenv("GH_TOKEN", "leaked-gh-token")
    monkeypatch.setenv("DB_PASSWORD", "leaked-db-password")

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(api_key_envs=("CUSTOM_DS_KEY",))
    env = driver.env(settings, "a1", cfg, Session(provider="deepseek"))

    assert env["DEEPSEEK_API_KEY"] == "sk-custom-deepseek"
    for forbidden in (
        "CUSTOM_DS_KEY",
        "GLM_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GH_TOKEN",
        "DB_PASSWORD",
    ):
        assert forbidden not in env, f"parent secret {forbidden!r} leaked into child env"


def test_env_sets_empty_deepseek_api_key_when_unset_to_shadow_dotenv_fallback(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("CUSTOM_DS_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-parent-key-must-not-leak")

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_cfg(api_key_envs=("CUSTOM_DS_KEY",))
    env = driver.env(settings, "a1", cfg, Session(provider="deepseek"))

    # Exported empty string shadows DSH's <cwd>/.env and $DSH_HOME/.env fallbacks.
    assert "DEEPSEEK_API_KEY" in env
    assert env["DEEPSEEK_API_KEY"] == ""

    # Same when api_key_envs names DEEPSEEK_API_KEY and it is unset in parent.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg_default = _dsh_cfg(api_key_envs=("DEEPSEEK_API_KEY",))
    env_default = driver.env(settings, "a1", cfg_default, Session(provider="deepseek"))
    assert "DEEPSEEK_API_KEY" in env_default
    assert env_default["DEEPSEEK_API_KEY"] == ""


def test_env_sets_deepseek_base_url_only_when_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://ambient.invalid/anthropic")
    driver = _driver()
    settings = _dsh_settings(tmp_path)

    env_unset = driver.env(settings, "a1", _dsh_cfg(base_url=None), Session(provider="deepseek"))
    assert "DEEPSEEK_BASE_URL" not in env_unset

    env_set = driver.env(
        settings,
        "a1",
        _dsh_cfg(base_url="https://proxy.example/anthropic"),
        Session(provider="deepseek"),
    )
    assert env_set["DEEPSEEK_BASE_URL"] == "https://proxy.example/anthropic"


def test_env_sets_isolated_dsh_home_under_session_root_and_guard_hook_command(tmp_path: Path):
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_cfg()

    env = driver.env(settings, "agent-42", cfg, Session(provider="deepseek"))

    dsh_home = Path(env["DSH_HOME"]).resolve()
    assert dsh_home.is_relative_to(settings.session_root.resolve())
    assert dsh_home != (Path.home() / ".dsh").resolve()
    assert not dsh_home.is_relative_to(ws.resolve())

    # Point 12: driver exports SUBAGENT_GUARD_HOOK with the shared hook command line.
    assert env["SUBAGENT_GUARD_HOOK"] == settings.guard_hook_command("agent-42")
    assert env["SUBAGENT_APPROVAL_SOCKET"] == settings.approval_socket
    assert float(env["SUBAGENT_HOOK_TIMEOUT"]) > 0


# --- Event translation & session resume (points 6, 7, 10) --------------------


def test_translator_emits_init_text_tool_use_tool_result_and_one_final_result():
    driver = _driver()
    session = Session(provider="deepseek")
    translator = driver.translator(session)
    assert translator is not None

    out: list[dict] = []
    for raw in _load_fixture("success_tools_usage.jsonl"):
        out.extend(translator.feed(raw))
    if hasattr(translator, "finish"):
        finished = translator.finish(0, "")
        if finished is not None and not any(e.get("type") == "result" for e in out):
            out.append(finished)

    assert out[0]["type"] == "system"
    assert out[0]["subtype"] == "init"
    assert out[0]["session_id"] == "session-dsh-0001"

    texts = [b["text"] for b in _blocks(out, "assistant", "text")]
    assert "Inspecting the workspace." in texts
    assert "Done." in texts

    tool_uses = _blocks(out, "assistant", "tool_use")
    assert len(tool_uses) == 2
    assert tool_uses[0]["name"].lower() == "bash"
    assert tool_uses[0]["input"] == {"command": "pwd", "description": "Print working directory"}
    assert tool_uses[1]["name"].lower() == "read"
    assert tool_uses[1]["input"] == {"file_path": "README.md"}

    tool_results = _blocks(out, "user", "tool_result")
    assert len(tool_results) == 2
    assert tool_results[0]["tool_use_id"] == "call-1"
    assert tool_results[0]["is_error"] is False
    assert "/work/tree" in tool_results[0]["content"]
    assert tool_results[1]["tool_use_id"] == "call-2"
    assert tool_results[1]["is_error"] is True
    assert "permission denied" in tool_results[1]["content"]

    results = [e for e in out if e.get("type") == "result"]
    assert len(results) == 1
    result = results[0]
    assert result["is_error"] is False
    assert result["result"] == "Done."
    assert result["session_id"] == "session-dsh-0001"


def test_translator_and_run_sum_step_end_usage_fields(fake_dsh, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        run = agent.submit("check workspace", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error

        # Summed across the 3 step_end events in success_tools_usage.jsonl:
        # input: 120 + 200 + 80 = 400
        # output: 24 + 40 + 16 = 80
        # cache_read: 32 + 60 + 10 = 102
        # cache_write: 10 + 0 + 0 = 10
        # reasoning: 8 + 12 + 0 = 20
        assert run.usage.input == 400
        assert run.usage.output == 80
        assert run.usage.cache_read == 102
        assert run.usage.cache_write == 10
        assert run.usage.reasoning == 20
        assert run.usage.steps == 2
    finally:
        reg.shutdown()


def test_run_completes_captures_session_id_and_resumes_next_round(
    fake_dsh, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    ws = _workspace(tmp_path)
    reg = _registry_with_dsh(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek")
        assert agent.wait_ready(5) is None

        first = agent.submit("round one", verification="true")
        assert first.done.wait(10), f"round 1 did not finish: {first.state} {first.error}"
        assert first.state == COMPLETED, first.error
        assert first.session_id == agent.session_id == "session-dsh-0001"
        assert first.final_response == "Done."

        fake_dsh.use("resumed_session.jsonl")
        second = agent.follow_up("round two", verification="true")
        assert second.done.wait(10), f"round 2 did not finish: {second.state} {second.error}"
        assert second.state == COMPLETED, second.error
        assert second.session_id == "session-dsh-0001"
        assert second.final_response == "Continued and finished."
        assert second.usage.input == 50
        assert second.usage.output == 10
        assert second.usage.cache_read == 200

        call1, call2 = fake_dsh.calls()[:2]
        assert "--session-id" not in call1["argv"]
        assert call1["stdin"] == "round one"
        assert _flag_value(call2["argv"], "--session-id") == "session-dsh-0001"
        assert call2["stdin"] == "round two"
    finally:
        reg.shutdown()


def test_step_without_usage_marks_run_usage_unreported_instead_of_partial_sum(
    fake_dsh, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    fake_dsh.use("step_without_usage.jsonl")
    ws = _workspace(tmp_path)
    trace_path = tmp_path / "trace.jsonl"

    reg = _registry_with_dsh(tmp_path, fake_dsh, trace_path=trace_path)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek")
        assert agent.wait_ready(5) is None
        run = agent.submit("do work", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
        # Step 1 had 100 input / 20 output, step 2 had no usage sample: must NOT
        # present step 1's partial sum as complete.
        assert run.usage.input == 0
        assert run.usage.output == 0
        assert run.usage.total == 0
    finally:
        reg.shutdown()

    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_records = [r for r in records if r.get("kind") == "run"]
    assert len(run_records) == 1
    run_rec = run_records[0]
    unreported = run_rec.get("usage_unreported") or (run_rec.get("usage") or {}).get("usage_unreported")
    assert isinstance(unreported, list) and len(unreported) > 0, (
        f"expected usage_unreported on trace run record, got: {run_rec}"
    )
    assert {"input", "output"} <= set(unreported)
    assert set(unreported) <= USAGE_KEYS


# --- Outcomes, auth refusal & non-zero errors (points 8, 10) -----------------


@pytest.mark.parametrize(
    ("fixture", "stderr_text"),
    [
        ("missing_credential.jsonl", "dsh: MISSING_CREDENTIAL: DEEPSEEK_API_KEY is not set"),
        ("", "dsh: INVALID_CREDENTIAL: 401 Unauthorized authentication failed"),
    ],
    ids=["missing-credential", "http-401-auth"],
)
def test_missing_credential_and_auth_error_are_auth_refusals_that_close_lane(
    fake_dsh, tmp_path: Path, monkeypatch, fixture: str, stderr_text: str
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-rejected-test-key")
    fake_dsh.use(fixture, exit_code=1, stderr=stderr_text)
    ws = _workspace(tmp_path)

    reg = _registry_with_dsh(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert run.finish_reason == "refused"

        closed = reg.lane_state.closed("deepseek")
        assert closed is not None, f"expected auth refusal to close lane; hops={run.hops}"
        assert "auth" in str(closed.get("code") or "").lower()
    finally:
        reg.shutdown()

    # Also verify direct refusal() + router.close_until() contract.
    driver = _driver()
    cfg = _dsh_cfg()
    refusal = driver.refusal(
        cfg,
        [{"type": "result", "is_error": True, "subtype": "error", "error": stderr_text}],
    )
    assert refusal is not None
    assert "auth" in refusal.code.lower()
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is not None


def test_nonzero_exit_fails_with_dsh_stderr_diagnostic_without_closing_lane(
    fake_dsh, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    diag = "dsh: INTERNAL_ERROR: upstream stream closed"
    fake_dsh.use("nonzero_error.jsonl", exit_code=1, stderr=diag)
    ws = _workspace(tmp_path)

    reg = _registry_with_dsh(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert "dsh:" in (run.error or "")
        assert "upstream stream closed" in (run.error or "")
        assert reg.lane_state.closed("deepseek") is None
    finally:
        reg.shutdown()


def test_nonzero_exit_without_final_event_fails_with_dsh_stderr_diagnostic(
    fake_dsh, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dsh-key")
    diag = "dsh: profile load failed: unknown plugin"
    fake_dsh.use("", exit_code=1, stderr=diag)
    ws = _workspace(tmp_path)

    reg = _registry_with_dsh(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d", ws, provider="deepseek", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert "dsh:" in (run.error or "")
        assert "profile load failed" in (run.error or "")
        assert reg.lane_state.closed("deepseek") is None
    finally:
        reg.shutdown()


# --- Example config (point 9) ------------------------------------------------


def test_example_config_dsh_toml_exists_and_loads():
    assert EXAMPLE_CONFIG.is_file(), f"missing example config {EXAMPLE_CONFIG}"
    raw_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    # Never embed literal secret keys (AGENTS.md rule 1).
    assert not re.search(r"sk-[A-Za-z0-9]{10,}", raw_text)

    settings = config.load(extra=EXAMPLE_CONFIG)
    dsh_providers = [p for p in settings.providers.values() if p.driver == "dsh"]
    assert dsh_providers, f"no provider with driver = 'dsh' in {EXAMPLE_CONFIG}"

    cfg = dsh_providers[0]
    assert cfg.driver == "dsh"
    assert cfg.experimental is True
    assert "DEEPSEEK_API_KEY" in cfg.api_key_envs
    assert cfg.api_key_default is None
    if settings.default_provider:
        assert settings.provider(settings.default_provider).driver == "dsh"
