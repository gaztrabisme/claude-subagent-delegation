from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import smoke_deny_read as smoke
from subagent.runs import COMPLETED, Run

from .conftest import make_settings


@pytest.mark.parametrize("leak_source", [None, "event", "native_transcript"])
@pytest.mark.parametrize("edit_result", [None, "new", "old", "missing"])
def test_smoke_checks_all_events_and_native_transcripts_and_cleans_up(
    tmp_path: Path, monkeypatch, capsys, leak_source, edit_result,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    settings = make_settings(tmp_path)
    monkeypatch.setattr(smoke, "load", lambda _root: settings)
    monkeypatch.setattr(smoke, "load_provider_env", lambda: tmp_path / "keys.env")
    instances = []

    class Server:
        def __init__(self, session_root, settings):
            self.session_root = session_root
            self.registry = SimpleNamespace(create_agent=self.create_agent)
            self.stopped = False
            instances.append(self)

        def create_agent(self, *, on_event):
            self.capture = on_event

        def start(self):
            return self

        def delegate(self, *, provider, task, verification, workspace, deny_read, name):
            assert verification == "true"
            assert provider == "glm"
            assert "python3 leak.py" in task
            target = workspace / "target.txt"
            if edit_result is not None:
                assert "target.txt" in task and "old" in task and "new" in task
                assert target.read_text() == "old"
                if edit_result == "missing":
                    target.unlink()
                else:
                    target.write_text(edit_result)
            else:
                assert not target.exists()
            assert len(deny_read) == 1
            secret = Path(deny_read[0]) / "secret.txt"
            assert str(secret.resolve()) in task
            token = secret.read_text()
            self.registry.create_agent()
            self.capture({"early_tool_output": token if leak_source == "event" else "blocked"})
            self.session_root.mkdir()
            (self.session_root / "native.jsonl").write_text(json.dumps({
                "result": token if leak_source == "native_transcript" else "Operation not permitted",
            }))
            run = Run("r1", "a1", task, state=COMPLETED, event_count=1)
            run.done.set()
            return run

        def close_agent(self, agent_id):
            assert agent_id == "a1"

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(smoke, "InProcessServer", Server)

    args = ["--provider", "glm"] + (["--edit-check"] if edit_result is not None else [])
    expected_failure = bool(leak_source) or edit_result in ("old", "missing")
    assert smoke.main(args) == (1 if expected_failure else 0)
    output = capsys.readouterr().out
    assert f"token_in_output_or_transcript: {str(bool(leak_source)).lower()}" in output
    if edit_result is not None:
        assert f"edit_landed: {str(edit_result == 'new').lower()}" in output
    assert instances[0].stopped
    assert not instances[0].session_root.parent.exists()
    assert list((home / ".autoresearch").iterdir()) == []


def test_smoke_reports_setup_failure_without_claiming_no_leak(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(smoke, "load_provider_env", lambda: None)
    # A file at the parent path gives a deterministic setup error on every OS.
    (tmp_path / ".autoresearch").write_text("not a directory")
    assert smoke.main(["--provider", "glm"]) == 2
    output = capsys.readouterr().out
    assert "state=not_started" in output
    assert "token_in_output_or_transcript: unknown" in output
    assert "INCOMPLETE" in output
