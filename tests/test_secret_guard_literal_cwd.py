"""The process directory is a literal path, even when its name looks like a glob."""

import pytest

from subagent.guard import classify


@pytest.mark.parametrize("directory", ["project[ab]", "project{a,b}", "project*data",
                                       "project?data", "project\ue000"])
def test_secret_guard_treats_submission_directory_as_literal(tmp_path, monkeypatch, directory):
    workspace = tmp_path / directory
    workspace.mkdir()
    token = workspace / "caller.txt"
    token.write_text("test-token-canary\n")
    (workspace / "public.py").touch()
    monkeypatch.setattr(classify, "_provider_secret_files", (token,))
    monkeypatch.setattr(classify, "_provider_homes", ())
    denied = classify.classify_bash("cat *.txt", workspace)
    assert denied.action == "deny", denied
    allowed = classify.classify_bash("cat *.py", workspace)
    assert allowed.action == "allow", allowed
