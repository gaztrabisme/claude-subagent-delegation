"""Orchestrator harnesses for the bench matrix: one per orchestrator CLI.

`Orchestrator.argv()` builds the command line for one headless run in a
workspace; `Orchestrator.parse()` reads that CLI's output into the numbers the
matrix records. The shapes are the documented headless ones (claude
`--output-format json`, codex `exec --json` JSONL, gemini `stats.models`,
grok `sessionId`, copilot JSONL with `session.usage_checkpoint`); the fields
that are not settled (grok usage and cost, copilot tokens) are read
defensively and left None rather than guessed.

`DELEGATE_PROMPTS` holds the prompt for each mode (`alone`, `delegate`,
`force`) per harness: claude has the `/delegate` skill command, the others are
told to run `subagent run …` per skills/delegate/SKILL.md.
"""

from __future__ import annotations

import json
from typing import Any

ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill"]

TASK_PROMPT = "Implement the task described in TASK.md in this repository."

CLAUDE_DELEGATE = "/delegate Implement the task described in TASK.md in this repository."
CLAUDE_FORCE = "/delegate force Implement the task described in TASK.md in this repository."
GENERIC_DELEGATE = (
    TASK_PROMPT + " Delegate the implementation with the delegate skill: run `subagent run …` "
    "for the work itself, following skills/delegate/SKILL.md, then review what comes back "
    "before finishing."
)
GENERIC_FORCE = (
    TASK_PROMPT + " You must delegate: run `subagent run …` per skills/delegate/SKILL.md to do "
    "the implementation and do not write it yourself; review what comes back before finishing."
)

DELEGATE_PROMPTS: dict[str, dict[str, str]] = {
    "claude": {
        "alone": TASK_PROMPT + " Verify your work before finishing.",
        "delegate": CLAUDE_DELEGATE,
        "force": CLAUDE_FORCE,
    },
    # Every orchestrator without its own delegate skill command.
    "default": {
        "alone": TASK_PROMPT + " Verify your work before finishing.",
        "delegate": GENERIC_DELEGATE,
        "force": GENERIC_FORCE,
    },
}

# Claude's usage keys, as the other harnesses report them.
USAGE_KEYS = ("input", "output", "cache_read", "cache_write")


def prompts(harness: str) -> dict[str, str]:
    """The three mode prompts for one harness."""
    return DELEGATE_PROMPTS.get(harness) or DELEGATE_PROMPTS["default"]


# --- parsing helpers -------------------------------------------------------------


def _empty_usage() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


def _int(raw: Any) -> int:
    """A non-negative int, or 0 for anything else (stray None, str, bool)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return max(0, int(raw))


def _json_tail(raw: str) -> dict[str, Any] | None:
    """The last line of `raw` that is a JSON object, or None.

    The JSON result is the last line starting with "{": the CLI may print
    warnings before it.
    """
    line = next((one for one in reversed((raw or "").splitlines()) if one.startswith("{")), "")
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _jsonl(raw: str) -> list[dict[str, Any]]:
    """Every JSON object line in `raw`; bad lines are skipped."""
    out = []
    for line in (raw or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _sum_usages(usages: list[dict[str, Any]]) -> dict[str, int]:
    """Claude-shaped usage dicts summed into the bench keys."""
    out = _empty_usage()
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        out["input"] += _int(usage.get("input_tokens"))
        out["output"] += _int(usage.get("output_tokens"))
        out["cache_read"] += _int(usage.get("cache_read_input_tokens"))
        out["cache_write"] += _int(usage.get("cache_creation_input_tokens"))
    return out


def _result(ok: bool, raw: str, **fields: Any) -> dict[str, Any]:
    out = {
        "ok": ok,
        "cost_usd": None,
        "usage": _empty_usage(),
        "turns": None,
        "models": [],
        "session_id": None,
        "credits": None,
        "raw": raw,
    }
    out.update(fields)
    return out


# --- the harnesses ----------------------------------------------------------------


class Orchestrator:
    """One orchestrator CLI: how to run it and how to read what it printed."""

    name = ""

    def argv(self, prompt: str, workspace: str, model: str | None = None) -> list[str]:
        """The command line for one headless run, run with cwd=`workspace`."""
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str) -> dict[str, Any]:
        """The run's outcome from its output. Never raises."""
        raise NotImplementedError


class ClaudeOrchestrator(Orchestrator):
    """`claude -p --output-format json`: `total_cost_usd`, `usage`, `modelUsage`."""

    name = "claude"

    def argv(self, prompt: str, workspace: str, model: str | None = None) -> list[str]:
        argv = ["claude", "-p", prompt, "--output-format", "json", "--no-session-persistence",
                "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS]
        if model:
            argv += ["--model", model]
        return argv

    def parse(self, stdout: str, stderr: str) -> dict[str, Any]:
        raw = stdout or ""
        data = _json_tail(raw)
        if data is None:
            return _result(False, raw)
        # modelUsage covers the subagents too; the top-level usage does not.
        per_model = data.get("modelUsage")
        sums = [entry.get("usage") for entry in (per_model or {}).values()
                if isinstance(entry, dict)] if isinstance(per_model, dict) else []
        usage = _sum_usages(sums or [data.get("usage") or {}])
        models = sorted(k for k in (per_model or {}) if isinstance(k, str))
        if not models and isinstance(data.get("model"), str):
            models = [data["model"]]
        return _result(not data.get("is_error", False), raw, cost_usd=data.get("total_cost_usd"),
                       usage=usage, turns=_int(data.get("num_turns")) or None,
                       models=models, session_id=data.get("session_id"))


class CodexOrchestrator(Orchestrator):
    """`codex exec --json`: usage on the last `turn.completed`; no USD.

    Codex's `input_tokens` includes `cached_input_tokens`; reported uncached
    so the bench does not count cache reads twice.
    """

    name = "codex"

    def argv(self, prompt: str, workspace: str, model: str | None = None) -> list[str]:
        # danger-full-access, not workspace-write: Codex's sandbox forbids
        # binding Unix sockets, so the `subagent run` inside the cell dies on
        # the approval socket. The cell workspace is throwaway, so the bench
        # gives up the sandbox rather than measure a cell that cannot delegate.
        argv = ["codex", "exec", "--json", "-C", str(workspace),
                "-s", "danger-full-access", "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        return [*argv, prompt]

    def parse(self, stdout: str, stderr: str) -> dict[str, Any]:
        raw = stdout or ""
        events = _jsonl(raw)
        if not events:
            return _result(False, raw)
        positions = {id(event): index for index, event in enumerate(events)}
        done = [e for e in events if e.get("type") == "turn.completed"]
        failed = [e for e in events if e.get("type") == "turn.failed"]
        last = max(done + failed, key=lambda e: positions[id(e)]) if done or failed else None
        ok = bool(done) and (last is None or last in done)
        usage = _empty_usage()
        if done:
            raw_usage = done[-1].get("usage") if isinstance(done[-1].get("usage"), dict) else {}
            total_in, cached = _int(raw_usage.get("input_tokens")), _int(
                raw_usage.get("cached_input_tokens"))
            usage = {
                "input": max(0, total_in - cached),
                "output": _int(raw_usage.get("output_tokens")),
                "cache_read": cached,
                "cache_write": _int(raw_usage.get("cache_write_input_tokens")),
            }
        models = sorted({e["model"] for e in events if isinstance(e.get("model"), str)})
        thread = next((e for e in events if e.get("type") == "thread.started"), None)
        session_id = thread.get("thread_id") if isinstance(thread, dict) else None
        return _result(ok, raw, cost_usd=None, usage=usage,
                       turns=len(done) or None, models=models, session_id=session_id)


class GeminiOrchestrator(Orchestrator):
    """`gemini -p --output-format json`: `stats.models.<m>.tokens`."""

    name = "gemini"

    def argv(self, prompt: str, workspace: str, model: str | None = None) -> list[str]:
        argv = ["gemini", "-p", prompt, "--output-format", "json"]
        if model:
            argv += ["-m", model]
        return argv

    def parse(self, stdout: str, stderr: str) -> dict[str, Any]:
        raw = stdout or ""
        data = _json_tail(raw)
        if data is None:
            return _result(False, raw)
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        per_model = stats.get("models") if isinstance(stats.get("models"), dict) else {}
        usage, models = _empty_usage(), []
        for name, entry in per_model.items():
            models.append(str(name))
            tokens = entry.get("tokens") if isinstance(entry, dict) and isinstance(
                entry.get("tokens"), dict) else {}
            # prompt counts the cached reads too (the gemini driver's math).
            usage["input"] += max(0, _int(tokens.get("prompt")) - _int(tokens.get("cached")))
            usage["cache_read"] += _int(tokens.get("cached"))
            usage["output"] += _int(tokens.get("candidates")) + _int(tokens.get("thoughts"))
        error = data.get("error")
        turns = stats.get("turns")
        return _result(not error, raw, usage=usage, models=sorted(models),
                       turns=_int(turns) or None,
                       session_id=data.get("session_id") or data.get("sessionId"))


class GrokOrchestrator(Orchestrator):
    """`grok -p --output-format json`: `sessionId`; usage and cost read defensively."""

    name = "grok"

    def argv(self, prompt: str, workspace: str, model: str | None = None) -> list[str]:
        # --sandbox off, for the same reason as codex's danger-full-access
        # above: the sandbox forbids binding the approval socket, and the cell
        # workspace is throwaway.
        argv = ["grok", "-p", prompt, "--output-format", "json", "--sandbox", "off"]
        if model:
            argv += ["-m", model]
        return argv

    def parse(self, stdout: str, stderr: str) -> dict[str, Any]:
        raw = stdout or ""
        data = _json_tail(raw)
        if data is None:
            return _result(False, raw)
        usage_field = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = {
            "input": _int(usage_field.get("input_tokens") or usage_field.get("prompt_tokens")),
            "output": _int(usage_field.get("output_tokens")
                           or usage_field.get("completion_tokens")),
            "cache_read": _int(usage_field.get("cached_input_tokens")
                               or usage_field.get("cache_read_input_tokens")),
            "cache_write": _int(usage_field.get("cache_creation_input_tokens")),
        }
        cost = data.get("total_cost_usd")
        if cost is None:
            cost = data.get("cost_usd")
        models = sorted(k for k in (data.get("modelUsage") or {}) if isinstance(k, str))
        if not models and isinstance(data.get("model"), str):
            models = [data["model"]]
        turns = data.get("num_turns")
        return _result(not (data.get("is_error") or data.get("error")), raw, cost_usd=cost,
                       usage=usage, turns=_int(turns) or None, models=models,
                       session_id=data.get("sessionId") or data.get("session_id"))


class CopilotOrchestrator(Orchestrator):
    """`copilot -p --output-format json`: JSONL; the last usage checkpoint.

    Copilot bills AI units (`totalNanoAiu`, billionths), not USD; the units
    land in `credits` and `cost_usd` stays None.
    """

    name = "copilot"

    def argv(self, prompt: str, workspace: str, model: str | None = None) -> list[str]:
        argv = ["copilot", "-p", prompt, "--output-format", "json"]
        if model:
            argv += ["--model", model]
        return argv

    def parse(self, stdout: str, stderr: str) -> dict[str, Any]:
        raw = stdout or ""
        events = _jsonl(raw)
        if not events:
            return _result(False, raw)
        checkpoints = [e.get("data") for e in events
                       if e.get("type") == "session.usage_checkpoint"
                       and isinstance(e.get("data"), dict)]
        usage = _empty_usage()
        credits = None
        if checkpoints:
            last = checkpoints[-1]
            usage = {
                "input": _int(last.get("input_tokens")),
                "output": _int(last.get("output_tokens")),
                "cache_read": _int(last.get("cache_read_input_tokens")),
                "cache_write": _int(last.get("cache_creation_input_tokens")),
            }
            nano = last.get("totalNanoAiu")
            if isinstance(nano, (int, float)) and not isinstance(nano, bool):
                credits = round(nano / 1e9, 3)
        turns = sum(1 for e in events if e.get("type") == "assistant.turn_start")
        errors = [e for e in events if str(e.get("type") or "").endswith("error")]
        return _result(bool(events) and not errors, raw, usage=usage, credits=credits,
                       turns=turns or None, session_id=events[0].get("sessionId"))


HARNESSES: dict[str, Orchestrator] = {cls.name: cls() for cls in (
    ClaudeOrchestrator, CodexOrchestrator, GeminiOrchestrator, GrokOrchestrator,
    CopilotOrchestrator,
)}


def get(harness: str) -> Orchestrator:
    """The harness by name; KeyError for a name the matrix does not know."""
    return HARNESSES[harness]
