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
        f'(deny file-write* (subpath "{tmp_path.resolve()}/second"))'
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
    ]


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
