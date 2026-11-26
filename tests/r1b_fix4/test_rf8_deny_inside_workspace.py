"""R1b fix round 4 (G4, G5): read denies inside the workspace and the codex auth file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from subagent.verify import run_verification
from tests.conftest import make_settings
from tests.r1b_fix._helpers import HEADER

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_SECRET = "G4-INSIDE-WORKSPACE-SECRET"
_CODEX_SECRET = "G5-CODEX-AUTH-SECRET"

_READ_SCRIPT = HEADER + """
for label, target in T["reads"].items():
    try:
        R[label] = {"content": Path(target).read_text(encoding="utf-8")}
    except PermissionError as exc:
        R[label] = {"err": f"PermissionError:{exc.errno}"}
    except OSError as exc:
        R[label] = {"err": f"OSError:{exc.errno}"}
try:
    Path("own.txt").write_text("ok", encoding="utf-8")
    R["own_write"] = "ok"
except OSError as exc:
    R["own_write"] = f"err:{exc.errno}"
done()
print(R)
"""


def _run(ws: Path, settings, reads: dict[str, str]):
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "probe.py").write_text(_READ_SCRIPT, encoding="utf-8")
    (ws / "targets.json").write_text(json.dumps({"reads": reads}), encoding="utf-8")
    result = run_verification("python3 probe.py", ws, settings)
    out = ws / "out.json"
    data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    return result, data


def _deny_settings(tmp_path, denied: Path):
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "m",
            "api_key_env": "GLM_API_KEY",
            "vendor": "zai",
            "deny_read": [str(denied)],
        }
    }
    return make_settings(tmp_path, providers=providers, verify_timeout=15.0)


def test_rf8_g4a_deny_read_file_inside_workspace_is_unreadable(tmp_path) -> None:
    """RF8-G4a: a provider deny_read file inside the workspace is unreadable by a verification command, while other workspace files stay readable and writable."""
    ws = tmp_path / "ws"
    (ws / "secrets").mkdir(parents=True)
    secret = ws / "secrets" / "k.txt"
    secret.write_text(_SECRET, encoding="utf-8")
    other = ws / "readme.txt"
    other.write_text("plain", encoding="utf-8")
    settings = _deny_settings(tmp_path, secret)
    result, data = _run(ws, settings, {"secret": str(secret), "other": str(other)})
    assert data.get("secret", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _SECRET not in result.output
    assert data.get("other", {}).get("content") == "plain", data
    assert data.get("own_write") == "ok", data


def test_rf8_g4b_guard_secret_file_inside_workspace_is_unreadable(tmp_path) -> None:
    """RF8-G4b: an epi token_file (guard secret file) placed inside the workspace is unreadable by a verification command."""
    ws = tmp_path / "ws"
    (ws / "private").mkdir(parents=True)
    token = ws / "private" / "caller.token"
    token.write_text(_SECRET, encoding="utf-8")
    other = ws / "readme.txt"
    other.write_text("plain", encoding="utf-8")
    providers = {
        "epi": {
            "driver": "epi",
            "binary": "/nonexistent/epi",
            "token_file": str(token),
            "project_root": str(tmp_path / "proj"),
            "experimental": True,
            "local": True,
        }
    }
    settings = make_settings(tmp_path, providers=providers, verify_timeout=15.0)
    result, data = _run(ws, settings, {"token": str(token), "other": str(other)})
    assert data.get("token", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _SECRET not in result.output
    assert data.get("other", {}).get("content") == "plain", data
    assert data.get("own_write") == "ok", data


def test_rf8_g4c_deny_inside_workspace_survives_covering_deny(tmp_path) -> None:
    """RF8-G4c: with deny_read D covering the workspace D/proj, a deny_read file D/proj/secrets/k.txt inside the workspace is still unreadable (the re-open does not undo it)."""
    denied = tmp_path / "denied"
    ws = denied / "proj"
    (ws / "secrets").mkdir(parents=True)
    secret = ws / "secrets" / "k.txt"
    secret.write_text(_SECRET, encoding="utf-8")
    other = ws / "readme.txt"
    other.write_text("plain", encoding="utf-8")
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "m",
            "api_key_env": "GLM_API_KEY",
            "vendor": "zai",
            "deny_read": [str(denied), str(secret)],
        }
    }
    settings = make_settings(tmp_path, providers=providers, verify_timeout=15.0)
    result, data = _run(ws, settings, {"secret": str(secret), "other": str(other)})
    assert data.get("secret", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _SECRET not in result.output
    assert data.get("other", {}).get("content") == "plain", data
    assert data.get("own_write") == "ok", data


def test_rf8_g5_codex_auth_file_is_unreadable(tmp_path, monkeypatch) -> None:
    """RF8-G5: with codex configured and CODEX_HOME at a tmp home outside the workspace, a verification command cannot read <codex home>/auth.json (codex declares its auth file as a guard secret file)."""
    home = tmp_path / "codex_owner_home"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text(_CODEX_SECRET, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    providers = {"codex": {"driver": "codex", "binary": "/nonexistent/codex"}}
    settings = make_settings(tmp_path, providers=providers, verify_timeout=15.0)
    result, data = _run(tmp_path / "ws", settings, {"auth": str(auth)})
    assert data.get("auth", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _CODEX_SECRET not in result.output
    assert data.get("own_write") == "ok", data
