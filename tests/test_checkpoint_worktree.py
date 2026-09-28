"""Worktree setup cleans up its temporary parent if Git cannot add the tree."""

import subprocess

import pytest

from subagent.loop import checkpoint


def test_failed_worktree_add_removes_temp_parent_and_prunes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.com",
         "commit", "-qm", "initial"],
        check=True,
    )
    base = tmp_path / "delegate-wt-test"
    prune_calls = []
    real_git = checkpoint.git

    def mkdtemp(*, prefix):
        assert prefix == "delegate-wt-"
        base.mkdir()
        return str(base)

    def record_prune(repo, *args, **kwargs):
        if args[:2] == ("worktree", "prune"):
            prune_calls.append(True)
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(checkpoint.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(checkpoint, "git", record_prune)

    with pytest.raises(RuntimeError, match="invalid reference"):
        checkpoint.worktree_add(root, {"kind": "git", "commit": "missing-commit"})

    assert not base.exists()
    assert prune_calls == [True]
