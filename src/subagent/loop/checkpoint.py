"""Checkpoints of the project's working tree, taken before every worker round, so a round can be undone.

In a git repository a checkpoint is a commit object built from a temporary index (the user's branch,
index and stash are never touched), kept alive by a ref under refs/subagent/checkpoints/. Outside git
it is a tarball in .subagent/checkpoints/. Ignored files (.gitignore, or SKIP_DIRS without git) and
.subagent/ itself are not part of checkpoints.
"""

import hashlib
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from .common import STATE_DIR, file_hash, git, git_prefix, read_json, walk_files, write_json

INDEX_FILE = "checkpoints.json"


def _records(root):
    return read_json(root / STATE_DIR / INDEX_FILE) or []


def _save_records(root, records):
    write_json(root / STATE_DIR / INDEX_FILE, records)


def _snapshot_tree(root):
    """Write the current working tree (minus ignored files) as a git tree and return its hash."""
    real_index = Path(root) / git(root, "rev-parse", "--git-path", "index")
    tmp = Path(tempfile.mkdtemp(prefix="delegate-index-")) / "index"
    if real_index.exists():
        shutil.copy2(real_index, tmp)  # reuse the stat cache: much faster on big repos
    env = {"GIT_INDEX_FILE": str(tmp)}
    git(root, "add", "-A", "--", ".", env=env)
    tree = git(root, "write-tree", env=env)
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return tree


def create(root, label):
    root = Path(root)
    record = {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "label": label,
              "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if git_prefix(root) is not None:
        tree = _snapshot_tree(root)
        parent = git(root, "rev-parse", "-q", "--verify", "HEAD", check=False)
        args = ["commit-tree", tree, "-m", f"delegate checkpoint: {label}"] + (["-p", parent] if parent else [])
        commit = git(root, *args)
        git(root, "update-ref", f"refs/subagent/checkpoints/{record['id']}", commit)
        record.update(kind="git", commit=commit)
    else:
        archive = root / STATE_DIR / "checkpoints" / f"{record['id']}.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as tar:
            for rel in walk_files(root):
                tar.add(root / rel, arcname=rel)
        record.update(kind="tar", archive=str(archive.relative_to(root)))
    records = _records(root)
    records.append(record)
    _save_records(root, records)
    return record


def list_all(root):
    return _records(Path(root))


def get(root, checkpoint_id=None):
    records = _records(Path(root))
    if not records:
        return None
    if checkpoint_id is None:
        return records[-1]
    return next((r for r in records if r["id"] == checkpoint_id or r["id"].startswith(checkpoint_id)), None)


def _tar_members(root, record):
    with tarfile.open(Path(root) / record["archive"], "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def changes(root, record):
    """[(status, path)] from the checkpoint to now: A = added since, M = modified, D = deleted."""
    root = Path(root)
    if record["kind"] == "git":
        tree = _snapshot_tree(root)
        out = git(root, "diff", "--name-status", "--no-renames", "--relative", record["commit"], tree)
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]
    members = {name: hashlib.sha256(data).hexdigest() for name, data in _tar_members(root, record).items()}
    current = {rel: file_hash(root / rel) for rel in walk_files(root)}
    result = [("A", p) for p in current if p not in members]
    result += [("D", p) for p in members if p not in current]
    result += [("M", p) for p in current if p in members and members[p] != current[p]]
    return sorted(result, key=lambda x: x[1])


def file_at(root, record, path):
    """Content of a file at the checkpoint, or None if it did not exist."""
    if record["kind"] == "git":
        try:
            return git(root, "show", f"{record['commit']}:./{path}", text=False)
        except RuntimeError:
            return None
    return _tar_members(root, record).get(path)


def restore(root, record):
    """Make the working tree match the checkpoint again. Returns the list of (status, path) undone."""
    root = Path(root)
    diff = changes(root, record)
    for status, rel in diff:
        path = root / rel
        if status == "A":
            path.unlink(missing_ok=True)
            _remove_empty_dirs(root, path.parent)
    to_restore = [rel for status, rel in diff if status != "A"]
    if record["kind"] == "git" and to_restore:
        git(root, "restore", f"--source={record['commit']}", "--worktree", "--", *to_restore)
    elif to_restore:
        members = _tar_members(root, record)
        for rel in to_restore:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                os.chmod(path, 0o644)
            path.write_bytes(members[rel])
    return diff


def _remove_empty_dirs(root, directory):
    while directory != root and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


def prune(root, keep):
    root = Path(root)
    records = _records(root)
    old, kept = records[:-keep] if keep else [], records[-keep:] if keep else records
    for record in old:
        if record["kind"] == "git":
            git(root, "update-ref", "-d", f"refs/subagent/checkpoints/{record['id']}", check=False)
        else:
            (root / record["archive"]).unlink(missing_ok=True)
    if old:
        _save_records(root, kept)


# ---------------------------------------------------------------- worktrees (parallel rounds)

def worktree_add(root, record):
    """Check the checkpoint out in a temporary worktree outside the project.

    Returns (worktree_dir, project_root_inside_it). Dependency dirs are symlinked in by the caller.
    """
    if record["kind"] != "git":
        raise RuntimeError("worktrees need a git repository")
    base = Path(tempfile.mkdtemp(prefix="delegate-wt-"))
    worktree = base / "wt"
    git(root, "worktree", "add", "--detach", str(worktree), record["commit"])
    return worktree, worktree / (git_prefix(root) or "")


def worktree_remove(root, worktree):
    git(root, "worktree", "remove", "--force", str(worktree), check=False)
    shutil.rmtree(Path(worktree).parent, ignore_errors=True)
    git(root, "worktree", "prune", check=False)

