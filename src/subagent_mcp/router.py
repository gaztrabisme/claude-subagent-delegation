"""Which lanes a delegation may run on, and what counts as a refusal.

A delegation names a primary lane and a fallback mode. The chain is the order
the lanes are tried in. A lane that refuses before the child has done any work
(plan quota spent, balance empty, usage limit hit, health gate failed) hands
the run to the next lane. A failure after work started is the run's result on
that lane and is never rerouted: the child may have edited files, and a second
child starting from the task prompt would not know that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# Chain order after the primary: the cloud lanes, then the local ones.
CLOUD_ORDER = ("codex", "deepseek", "glm")
LOCAL_ORDER = ("bppc", "omlx")

# Refusal codes.
ZAI_1308 = "zai_1308"
ZAI_1310 = "zai_1310"
ZAI_1313_EXHAUSTED = "zai_1313_exhausted"
DEEPSEEK_BALANCE = "deepseek_balance"
CODEX_USAGE_LIMIT = "codex_usage_limit"
HEALTH_FAILED = "health_failed"

# Hop outcomes.
HOP_RAN = "ran"
HOP_REFUSED = "refused"
HOP_SKIPPED_CLOSED = "skipped_closed"
HOP_HEALTH_FAILED = "health_failed"
HOP_UNAVAILABLE = "unavailable"

_BALANCE_RE = re.compile(r"insufficient balance|\b402\b", re.IGNORECASE)
_CODEX_LIMIT = "you've hit your usage limit"
_CODEX_AT_RE = re.compile(
    r"try again at\s+("
    r"[A-Z][a-z]{2,8}\s+\d{1,2}(?:st|nd|rd|th)?,\s*\d{4}\s+\d{1,2}:\d{2}\s*[AP]M"
    r"|\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)


def no_retry(text: str) -> bool:
    """Whether an error is a limit that waiting seconds will not clear: an
    empty balance (HTTP 402) or a Codex usage limit."""
    text = text or ""
    return bool(_BALANCE_RE.search(text)) or _CODEX_LIMIT in text.lower().replace("\u2019", "'")


def chain(primary: str, fallback: str) -> list[str]:
    """Lane names in the order they are tried.

    full: the primary, the other cloud lanes (codex, deepseek, glm), then bppc
    and omlx. local: the primary, then bppc and omlx. none: the primary only.
    The primary is always first and no lane appears twice.
    """
    if fallback == "none":
        rest: tuple[str, ...] = ()
    elif fallback == "local":
        rest = LOCAL_ORDER
    elif fallback == "full":
        rest = CLOUD_ORDER + LOCAL_ORDER
    else:
        raise ValueError(f"unknown fallback {fallback!r}")
    out = [primary]
    for name in rest:
        if name not in out:
            out.append(name)
    return out


@dataclass(frozen=True, slots=True)
class Refusal:
    """A lane saying no before the child did anything."""

    code: str
    message: str
    reset_at: datetime | None = None


@dataclass(slots=True)
class Hop:
    """One lane the router tried for a run, and what happened there."""

    index: int
    lane: str
    provider: str
    model: str | None
    outcome: str
    code: str | None = None
    message: str | None = None
    reset_at: datetime | None = None
    closed_until: datetime | None = None
    # The lane's driver ("claude" / "codex") and its guard ("hook" /
    # "sandbox+hook"), whether or not the hop ran.
    driver: str | None = None
    guard: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hop": self.index,
            "lane": self.lane,
            "provider": self.provider,
            "driver": self.driver,
            "guard": self.guard,
            "model": self.model,
            "outcome": self.outcome,
            "code": self.code,
            "message": self.message,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "closed_until": self.closed_until.isoformat() if self.closed_until else None,
        }


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _error_text(events: list[dict[str, Any]]) -> str:
    """What the stream itself reports as an error, never the model's own text.

    Claude driver: a result event's `error`, and its `result` when is_error is
    set. Codex driver: `error` events and `turn.failed` error messages.
    """
    parts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "result":
            if isinstance(event.get("error"), str):
                parts.append(event["error"])
            if event.get("is_error") and isinstance(event.get("result"), str):
                parts.append(event["result"])
        elif kind == "error" and isinstance(event.get("message"), str):
            parts.append(event["message"])
        elif kind == "turn.failed":
            err = event.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                parts.append(err["message"])
            elif isinstance(err, str):
                parts.append(err)
    return "\n".join(parts)


def codex_reset(text: str, now: datetime | None = None) -> datetime | None:
    """The time a Codex usage-limit message says to try again, in local time.

    "1:01 PM" is today, or tomorrow when that time has already passed.
    "Sep 20th, 2026 1:29 PM" is that date.
    """
    match = _CODEX_AT_RE.search(text or "")
    if match is None:
        return None
    raw = " ".join(match.group(1).split())
    now = now or _local_now()
    if raw[0].isdigit():
        try:
            clock = datetime.strptime(raw.upper().replace(" ", ""), "%I:%M%p")
        except ValueError:
            return None
        when = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(cleaned, fmt).astimezone()
        except ValueError:
            continue
    return None


def zai_reset_at(raw: str | None) -> datetime | None:
    """z.ai's quoted reset time, read as local time. None when absent or unreadable."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")).astimezone()
    except ValueError:
        return None


def classify_refusal(lane: str, events_or_error: list[dict[str, Any]] | str) -> Refusal | None:
    """The refusal in a run's events (or an error string), or None.

    None means whatever went wrong is not a refusal the router acts on: a
    plain 429 without a z.ai code, an auth error, a crash. Those fail the run
    on its lane. A terminal 1313 counts as exhausted because the run loop has
    already spent its throttle retries by the time the events come back.
    """
    from . import runs  # runs imports this module; bind late.

    if isinstance(events_or_error, str):
        text = events_or_error
        evidence = text
    else:
        text = _error_text(events_or_error)
        terminal = next(
            (e for e in reversed(events_or_error)
             if isinstance(e, dict) and e.get("type") == "result"),
            None,
        )
        evidence = runs._code_evidence(text, terminal)
    if not text.strip():
        return None
    message = runs._clip(text, 300)
    low = text.lower()

    if _CODEX_LIMIT in low.replace("’", "'"):
        return Refusal(CODEX_USAGE_LIMIT, message, codex_reset(text))
    code = runs.zai_code(evidence)
    if code in runs.ZAI_QUOTA_CODES:
        return Refusal(f"zai_{code}", message, zai_reset_at(runs.zai_reset(evidence)))
    if code == runs.ZAI_THROTTLE_CODE:
        return Refusal(ZAI_1313_EXHAUSTED, message, None)
    if _BALANCE_RE.search(text):
        return Refusal(DEEPSEEK_BALANCE, message, None)
    return None


def did_work(events: list[dict[str, Any]]) -> bool:
    """Whether the child did anything in these events.

    Work is a tool call or assistant output tokens above zero. Assistant text
    alone is not: Claude Code reports an API error as a synthetic assistant
    message with zero usage, and that is exactly the refusal case.
    """
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else event
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            ):
                return True
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict) and int(usage.get("output_tokens") or 0) > 0:
                return True
        elif kind == "result":
            usage = event.get("usage")
            if isinstance(usage, dict) and int(usage.get("output_tokens") or 0) > 0:
                return True
        elif kind in ("item.started", "item.completed"):
            # Codex: any item (command, file change, message) is work.
            return True
        elif kind == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict) and int(usage.get("output_tokens") or 0) > 0:
                return True
    return False


def close_until(
    refusal: Refusal,
    *,
    balance_close_hours: float,
    throttle_close_minutes: float,
    now: datetime | None = None,
) -> datetime | None:
    """How long lane memory keeps the lane closed after `refusal`, or None.

    A quoted reset time closes it until then. An empty balance gives no reset
    time, so it closes for balance_close_hours; a spent 1313 throttle for
    throttle_close_minutes. A failed health gate never closes a lane: it is
    checked again on every dispatch.
    """
    now = now or _local_now()
    if refusal.code == HEALTH_FAILED:
        return None
    if refusal.reset_at is not None:
        return refusal.reset_at if refusal.reset_at > now else None
    if refusal.code == DEEPSEEK_BALANCE:
        return now + timedelta(hours=balance_close_hours)
    if refusal.code == ZAI_1313_EXHAUSTED:
        return now + timedelta(minutes=throttle_close_minutes)
    return None
