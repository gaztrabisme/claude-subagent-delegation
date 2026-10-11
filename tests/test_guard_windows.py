"""Windows shell policy regressions (native Windows path semantics required)."""

import importlib
import sys

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


@pytest.mark.parametrize("verb", [
    "type", "Get-Content", "gc", "cat", "more", "Select-String", "sls",
])
@pytest.mark.parametrize("prefix", ["~", "$HOME", "$env:USERPROFILE"])
def test_sensitive_home_reads(verb, prefix, home, tmp_path):
    for suffix in [r"\.ssh\id_rsa", r"\.ssh\config", r"\.codex\auth.json"]:
        assert guard.classify_bash(f"{verb} {prefix}{suffix}", tmp_path).action == guard.DENY


def test_absolute_and_option_paths(home, tmp_path):
    path = home / ".ssh" / "config"
    for command in [f'type "{path}"', f'Get-Content -LiteralPath "{path}"',
                    f'Select-String -Pattern x -Path "{path}"']:
        assert guard.classify_bash(command, tmp_path).action == guard.DENY


def test_protected_root_argument(home, tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(guard, "PROTECTED_ROOTS", {root})
    assert guard.classify_bash(f'type "{root / "settings.json"}"', tmp_path).action == guard.DENY


def test_shell_tool_uses_windows_policy(home, tmp_path):
    verdict = guard.classify("shell", {"command": r"type ~\.ssh\id_rsa"}, tmp_path)
    assert verdict.action == guard.DENY


@pytest.mark.parametrize("command", [
    "python -m pytest", "uv run pytest", "type README.md",
    r"Get-Content .\src\module.py", 'gc "file with spaces.txt"', "sls needle README.md",
])
def test_workspace_commands(command, tmp_path):
    assert guard.classify_bash(command, tmp_path).action == guard.ALLOW


@pytest.mark.parametrize("command", [
    "Invoke-UnknownThing README.md", 'type "unterminated',
    "Get-Content $unknown", "type %USERPROFILE%\\file.txt",
    "gc ('file.txt')", "gc file` name.txt", "type file^ name.txt",
    "type file.txt & mystery", "gc file.txt; mystery", "gc @('file.txt')",
    "gc file.txt,other.txt", "", "# comment",
    "CI=1", "sort README.md /o output.txt", "gc .\\*.txt",
])
def test_unreadable_commands_escalate(command, tmp_path):
    assert guard.classify_bash(command, tmp_path).action == guard.ESCALATE


@pytest.mark.parametrize("command", [
    "pwd && ls", "cat notes.txt | grep TODO", "ls 2>/dev/null",
    "cat notes.txt > copy.txt", "cat < notes.txt", "pwd; ls",
    "CI=1 python -m pytest", "ls \\\nREADME.md",
])
def test_bash_forms_on_windows(command, tmp_path):
    assert guard.classify_bash(command, tmp_path).action == guard.ALLOW


@pytest.mark.parametrize("command", [
    "sort -o output.txt notes.txt", "sort --compress-program=helper notes.txt",
    "find . -exec helper {} \\;", "rg --pre helper notes.txt",
    "cat <<EOF\nhello\nEOF", "cat notes.txt | mystery",
])
def test_bash_exposures_still_escalate(command, tmp_path):
    assert guard.classify_bash(command, tmp_path).action == guard.ESCALATE


@pytest.mark.parametrize("verb", ["type", "Get-Content", "gc", "more", "Select-String", "sls"])
def test_native_reads_outside_workspace_escalate(verb, tmp_path):
    command = f'{verb} "{tmp_path.parent / "notes.txt"}"'
    assert guard.classify_bash(command, tmp_path).action == guard.ESCALATE


def test_windows_denial_overrides_bash_allow(home, tmp_path):
    assert guard.classify_bash(r"pwd && cat ~\.ssh\config", tmp_path).action == guard.DENY


@pytest.mark.parametrize("command", [
    "pwd && ls", "cat notes.txt | grep TODO", "ls 2>/dev/null",
    "cat notes.txt > copy.txt", "cat < notes.txt",
    "cat <<EOF\nhello\nEOF", "cat notes.txt | mystery",
    "sort -o output.txt notes.txt", "find . -exec helper {} \\;",
    "CI=1 python -m pytest",
])
def test_windows_bash_verdict_matches_original_policy(command, tmp_path):
    original = guard._classify_bash(command, tmp_path)
    assert guard.classify_bash(command, tmp_path).action == original.action


@pytest.mark.parametrize("form", [
    'type notes.txt > {target}', 'type notes.txt >> {target}',
    'type notes.txt 2>{target}', 'type notes.txt &> {target}',
    'type notes.txt >| {target}', 'Out-File -FilePath {target}',
    'Set-Content -LiteralPath {target} -Value hello',
    'Add-Content -Path {target} -Value hello', 'Tee-Object -FilePath {target}',
    'Copy-Item notes.txt -Destination {target}', 'copy notes.txt {target}',
    'cp notes.txt {target}', 'Move-Item notes.txt -Destination {target}',
    'move notes.txt {target}', 'mv notes.txt {target}',
    'Remove-Item -LiteralPath {target}', 'del {target}', 'rm {target}',
    'touch {target}', 'mkdir {target}',
    'type notes.txt | Out-File -FilePath:{target}',
    'type notes.txt & copy notes.txt {target}',
    'type notes.txt; Set-Content -Path:{target} -Value hello',
])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
def test_native_write_boundaries(form, outside, quoted, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = (tmp_path if outside else workspace) / "output.txt"
    spelling = f'"{target}"' if quoted else str(target)
    verdict = guard.classify_bash(form.format(target=spelling), workspace)
    if outside:
        assert verdict.action == guard.DENY
        assert "outside the workspace" in verdict.reason
    else:
        assert verdict.action != guard.DENY


def test_native_redirect_protects_context(tmp_path):
    target = tmp_path / "locked.py"
    verdict = guard.classify_bash(
        f'type notes.txt > "{target}"', tmp_path,
        context={"protected": [str(target)]},
    )
    assert verdict.action == guard.DENY
