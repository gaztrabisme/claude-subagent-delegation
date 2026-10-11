"""The PowerShell reading Codex children on Windows get (classify_powershell).

Codex on Windows runs its shell calls in PowerShell. The bash reading judges
each line first and keeps its denials; the PowerShell reading can only clear a
line, for Get-Content, Set-Content, Out-File, Get-ChildItem, Select-String and
Test-Path on workspace paths and pipelines of them.
"""

import importlib
import sys
from types import SimpleNamespace

import pytest

guard = importlib.import_module("subagent.guard.classify")
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows shell and path semantics"
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "User Home"
    root.mkdir()
    monkeypatch.setattr(guard, "_home", lambda: root)
    monkeypatch.setattr(guard, "_provider_homes", (".codex",))
    return root


@pytest.fixture
def ws(tmp_path, home):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "notes.txt").write_text("hello\n")
    return root


def _ps(command, ws, **kw):
    return guard.classify("Bash", {"command": command}, ws, shell=guard.POWERSHELL, **kw)


def _bash(command, ws):
    return guard.classify("Bash", {"command": command}, ws)


# What Codex sent in a real run on this machine, and the plain forms of each cmdlet.
ALLOWED = [
    "Set-Content -LiteralPath notes.txt -Value 'hello'",
    'Set-Content -Path notes.txt -Value "hello world"',
    "Set-Content notes.txt 'it''s'",
    "Set-Content -Path:src\\app.py -Value 'print(1)' -NoNewline -Encoding utf8",
    "Get-Content notes.txt",
    "Get-Content -Raw -Path .\\notes.txt",
    "Get-Content -TotalCount 5 notes.txt",
    "gc notes.txt",
    "Get-ChildItem",
    "Get-ChildItem -Recurse -File src",
    "Get-ChildItem src *.py",
    "ls -Name",
    "Select-String -Pattern world -Path notes.txt",
    "Select-String world notes.txt",
    "sls -SimpleMatch 'a b' notes.txt",
    "Test-Path notes.txt",
    "Test-Path -LiteralPath src -PathType Container",
    "Get-Content notes.txt | Select-String world",
    "Get-Content notes.txt | Out-File copy.txt",
    "Get-ChildItem -Name | Out-File -FilePath list.txt -Encoding utf8",
    "Get-Content notes.txt | Set-Content src\\copy.txt",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_workspace_cmdlets_are_allowed(command, ws):
    verdict = _ps(command, ws)
    assert verdict.action == guard.ALLOW, verdict.reason
    assert verdict.facts["shell"] == "powershell"


def test_writes_are_reported_as_facts(ws):
    verdict = _ps("Get-Content notes.txt | Out-File copy.txt", ws)
    assert verdict.facts["programs"] == ["Get-Content", "Out-File"]
    assert verdict.facts["writes"] == [str(ws / "copy.txt")]
    assert verdict.facts["reads"] == [str(ws / "notes.txt")]


def test_relative_paths_follow_the_workdir(ws):
    verdict = guard.classify(
        "Bash", {"command": "Set-Content app.py 'x'", "workdir": str(ws / "src")}, ws,
        shell=guard.POWERSHELL,
    )
    assert verdict.action == guard.ALLOW
    assert verdict.facts["writes"] == [str(ws / "src" / "app.py")]


def test_bash_reading_is_unchanged_without_the_shell(ws):
    """The same lines from a bash child keep the bash verdict."""
    assert _bash("Set-Content -LiteralPath notes.txt -Value 'hello'", ws).action == guard.ESCALATE
    assert _bash("Get-ChildItem", ws).action == guard.ESCALATE


# Everything outside the list keeps the verdict the bash reading gives it.
NOT_CLEARED = [
    "Remove-Item notes.txt",
    "Add-Content notes.txt 'x'",
    "Copy-Item notes.txt b.txt",
    "Invoke-WebRequest http://example.com -OutFile a.txt",
    "Get-Content notes.txt | Invoke-Expression",
    "Get-Content notes.txt; Remove-Item notes.txt",
    "Get-Content notes.txt && Remove-Item notes.txt",
    "Get-Content notes.txt > out.txt",
    "Set-Content notes.txt $env:OPENAI_API_KEY",
    'Set-Content notes.txt "$(whoami)"',
    "Set-Content notes.txt \"a`nb\"",
    "Set-Content -Path notes.txt -Value hi -Force",
    "Set-Content -Pa notes.txt -Value hi",
    "Set-Content notes.txt hi extra",
    "Set-Content -Value hi",
    "Out-File a.txt b",
    "Get-ChildItem -Recurse:$false",
    "Get-Content @('notes.txt')",
    "Get-Content notes.txt,other.txt",
    "Get-Content -Path",
    "Select-String -Path notes.txt",
    "[System.IO.File]::WriteAllText('a.txt','x')",
    "& Get-Content notes.txt",
    "Get-Content notes.txt # comment",
    "Get-Content src\\[a]*.py",
    "Microsoft.PowerShell.Management\\Get-Content notes.txt",
    "Get-Content 'notes.txt",
    "Get-Content notes.txt |",
    "| Get-Content notes.txt",
    "Get-Content notes.txt\nRemove-Item notes.txt",
    "Set-Content \u2013Path notes.txt 'x'",
    "Set-Content \u201cnotes.txt\u201d 'x'",
]


@pytest.mark.parametrize("command", NOT_CLEARED)
def test_other_lines_keep_the_bash_verdict(command, ws):
    assert guard.classify_powershell(command, ws) is None
    assert _ps(command, ws) == _bash(command, ws)


@pytest.mark.parametrize("command", [
    "Get-Content Env:OPENAI_API_KEY",
    "Get-Content notes.txt:hidden",
    "Set-Content HKCU:\\Software\\x 'y'",
    "Set-Content \\\\server\\share\\a.txt 'x'",
    "Get-Content \\\\?\\C:\\Windows\\win.ini",
])
def test_provider_device_and_stream_paths_are_not_cleared(command, ws):
    assert guard.classify_powershell(command, ws) is None


def test_writes_outside_the_workspace_are_denied(ws, tmp_path):
    outside = tmp_path / "elsewhere" / "x.txt"
    for command in [
        f"Set-Content -Path '{outside}' -Value hi",
        "Set-Content -Path ..\\x.txt -Value hi",
        f"Get-Content notes.txt | Out-File '{outside}'",
    ]:
        assert guard.classify_powershell(command, ws) is None
        assert _ps(command, ws).action == guard.DENY, command


def test_reads_outside_the_workspace_are_not_cleared(ws, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    assert guard.classify_powershell(f"Get-Content '{outside}'", ws) is None
    assert _ps(f"Get-Content '{outside}'", ws).action != guard.ALLOW
    assert guard.classify_powershell("Get-ChildItem ..", ws) is None


@pytest.mark.parametrize("command", [
    "Get-Content ~\\.ssh\\id_rsa",
    "Get-Content $HOME\\.codex\\auth.json",
    "Set-Content .env 'x'",
    "Get-Content .git\\config",
    "Set-Content .git\\hooks\\pre-commit 'x'",
    "Set-Content .codex\\config.toml 'x'",
])
def test_sensitive_paths_stay_denied(command, ws):
    assert guard.classify_powershell(command, ws) is None
    assert _ps(command, ws).action == guard.DENY


def test_symlink_out_of_the_workspace_is_not_cleared(ws, tmp_path):
    target = tmp_path / "outside-dir"
    target.mkdir()
    link = ws / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need Developer Mode or admin on Windows")
    assert guard.classify_powershell("Set-Content link\\a.txt 'x'", ws) is None


def test_guard_context_protected_files_are_not_cleared(ws):
    context = {"protected": [str(ws / "tests")], "state_allow": []}
    assert guard.classify_powershell("Set-Content tests\\test_a.py 'x'", ws, context=context) is None
    assert guard.classify_powershell("Set-Content .subagent\\x 'x'", ws, context=context) is None
    assert guard.classify_powershell("Set-Content src\\a.py 'x'", ws, context=context) is not None


def test_supervisor_reads_powershell_only_for_codex(tmp_path):
    from subagent.guard.supervisor import Supervisor

    agents = {
        "cx": SimpleNamespace(driver=SimpleNamespace(name="codex")),
        "cl": SimpleNamespace(driver=SimpleNamespace(name="claude")),
    }
    registry = SimpleNamespace(find_agent=agents.get, hooks_counted=False)
    settings = SimpleNamespace(supervisor="auto", approval_socket=str(tmp_path / "s.sock"))
    supervisor = Supervisor(settings, registry)
    assert supervisor.shell_for("cx") == guard.POWERSHELL
    assert supervisor.shell_for("cl") is None
    assert supervisor.shell_for("missing") is None
    assert supervisor.shell_for(None) is None
