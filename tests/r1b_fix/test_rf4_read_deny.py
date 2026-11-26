"""RF4 (F4): verification honours provider deny_read and other agents' session dirs."""

from __future__ import annotations

import sys

import pytest

from tests.conftest import make_settings
from tests.r1b_fix._helpers import HEADER, run_script

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_SCRIPT = HEADER + """
for label in ("denied_file", "agent_key"):
    try:
        R[label] = {"content": Path(T[label]).read_text(encoding="utf-8")}
    except PermissionError as exc:
        R[label] = {"err": f"PermissionError:{exc.errno}"}
    except OSError as exc:
        R[label] = {"err": f"OSError:{exc.errno}"}
done()
print(R)
"""

_KEY = "RF4-APPROVAL-KEY-VALUE"
_SECRET = "RF4-SECRET-CONTENT"


def _setup(tmp_path):
    secret = tmp_path / "outside_secret.txt"
    secret.write_text(_SECRET, encoding="utf-8")
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "m",
            "api_key_env": "GLM_API_KEY",
            "vendor": "zai",
            "deny_read": [str(secret)],
        }
    }
    settings = make_settings(tmp_path, providers=providers, verify_timeout=15.0)
    agent_dir = settings.session_root / "agents" / "other-agent"
    agent_dir.mkdir(parents=True)
    key = agent_dir / "approval.key"
    key.write_text(_KEY, encoding="utf-8")
    return settings, secret, key


def test_rf4_deny_read_file_is_unreadable(tmp_path) -> None:
    """RF4: a file listed in a provider's deny_read cannot be read by a verification command."""
    settings, secret, key = _setup(tmp_path)
    _, result, data = run_script(
        tmp_path, _SCRIPT, settings, {"denied_file": str(secret), "agent_key": str(key)}
    )
    assert data["denied_file"].get("err", "").startswith("PermissionError"), data
    assert _SECRET not in result.output


def test_rf4_other_agents_session_files_are_unreadable(tmp_path) -> None:
    """RF4: <session_root>/agents/<other>/approval.key cannot be read by a verification command."""
    settings, secret, key = _setup(tmp_path)
    _, result, data = run_script(
        tmp_path, _SCRIPT, settings, {"denied_file": str(secret), "agent_key": str(key)}
    )
    assert data["agent_key"].get("err", "").startswith("PermissionError"), data
    assert _KEY not in result.output
