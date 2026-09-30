from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from subagent.config import LoopSettings, LoopTarget, Settings, load
from subagent.loop.detect import feedback_output
from subagent.loop.loop import _prepare_tests, _tests_from_verification, build_prompt
from subagent.providers.antigravity import AntigravityProvider
from subagent.providers.base import ProviderConfig, Session
from subagent.providers.codex import CodexProvider
from subagent.providers.omp import OmpProvider


def test_knobs_defaults_preserve_existing_behavior():
    settings = LoopSettings()
    prompt = build_prompt("the plan", "", {"test_cmd": "pytest", "test_globs": []}, [])

    assert settings.test_output_cap is None
    assert settings.parallel_tool_calls is False
    assert settings.no_narration is False
    assert settings.delegate_test_writer is False
    assert "Issue independent model tool calls together" not in prompt
    assert "Do not narrate progress" not in prompt
    assert feedback_output("line\n" * 35) == "\n".join(["line"] * 30)


def test_knobs_test_output_cap_is_characters_and_keeps_full_log(tmp_path: Path):
    log_dir = tmp_path / ".subagent" / "logs"
    log_dir.mkdir(parents=True)
    output = "first line\n" + ("0123456789\n" * 20)
    verification = SimpleNamespace(
        exit_code=1,
        passed=False,
        command="pytest",
        counts=None,
        output=output,
    )

    tests = _tests_from_verification(
        verification, tmp_path / ".subagent", test_output_cap=17
    )

    assert len(tests["output_tail"]) == 17
    assert tests["output_tail"] == output[-17:]
    assert next(log_dir.glob("test-*.log")).read_text() == output


def test_knobs_compact_window_provider_overrides_core(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    session_root = tmp_path / "sessions"
    config.write_text(
        f"""[core]
default_provider = "claude"
compact_window = 5000
session_root = "{session_root}"

[providers.claude]
driver = "claude"
base_url = "http://127.0.0.1:1"
compact_window = 7000
"""
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SUBAGENT_CONFIG", raising=False)
    settings = load(extra=config)
    cfg = settings.providers["claude"]

    hooks_path = settings.hooks_config("agent-test", cfg)
    assert settings.compact_window == 5000
    assert cfg.compact_window == 7000
    assert json.loads(hooks_path.read_text())["autoCompactWindow"] == 7000


def test_knobs_parallel_tool_calls_batches_model_tools():
    prompt = build_prompt(
        "the plan", "", {"test_cmd": "pytest", "test_globs": []}, [],
        parallel_tool_calls=True,
    )

    assert "Issue independent model tool calls together in a single turn" in prompt
    assert "does not change concurrent workers" in prompt


def test_knobs_no_narration_changes_worker_prompt_only():
    info = {"test_cmd": "pytest", "test_globs": []}
    base = build_prompt("the plan", "", info, [])
    concise = build_prompt(
        "the plan", "", info, [], no_narration=True
    )

    assert "Do not narrate progress" not in base
    assert "Do not narrate progress" in concise
    assert "the plan" in concise


def test_knobs_delegate_test_writer_uses_configured_provider(monkeypatch, tmp_path: Path):
    target = LoopTarget(provider="writer-provider", model="writer-model")
    settings = SimpleNamespace(
        loop=SimpleNamespace(
            delegate_test_writer=True,
            review_tests=True,
            test_writer=target,
        )
    )
    server = SimpleNamespace(settings=settings)
    args = SimpleNamespace(
        plan=str(tmp_path / "plan.md"),
        parallel=None,
        test_outline=str(tmp_path / "outline.md"),
        tier="normal",
    )
    calls = []

    def write_outline(root, server, outline_path, plans, target=None):
        calls.append((outline_path, plans, target))
        return {"status": "done", "worker_credits": None, "_runs": []}

    monkeypatch.setattr("subagent.loop.loop.write_tests_from_outline", write_outline)
    monkeypatch.setattr(
        "subagent.loop.loop.do_test_review",
        lambda *args, **kwargs: {"worker_credits": None, "_runs": [], "issues": []},
    )

    hand_back, extras = _prepare_tests(tmp_path, server, args)

    assert hand_back is None
    assert extras["test_review"]["issues"] == []
    assert calls == [(str(tmp_path / "outline.md"), [args.plan], target)]

    settings.loop.delegate_test_writer = False
    hand_back, extras = _prepare_tests(tmp_path, server, args)

    assert hand_back is None
    assert extras["test_review"]["issues"] == []
    assert calls[-1] == (str(tmp_path / "outline.md"), [args.plan], None)


def test_knobs_thinking_maps_supported_native_values():
    root = Path(".")
    settings = Settings(workspace=root, session_root=root / ".sessions")

    omp_cfg = ProviderConfig(
        name="omp", driver="omp", vendor="local", thinking="low", effort="high"
    )
    omp_argv = OmpProvider().argv(
        omp_cfg, settings, "a", "prompt", root, Session(provider="omp"), None
    )
    assert omp_argv[omp_argv.index("--thinking") + 1] == "low"

    codex_cfg = ProviderConfig(
        name="codex", driver="codex", vendor="openai", thinking="low", effort="high"
    )
    codex_argv = CodexProvider().argv(
        codex_cfg, settings, "a", "prompt", root, Session(provider="codex"), None
    )
    assert "model_reasoning_effort=low" in codex_argv
    assert "model_reasoning_effort=high" not in codex_argv

    antigravity_cfg = ProviderConfig(
        name="antigravity", driver="antigravity", vendor="google",
        thinking="low", effort="high",
    )
    antigravity_argv = AntigravityProvider().argv(
        antigravity_cfg, settings, "a", "prompt", root, Session(provider="antigravity"), None
    )
    assert antigravity_argv[antigravity_argv.index("--effort") + 1] == "low"
