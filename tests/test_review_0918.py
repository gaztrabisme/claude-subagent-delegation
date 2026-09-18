"""Regression tests for wiki/review.md (the 2026-09-18 adversarial review of
750218f), one block per finding ID, using the review's own cases."""

from __future__ import annotations

import importlib.util
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from subagent_mcp.config import APPROVAL_HOOK
from subagent_mcp.guard import ALLOW, DENY, classify

HOME = Path.home()


def _hook_module():
    spec = importlib.util.spec_from_file_location("sam_hook_under_test", APPROVAL_HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HOOK = _hook_module()


@pytest.fixture
def ws() -> Iterator[Path]:
    """A workspace under $HOME, like a real one, with a subdirectory."""
    root = Path(tempfile.mkdtemp(dir=HOME, prefix=".sam-review-0918-")).resolve()
    (root / "sub").mkdir()
    yield root
    (root / "sub").rmdir()
    root.rmdir()


def _codex(tool_name: str, tool_input: dict, ws: Path, cwd: str | None = None) -> list[str]:
    """Every verdict action for a Codex call, as the hook asks them."""
    return [classify(name, args, ws, cwd if cwd is not None else str(ws)).action
            for name, args in HOOK.requests(tool_name, tool_input, True)]


# --- H2: ~/.codex and ~/.git-credentials are sensitive -------------------------------


@pytest.mark.parametrize("path", ["~/.codex/auth.json", "~/.codex/sessions",
                                  "~/.codex/sessions/2026/09/18/rollout.jsonl",
                                  "~/.git-credentials"])
def test_h2_cat_of_codex_login_and_git_credentials_is_denied(ws, path):
    verdict = classify("Bash", {"command": f"cat {path}"}, ws)
    assert verdict.action == DENY, verdict.reason


@pytest.mark.parametrize("path", [f"{HOME}/.codex/auth.json", "~/.codex/auth.json",
                                  f"{HOME}/.git-credentials", f"{HOME}/.codex/sessions"])
def test_h2_read_of_codex_login_and_git_credentials_is_denied(ws, path):
    assert classify("Read", {"file_path": path}, ws).action == DENY


def test_h2_controls_still_denied(ws):
    for path in ("~/.claude.json", "~/.omlx", "~/.aws/credentials", "~/.netrc", "~/.kube"):
        assert classify("Bash", {"command": f"cat {path}"}, ws).action == DENY, path


# --- H1: Codex workdir and the payload cwd reach the classifier -----------------------


def test_h1_exec_command_workdir_ssh_is_denied(ws):
    assert _codex("exec_command", {"cmd": "cat id_ed25519", "workdir": "~/.ssh"}, ws) == [DENY]


def test_h1_shell_workdir_codex_is_denied(ws):
    assert _codex("shell", {"command": ["cat", "auth.json"], "workdir": "~/.codex"}, ws) == [DENY]


def test_h1_control_absolute_ssh_path_is_denied(ws):
    assert _codex("exec_command", {"cmd": "cat ~/.ssh/id_ed25519"}, ws) == [DENY]


def test_h1_workdir_outside_the_workspace_is_denied(ws):
    assert _codex("exec_command", {"cmd": "ls", "workdir": "/etc"}, ws) == [DENY]
    assert _codex("exec_command", {"cmd": "ls", "workdir": "../"}, ws) == [DENY]
    assert _codex("exec_command", {"cmd": "ls", "workdir": "$HOME"}, ws) == [DENY]


def test_h1_workdir_inside_the_workspace_is_allowed_and_used(ws):
    assert _codex("exec_command", {"cmd": "ls", "workdir": "sub"}, ws) == [ALLOW]
    assert _codex("exec_command", {"cmd": "ls", "workdir": str(ws / "sub")}, ws) == [ALLOW]
    # Relative paths resolve against the workdir: `..` from sub is the workspace.
    verdict = classify("Bash", {"command": "cat ../x.txt", "workdir": "sub"}, ws)
    assert verdict.action == ALLOW, verdict.reason


def test_h1_payload_cwd_outside_the_workspace_is_denied(ws):
    ssh = str(HOME / ".ssh")
    assert classify("Bash", {"command": "cat id_ed25519"}, ws, ssh).action == DENY
    assert classify("Read", {"file_path": "id_ed25519"}, ws, ssh).action == DENY
    assert classify("Bash", {"command": "ls"}, ws, "/tmp").action == DENY
    assert classify("Bash", {"command": "ls"}, ws, str(ws)).action == ALLOW


def test_h1_hook_forwards_workdir():
    pairs = HOOK.requests("exec_command", {"cmd": "ls", "workdir": "/w/sub"}, True)
    assert pairs == [("Bash", {"command": "ls", "workdir": "/w/sub"})]


# --- H3: agent ids never repeat across server processes on one session root ---------


def test_h3_two_registries_on_one_session_root_never_share_an_agent_home(tmp_path):
    from dataclasses import replace

    from subagent_mcp.runs import Registry

    from .conftest import make_settings

    base = make_settings(tmp_path, trace="off")
    first = Registry(base, start_reaper=False)
    second = Registry(replace(base), start_reaper=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        a = first.create_agent("a", ws, lane="glm", fallback="none")
        b = second.create_agent("b", ws, lane="glm", fallback="none")
        assert a.agent_id != b.agent_id
        glm, deepseek = base.lanes["glm"], base.lanes["deepseek"]
        path_a = base.hooks_config(a.agent_id, glm)
        path_b = base.hooks_config(b.agent_id, deepseek)
        assert path_a != path_b
        assert glm.base_url in path_a.read_text()
        assert deepseek.base_url not in path_a.read_text()
        # Ids claimed by one process are skipped by the other even with equal tags.
        import itertools

        second._tag, second._counter = first._tag, itertools.count(1)
        c = second.create_agent("c", ws, lane="omlx", fallback="none")
        assert c.agent_id not in (a.agent_id, b.agent_id)
    finally:
        first.shutdown()
        second.shutdown()


# --- M1: refusal codes only on their own lane, 402 only as a status, 7-day cap --------


def _cli(text: str) -> list[dict]:
    from subagent_mcp.runs import exit_event

    cli = {"type": "result", "subtype": "success", "is_error": True, "result": text,
           "session_id": "s"}
    return [cli, exit_event(1, "", cli)]


def test_m1_omlx_timeout_402_seconds_is_not_a_balance_refusal():
    from subagent_mcp import router

    text = "API Error: 500 upstream request timed out after 402 s"
    assert router.classify_refusal("omlx", _cli(text)) is None
    assert router.classify_refusal("deepseek", _cli(text)) is None
    assert not router.no_retry(text)


def test_m1_bppc_402_payment_required_does_not_close_bppc():
    from subagent_mcp import router

    assert router.classify_refusal("bppc", _cli("402 Payment Required from proxy")) is None
    found = router.classify_refusal("deepseek", _cli("402 Payment Required from proxy"))
    assert found is not None and found.code == "deepseek_balance"
    assert router.classify_refusal(
        "deepseek", _cli('API Error: 402 {"error":{"message":"x"}}')).code == "deepseek_balance"


def test_m1_zai_codes_only_on_glm_and_codex_limit_only_on_codex():
    from subagent_mcp import router

    zai = "API Error: Request rejected (429) · [1308][Usage limit reached. reset at 2099-01-01 00:00:00]"
    assert router.classify_refusal("deepseek", _cli(zai)) is None
    assert router.classify_refusal("omlx", _cli(zai)) is None
    assert router.classify_refusal("glm", _cli(zai)).code == "zai_1308"
    codex = "You've hit your usage limit. Try again at 1:01 PM."
    assert router.classify_refusal("glm", _cli(codex)) is None
    assert router.classify_refusal("codex", codex).code == "codex_usage_limit"


def test_m1_glm_reset_2099_closes_at_most_seven_days():
    from datetime import datetime, timedelta

    from subagent_mcp import router

    zai = "API Error: Request rejected (429) · [1308][Usage limit reached. reset at 2099-01-01 00:00:00]"
    refusal = router.classify_refusal("glm", _cli(zai))
    now = datetime(2026, 9, 18, 12, 0).astimezone()
    until = router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15,
                               now=now)
    assert until == now + timedelta(days=7)


def test_m1_lane_state_caps_old_entries_and_reopens(tmp_path):
    import json
    from datetime import datetime, timedelta

    from subagent_mcp.lane_state import LaneState

    state = LaneState(tmp_path)
    state.close("glm", datetime(2099, 1, 1).astimezone(), "zai_1308", "m")
    entry = state.closed("glm")
    assert entry is not None
    assert entry["closed_until"] <= datetime.now().astimezone() + timedelta(days=7, seconds=5)
    # An entry written before the cap existed is capped on read from its set_at.
    set_at = datetime.now().astimezone() - timedelta(days=8)
    (tmp_path / "lane_state.json").write_text(json.dumps({"glm": {
        "closed_until": "2099-01-01T00:00:00+07:00", "code": "zai_1308", "message": "m",
        "set_at": set_at.isoformat()}}))
    assert state.closed("glm") is None
    state.close("deepseek", datetime.now().astimezone() + timedelta(hours=1), "deepseek_balance",
                "m")
    assert state.reopen("deepseek") is not None
    assert state.closed("deepseek") is None
    assert state.reopen("deepseek") is None


def test_m1_reopen_cli(tmp_path, capsys):
    import sys
    from datetime import datetime, timedelta

    from subagent_mcp.lane_state import LaneState

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import reopen_lane

    LaneState(tmp_path).close("glm", datetime.now().astimezone() + timedelta(hours=1),
                              "zai_1308", "m")
    assert reopen_lane.main(["--session-root", str(tmp_path)]) == 0
    assert "glm: closed until" in capsys.readouterr().out
    assert reopen_lane.main(["--session-root", str(tmp_path), "glm"]) == 0
    assert "glm: reopened" in capsys.readouterr().out
    assert LaneState(tmp_path).closed("glm") is None


# --- L4: one Codex reset parser; the year is optional ---------------------------------


def test_l4_codex_reset_without_a_year():
    from datetime import datetime

    from subagent_mcp import codex_driver, router

    now = datetime(2026, 9, 18, 12, 0).astimezone()
    text = "try again at Sep 20th 1:29 PM"
    assert router.codex_reset(text, now) == datetime(2026, 9, 20, 13, 29).astimezone()
    assert codex_driver.parse_reset(text, now) == router.codex_reset(text, now)
    # A date already past this year is next year's.
    assert router.codex_reset("try again at Jan 2nd 9:00 AM", now) == datetime(
        2027, 1, 2, 9, 0).astimezone()
    assert router.codex_reset("try again at Sep 20th, 2026 1:29 PM", now) == datetime(
        2026, 9, 20, 13, 29).astimezone()


# --- M3: bppc is the peer holding its tailnet IP, and only an RFC1918 address -------


def test_m3_crafted_peer_named_bppc_is_ignored():
    from subagent_mcp import health

    status = {"Peer": {
        "nodekey:evil": {"HostName": "bppc-evil", "TailscaleIPs": ["100.64.0.9"],
                         "CurAddr": "203.0.113.9:41641", "Addrs": ["192.168.1.66:41641"]},
        "nodekey:real": {"HostName": "bppc-System-Product-Name",
                         "TailscaleIPs": ["100.106.185.34"],
                         "CurAddr": "", "Addrs": ["203.0.113.50:41641", "192.168.1.17:41641"]},
    }}
    assert health.bppc_lan_from_tailscale(status) == "192.168.1.17"
    assert health.bppc_hosts({}, status)[0] == "192.168.1.17"


def test_m3_public_endpoint_is_never_used():
    from subagent_mcp import health

    status = {"Peer": {"nodekey:real": {
        "HostName": "bppc", "TailscaleIPs": ["100.106.185.34"],
        "CurAddr": "203.0.113.9:41641", "Addrs": ["100.106.185.34:41641", "8.8.8.8:1"]}}}
    assert health.bppc_lan_from_tailscale(status) is None
    assert health.bppc_lan_from_tailscale({"Peer": {"n": {
        "HostName": "x", "TailscaleIPs": ["100.106.185.34"], "CurAddr": "10.1.2.3:4"}}}) == "10.1.2.3"
    assert health.bppc_lan_from_tailscale({"Peer": {"n": {
        "HostName": "x", "TailscaleIPs": ["100.106.185.34"], "CurAddr": "172.32.0.1:4"}}}) is None


# --- M8: omlx cold_load is about the lane's model, not the server-wide count ---------


def test_m8_omlx_cold_when_its_model_is_not_loaded(monkeypatch, mock_endpoint):
    from dataclasses import replace

    from subagent_mcp import health
    from subagent_mcp.lanes import load_lanes

    monkeypatch.setenv("SAM_OMLX_API_KEY", "k")
    lane = load_lanes({"SAM_OMLX_BASE_URL": mock_endpoint.url("omlx")})["omlx"]
    lane = replace(lane, health_url=f"{mock_endpoint.url('omlx')}/api/status")
    mock_endpoint.routes["/omlx/api/status"] = (200, {
        "status": "ok", "models_loaded": 1, "loaded_models": ["some-other-model"]})
    assert health.check(lane).cold_load is True
    mock_endpoint.routes["/omlx/api/status"] = (200, {
        "status": "ok", "models_loaded": 1, "loaded_models": [lane.model]})
    assert health.check(lane).cold_load is False
    # A server that does not list models falls back to the count.
    assert health.omlx_cold({"models_loaded": 0}, lane.model) is True
    assert health.omlx_cold({"models_loaded": 2}, lane.model) is False
