"""R1b fix round 3 (G1, G2, G3): verification read denies and private temp."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subagent.verify import run_verification
from tests.conftest import make_settings
from tests.r1b_fix._helpers import HEADER

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_DSH_SECRET = "G1-DSH-CREDENTIAL-CONTENT"
_EPI_SECRET = "G1-EPI-TOKEN-CONTENT"
_SIBLING_SECRET = "G2-SIBLING-SECRET-CONTENT"

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
try:
    import os
    p = Path(os.environ["TMPDIR"]) / "t.txt"
    p.write_text("ok", encoding="utf-8")
    R["tmp_write"] = "ok"
except OSError as exc:
    R["tmp_write"] = f"err:{exc.errno}"
try:
    R["cwd"] = str(Path.cwd())
except OSError as exc:
    R["cwd"] = f"err:{exc.errno}"
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


def test_rf7_g1_dsh_credentials_file_is_unreadable(tmp_path, monkeypatch) -> None:
    """RF7-G1a: the owner DSH home's .credentials.yaml (declared by the dsh provider) cannot be read by a verification command."""
    home = tmp_path / "dsh_owner_home"
    home.mkdir()
    cred = home / ".credentials.yaml"
    cred.write_text(_DSH_SECRET, encoding="utf-8")
    monkeypatch.setenv("DSH_OWNER_HOME", str(home))
    providers = {"deepseek": {"driver": "dsh", "model": "m", "experimental": True}}
    settings = make_settings(tmp_path, providers=providers, verify_timeout=15.0)
    result, data = _run(tmp_path / "ws", settings, {"cred": str(cred)})
    assert data.get("cred", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _DSH_SECRET not in result.output
    assert data.get("own_write") == "ok", data


def test_rf7_g1_epi_token_file_is_unreadable(tmp_path) -> None:
    """RF7-G1b: an epi token_file declared as a secret file cannot be read by a verification command."""
    token = tmp_path / "private" / "caller.token"
    token.parent.mkdir()
    token.write_text(_EPI_SECRET, encoding="utf-8")
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
    result, data = _run(tmp_path / "ws", settings, {"token": str(token)})
    assert data.get("token", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _EPI_SECRET not in result.output
    assert data.get("own_write") == "ok", data


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


def test_rf7_g2_deny_read_containing_workspace_still_hides_siblings(tmp_path) -> None:
    """RF7-G2a: with deny_read D containing the workspace D/proj, the command reads/writes its workspace and temp but cannot read D/other-project/secret.txt."""
    denied = tmp_path / "denied"
    other = denied / "other-project"
    other.mkdir(parents=True)
    secret = other / "secret.txt"
    secret.write_text(_SIBLING_SECRET, encoding="utf-8")
    ws = denied / "proj"
    settings = _deny_settings(tmp_path, denied)
    result, data = _run(ws, settings, {"sibling": str(secret)})
    assert data.get("sibling", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _SIBLING_SECRET not in result.output
    assert data.get("own_write") == "ok", data
    assert data.get("tmp_write") == "ok", data
    assert data.get("cwd") == str(ws.resolve()), data
    assert result.passed is True, result.reason


def test_rf7_g2_agents_dir_containing_workspace_still_hides_other_agents(tmp_path) -> None:
    """RF7-G2b: with the workspace under <session_root>/agents, another agent's file there is still unreadable while own workspace and temp work."""
    settings = make_settings(tmp_path, verify_timeout=15.0)
    agents = Path(settings.session_root) / "agents"
    other = agents / "other-agent"
    other.mkdir(parents=True)
    secret = other / "approval.key"
    secret.write_text(_SIBLING_SECRET, encoding="utf-8")
    ws = agents / "mine" / "ws"
    result, data = _run(ws, settings, {"sibling": str(secret)})
    assert data.get("sibling", {}).get("err", "").startswith("PermissionError"), (data, result.reason)
    assert _SIBLING_SECRET not in result.output
    assert data.get("own_write") == "ok", data
    assert data.get("tmp_write") == "ok", data
    assert result.passed is True, result.reason


_TEMP_SCRIPT = HEADER + """
import os, time
E = Path(T["shared"])
def attempt(path):
    try:
        path.write_text("x", encoding="utf-8")
        return "ok"
    except PermissionError as exc:
        return f"PermissionError:{exc.errno}"
    except OSError as exc:
        return f"OSError:{exc.errno}"
R["direct"] = attempt(E / "direct.txt")
sib = E / "sibling"
deadline = time.monotonic() + 8
while not sib.is_dir() and time.monotonic() < deadline:
    time.sleep(0.1)
R["sibling_seen"] = sib.is_dir()
R["sibling_write"] = attempt(sib / "x.txt")
R["own_tmp"] = os.environ["TMPDIR"]
R["own_write"] = attempt(Path(os.environ["TMPDIR"]) / "own.txt")
done()
print(R)
"""


def test_rf7_g3_empty_shared_temp_is_not_writable(tmp_path, monkeypatch) -> None:
    """RF7-G3: with the server TMPDIR at an empty dir E, a command cannot create a file in E or in a sibling made in E during the call, but can write its own TMPDIR."""
    ws = tmp_path / "ws"
    ws.mkdir()
    shared = tmp_path / "empty_shared_tmp"
    shared.mkdir()
    monkeypatch.setenv("TMPDIR", str(shared))
    monkeypatch.setattr(tempfile, "tempdir", None)
    (ws / "probe.py").write_text(_TEMP_SCRIPT, encoding="utf-8")
    (ws / "targets.json").write_text(json.dumps({"shared": str(shared)}), encoding="utf-8")
    settings = make_settings(tmp_path, verify_timeout=20.0)

    def make_sibling() -> None:
        time.sleep(1.0)
        (shared / "sibling").mkdir()

    thread = threading.Thread(target=make_sibling, daemon=True)
    thread.start()
    result = run_verification("python3 probe.py", ws, settings)
    thread.join(timeout=5)
    data = json.loads((ws / "out.json").read_text(encoding="utf-8"))
    assert data["direct"].startswith("PermissionError"), data
    assert data["sibling_seen"] is True, data
    assert data["sibling_write"].startswith("PermissionError"), data
    assert data["own_write"] == "ok", data
    assert not (shared / "direct.txt").exists()
    assert not (shared / "sibling" / "x.txt").exists()
    assert result.passed is True, result.reason
