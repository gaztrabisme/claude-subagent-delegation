"""Test protection: the worker may run tests but must not change them or the test configuration."""

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .common import changed_between, file_hash, hash_tree, matches, walk_files
from .detect import restore_config_fragment, test_config_fragments


class TestGuard:
    """Snapshot test files and test config before a worker run; after it, revert any change.

    Usage: guard = TestGuard(root, globs); guard.lock(); ...worker runs...; guard.release()
    """

    def __init__(self, root, globs):
        self.root = Path(root)
        self.globs = globs
        self.before = hash_tree(self.root)
        self.protected = sorted(f for f in self.before if matches(f, globs))
        self.fragments = test_config_fragments(self.root)
        self.snapshot = Path(tempfile.mkdtemp(prefix="delegate-snapshot-"))
        self.modes = {}
        for rel in self.protected:
            dst = self.snapshot / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, dst)
            self.modes[rel] = (self.root / rel).stat().st_mode

    def protected_hash(self):
        """One hash for the whole protected set, to notice when the test owner changed the tests."""
        return _hash_items([(rel, self.before[rel]) for rel in self.protected])

    def lock(self):
        for rel in self.protected:
            os.chmod(self.root / rel, self.modes[rel] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def release(self):
        """Restore protected files and test config. Returns (violations, changed_files)."""
        violations = []
        for rel in self.protected:
            path = self.root / rel
            if not path.exists() or file_hash(path) != self.before[rel]:
                violations.append({"file": rel, "change": "deleted" if not path.exists() else "modified"})
                if path.exists():
                    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.snapshot / rel, path)
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


def protected_hash(root, globs):
    root = Path(root)
    return _hash_items([(rel, file_hash(root / rel)) for rel in walk_files(root) if matches(rel, globs)])


def _hash_items(items):
    digest = hashlib.sha256()
    for rel, h in sorted(items):
        digest.update(f"{rel}\0{h}\n".encode())
    return digest.hexdigest()
