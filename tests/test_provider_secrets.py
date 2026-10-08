"""Configured provider credentials are private to the server, across workers."""

from __future__ import annotations

import dataclasses
import os
import shlex
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from subagent import providers
from subagent.config import Settings
from subagent.guard import classify
from subagent.providers.base import DRIVERS, ProviderConfig
from subagent.providers.epi import EPI_PROVIDER

FAKE_EPI = Path(__file__).parent / "fakes" / "fake_epi"


@pytest.fixture(autouse=True)
def _reset_protection(monkeypatch):
    monkeypatch.setattr(classify, "_provider_homes", ())
    monkeypatch.setattr(classify, "_provider_secret_files", (), raising=False)


def _token(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test-token-canary\n")
    path.chmod(0o600)
    return path


def _epi_settings(workspace: Path, token: Path, driver: str = "claude") -> Settings:
    epi = ProviderConfig(name="epi", driver="epi", vendor="epi", binary=str(FAKE_EPI),
                         experimental=True, extra={"token_file": str(token)})
    worker = ProviderConfig(name="worker", driver=driver, vendor="none")
    return Settings(workspace=workspace, session_root=workspace / "sessions",
                    providers={"epi": epi, "worker": worker})


@pytest.mark.parametrize("worker_driver", DRIVERS)
@pytest.mark.parametrize("location", ["workspace", "outside"])
def test_guard_denies_provider_secret_file_reads(tmp_path, worker_driver, location):
    """R1b caller-file probe, without replacing HOME or contacting a service."""
    workspace = tmp_path / "worker-project"
    workspace.mkdir()
    root = workspace if location == "workspace" else tmp_path / "private-store"
    token = _token(root / "arbitrary caller file")
    settings = _epi_settings(workspace, token, worker_driver)
    error, _ = EPI_PROVIDER.boot(settings, "guard-probe", settings.providers["epi"])
    assert error is None
    classify.protect_provider_homes(settings)
    read = classify.classify("Read", {"file_path": str(token)}, workspace)
    shell = classify.classify_bash(f"cat {shlex.quote(str(token))}", workspace)
    assert (read.action, shell.action) == ("deny", "deny"), (read, shell)


@pytest.fixture
def declared_secret(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    token = _token(workspace / "private" / "ordinary-name")
    declaration = SimpleNamespace(secret_files=lambda cfg: tuple(cfg.extra["secret_files"]))
    original = providers.for_driver
    monkeypatch.setattr(providers, "for_driver", lambda name: declaration if name == "declared"
                        else original(name))
    cfg = ProviderConfig(name="declared", driver="declared", vendor="none",
                         extra={"secret_files": (token,)})
    settings = Settings(workspace=workspace, session_root=tmp_path / "sessions",
                        providers={"declared": cfg})
    classify.protect_provider_homes(settings)
    return workspace, token, settings


@pytest.mark.parametrize("alias", ["relative", "dotdot", "symlink", "hardlink", "case"])
def test_guard_protects_secret_file_aliases(declared_secret, alias):
    workspace, token, _ = declared_secret
    if alias == "relative":
        target = "private/ordinary-name"
    elif alias == "dotdot":
        target = "private/../private/ordinary-name"
    elif alias == "case":
        target = str(token).upper()
    else:
        path = workspace / alias
        if alias == "symlink":
            path.symlink_to(token)
        else:
            os.link(token, path)
        target = str(path)
    for tool in ("Read", "read", "read_image", "Grep", "grep", "Glob", "glob"):
        verdict = classify.classify(tool, {"file_path": target}, workspace)
        assert verdict.action == "deny", (tool, verdict)
    for tool in ("Write", "write", "Edit", "edit", "str_replace_editor", "NotebookEdit"):
        verdict = classify.classify(tool, {"path": target}, workspace)
        assert verdict.action == "deny", (tool, verdict)
    for command in (f"cat {shlex.quote(target)}", f"echo changed > {shlex.quote(target)}",
                    f"tee {shlex.quote(target)}", f"rm {shlex.quote(target)}"):
        for tool in ("Bash", "bash", "shell", "run_command"):
            verdict = classify.classify(tool, {"command": command}, workspace)
            assert verdict.action == "deny", (tool, command, verdict)


def test_guard_protects_bare_secret_names_and_globs(declared_secret):
    workspace, token, _ = declared_secret
    for command in ("cat ordinary-name", "cat ordinary-*", "echo changed > ordinary-name"):
        verdict = classify.classify("Bash", {"command": command}, workspace, cwd=token.parent)
        assert verdict.action == "deny", (command, verdict)


def test_guard_protects_every_multiedit_target(declared_secret):
    workspace, token, _ = declared_secret
    verdict = classify.classify("MultiEdit", {"edits": [
        {"file_path": str(workspace / "safe.py")}, {"file_path": str(token)},
    ]}, workspace, context={"protected": []})
    assert verdict.action == "deny", verdict


def test_guard_denies_searches_that_include_secret_files(declared_secret):
    workspace, token, _ = declared_secret
    for tool, payload in (("Grep", {"path": str(token.parent), "pattern": "."}),
                          ("Grep", {"pattern": "."}),
                          ("Glob", {"path": str(token.parent), "pattern": "*"})):
        verdict = classify.classify(tool, payload, workspace)
        assert verdict.action == "deny", (tool, verdict)
    for command in ("rg . private", "grep -r . private", "rg .", "cat private/ordinary-*"):
        verdict = classify.classify_bash(command, workspace)
        assert verdict.action == "deny", (command, verdict)
    safe = workspace / "public"
    safe.mkdir()
    assert classify.classify("Grep", {"path": str(safe), "pattern": "."}, workspace).action == "allow"
    assert classify.classify_bash("rg . public", workspace).action == "allow"


def test_guard_collects_all_provider_files_and_replaces_previous_set(declared_secret):
    workspace, first, settings = declared_secret
    second = _token(workspace / "elsewhere" / "another-name")
    cfg = dataclasses.replace(settings.providers["declared"], name="second",
                              extra={"secret_files": (second,)})
    classify.protect_provider_homes(dataclasses.replace(settings, providers={
        **settings.providers, "second": cfg,
    }))
    for token in (first, second):
        assert classify.classify("Read", {"path": str(token)}, workspace).action == "deny"
    classify.protect_provider_homes(dataclasses.replace(settings, providers={"second": cfg}))
    assert classify.classify("Read", {"path": str(first)}, workspace).action == "allow"
    assert classify.classify("Read", {"path": str(second)}, workspace).action == "deny"
    assert classify.classify("Write", {"path": str(second.with_suffix(".backup"))},
                             workspace).action == "allow"


def test_guard_keeps_missing_and_replaced_secret_paths_protected(declared_secret):
    workspace, token, _ = declared_secret
    token.unlink()
    assert classify.classify("Write", {"path": str(token)}, workspace).action == "deny"
    replacement = _token(workspace / "replacement")
    token.symlink_to(replacement)
    assert classify.classify("Read", {"path": str(replacement)}, workspace).action == "deny"
    assert classify.classify("Read", {"path": str(token)}, workspace).action == "deny"


def test_guard_uses_secret_files_from_worker_context(declared_secret, monkeypatch):
    workspace, token, _ = declared_secret
    monkeypatch.setattr(classify, "_provider_secret_files", ())
    context = {"secret_files": [str(token)]}
    for tool, payload in (("Read", {"path": str(token)}), ("Write", {"path": str(token)}),
                          ("Bash", {"command": f"cat {shlex.quote(str(token))}"}),
                          ("Grep", {"pattern": "."})):
        verdict = classify.classify(tool, payload, workspace, context=context)
        assert verdict.action == "deny", (tool, verdict)


def test_guard_denies_mutating_credential_ancestors(declared_secret):
    workspace, token, _ = declared_secret
    for command in ("rm -rf private", "mv private moved", "chmod -R 777 private"):
        verdict = classify.classify_bash(command, workspace)
        assert verdict.action == "deny", (command, verdict)
    assert classify.classify("Write", {"path": str(token.parent / "safe.py")},
                             workspace).action == "allow"


def test_guard_search_option_values_do_not_hide_default_secret_scan(declared_secret):
    workspace, _, _ = declared_secret
    for command in ("rg --max-columns 100 canary", "rg -m 10 canary",
                    "rg -e canary", "rg --unrecognized-option value canary"):
        verdict = classify.classify_bash(command, workspace)
        assert verdict.action == "deny", (command, verdict)
    safe = workspace / "public"
    safe.mkdir()
    for command in ("rg -e canary public", "rg --hidden canary public",
                    "rg -m 10 canary public"):
        assert classify.classify_bash(command, workspace).action == "allow", command


def test_guard_denies_globs_of_secret_hardlinks(declared_secret):
    workspace, token, _ = declared_secret
    os.link(token, workspace / "alias-name")
    verdict = classify.classify_bash("cat alias-*", workspace)
    assert verdict.action == "deny", verdict


@pytest.mark.parametrize("alias", ["hardlink", "symlink"])
def test_guard_denies_directory_scans_of_secret_aliases(declared_secret, alias):
    workspace, token, _ = declared_secret
    public = workspace / "public"
    public.mkdir()
    linked = public / "ordinary-alias"
    if alias == "hardlink":
        os.link(token, linked)
    else:
        linked.symlink_to(token)
    assert classify.classify("Grep", {"path": str(public), "pattern": "."},
                             workspace).action == "deny"
    assert classify.classify_bash("rg --follow . public", workspace).action == "deny"
    assert classify.classify("Write", {"path": str(public / "safe.py")},
                             workspace).action == "allow"


def test_guard_scan_checks_files_after_a_dangling_link(declared_secret, monkeypatch):
    workspace, token, _ = declared_secret
    public = workspace / "public"
    public.mkdir()
    (public / "a-dangling").symlink_to(public / "missing")
    os.link(token, public / "z-alias")
    original = os.scandir

    @contextmanager
    def ordered(path):
        with original(path) as children:
            yield iter(sorted(children, key=lambda child: child.name))

    monkeypatch.setattr(classify.os, "scandir", ordered)
    assert classify.classify("Grep", {"path": str(public), "pattern": "."},
                             workspace).action == "deny"


def test_guard_scan_handles_directory_cycles(declared_secret):
    workspace, _, _ = declared_secret
    public = workspace / "public"
    public.mkdir()
    (public / "cycle").symlink_to(public, target_is_directory=True)
    assert classify.classify("Grep", {"path": str(public), "pattern": "."},
                             workspace).action == "allow"


@pytest.mark.parametrize("basename", ["caller=token", "@caller.token", "-caller.token"])
@pytest.mark.parametrize("spelling", ["absolute", "relative"])
@pytest.mark.parametrize("operation", ["cat", "write", "append", "stdin", "option"])
def test_guard_denies_secret_files_with_literal_shell_prefixes(
    declared_secret, basename, spelling, operation,
):
    workspace, original, settings = declared_secret
    token = _token(original.parent / basename)
    cfg = dataclasses.replace(settings.providers["declared"], extra={"secret_files": (token,)})
    classify.protect_provider_homes(dataclasses.replace(settings, providers={"declared": cfg}))
    target = shlex.quote(str(token) if spelling == "absolute" else token.name)
    command = {"cat": f"cat -- {target}", "write": f"echo changed > {target}",
               "append": f"echo changed >> {target}", "stdin": f"cat < {target}",
               "option": f"grep --file={target} safe.py"}[operation]
    verdict = classify.classify("Bash", {"command": command}, workspace, cwd=token.parent)
    assert verdict.action == "deny", (command, verdict)


@pytest.mark.parametrize("command", ["cat ordinary-{name,safe}", "cat ordi{nary-name,missing}",
                                      "echo changed > ordinary-{name,safe}"])
def test_guard_denies_ambiguous_shell_brace_expansions(declared_secret, command):
    workspace, token, _ = declared_secret
    verdict = classify.classify("Bash", {"command": command}, workspace, cwd=token.parent)
    assert verdict.action == "deny", (command, verdict)


@pytest.mark.parametrize("command", ["cat ordinary-${EPI_REVIEW_UNSET}name",
                                      "cat ordinary-$'\\156ame'",
                                      'cat ordinary-$EPI_REVIEW_UNSET"name"'])
def test_guard_denies_unresolved_shell_secret_operands(declared_secret, command):
    workspace, token, _ = declared_secret
    verdict = classify.classify("Bash", {"command": command}, workspace, cwd=token.parent)
    assert verdict.action == "deny", (command, verdict)


@pytest.mark.parametrize("command", ["diff public private", "diff -r public private",
                                      "diff --recursive public private", "diff -Nru public private",
                                      "grep --directories=recurse . private",
                                      "grep -d recurse . private"])
def test_guard_denies_additional_directory_read_commands(declared_secret, command):
    workspace, token, _ = declared_secret
    public = workspace / "public"
    public.mkdir()
    (public / token.name).write_text("ordinary safe file")
    verdict = classify.classify_bash(command, workspace)
    assert verdict.action == "deny", (command, verdict)


@pytest.mark.parametrize("command", ["cat {ordinary-{token},safe}",
                                      "cat {safe,ordinary-{token}}",
                                      "echo changed > {ordinary-{token},safe}"])
def test_guard_denies_nested_shell_braces(declared_secret, command):
    workspace, original, settings = declared_secret
    token = _token(original.parent / "ordinary-{token}")
    cfg = dataclasses.replace(settings.providers["declared"], extra={"secret_files": (token,)})
    classify.protect_provider_homes(dataclasses.replace(settings, providers={"declared": cfg}))
    verdict = classify.classify("Bash", {"command": command}, workspace, cwd=token.parent)
    assert verdict.action == "deny", (command, verdict)


@pytest.mark.parametrize("alias", ["hardlink", "symlink"])
@pytest.mark.parametrize("command", ["cat public/**/alias", "cat public/**/a*"])
def test_guard_denies_recursive_shell_globs(declared_secret, alias, command):
    workspace, token, _ = declared_secret
    linked = workspace / "public" / "one" / "two" / "alias"
    linked.parent.mkdir(parents=True)
    if alias == "hardlink":
        os.link(token, linked)
    else:
        linked.symlink_to(token)
    verdict = classify.classify_bash(command, workspace)
    assert verdict.action == "deny", (command, verdict)


@pytest.mark.parametrize("pattern", ["caller<1-5>.token", "caller<->.token"])
@pytest.mark.parametrize("spelling", ["absolute", "relative"])
def test_guard_denies_zsh_numeric_globs(declared_secret, pattern, spelling):
    workspace, original, settings = declared_secret
    token = _token(original.parent / "caller1.token")
    cfg = dataclasses.replace(settings.providers["declared"], extra={"secret_files": (token,)})
    classify.protect_provider_homes(dataclasses.replace(settings, providers={"declared": cfg}))
    target = str(token.parent / pattern) if spelling == "absolute" else pattern
    verdict = classify.classify("Bash", {"command": f"cat {target}"},
                                workspace, cwd=token.parent)
    assert verdict.action == "deny", verdict


@pytest.mark.parametrize("pattern", ["caller[[:digit:]].token", "caller[[:alnum:]].token",
                                     "caller[^a].token"])
def test_guard_denies_shell_bracket_globs(declared_secret, pattern):
    workspace, original, settings = declared_secret
    token = _token(original.parent / "caller1.token")
    cfg = dataclasses.replace(settings.providers["declared"], extra={"secret_files": (token,)})
    classify.protect_provider_homes(dataclasses.replace(settings, providers={"declared": cfg}))
    verdict = classify.classify("Bash", {"command": f"cat {pattern}"},
                                workspace, cwd=token.parent)
    assert verdict.action == "deny", verdict


@pytest.mark.parametrize("basename", ["cat", "ordinary-name"])
@pytest.mark.parametrize("tail", ["", " true"])
def test_guard_denies_leading_secret_redirections(declared_secret, basename, tail):
    workspace, original, settings = declared_secret
    token = _token(original.parent / basename)
    cfg = dataclasses.replace(settings.providers["declared"], extra={"secret_files": (token,)})
    classify.protect_provider_homes(dataclasses.replace(settings, providers={"declared": cfg}))
    verdict = classify.classify("Bash", {"command": f"> {basename}{tail}"},
                                workspace, cwd=token.parent)
    assert verdict.action == "deny", verdict
