"""Test protection: the worker may run tests but must not change them or the test configuration."""

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .common import SKIP_DIRS, changed_between, file_hash, hash_tree, matches, walk_files
from .detect import restore_config_fragment, test_config_fragments


class TestGuard:
    """Snapshot test files and test config before a worker run; after it, revert any change.

    Usage: guard = TestGuard(root, globs); guard.lock(); ...worker runs...; guard.release()
    """

    def __init__(self, root, globs, protected_paths=()):
        self.root = Path(root)
        self.globs = globs
        self.before = hash_tree(self.root)
        protected = {f for f in self.before if matches(f, globs)}
        root_path = self.root.resolve()
        self.links = {}
        self.link_targets = {}
        for raw in protected_paths:
            path = Path(raw)
            if not path.is_absolute():
                path = root_path / path
            try:
                rel = Path(os.path.abspath(path)).relative_to(root_path).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue
            if rel in self.before:
                protected.add(rel)
            if path.is_symlink():
                self.links[rel] = os.readlink(path)
                protected.add(rel)
                try:
                    target = path.resolve()
                    target_rel = target.relative_to(root_path).as_posix()
                except (OSError, RuntimeError, ValueError):
                    continue
                self.link_targets[rel] = target_rel
                if target_rel in self.before:
                    protected.add(target_rel)
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    continue
                try:
                    target_rel = path.resolve().relative_to(root_path).as_posix()
                except (OSError, RuntimeError, ValueError):
                    continue
                if target_rel not in protected:
                    continue
                rel = path.relative_to(root_path).as_posix()
                self.links[rel] = os.readlink(path)
                self.link_targets[rel] = target_rel
                protected.add(rel)
        self.protected = sorted(protected)
        self.fragments = test_config_fragments(self.root)
        self.snapshot = Path(tempfile.mkdtemp(prefix="subagent-snapshot-"))
        self.modes = {}
        self.released = False
        for rel in self.protected:
            if rel in self.links:
                continue
            dst = self.snapshot / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, dst)
            self.modes[rel] = (self.root / rel).stat().st_mode

    def protected_hash(self):
        """One hash for the whole protected set, to notice when the test owner changed the tests."""
        return _hash_items([
            (
                rel,
                hashlib.sha256(f"symlink\0{self.links[rel]}".encode()).hexdigest()
                if rel in self.links else self.before[rel],
            )
            for rel in self.protected
        ])

    def lock(self):
        for rel in self.protected:
            if rel in self.links:
                continue
            os.chmod(
                self.root / rel, self.modes[rel] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            )

    def release(self):
        """Restore protected files and test config. Returns (violations, changed_files).

        Idempotent: a second call (e.g. a loop safety net after an agent already
        released through pre_verify) reports the same result without touching the
        now-removed snapshot.
        """
        if self.released:
            return [], changed_between(self.before, hash_tree(self.root))
        self.released = True
        violations = []
        root = self.root.resolve()
        for rel in self.protected:
            path = root / rel
            if rel in self.links:
                if not path.is_symlink() or os.readlink(path) != self.links[rel]:
                    violations.append({"file": rel, "change": "modified"})
                    target_rel = self.link_targets.get(rel)
                    if target_rel and target_rel != rel:
                        violations.append({"file": target_rel, "change": "modified"})
                    _restore_protected_symlink(root, path, self.links[rel])
                continue
            safe_path = _safe_protected_path(root, path)
            exists = safe_path and path.exists()
            if not exists or not path.is_file() or file_hash(path) != self.before[rel]:
                violations.append(
                    {"file": rel, "change": "deleted" if safe_path and not exists else "modified"}
                )
                if exists and path.is_file():
                    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
                _restore_protected_file(root, path, self.snapshot / rel, self.modes[rel])
            else:
                os.chmod(path, self.modes[rel])
        after = hash_tree(self.root)
        for rel in sorted(set(after) - set(self.before)):
            if matches(rel, self.globs):
                violations.append({"file": rel, "change": "added"})
                (self.root / rel).unlink()
                after.pop(rel)
        now = test_config_fragments(self.root)
        for key, original in self.fragments.items():
            if now.get(key) != original:
                violations.append({"file": key, "change": "test config modified"})
                restore_config_fragment(self.root, key, original)
        if any(v["change"] == "test config modified" for v in violations):
            after = hash_tree(self.root)
        shutil.rmtree(self.snapshot, ignore_errors=True)
        return violations, changed_between(self.before, after)


def _safe_protected_path(root, path):
    """Check each component without following a worker-created symlink."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    current = root
    for part in parts:
        if current.is_symlink():
            return False
        current = current / part
        if current.is_symlink():
            return False
    return True


def _restore_protected_file(root, path, backup, mode):
    """Rebuild parent directories and replace the target without following symlinks."""
    rel = path.relative_to(root)
    parent = root
    for part in rel.parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            parent.unlink()
        parent.mkdir(exist_ok=True)
    target = parent / rel.parts[-1]
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    shutil.copy2(backup, target)
    os.chmod(target, mode)


def _restore_protected_symlink(root, path, link_target):
    rel = path.relative_to(root)
    parent = root
    for part in rel.parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            parent.unlink()
        parent.mkdir(exist_ok=True)
    target = parent / rel.parts[-1]
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.symlink_to(link_target)


def protected_paths(root, globs):
    """The protected files as paths relative to `root` (for the guard context)."""
    root = Path(root)
    return [rel for rel in walk_files(root) if matches(rel, globs)]


def protected_hash(root, globs):
    root = Path(root)
    return _hash_items(
        [(rel, file_hash(root / rel)) for rel in walk_files(root) if matches(rel, globs)]
    )


def _hash_items(items):
    digest = hashlib.sha256()
    for rel, h in sorted(items):
        digest.update(f"{rel}\0{h}\n".encode())
    return digest.hexdigest()
