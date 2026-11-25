#!/usr/bin/env python3
"""M0 coordination meter: one JSON report of tokens, cost and minutes per role.

Reads Claude Code transcripts under a projects dir (one `<sid>.jsonl` per session
plus `<sid>/subagents/agent-<id>.jsonl` per delegated agent) and counts one API
message per message.id, priced by src/subagent/pricing.toml. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICING = REPO_ROOT / "src" / "subagent" / "pricing.toml"
ACTIVE_GAP_SECONDS = 300.0
DEFAULT_WRITE_5M = 1.25
DEFAULT_WRITE_1H = 2.0
DEFAULT_READ = 0.1
TOKEN_KEYS = ("input", "cache_write", "cache_read", "output", "total")


class _File:
    """One transcript file: path, role, --session position, windowed records.

    A plain class, not @dataclass: the test fixture execs this module without
    registering it in sys.modules, which crashes dataclass processing.
    """

    def __init__(self, path, role, session_index, is_subagent, records) -> None:
        self.path = path
        self.role = role
        self.session_index = session_index
        self.is_subagent = is_subagent
        self.records = records


def _parse_ts(raw: Any) -> dt.datetime | None:
    """Parse an ISO 8601 timestamp; None when absent, naive or malformed."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _iso_z(when: dt.datetime) -> str:
    """Format an aware datetime as UTC ISO 8601 with a Z suffix."""
    return when.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _aware_cli(raw: str) -> dt.datetime:
    """argparse type: an ISO 8601 timestamp with a Z suffix or offset (naive is an error)."""
    when = _parse_ts(raw)
    if when is None:
        raise argparse.ArgumentTypeError(
            f"need timezone-aware ISO 8601 (Z or offset), got {raw!r}"
        )
    return when


def _num(value: Any) -> float | None:
    """A pricing value as a float; None for 'null', 'unknown', missing or non-numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int:
    """A count field as an int; 0 when absent or not numeric."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _read_jsonl(path: Path) -> list[tuple[dt.datetime, dict[str, Any]]]:
    """Parse a transcript file into (timestamp, record) pairs, skipping unparsable lines."""
    out: list[tuple[dt.datetime, dict[str, Any]]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            out.append((ts, rec))
    return out


def _collect_files(projects: Path, sessions: list[str], roles: dict[str, str]) -> list[_File]:
    """List the session and subagent files, assigning each a role name."""
    agent_to_role = {agent: name for name, agent in roles.items()}
    files: list[_File] = []
    for index, sid in enumerate(sessions):
        session_file = projects / f"{sid}.jsonl"
        if session_file.is_file():
            session_records = _read_jsonl(session_file)
            files.append(_File(session_file, "coordinator", index, False, session_records))
        subagents = projects / sid / "subagents"
        if subagents.is_dir():
            for path in sorted(subagents.glob("agent-*.jsonl")):
                agent = path.name.removeprefix("agent-").removesuffix(".jsonl")
                role = agent_to_role.get(agent, "unassigned")
                files.append(_File(path, role, index, True, _read_jsonl(path)))
    return files


def _message_id(rec: dict[str, Any]) -> str | None:
    """The assistant record's message.id, or None for non-assistant / id-less records."""
    if rec.get("type") != "assistant":
        return None
    message = rec.get("message")
    if isinstance(message, dict) and isinstance(message.get("id"), str):
        return str(message["id"])
    return None


def _usage_of(rec: dict[str, Any]) -> dict[str, Any]:
    """The record's message.usage dict, or an empty one."""
    message = rec.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    return usage if isinstance(usage, dict) else {}


def _field_sum(sources: list[dict[str, Any]], key: str, nested: str | None = None) -> int:
    """Sum one usage field (optionally inside a nested dict) over the source usages."""
    total = 0
    for source in sources:
        holder = source
        if nested is not None:
            holder = source.get(nested)
            if not isinstance(holder, dict):
                continue
        total += _as_int(holder.get(key))
    return total


def _usage_counts(usage: dict[str, Any]) -> dict[str, int]:
    """Message counts: iterations summed per field when non-empty, else the top-level fields."""
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        sources = [item for item in iterations if isinstance(item, dict)]
    else:
        sources = [usage]
    cache_write = _field_sum(sources, "cache_creation_input_tokens")
    write_5m = _field_sum(sources, "ephemeral_5m_input_tokens", nested="cache_creation")
    write_1h = _field_sum(sources, "ephemeral_1h_input_tokens", nested="cache_creation")
    if write_5m + write_1h < cache_write:  # unattributed remainder prices as 5-minute
        write_5m += cache_write - write_5m - write_1h
    return {
        "input": _field_sum(sources, "input_tokens"),
        "output": _field_sum(sources, "output_tokens"),
        "cache_read": _field_sum(sources, "cache_read_input_tokens"),
        "cache_write": cache_write,
        "cw5m": write_5m,
        "cw1h": write_1h,
    }


def _price_entry(models: dict[str, Any], model: str) -> dict[str, Any] | None:
    """The longest [models] key equal to the model id, or a prefix ending at a '-' boundary."""
    best: str | None = None
    for key in models:
        if not isinstance(key, str) or not isinstance(models[key], dict):
            continue
        boundary = model.startswith(key) and model[len(key) : len(key) + 1] == "-"
        if (model == key or boundary) and (best is None or len(key) > len(best)):
            best = key
    return models[best] if best is not None else None


def _model_usd(model: str, totals: dict[str, int], pricing: dict[str, Any]) -> float | None:
    """USD for one model's aggregated counts; None when the model is unpriced."""
    models = pricing.get("models")
    entry = _price_entry(models if isinstance(models, dict) else {}, model)
    if entry is None:
        return None
    rate_in = _num(entry.get("input"))
    rate_out = _num(entry.get("output"))
    if rate_in is None or rate_out is None:
        return None
    mults = pricing.get("cache_multipliers")
    mults = mults if isinstance(mults, dict) else {}
    write_5m = _num(mults.get("cache_write_5m"))
    write_1h = _num(mults.get("cache_write_1h"))
    read = _num(entry.get("cache_read_multiplier"))
    if read is None:
        read = _num(mults.get("cache_read"))
    write_5m = DEFAULT_WRITE_5M if write_5m is None else write_5m
    write_1h = DEFAULT_WRITE_1H if write_1h is None else write_1h
    read = DEFAULT_READ if read is None else read
    return (
        totals["input"] * rate_in
        + totals["output"] * rate_out
        + totals["cw5m"] * rate_in * write_5m
        + totals["cw1h"] * rate_in * write_1h
        + totals["cache_read"] * rate_in * read
    ) / 1_000_000.0


def _minutes(timestamps: list[dt.datetime]) -> tuple[float, float]:
    """(wall_minutes, active_minutes): span of own records, and gaps of 5 minutes or less."""
    times = sorted(timestamps)
    if len(times) < 2:
        return 0.0, 0.0
    wall = (times[-1] - times[0]).total_seconds() / 60.0
    gaps = ((b - a).total_seconds() for a, b in zip(times, times[1:], strict=False))
    active = sum(gap for gap in gaps if gap <= ACTIVE_GAP_SECONDS) / 60.0
    return wall, active


def _is_human(rec: dict[str, Any]) -> bool:
    """A coordinator-file user record a human typed (see the spec's Minutes section)."""
    if rec.get("type") != "user" or rec.get("userType") != "external":
        return False
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return False
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if not all(isinstance(b, dict) and b.get("type") == "text" for b in content):
            return False
        text = "".join(str(b.get("text", "")) for b in content)
    else:
        return False
    return not text.strip().startswith("<")


def meter(
    projects_dir: Path | str,
    sessions: list[str],
    start: dt.datetime,
    end: dt.datetime,
    roles: dict[str, str],
    pricing: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the report dict for the listed sessions inside the inclusive window."""
    projects = Path(projects_dir)
    role_map = dict(roles)
    if pricing is None:
        pricing = tomllib.loads(DEFAULT_PRICING.read_text(encoding="utf-8"))

    files = _collect_files(projects, list(sessions), role_map)
    for file in files:
        file.records = [(ts, rec) for ts, rec in file.records if start <= ts <= end]

    # Ownership: a uuid (and a message.id) belongs to the file whose first record of it
    # has the earliest timestamp; ties break by earlier --session, then file name.
    best_uuid: dict[str, tuple[dt.datetime, int, Path]] = {}
    msg_first: dict[tuple[str, Path], tuple[dt.datetime, int]] = {}
    msg_best: dict[tuple[str, Path], tuple[int, dict[str, Any]]] = {}
    for file in files:
        for ts, rec in file.records:
            uuid = rec.get("uuid")
            if isinstance(uuid, str):
                key = (ts, file.session_index, file.path)
                if uuid not in best_uuid or key < best_uuid[uuid]:
                    best_uuid[uuid] = key
            mid = _message_id(rec)
            if mid is not None:
                stamp = (ts, file.session_index)
                pair = (mid, file.path)
                if pair not in msg_first or stamp < msg_first[pair]:
                    msg_first[pair] = stamp
                output = _as_int(_usage_of(rec).get("output_tokens"))
                if pair not in msg_best or output > msg_best[pair][0]:
                    msg_best[pair] = (output, rec)
    msg_owner: dict[str, tuple[dt.datetime, int, Path]] = {}
    for (mid, path), (ts, index) in msg_first.items():
        key = (ts, index, path)
        if mid not in msg_owner or key < msg_owner[mid]:
            msg_owner[mid] = key
    by_path = {file.path: file for file in files}

    role_names = ["coordinator", *role_map]
    if any(file.role == "unassigned" for file in files):
        role_names.append("unassigned")
    stats: dict[str, dict[str, Any]] = {
        name: {"times": [], "human": 0, "models": {}} for name in role_names
    }

    for file in files:
        own = stats[file.role]
        for ts, rec in file.records:
            uuid = rec.get("uuid")
            if isinstance(uuid, str):
                owner = best_uuid.get(uuid)
                if owner is not None and owner[2] != file.path:
                    continue  # a replayed copy: not an own record
            own["times"].append(ts)
            if not file.is_subagent and _is_human(rec):
                own["human"] += 1

    for (mid, path), (_output, rec) in msg_best.items():
        owner = msg_owner.get(mid)
        if owner is None or owner[2] != path:
            continue  # this file replays the message; its owner counts it
        model_value = _usage_rec_model(rec)
        counts = _usage_counts(_usage_of(rec))
        if model_value == "<synthetic>":
            continue
        if counts["input"] + counts["cache_write"] + counts["cache_read"] + counts["output"] == 0:
            continue
        models = stats[by_path[path].role]["models"]
        totals = models.setdefault(
            model_value,
            {
                "input": 0,
                "cache_write": 0,
                "cache_read": 0,
                "output": 0,
                "cw5m": 0,
                "cw1h": 0,
                "messages": 0,
            },
        )
        for key in ("input", "output", "cache_read", "cache_write", "cw5m", "cw1h"):
            totals[key] += counts[key]
        totals["messages"] += 1

    blocks: dict[str, Any] = {}
    grand_tokens = 0
    for name in role_names:
        own = stats[name]
        model_blocks: dict[str, Any] = {}
        role_tokens = dict.fromkeys(TOKEN_KEYS, 0)
        prices: list[float | None] = []
        for model, totals in own["models"].items():
            model_total = totals["input"] + totals["cache_write"]
            model_total += totals["cache_read"] + totals["output"]
            model_usd = _model_usd(model, totals, pricing)
            prices.append(model_usd)
            model_blocks[model] = {
                "input": totals["input"],
                "cache_write": totals["cache_write"],
                "cache_read": totals["cache_read"],
                "output": totals["output"],
                "total": model_total,
                "messages": totals["messages"],
                "usd": model_usd,
            }
            for key in ("input", "cache_write", "cache_read", "output"):
                role_tokens[key] += totals[key]
            role_tokens["total"] += model_total
        wall, active = _minutes(own["times"])
        grand_tokens += role_tokens["total"]
        blocks[name] = {
            "records": len(own["times"]),
            "messages": sum(t["messages"] for t in own["models"].values()),
            "models": model_blocks,
            "tokens": role_tokens,
            "usd": None if any(p is None for p in prices) else sum(prices),
            "wall_minutes": wall,
            "active_minutes": active,
            "human_messages": own["human"],
        }
    for name in role_names:
        share = blocks[name]["tokens"]["total"]
        blocks[name]["share_of_claude_tokens"] = share / grand_tokens if grand_tokens else 0.0

    total_tokens = dict.fromkeys(TOKEN_KEYS, 0)
    for block in blocks.values():
        for key in TOKEN_KEYS:
            total_tokens[key] += block["tokens"][key]
    role_usds = [block["usd"] for block in blocks.values()]
    return {
        "window": {"start": _iso_z(start), "end": _iso_z(end)},
        "sessions": list(sessions),
        "roles": blocks,
        "total": {
            "tokens": total_tokens,
            "usd": None if any(u is None for u in role_usds) else sum(role_usds),
            "messages": sum(block["messages"] for block in blocks.values()),
        },
    }


def _usage_rec_model(rec: dict[str, Any]) -> str:
    """The record's message.model as a string ('' when absent)."""
    message = rec.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    return model if isinstance(model, str) else ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry: parse arguments, meter the sessions, print or write the report."""
    parser = argparse.ArgumentParser(
        prog="coord_meter",
        description="Report tokens, cost and minutes per role from Claude Code transcripts.",
    )
    parser.add_argument("--projects-dir", required=True, type=Path)
    parser.add_argument("--session", action="append", required=True)
    parser.add_argument("--start", required=True, type=_aware_cli)
    parser.add_argument("--end", required=True, type=_aware_cli)
    parser.add_argument("--role", action="append", default=[])
    parser.add_argument("--pricing", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    roles: dict[str, str] = {}
    for spec in args.role:
        if "=" not in spec:
            parser.error(f"--role needs NAME=AGENT_ID, got {spec!r}")
        name, agent = spec.split("=", 1)
        roles[name] = agent

    for sid in args.session:
        session_file = args.projects_dir / f"{sid}.jsonl"
        if not session_file.is_file():
            print(f"coord_meter: missing session file: {session_file}", file=sys.stderr)
            return 2

    pricing: dict[str, Any] | None = None
    if args.pricing is not None:
        try:
            pricing = tomllib.loads(args.pricing.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"coord_meter: cannot read pricing file {args.pricing}: {exc}", file=sys.stderr)
            return 2

    report = meter(args.projects_dir, args.session, args.start, args.end, roles, pricing)
    text = json.dumps(report, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
