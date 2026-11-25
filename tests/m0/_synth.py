"""Helpers to build synthetic Claude Code transcripts in tmp_path for M0 tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def make_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    ephemeral_5m: int | None = None,
    ephemeral_1h: int | None = None,
    iterations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Claude Code message.usage object."""
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
    }
    if ephemeral_5m is not None or ephemeral_1h is not None:
        cc: dict[str, int] = {}
        if ephemeral_5m is not None:
            cc["ephemeral_5m_input_tokens"] = ephemeral_5m
        if ephemeral_1h is not None:
            cc["ephemeral_1h_input_tokens"] = ephemeral_1h
        usage["cache_creation"] = cc
    if iterations is not None:
        usage["iterations"] = iterations
    return usage


def assistant_record(
    *,
    uuid: str,
    timestamp: str,
    msg_id: str,
    model: str = "claude-opus-5",
    usage: dict[str, Any] | None = None,
    content: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
    is_sidechain: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build a synthetic assistant transcript record."""
    rec: dict[str, Any] = {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": content if content is not None else [{"type": "text", "text": "ok"}],
            "usage": usage if usage is not None else make_usage(input_tokens=10, output_tokens=5),
        },
    }
    if session_id is not None:
        rec["sessionId"] = session_id
    if is_sidechain:
        rec["isSidechain"] = True
    if agent_id is not None:
        rec["agentId"] = agent_id
    return rec


def user_record(
    *,
    uuid: str,
    timestamp: str,
    content: str | list[dict[str, Any]] = "please help",
    session_id: str | None = None,
    user_type: str | None = "external",
    is_meta: bool = False,
    is_compact_summary: bool = False,
    is_sidechain: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build a synthetic user transcript record."""
    rec: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {"role": "user", "content": content},
    }
    if user_type is not None:
        rec["userType"] = user_type
    if is_meta:
        rec["isMeta"] = True
    if is_compact_summary:
        rec["isCompactSummary"] = True
    if is_sidechain:
        rec["isSidechain"] = True
    if agent_id is not None:
        rec["agentId"] = agent_id
    if session_id is not None:
        rec["sessionId"] = session_id
    return rec


def tool_result_record(
    *,
    uuid: str,
    timestamp: str,
    tool_use_id: str = "toolu_01",
    content: str | list[dict[str, Any]] = "done",
    session_id: str | None = None,
    user_type: str | None = "external",
    is_sidechain: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build a synthetic user record carrying a tool_result block."""
    return user_record(
        uuid=uuid,
        timestamp=timestamp,
        content=[{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        session_id=session_id,
        user_type=user_type,
        is_sidechain=is_sidechain,
        agent_id=agent_id,
    )


def meta_record(
    *,
    uuid: str,
    timestamp: str,
    content: str = "meta",
    session_id: str | None = None,
    user_type: str | None = "external",
    is_sidechain: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build a synthetic user record flagged with isMeta=True."""
    return user_record(
        uuid=uuid,
        timestamp=timestamp,
        content=content,
        session_id=session_id,
        user_type=user_type,
        is_meta=True,
        is_sidechain=is_sidechain,
        agent_id=agent_id,
    )


def write_jsonl(path: Path, records: list[dict[str, Any] | str]) -> Path:
    """Write dict records (serialized as JSON) or raw string lines to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [rec if isinstance(rec, str) else json.dumps(rec) for rec in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def write_session(
    projects_dir: Path,
    session_id: str,
    records: list[dict[str, Any] | str],
) -> Path:
    """Write a coordinator session transcript at <projects_dir>/<session_id>.jsonl."""
    return write_jsonl(projects_dir / f"{session_id}.jsonl", records)


def write_subagent(
    projects_dir: Path,
    session_id: str,
    agent_id: str,
    records: list[dict[str, Any] | str],
) -> Path:
    """Write a subagent transcript at <projects_dir>/<session_id>/subagents/agent-<agent_id>.jsonl."""
    stamped: list[dict[str, Any] | str] = []
    for rec in records:
        if isinstance(rec, dict):
            clone = copy.deepcopy(rec)
            clone.setdefault("isSidechain", True)
            clone.setdefault("agentId", agent_id)
            stamped.append(clone)
        else:
            stamped.append(rec)
    return write_jsonl(projects_dir / session_id / "subagents" / f"agent-{agent_id}.jsonl", stamped)


def clone_for_resume(
    records: list[dict[str, Any]],
    child_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Clone parent records for a resumed child session keeping uuid, message.id, and timestamp."""
    cloned: list[dict[str, Any]] = []
    for rec in records:
        item = copy.deepcopy(rec)
        if child_session_id is not None and "sessionId" in item:
            item["sessionId"] = child_session_id
        cloned.append(item)
    return cloned


def write_pricing(
    path: Path,
    models: dict[str, dict[str, Any]],
    cache_multipliers: dict[str, float] | None = None,
) -> Path:
    """Write a pricing TOML file in the shape of src/subagent/pricing.toml."""
    mults = cache_multipliers or {
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.1,
    }
    lines = ["[cache_multipliers]"]
    for key, val in mults.items():
        lines.append(f"{key} = {val}")
    for model_id, cfg in models.items():
        lines.append("")
        lines.append(f"[models.{model_id}]")
        for key, val in cfg.items():
            if isinstance(val, str):
                lines.append(f'{key} = "{val}"')
            else:
                lines.append(f"{key} = {val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
