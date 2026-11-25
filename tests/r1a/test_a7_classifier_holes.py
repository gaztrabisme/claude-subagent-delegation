"""A7 acceptance tests: executed classifier holes return DENY."""

from __future__ import annotations

from pathlib import Path

import pytest

from subagent.guard.classify import DENY, classify, classify_bash


def _setup_env(tmp_path: Path, monkeypatch, *, with_secret_file: bool = False):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    (home / ".ssh" / "config").write_text("Host *\n", encoding="utf-8")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    ws.mkdir()

    (ws / "link_ssh").symlink_to(home / ".ssh" / "config")
    (ws / "linkdir").symlink_to(home / ".ssh")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    ctx: dict[str, list[str]] = {
        "secret_env_names": ["SOME_SECRET_NAME"],
        "secret_files": [],
        "provider_homes": [],
    }
    if with_secret_file:
        secret = ws / "secret.tok"
        secret.write_text("token-secret\n", encoding="utf-8")
        ctx["secret_files"] = [str(secret)]
    return home, ws, ctx


def _assert_deny(command: str, ws: Path, ctx: dict) -> None:
    v1 = classify_bash(command, ws, context=ctx)
    v2 = classify("Bash", {"command": command}, ws, context=ctx)
    assert v1.action == DENY, f"classify_bash({command!r}) returned {v1.action}: {v1.reason}"
    assert v2.action == DENY, f"classify(Bash, {command!r}) returned {v2.action}: {v2.reason}"


@pytest.mark.parametrize(
    "command",
    [
        "cat link_ssh",
        "cat linkdir/config",
        "cat linkdir/id_rsa",
    ],
)
def test_a7_1_workspace_symlink_to_ssh_is_denied(tmp_path: Path, monkeypatch, command: str) -> None:
    """A7.1: cat through a workspace symlink to ~/.ssh or a symlinked directory returns deny."""
    _, ws, ctx = _setup_env(tmp_path, monkeypatch)
    _assert_deny(command, ws, ctx)


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ss?/*",
        "cat ~/.cl*/settings.json",
    ],
)
def test_a7_2_globs_reaching_protected_home_are_denied(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    """A7.2: globs reaching a protected home (~/.ss?/*, ~/.cl*/settings.json) return deny."""
    _, ws, ctx = _setup_env(tmp_path, monkeypatch)
    _assert_deny(command, ws, ctx)


@pytest.mark.parametrize(
    "command",
    [
        "x=SOME_SECRET_NAME; echo ${!x}",
        "x=OTHER_VAR; echo ${!x}",
    ],
)
def test_a7_3_indirect_parameter_expansion_is_denied(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    """A7.3: ${!...} indirect expansion (secret or arbitrary) returns deny."""
    _, ws, ctx = _setup_env(tmp_path, monkeypatch)
    _assert_deny(command, ws, ctx)


@pytest.mark.parametrize(
    "command",
    [
        "cat $'\\x2eenv'",
        "cat $'\\x7e/.ssh/id_rsa'",
    ],
)
def test_a7_4_ansi_c_quoting_spelling_protected_path_is_denied(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    """A7.4: ANSI-C quoting ($'\\x2eenv', $'\\x7e/.ssh/id_rsa') returns deny."""
    _, ws, ctx = _setup_env(tmp_path, monkeypatch)
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _assert_deny(command, ws, ctx)


@pytest.mark.parametrize(
    "command",
    [
        "cat < /dev/tcp/1.2.3.4/80",
        "exec 3<>/dev/tcp/1.2.3.4/80",
        "cat < /dev/udp/1.2.3.4/53",
        "echo hi > /dev/udp/1.2.3.4/53",
    ],
)
def test_a7_5_dev_tcp_and_udp_redirects_are_denied(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    """A7.5: any redirect from or to /dev/tcp/ or /dev/udp/ returns deny."""
    _, ws, ctx = _setup_env(tmp_path, monkeypatch)
    _assert_deny(command, ws, ctx)


@pytest.mark.parametrize(
    "command",
    [
        "ps -Eww",
        "ps eww",
        "ps auxe",
        "ps -E",
    ],
)
def test_a7_6_ps_environment_forms_are_denied(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    """A7.6: ps -Eww, ps eww, and other ps forms printing environments return deny."""
    _, ws, ctx = _setup_env(tmp_path, monkeypatch)
    _assert_deny(command, ws, ctx)


def test_a7_7_recursive_search_over_home_is_denied(tmp_path: Path, monkeypatch) -> None:
    """A7.7: recursive searches whose root contains a protected home path return deny."""
    home, ws, ctx = _setup_env(tmp_path, monkeypatch)
    for command in (
        "grep -r BEGIN ~",
        f"grep -r x {home}",
        "rg x ~",
        "find ~ -name '*.json' -exec cat {} +",
    ):
        _assert_deny(command, ws, ctx)


@pytest.mark.parametrize("interp", ["python3", "node", "ruby", "perl", "bash"])
@pytest.mark.parametrize("target_kind", ["declared_secret", "protected_home", "symlink_to_secret"])
def test_a7_8_interpreter_running_protected_file_is_denied(
    tmp_path: Path, monkeypatch, interp: str, target_kind: str
) -> None:
    """A7.8: interpreters given a declared secret file, protected home file, or symlink return deny."""
    if target_kind == "declared_secret":
        home, ws, ctx = _setup_env(tmp_path, monkeypatch, with_secret_file=True)
        target = "secret.tok"
    elif target_kind == "protected_home":
        home, ws, ctx = _setup_env(tmp_path, monkeypatch, with_secret_file=False)
        target = str(home / ".ssh" / "config")
    else:
        home, ws, ctx = _setup_env(tmp_path, monkeypatch, with_secret_file=False)
        target = "link_ssh"
    _assert_deny(f"{interp} {target}", ws, ctx)
