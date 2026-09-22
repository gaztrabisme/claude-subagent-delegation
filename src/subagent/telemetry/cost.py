"""Join subagent child runs to the parent Claude Code turns that issued them, and price both.

For every child run recorded in a subagent trace file, this computes:
  - counterfactual_usd: the child's tokens priced at a Claude model (default claude-opus-5),
    i.e. what the same work would have cost had the parent done it on Claude;
  - parent_usd: what the parent's own delegating turns cost (the Claude messages that
    issued delegate/await/cancel/... calls for that run);
  - provider_usd: what the provider actually charged, when pricing.toml knows it;
  - net_saving_usd = counterfactual_usd - parent_usd - provider_usd.

Run:  uv run --group report python -m subagent.telemetry.cost --out ./cost-out
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
DEFAULT_TRACES = [
    HOME / ".glm-subagent/sessions/trace.jsonl",
    HOME / ".deepseek-subagent/sessions/trace.jsonl",
    HOME / ".qwen-subagent/sessions/trace.jsonl",
    HOME / ".subagent-mcp/sessions/trace.jsonl",
]
DEFAULT_TRANSCRIPTS = HOME / ".claude/projects"
# pricing.toml stays beside the repo's scripts/, not inside the package.
DEFAULT_PRICING = Path(__file__).resolve().parents[3] / "scripts" / "pricing.toml"

# Trace directory name -> lane, for schema 1/2 records that carry no "lane" field.
TRACE_SOURCE_LANE = {
    "glm-subagent": "glm",
    "deepseek-subagent": "deepseek",
    "qwen-subagent": "bppc",
}
# MCP server name in a parent tool call -> lane. "subagent" is multi-lane: lane comes
# from the tool result, or from the runs recorded in the subagent-mcp trace.
SERVER_LANE = {
    "glm-subagent": "glm",
    "deepseek-subagent": "deepseek",
    "qwen-subagent": "bppc",
    "subagent": None,
}
SERVER_SOURCE = {
    "glm-subagent": "glm-subagent",
    "deepseek-subagent": "deepseek-subagent",
    "qwen-subagent": "qwen-subagent",
    "subagent": "subagent-mcp",
}
TOOL_RE = re.compile(r"^mcp__(glm-subagent|deepseek-subagent|qwen-subagent|subagent)__(\w+)$")
TOOL_USE_ID_RE = re.compile(r'"tool_use_id"\s*:\s*"([^"]+)"')
RUN_ID_RE = re.compile(r"^run-[0-9a-zA-Z]+$")
M = 1_000_000


# --------------------------------------------------------------------------- pricing


def _num(v):
    """A pricing value, or None when unknown."""
    if v is None or (isinstance(v, str) and v.strip().lower() in ("null", "unknown", "")):
        return None
    return float(v)


class Pricing:
    def __init__(self, data: dict, counterfactual: str | None = None):
        mult = data.get("cache_multipliers", {})
        self.w5 = float(mult.get("cache_write_5m", 1.25))
        self.w1h = float(mult.get("cache_write_1h", 2.0))
        self.read = float(mult.get("cache_read", 0.1))
        self.models = data.get("models", {})
        self.providers = data.get("provider", {})
        self.counterfactual = counterfactual or data.get("counterfactual_model", "claude-opus-5")
        if self.resolve(self.counterfactual) is None:
            raise SystemExit(f"counterfactual model {self.counterfactual!r} not in pricing.toml")

    def resolve(self, model: str | None) -> str | None:
        """Map a model string (possibly with a [1m] suffix or date) to a priced model."""
        if not model:
            return None
        m = re.sub(r"\[.*?\]$", "", model.strip())
        if m in self.models:
            return m
        for name in sorted(self.models, key=len, reverse=True):
            if m.startswith(name):
                return name
        return None

    def rates(self, model: str) -> tuple[float, float, float]:
        spec = self.models[model]
        inp = float(spec["input"])
        read_mult = float(spec.get("cache_read_multiplier", self.read))
        return inp, float(spec["output"]), inp * read_mult

    def usd(self, model, input_t, output_t, cache_read_t, write_5m_t, write_1h_t) -> float:
        inp, out, read = self.rates(model)
        return (
            input_t * inp
            + output_t * out
            + cache_read_t * read
            + write_5m_t * inp * self.w5
            + write_1h_t * inp * self.w1h
        ) / M


def load_pricing(path: Path, counterfactual: str | None) -> Pricing:
    with open(path, "rb") as fh:
        return Pricing(tomllib.load(fh), counterfactual)


# --------------------------------------------------------------------------- traces


def _source_of(path: Path) -> str:
    s = str(path)
    for name in ("glm-subagent", "deepseek-subagent", "qwen-subagent", "subagent-mcp"):
        if name in s:
            return name
    return path.parent.name


def read_traces(paths: list[Path]) -> list[dict]:
    runs = []
    for path in paths:
        if not path.is_file():
            print(f"skip missing trace: {path}", file=sys.stderr)
            continue
        source = _source_of(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"run"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("kind") != "run":
                    continue
                u = r.get("usage") or {}
                ver = r.get("verification")
                lane = r.get("lane") or TRACE_SOURCE_LANE.get(source)
                runs.append(
                    {
                        "lane": lane,
                        "provider": r.get("provider"),
                        "source": source,
                        "schema": r.get("schema"),
                        "model": r.get("model"),
                        "run_id": r.get("run_id"),
                        "agent_id": r.get("agent_id"),
                        "session_id": r.get("session_id"),
                        "ts": r.get("ts"),
                        "state": r.get("state"),
                        "finish_reason": r.get("finish_reason"),
                        "elapsed_seconds": r.get("elapsed_seconds"),
                        "verification_passed": (
                            ver.get("passed") if isinstance(ver, dict) else None
                        ),
                        "error": r.get("error"),
                        "input": int(u.get("input") or 0),
                        "output": int(u.get("output") or 0),
                        "cache_read": int(u.get("cache_read") or 0),
                        "cache_write": int(u.get("cache_write") or 0),
                    }
                )
    counts = defaultdict(int)
    for r in runs:
        counts[(r["lane"], r["run_id"])] += 1
    for i, r in enumerate(runs):
        r["idx"] = i
        r["duplicate_run_id"] = counts[(r["lane"], r["run_id"])] > 1
    return runs


# --------------------------------------------------------------------------- transcripts


def _iso_to_epoch(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _parse_json_obj(text: str) -> dict | None:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def iter_transcripts(root: Path):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".jsonl"):
                yield Path(dirpath) / f


def scan_transcripts(root: Path) -> tuple[dict, dict]:
    """Return (calls, messages).

    calls: tool_use_id -> {server, tool, input, msg_id, session_id, project, ts, result}
    messages: message.id -> {model, usage}  (each message.id counted once)
    """
    calls: dict[str, dict] = {}
    messages: dict[str, dict] = {}
    for path in iter_transcripts(root):
        project = path.relative_to(root).parts[0] if path != root else ""
        try:
            fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            continue
        with fh:
            for line in fh:
                has_use = "subagent__" in line
                has_result = "tool_result" in line
                if not (has_use or has_result):
                    continue
                if not has_use:
                    # Cheap pre-filter: parse a result line only if it answers a call we hold.
                    ids = TOOL_USE_ID_RE.findall(line)
                    if not any(i in calls for i in ids):
                        continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        m = TOOL_RE.match(block.get("name") or "")
                        if not m or not block.get("id"):
                            continue
                        tid = block["id"]
                        mid = msg.get("id") or f"noid:{tid}"
                        if mid not in messages:
                            messages[mid] = {
                                "model": msg.get("model"),
                                "usage": msg.get("usage") or {},
                            }
                        if tid in calls:
                            continue  # repeated line
                        calls[tid] = {
                            "server": m.group(1),
                            "tool": m.group(2),
                            "input": block.get("input")
                            if isinstance(block.get("input"), dict)
                            else {},
                            "msg_id": mid,
                            "session_id": rec.get("sessionId"),
                            "project": project,
                            "ts": _iso_to_epoch(rec.get("timestamp")),
                            "result": None,
                        }
                    elif btype == "tool_result":
                        tid = block.get("tool_use_id")
                        call = calls.get(tid)
                        if call is None or call["result"] is not None:
                            continue
                        call["result"] = _parse_json_obj(_result_text(block.get("content"))) or {}
    return calls, messages


# --------------------------------------------------------------------------- join


def _pick_run(candidates: list[dict], call_ts: float | None) -> dict:
    """Among runs sharing (lane, run_id), take the one whose [start, end] is nearest the call."""
    if len(candidates) == 1 or call_ts is None:
        return candidates[0]

    def distance(r):
        end = r["ts"] or 0.0
        start = end - float(r["elapsed_seconds"] or 0.0)
        return max(0.0, start - call_ts, call_ts - end)

    return min(candidates, key=distance)


def join(runs: list[dict], calls: dict) -> dict[int, list[str]]:
    """Map run idx -> list of tool_use ids that referenced it."""
    by_lane_run: dict[tuple, list[dict]] = defaultdict(list)
    by_source_run: dict[tuple, list[dict]] = defaultdict(list)
    for r in runs:
        by_lane_run[(r["lane"], r["run_id"])].append(r)
        by_source_run[(r["source"], r["run_id"])].append(r)

    linked: dict[int, list[str]] = defaultdict(list)
    last_run_for_agent: dict[tuple, str] = {}
    for tid, c in sorted(calls.items(), key=lambda kv: (kv[1]["ts"] or 0, kv[0])):
        res = c["result"] or {}
        inp = c["input"]
        run_id = res.get("run_id") or inp.get("run_id")
        agent_id = res.get("agent_id") or inp.get("agent_id")
        agent_key = (c["session_id"], c["server"], agent_id)
        if not (isinstance(run_id, str) and RUN_ID_RE.match(run_id)):
            run_id = last_run_for_agent.get(agent_key) if agent_id else None
        if not run_id:
            continue
        if agent_id:
            last_run_for_agent[agent_key] = run_id
        lane = res.get("lane") or SERVER_LANE[c["server"]]
        if lane:
            cands = by_lane_run.get((lane, run_id))
        else:
            cands = by_source_run.get((SERVER_SOURCE[c["server"]], run_id))
        if not cands:
            continue
        linked[_pick_run(cands, c["ts"])["idx"]].append(tid)
    return linked


# --------------------------------------------------------------------------- costing


def message_usd(pricing: Pricing, msg: dict) -> tuple[float, bool]:
    """Price one parent message by its own model. Returns (usd, unknown_model)."""
    model = pricing.resolve(msg.get("model"))
    unknown = model is None
    if unknown:
        model = pricing.resolve(pricing.counterfactual)
    u = msg.get("usage") or {}
    cc_total = int(u.get("cache_creation_input_tokens") or 0)
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
    w5 = int(split.get("ephemeral_5m_input_tokens") or 0)
    if w1h + w5 != cc_total:
        # No split (or an inconsistent one): whatever is unaccounted is billed as 5m writes.
        w5 = max(cc_total - w1h, 0)
    usd = pricing.usd(
        model,
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        int(u.get("cache_read_input_tokens") or 0),
        w5,
        w1h,
    )
    return usd, unknown


def provider_costs(pricing: Pricing, runs: list[dict]) -> None:
    month_counts: dict[tuple, int] = defaultdict(int)

    def month(r):
        return dt.datetime.fromtimestamp(r["ts"] or 0, dt.UTC).strftime("%Y-%m")

    for r in runs:
        month_counts[(r["lane"], month(r))] += 1
    for r in runs:
        spec = pricing.providers.get(r["lane"])
        cost = None
        if spec:
            kind = spec.get("kind")
            if kind == "local":
                cost = _num(spec.get("usd"))
            elif kind == "flat_plan":
                monthly = _num(spec.get("monthly_usd"))
                if monthly is not None:
                    cost = monthly / month_counts[(r["lane"], month(r))]
            elif kind == "per_token":
                rin, rout, rread = (_num(spec.get(k)) for k in ("input", "output", "cache_read"))
                if None not in (rin, rout, rread):
                    cost = (
                        (r["input"] + r["cache_write"]) * rin
                        + r["output"] * rout
                        + r["cache_read"] * rread
                    ) / M
        r["provider_usd"] = cost


def build_rows(pricing: Pricing, runs, calls, messages, linked) -> list[dict]:
    cf = pricing.resolve(pricing.counterfactual)
    msg_cache: dict[str, tuple[float, bool]] = {}
    provider_costs(pricing, runs)
    # A parent message can issue calls for several runs (e.g. two awaits in one turn).
    # Its cost is split evenly across those runs so lane totals do not double count.
    msg_runs: dict[str, set[int]] = defaultdict(set)
    for idx, tids in linked.items():
        for t in tids:
            msg_runs[calls[t]["msg_id"]].add(idx)
    rows = []
    for r in runs:
        tids = linked.get(r["idx"], [])
        mids = []
        for t in tids:
            if calls[t]["msg_id"] not in mids:
                mids.append(calls[t]["msg_id"])
        parent_usd = 0.0
        parent_usd_unsplit = 0.0
        unknown_model = False
        for mid in mids:
            if mid not in msg_cache:
                msg_cache[mid] = message_usd(pricing, messages[mid])
            usd, unk = msg_cache[mid]
            parent_usd += usd / len(msg_runs[mid])
            parent_usd_unsplit += usd
            unknown_model |= unk
        first = min((calls[t] for t in tids), key=lambda c: c["ts"] or 0, default=None)
        counterfactual = pricing.usd(
            cf, r["input"], r["output"], r["cache_read"], r["cache_write"], 0
        )
        prov = r["provider_usd"]
        excl = counterfactual - parent_usd
        rows.append(
            {
                "lane": r["lane"],
                "model": r["model"],
                "run_id": r["run_id"],
                "agent_id": r["agent_id"],
                "session_id": r["session_id"],
                "source": r["source"],
                "ts": r["ts"],
                "state": r["state"],
                "finish_reason": r["finish_reason"],
                "elapsed_seconds": r["elapsed_seconds"],
                "verification_passed": r["verification_passed"],
                "error": None if r["error"] is None else str(r["error"]),
                "input": r["input"],
                "output": r["output"],
                "cache_read": r["cache_read"],
                "cache_write": r["cache_write"],
                "counterfactual_usd": counterfactual,
                "provider_usd": prov,
                "parent_session_id": first["session_id"] if first else None,
                "parent_project": first["project"] if first else None,
                "parent_calls": len(tids),
                "parent_messages": len(mids),
                "parent_usd": parent_usd,
                "parent_usd_unsplit": parent_usd_unsplit,
                "parent_shared_messages": sum(1 for m in mids if len(msg_runs[m]) > 1),
                "parent_unknown_model": unknown_model,
                "net_saving_usd": None if prov is None else excl - prov,
                "net_saving_usd_excl_provider": excl,
                "duplicate_run_id": r["duplicate_run_id"],
            }
        )
    return rows


def _is_refusal(row) -> bool:
    return row["state"] == "failed" and not (
        row["input"] or row["output"] or row["cache_read"] or row["cache_write"]
    )


def _sum_or_null(values):
    if any(v is None for v in values):
        return None
    return float(sum(values))


def lane_rows(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["lane"] or "?"].append(r)
    out = []
    for lane in sorted(groups):
        g = groups[lane]
        n = len(g)
        passed = sum(1 for r in g if r["verification_passed"] is True)
        out.append(
            {
                "lane": lane,
                "runs": n,
                "verified_passed": passed,
                "verified_rate": passed / n if n else None,
                "refusals": sum(1 for r in g if _is_refusal(r)),
                "child_tokens": sum(
                    r["input"] + r["output"] + r["cache_read"] + r["cache_write"] for r in g
                ),
                "counterfactual_usd": float(sum(r["counterfactual_usd"] for r in g)),
                "parent_usd": float(sum(r["parent_usd"] for r in g)),
                "provider_usd": _sum_or_null([r["provider_usd"] for r in g]),
                "net_saving_usd": _sum_or_null([r["net_saving_usd"] for r in g]),
                "net_saving_usd_excl_provider": float(
                    sum(r["net_saving_usd_excl_provider"] for r in g)
                ),
                "joined_fraction": sum(1 for r in g if r["parent_calls"] > 0) / n if n else None,
            }
        )
    return out


# --------------------------------------------------------------------------- output


def _fmt(v, kind):
    if v is None:
        return "null"
    if kind == "usd":
        return f"{v:,.2f}"
    if kind == "pct":
        return f"{v * 100:.0f}%"
    if kind == "int":
        return f"{v:,}"
    return str(v)


def print_table(lanes: list[dict], out=sys.stdout) -> None:
    cols = [
        ("lane", "lane", "str"),
        ("runs", "runs", "int"),
        ("verified_passed", "passed", "int"),
        ("verified_rate", "pass%", "pct"),
        ("refusals", "refused", "int"),
        ("child_tokens", "child_tok", "int"),
        ("counterfactual_usd", "cf_usd", "usd"),
        ("parent_usd", "parent_usd", "usd"),
        ("provider_usd", "prov_usd", "usd"),
        ("net_saving_usd", "net_usd", "usd"),
        ("net_saving_usd_excl_provider", "net_excl_prov", "usd"),
        ("joined_fraction", "joined", "pct"),
    ]
    body = [[_fmt(r[k], kind) for k, _h, kind in cols] for r in lanes]
    widths = [
        max(len(h), *(len(b[i]) for b in body)) if body else len(h)
        for i, (_k, h, _) in enumerate(cols)
    ]
    line = "  ".join(
        h.ljust(w) if i == 0 else h.rjust(w)
        for i, ((_k, h, _), w) in enumerate(zip(cols, widths, strict=True))
    )
    print(line, file=out)
    print("-" * len(line), file=out)
    for b in body:
        print(
            "  ".join(
                v.ljust(w) if i == 0 else v.rjust(w)
                for i, (v, w) in enumerate(zip(b, widths, strict=True))
            ),
            file=out,
        )


def write_outputs(out_dir: Path, rows: list[dict], lanes: list[dict]) -> None:
    import csv

    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    runs_df = pd.DataFrame(rows)
    for col in ("provider_usd", "net_saving_usd"):
        runs_df[col] = runs_df[col].astype("Float64") if len(runs_df) else runs_df[col]
    runs_df.to_parquet(out_dir / "runs.parquet", index=False)
    lanes_df = pd.DataFrame(lanes)
    for col in ("provider_usd", "net_saving_usd"):
        if len(lanes_df):
            lanes_df[col] = lanes_df[col].astype("Float64")
    lanes_df.to_parquet(out_dir / "lanes.parquet", index=False)

    fields = [
        "lane",
        "run_id",
        "agent_id",
        "source",
        "model",
        "ts",
        "state",
        "error",
        "duplicate_run_id",
    ]
    with open(out_dir / "unmatched.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if r["parent_calls"] == 0:
                w.writerow(r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--traces", nargs="+", type=Path, default=DEFAULT_TRACES)
    ap.add_argument("--transcripts", type=Path, default=DEFAULT_TRANSCRIPTS)
    ap.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    ap.add_argument("--out", type=Path, default=Path("./cost-out"))
    ap.add_argument("--counterfactual", default=None)
    args = ap.parse_args(argv)

    pricing = load_pricing(args.pricing, args.counterfactual)
    runs = read_traces([p.expanduser() for p in args.traces])
    calls, messages = scan_transcripts(args.transcripts.expanduser())
    linked = join(runs, calls)
    rows = build_rows(pricing, runs, calls, messages, linked)
    lanes = lane_rows(rows)
    write_outputs(args.out, rows, lanes)

    print(f"counterfactual model: {pricing.counterfactual}")
    print(
        f"runs: {len(rows)}  parent subagent calls: {len(calls)}  "
        f"unmatched runs: {sum(1 for r in rows if r['parent_calls'] == 0)}  "
        f"duplicate run_ids: {sum(1 for r in rows if r['duplicate_run_id'])}  "
        f"runs with unknown parent model: {sum(1 for r in rows if r['parent_unknown_model'])}"
    )
    print_table(lanes)
    print(f"wrote {args.out}/runs.parquet, lanes.parquet, unmatched.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
