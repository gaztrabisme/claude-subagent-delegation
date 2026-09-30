"""Build a project-scoped set of harness prompts and MCP registrations."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

INSTALL_ROOT_ENV = "SUBAGENT_INSTALL_ROOT"
HARNESS_NAMES = ("claude", "codex", "gemini", "copilot")
PROMPT_NAMES = ("plan", "goal")


class InstallError(Exception):
    """An installation error with user-facing context."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    harness: str
    target: Path
    written: tuple[Path, ...]
    registration: Path
    timeout_seconds: int | float


def resolve_target(target_dir: str | None, root: str | None) -> Path:
    """Resolve explicit target, environment target, global root, then cwd."""
    selected = target_dir or os.environ.get(INSTALL_ROOT_ENV) or root or "."
    return Path(selected).expanduser().resolve()


def _source_skills() -> dict[str, str]:
    contents: dict[str, str] = {}
    for name in ("delegate", *PROMPT_NAMES):
        path = resources.files("subagent").joinpath("skills", name, "SKILL.md")
        try:
            contents[name] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InstallError(f"cannot read skill source '{path}': {exc}") from exc
    return contents


def _managed_files(target: Path, harness: str, skills: dict[str, str]) -> dict[Path, str]:
    if harness == "claude":
        skill_dir = target / ".claude/skills/delegate"
        prompt_dir = target / ".claude/commands"
    elif harness == "codex":
        skill_dir = target / ".agents/skills/delegate"
        prompt_dir = target / ".codex/prompts"
    elif harness == "gemini":
        skill_dir = target / ".agents/skills/delegate"
        prompt_dir = target / ".gemini/commands"
    else:
        skill_dir = target / ".agents/skills/delegate"
        prompt_dir = target / ".github/prompts"

    files = {skill_dir / "SKILL.md": skills["delegate"]}
    if harness == "gemini":
        files[target / ".gemini/GEMINI.md"] = (
            "For delegation workflow, follow the project skill at "
            "`.agents/skills/delegate/SKILL.md`.\n"
        )
    elif harness == "copilot":
        files[target / ".github/copilot-instructions.md"] = (
            "For delegation workflow, follow the project skill at "
            "`.agents/skills/delegate/SKILL.md`.\n"
        )

    for name in PROMPT_NAMES:
        prompt = skills[name]
        if harness == "gemini":
            path = prompt_dir / f"{name}.toml"
            description = (
                "Write a plan that ends with a goal block."
                if name == "plan"
                else "Write the workspace GOAL.md file."
            )
            content = (
                f"description = {json.dumps(description, ensure_ascii=False)}\n"
                f"prompt = {json.dumps(prompt, ensure_ascii=False)}\n"
            )
        elif harness == "copilot":
            path = prompt_dir / f"{name}.prompt.md"
            description = (
                "Write a plan that ends with a goal block."
                if name == "plan"
                else "Write the workspace GOAL.md file."
            )
            content = f"---\nname: {name}\ndescription: {description}\n---\n\n{prompt}"
        else:
            path = prompt_dir / f"{name}.md"
            content = prompt
        files[path] = content
    return files


def _server_config(harness: str, timeout_seconds: int | float) -> dict:
    if harness == "copilot":
        return {
            "type": "local",
            "command": "subagent-mcp",
            "args": [],
            "tools": ["*"],
            "timeout": int(round(timeout_seconds * 1000)),
        }
    if harness in ("claude", "gemini"):
        return {
            "command": "subagent-mcp",
            "timeout": int(round(timeout_seconds * 1000)),
        }
    return {"command": "subagent-mcp", "tool_timeout_sec": timeout_seconds}


def _json_registration(path: Path, server: dict, force: bool) -> str:
    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else "{}"
    except OSError as exc:
        raise InstallError(f"error: cannot update '{path}': {exc}") from exc
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("configuration root must be an object")
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("'mcpServers' must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"error: cannot update '{path}': {exc}") from exc
    if "subagent" in servers and not force:
        raise InstallError("error: MCP server 'subagent' already exists")
    servers["subagent"] = server
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _toml_timeout(value: int | float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return format(float(value), ".15g")


def _inline_server(timeout: int | float) -> str:
    return (
        '{ command = "subagent-mcp", '
        f"tool_timeout_sec = {_toml_timeout(timeout)} }}"
    )


def _toml_value_end(raw: str, start: int) -> int:
    """Find the end of a TOML value, including nested inline tables and arrays."""
    while start < len(raw) and raw[start].isspace():
        start += 1
    if start == len(raw):
        return start
    if raw[start] not in "[{":
        end = raw.find("\n", start)
        return len(raw) if end < 0 else end

    stack = ["]" if raw[start] == "[" else "}"]
    quote: str | None = None
    triple = False
    index = start + 1
    while index < len(raw):
        char = raw[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if triple and raw.startswith(quote * 3, index):
                quote = None
                triple = False
                index += 3
                continue
            if not triple and char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            triple = raw.startswith(char * 3, index)
            quote = char
            index += 3 if triple else 1
        elif char == "#" and not stack:
            end = raw.find("\n", index)
            return len(raw) if end < 0 else end
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
            index += 1
        elif char in "]}":
            if not stack or stack.pop() != char:
                return index
            index += 1
            if not stack:
                return index
        else:
            index += 1
    return len(raw)


def _replace_inline_server(raw: str, timeout: int | float) -> str | None:
    """Replace an inline subagent table without reserializing its neighbors."""
    section_header = re.search(r"(?m)^\[mcp_servers\]\s*(?:#.*)?$", raw)
    if section_header:
        next_header = re.search(r"(?m)^\[[^\]]+\]\s*(?:#.*)?$", raw[section_header.end():])
        section_end = section_header.end() + next_header.start() if next_header else len(raw)
        section = raw[section_header.end():section_end]
        assignment = re.search(r"(?m)^\s*subagent\s*=", section)
        if assignment:
            start = section_header.end() + assignment.end()
            end = _toml_value_end(raw, start)
            return raw[:start] + _inline_server(timeout) + raw[end:]

    root_assignment = re.search(r"(?m)^mcp_servers\s*=\s*\{", raw)
    if root_assignment:
        table_start = root_assignment.end() - 1
        table_end = _toml_value_end(raw, table_start)
        table_body = raw[table_start + 1:table_end - 1]
        assignment = re.search(r"(?:^|[,\s])subagent\s*=", table_body)
        if assignment:
            start = table_start + 1 + assignment.end()
            end = _toml_value_end(raw, start)
            return raw[:start] + _inline_server(timeout) + raw[end:]
    return None


def _toml_registration(path: Path, timeout: int | float, force: bool) -> str:
    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise InstallError(f"error: cannot update '{path}': {exc}") from exc
    try:
        document = tomllib.loads(raw)
        servers = document.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("'mcp_servers' must be a table")
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise InstallError(f"error: cannot update '{path}': {exc}") from exc
    if "subagent" in servers and not force:
        raise InstallError("error: MCP server 'subagent' already exists")

    section = (
        "[mcp_servers.subagent]\n"
        'command = "subagent-mcp"\n'
        f"tool_timeout_sec = {_toml_timeout(timeout)}\n"
    )
    header = re.search(r"(?m)^\[mcp_servers\.subagent\]\s*(?:#.*)?\r?$", raw)
    if header:
        next_header = None
        for candidate in re.finditer(
            r"(?m)^\[{1,2}[^\]\r\n]+\]{1,2}\s*(?:#.*)?\r?$",
            raw[header.end():],
        ):
            table_name = candidate.group(0).split("#", 1)[0].strip().strip("[]")
            if table_name.startswith("mcp_servers.subagent."):
                continue
            next_header = candidate
            break
        end = header.end() + next_header.start() if next_header else len(raw)
        updated = raw[:header.start()] + section + raw[end:]
    elif "subagent" in servers:
        updated = _replace_inline_server(raw, timeout)
        if updated is None:
            raise InstallError(
                f"error: cannot update '{path}': existing subagent entry is not a table section"
            )
    else:
        if not raw or raw.endswith("\n\n"):
            separator = ""
        elif raw.endswith("\n"):
            separator = "\n"
        else:
            separator = "\n\n"
        updated = raw + separator + section
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"error: cannot update '{path}': {exc}") from exc
    return updated


def _registration_text(
    path: Path, harness: str, timeout_seconds: int | float, force: bool
) -> str:
    server = _server_config(harness, timeout_seconds)
    if harness == "codex":
        return _toml_registration(path, timeout_seconds, force)
    return _json_registration(path, server, force)


def _cannot_write(path: Path, reason: OSError, written: list[Path]) -> InstallError:
    message = f"error: cannot update '{path}': {reason}"
    if written:
        message += "\nFiles already written: " + ", ".join(str(item) for item in written)
    return InstallError(message)


def _ensure_no_symlinks(target: Path, path: Path) -> None:
    try:
        parts = path.relative_to(target).parts
    except ValueError as exc:
        raise InstallError(f"error: path '{path}' is outside install target '{target}'") from exc
    current = target
    for part in ("", *parts):
        if part:
            current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InstallError(f"error: cannot inspect '{current}': {exc}") from exc
        if stat.S_ISLNK(mode):
            raise InstallError(f"error: file '{current}' is a symlink")


def _write(target: Path, path: Path, content: str) -> None:
    _ensure_no_symlinks(target, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_no_symlinks(target, path)
    path.write_text(content, encoding="utf-8")


def install(
    harness: str,
    target: Path,
    timeout_seconds: int | float,
    force: bool = False,
) -> InstallResult:
    """Install prompt files and merge one MCP registration for a harness."""
    skills = _source_skills()
    files = _managed_files(target, harness, skills)
    registration = target / (
        ".codex/config.toml" if harness == "codex"
        else ".gemini/settings.json" if harness == "gemini"
        else ".mcp.json"
    )

    for path in (*files, registration):
        _ensure_no_symlinks(target, path)

    if not force:
        for path in files:
            if path.exists():
                raise InstallError(
                    f"error: file '{path}' already exists; use --force to overwrite"
                )
    registration_content = _registration_text(
        registration, harness, timeout_seconds, force
    )

    written: list[Path] = []
    for path, content in files.items():
        try:
            _write(target, path, content)
        except OSError as exc:
            raise _cannot_write(path, exc, written) from exc
        written.append(path)
    try:
        _write(target, registration, registration_content)
    except OSError as exc:
        raise _cannot_write(registration, exc, written) from exc
    written.append(registration)
    return InstallResult(harness, target, tuple(written), registration, timeout_seconds)
