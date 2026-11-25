"""Helpers for the G1 tests: tiny Claude Code transcript records and JSONL writing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def user(uuid: str, ts: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "userType": "external",
        "message": {"role": "user", "content": "go"},
    }


def assistant(
    uuid: str,
    ts: str,
    msg_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    agent: str | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": "claude-opus-5",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    }
    if agent is not None:
        rec["isSidechain"] = True
        rec["agentId"] = agent
    return rec


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
