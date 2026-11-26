"""B1 acceptance tests: verification commands run without the server's secrets."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from subagent.goal import parse_goal
from subagent.goal_runtime import evaluate_goal
from subagent.verify import run_verification
from tests.conftest import make_settings

_DUMP_ENV_SCRIPT = """\
import json
import os
from pathlib import Path

Path("env.json").write_text(json.dumps(dict(os.environ), sort_keys=True), encoding="utf-8")
"""


def test_b1_run_verification_strips_server_secrets_and_non_allowlisted_env(
    tmp_path: Path, monkeypatch
) -> None:
    """B1.1: run_verification uses Settings.base_child_env(), hiding server secrets and unallowlisted vars."""
    canary = f"canary-{secrets.token_hex(12)}"
    glm_key = f"glm-{secrets.token_hex(12)}"
    aws_secret = f"aws-{secrets.token_hex(12)}"
    extra_var = f"extra-{secrets.token_hex(12)}"
    safe_pass = f"pass-{secrets.token_hex(6)}"

    monkeypatch.setenv("SECRET_CANARY_TOKEN", canary)
    monkeypatch.setenv("GLM_API_KEY", glm_key)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", aws_secret)
    monkeypatch.setenv("UNALLOWLISTED_SERVER_VAR", extra_var)
    monkeypatch.setenv("SAFE_VERIFY_PASSTHROUGH", safe_pass)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dump_env.py").write_text(_DUMP_ENV_SCRIPT, encoding="utf-8")

    settings = make_settings(tmp_path, child_env_passthrough=["SAFE_VERIFY_PASSTHROUGH"])
    result = run_verification("python3 dump_env.py", ws, settings)
    assert result.passed is True, result.reason

    raw = (ws / "env.json").read_text(encoding="utf-8")
    env = json.loads(raw)

    for name, val in (
        ("SECRET_CANARY_TOKEN", canary),
        ("GLM_API_KEY", glm_key),
        ("AWS_SECRET_ACCESS_KEY", aws_secret),
        ("UNALLOWLISTED_SERVER_VAR", extra_var),
    ):
        assert name not in env, f"{name} leaked into verification env"
        assert name not in raw, f"{name} name leaked into verification env output"
        assert val not in raw, f"{name} value leaked into verification env output"

    assert env.get("SAFE_VERIFY_PASSTHROUGH") == safe_pass


def test_b1_goal_command_row_strips_server_secrets(tmp_path: Path, monkeypatch) -> None:
    """B1.2: goal_runtime command rows run through run_verification without server secrets."""
    canary = f"canary-{secrets.token_hex(12)}"
    glm_key = f"glm-{secrets.token_hex(12)}"

    monkeypatch.setenv("SECRET_CANARY_TOKEN", canary)
    monkeypatch.setenv("GLM_API_KEY", glm_key)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dump_env.py").write_text(_DUMP_ENV_SCRIPT, encoding="utf-8")

    goal = parse_goal(
        'Execute plan "B1 goal" (plan.md). Goal rows:\n1 envcheck: `python3 dump_env.py`\n',
        ws,
    )
    settings = make_settings(tmp_path)
    rows = evaluate_goal(goal, ws, settings)

    assert len(rows) == 1
    assert rows[0]["passed"] is True, rows[0]

    raw = (ws / "env.json").read_text(encoding="utf-8")
    env = json.loads(raw)
    for name, val in (("SECRET_CANARY_TOKEN", canary), ("GLM_API_KEY", glm_key)):
        assert name not in env, f"{name} leaked into goal command row env"
        assert name not in raw, f"{name} name leaked into goal command row env"
        assert val not in raw, f"{name} value leaked into goal command row env"
