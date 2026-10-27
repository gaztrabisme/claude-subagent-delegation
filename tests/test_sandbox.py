from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from subagent import sandbox


def test_profile_denies_reads_and_writes_for_each_realpath(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "session"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    profile = sandbox.build_profile(["~/alias", str(target), str(tmp_path / "second")])

    assert profile == (
        '(version 1)\n(allow default)\n'
        f'(deny file-read* (subpath "{target.resolve()}"))\n'
        f'(deny file-write* (subpath "{target.resolve()}"))\n'
        f'(deny file-read* (subpath "{tmp_path.resolve()}/second"))\n'
        f'(deny file-write* (subpath "{tmp_path.resolve()}/second"))\n'
        + "\n".join(
            f'(deny file-write* (literal "{parent}"))'
            for parent in target.resolve().parents if parent != Path("/")
        )
    )


def test_profile_escapes_sbpl_string_metacharacters(tmp_path: Path):
    path = tmp_path / 'a"b\\c\n\r\t☃'
    escaped = str(tmp_path.resolve()) + '/a\\"b\\\\c\\n\\r\\t☃'
    profile = sandbox.build_profile([str(path)])
    assert f'(deny file-read* (subpath "{escaped}"))' in profile
    assert f'(deny file-write* (subpath "{escaped}"))' in profile


def test_profile_write_allow_precedes_deny_read_and_resolves_paths(tmp_path: Path):
    allowed = tmp_path / 'allowed"\\\n\r\t☃'
    allowed.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(allowed, target_is_directory=True)
    denied = tmp_path / "private"
    escaped = str(tmp_path.resolve()) + '/allowed\\"\\\\\\n\\r\\t☃'

    profile = sandbox.build_profile([str(denied)], write_allow=[str(alias), str(allowed)])

    assert profile.splitlines() == [
        '(version 1)',
        '(allow default)',
        '(deny file-write* (subpath "/"))',
        f'(allow file-write* (subpath "{escaped}"))',
        f'(deny file-read* (subpath "{denied.resolve()}"))',
        f'(deny file-write* (subpath "{denied.resolve()}"))',
        *(f'(deny file-write* (literal "{parent}"))'
          for parent in denied.resolve().parents if parent != Path("/")),
    ]


def test_profile_locks_each_deny_read_ancestor_after_allows_without_blocking_siblings(tmp_path):
    first = tmp_path / "parent" / "session"
    second = tmp_path / "parent" / "other" / "session"
    profile = sandbox.build_profile([str(first), str(second)], write_allow=[str(tmp_path)])
    rules = profile.splitlines()
    ancestors = set(first.resolve().parents) | set(second.resolve().parents)
    ancestors.discard(Path("/"))
    for ancestor in ancestors:
        rule = f'(deny file-write* (literal "{ancestor}"))'
        assert rules.count(rule) == 1
        assert rules.index(rule) > 3  # after the write allow
        assert f'(deny file-write* (subpath "{ancestor}"))' not in rules
    assert '(deny file-write* (literal "/"))' not in rules


def test_profile_escapes_ancestor_literals(tmp_path):
    profile = sandbox.build_profile([str(tmp_path / 'parent"\\\n' / "session")])
    escaped = str(tmp_path.resolve()) + '/parent\\"\\\\\\n'
    assert f'(deny file-write* (literal "{escaped}"))' in profile


def test_profile_write_denies_protect_symlink_entry_and_target_after_allow(tmp_path):
    target = tmp_path / "source-auth.json"
    target.write_text("{}")
    link = tmp_path / "home" / "auth.json"
    link.parent.mkdir()
    link.symlink_to(target)
    profile = sandbox.build_profile([], write_allow=[str(tmp_path)], write_deny=[str(link)])
    rules = profile.splitlines()
    for path in (link, target):
        for kind in ("literal", "subpath"):
            rule = f'(deny file-write* ({kind} "{path}"))'
            assert rules.index(rule) > 3
    assert "deny file-read*" not in profile


def test_profile_write_denies_lock_ancestors_without_blocking_runtime_files(tmp_path):
    home = tmp_path / "codex-home"
    profile = sandbox.build_profile(
        [], write_allow=[str(home)], write_deny=[str(home / "hooks.json")],
    )
    rules = profile.splitlines()
    assert rules.index(f'(deny file-write* (literal "{home}"))') > 3
    assert f'(deny file-write* (subpath "{home}"))' not in rules
    assert "runtime.jsonl" not in profile


def test_profile_empty_write_allow_denies_all_writes():
    assert sandbox.build_profile([], write_allow=[]) == (
        '(version 1)\n(allow default)\n(deny file-write* (subpath "/"))'
    )


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x1b", "\x7f"])
def test_profile_rejects_unsupported_control_characters(tmp_path: Path, control):
    with pytest.raises(ValueError, match="control character"):
        sandbox.build_profile([str(tmp_path / ("session" + control))])


def test_profile_rejects_control_characters_in_symlink_target(tmp_path: Path):
    target = tmp_path / "session\x01"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="control character"):
        sandbox.build_profile([str(alias)])


def test_wrap_argv_on_macos_preserves_arguments(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    argv = ["python3", "script with spaces.py", '"quoted"']
    paths = [str(tmp_path / "private")]
    assert sandbox.wrap_argv(argv, paths) == [
        "/usr/bin/sandbox-exec", "-p", sandbox.build_profile(paths), *argv,
    ]
    assert argv == ["python3", "script with spaces.py", '"quoted"']


def test_wrap_argv_passes_write_allow_into_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    argv = ["python3", "worker.py"]
    denied = [str(tmp_path / "private")]
    allowed = [str(tmp_path)]
    assert sandbox.wrap_argv(argv, denied, write_allow=allowed) == [
        "/usr/bin/sandbox-exec", "-p", sandbox.build_profile(denied, write_allow=allowed), *argv,
    ]


def test_wrap_argv_passes_write_deny_into_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    denied = [str(tmp_path / "private")]
    writes = [str(tmp_path / "home" / "hooks.json")]
    argv = ["python3", "worker.py"]
    assert sandbox.wrap_argv(argv, denied, write_deny=writes) == [
        "/usr/bin/sandbox-exec", "-p", sandbox.build_profile(denied, write_deny=writes), *argv,
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_empty_deny_read_does_not_wrap(platform, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", platform)
    argv = ["python3", "worker.py"]
    assert sandbox.wrap_argv(argv, []) is argv


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_non_macos_deny_read_fails_closed(platform, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", platform)
    with pytest.raises(RuntimeError, match="deny_read.*macOS"):
        sandbox.wrap_argv(["python3", "worker.py"], [str(tmp_path)])


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_macos_sandbox_blocks_script_reads_and_writes_but_allows_other_files(
    tmp_path: Path, macos_sandbox_available,
):
    denied = tmp_path / 'denied"\\☃'
    denied.mkdir()
    secret = denied / "secret.txt"
    secret.write_text("hidden-token")
    outside = tmp_path / "outside.txt"
    outside.write_text("public")
    alias = tmp_path / "alias"
    alias.symlink_to(denied, target_is_directory=True)
    profile = sandbox.build_profile([str(alias)])

    def run(code):
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "python3", "-c", code],
            capture_output=True, text=True, timeout=10,
        )

    for path in (secret, alias / "secret.txt"):
        result = run(f"print(open({str(path)!r}).read())")
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr
        assert "hidden-token" not in result.stdout
    write = run(f"open({str(secret)!r}, 'w').write('changed')")
    assert write.returncode != 0
    assert "Operation not permitted" in write.stderr
    assert secret.read_text() == "hidden-token"
    readable = run(f"print(open({str(outside)!r}).read())")
    assert readable.returncode == 0, readable.stderr
    assert readable.stdout == "public\n"


@pytest.fixture
def macos_sandbox_available():
    try:
        probe = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        pytest.skip("sandbox-exec is unavailable")
    if probe.returncode != 0:
        pytest.skip(f"sandbox-exec cannot initialize in this environment: {probe.stderr.strip()}")


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_macos_write_allow_permits_inside_and_blocks_outside_and_denied_paths(
    tmp_path: Path, macos_sandbox_available,
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    denied = allowed / "denied"
    denied.mkdir()
    secret = denied / "secret.txt"
    secret.write_text("hidden")
    profile = sandbox.build_profile([str(denied)], write_allow=[str(allowed)])

    def run(code):
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )

    inside = allowed / "inside.txt"
    result = run(f"open({str(inside)!r}, 'w').write('new')")
    assert result.returncode == 0, result.stderr
    assert inside.read_text() == "new"
    outside = tmp_path / "outside.txt"
    for target in (outside, secret):
        result = run(f"open({str(target)!r}, 'w').write('changed')")
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr
    assert not outside.exists()
    assert secret.read_text() == "hidden"
    result = run(f"print(open({str(secret)!r}).read())")
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr
    assert "hidden" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_macos_deny_read_blocks_ancestor_rename_and_allows_sibling_writes(
    tmp_path: Path, macos_sandbox_available,
):
    denied = tmp_path / "parent" / "nested" / "session"
    denied.mkdir(parents=True)
    (denied / "secret.txt").write_text("hidden-token")
    profile = sandbox.build_profile([str(denied)], write_allow=[str(tmp_path)])
    for ancestor in (denied.parent, denied.parent.parent):
        renamed = ancestor.with_name(ancestor.name + "-renamed")
        secret = renamed / (denied / "secret.txt").relative_to(ancestor)
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c",
             f"import os; os.rename({str(ancestor)!r}, {str(renamed)!r}); "
             f"print(open({str(secret)!r}).read())"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr
        assert "hidden-token" not in result.stdout
        assert ancestor.is_dir() and not renamed.exists()
    sibling = denied.parent / "sibling.txt"
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c",
         f"open({str(sibling)!r}, 'w').write('edited')"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert sibling.read_text() == "edited"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_macos_write_deny_protects_metadata_and_symlinks_but_allows_workspace_edit(
    tmp_path: Path, macos_sandbox_available,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    metadata = workspace / ".git"
    metadata.mkdir()
    config = metadata / "config"
    config.write_text("original")
    hook = workspace / "hook.py"
    hook.write_text("original")
    protected = [hook]
    for name in ("hooks.json", "config.toml", "auth.json"):
        path = home / name
        path.write_text("original")
        protected.append(path)
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text("original")
    protected[-1].unlink()
    protected[-1].symlink_to(auth_source)
    profile = sandbox.build_profile(
        [str(tmp_path / "private")], write_allow=[str(tmp_path)],
        write_deny=[str(metadata), *(str(path) for path in protected)],
    )
    operations = [
        *(f"open({str(path)!r}, 'w').write('changed')" for path in [config, *protected, auth_source]),
        f"import os; os.unlink({str(protected[-1])!r})",
        f"import os; os.rename({str(metadata)!r}, {str(workspace / 'moved-git')!r})",
        f"import os; os.rename({str(home)!r}, {str(tmp_path / 'moved-home')!r})",
    ]
    for code in operations:
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0, code
        assert "Operation not permitted" in result.stderr
    assert all(path.read_text() == "original" for path in [config, *protected, auth_source])
    for target in (workspace / "target.txt", home / "runtime.jsonl"):
        target.write_text("old")
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c",
             f"open({str(target)!r}, 'w').write('new')"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert target.read_text() == "new"
