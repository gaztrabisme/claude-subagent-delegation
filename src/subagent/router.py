"""Which providers a delegation may run on, and what counts as a refusal.

A delegation names a primary provider and a fallback mode. The chain is the
order the providers are tried in: the primary, then the configured
`[fallback].chain`. A provider that refuses before the child has done any work
(plan quota spent, balance empty, usage limit hit, health gate failed) hands
the run to the next one. A failure after work started is the run's result on
that provider and is never rerouted: the child may have edited files, and a
second child starting from the task prompt would not know that.

Refusals are keyed by the provider's *vendor*, not its name: two providers on
z.ai speak the same refusal dialect, and a provider named "glm" pointed at
something else does not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

FALLBACK_MODES = ("full", "local", "none")

# Refusal codes.
ZAI_1308 = "zai_1308"
ZAI_1310 = "zai_1310"
ZAI_1313_EXHAUSTED = "zai_1313_exhausted"
DEEPSEEK_BALANCE = "deepseek_balance"
CODEX_USAGE_LIMIT = "codex_usage_limit"
GROK_BALANCE = "grok_balance"
# The Copilot CLI was asked for a model this account cannot use. It never
# closes the lane: trying the same provider with another model may work.
COPILOT_MODEL_UNAVAILABLE = "copilot_model_unavailable"
HEALTH_FAILED = "health_failed"

# z.ai puts its own code beside the HTTP status: "(429) · [1313][…]", or
# `"code":"1308"` in a JSON body. The code decides whether waiting helps.
_ZAI_CODE_RE = re.compile(r'\[(1[0-9]{3})\]|"code"\s*:\s*"?(1[0-9]{3})\b')
_ZAI_RESET_RE = re.compile(
    r"reset(?:s)?\s+(?:at|on)\s+([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)",
    re.IGNORECASE,
)
# Plan quota: the 5-hour (1308) or weekly/monthly (1310) allowance is spent.
# It resets hours later, so a retry seconds from now only burns wall clock.
ZAI_QUOTA_CODES = frozenset({"1308", "1310"})
# Fair-use throttle: clears on the order of minutes, not seconds.
ZAI_THROTTLE_CODE = "1313"


def clip(text: str, limit: int) -> str:
    """One line of `text`, at most `limit` characters."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def zai_code(text: str) -> str | None:
    """The z.ai error code in `text`, if it carries one."""
    match = _ZAI_CODE_RE.search(text or "")
    if match is None:
        return None
    return match.group(1) or match.group(2)


def zai_reset(text: str) -> str | None:
    """The quota reset time z.ai quoted, if any."""
    match = _ZAI_RESET_RE.search(text or "")
    return match.group(1) if match else None


def code_evidence(stderr: str, event: dict | None) -> str:
    """Where a vendor code may be read: stderr, the event's `error`, and its
    `result` only when that is an API error the CLI reports, never model text.
    A stray `[1308]` in an answer must not switch off a retry."""
    parts = [stderr or ""]
    if isinstance(event, dict):
        if isinstance(event.get("error"), str):
            parts.append(event["error"])
        result = event.get("result")
        if event.get("is_error") and isinstance(result, str) and result.lstrip().startswith(
            "API Error"
        ):
            parts.append(result)
    return "\n".join(parts)

# Hop outcomes.
HOP_RAN = "ran"
HOP_REFUSED = "refused"
HOP_SKIPPED_CLOSED = "skipped_closed"
HOP_HEALTH_FAILED = "health_failed"
HOP_UNAVAILABLE = "unavailable"

# Refusal code of a hop skipped by an enforced admission threshold. Not a lane
# closure: the next dispatch probes the lane again.
ADMISSION = "admission"

# An empty DeepSeek balance: its message, or HTTP 402 as a status (not any
# "402" in the text, such as "timed out after 402 s").
_BALANCE_RE = re.compile(
    r"insufficient balance"
    r"|(?:\bhttp(?:/\d(?:\.\d)?)?|\bstatus(?:[ _]code)?|\bapi error|\berror)\s*[:=]?\s*\(?402\b"
    r"|\b402\s+payment required",
    re.IGNORECASE,
)
_CODEX_LIMIT = "you've hit your usage limit"
# Grok: HTTP 402 with the account's balance spent.
_GROK_BALANCE_RE = re.compile(
    r"usage balance exhausted|\b402\b.*balance|balance.*\b402\b", re.IGNORECASE
)
# "try again at [Sep 20th[, 2026]] 1:29 PM". One parser for both drivers.
_CODEX_AT_RE = re.compile(
    r"try again at\s+"
    r"(?:(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4})\s+)?)?"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AaPp][Mm])",
)
_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

# The longest any refusal keeps a lane closed, whatever reset time it quotes.
MAX_CLOSE = timedelta(days=7)

# Which vendor each refusal code can come from. A code seen on another
# vendor is text that happens to match, not that provider's refusal.
VENDOR_OF_CODE = {
    ZAI_1308: "zai", ZAI_1310: "zai", ZAI_1313_EXHAUSTED: "zai",
    DEEPSEEK_BALANCE: "deepseek", CODEX_USAGE_LIMIT: "codex", GROK_BALANCE: "grok",
}


def no_retry(text: str) -> bool:
    """Whether an error is a limit that waiting seconds will not clear: an
    empty balance (HTTP 402) or a Codex usage limit."""
    text = text or ""
    return bool(_BALANCE_RE.search(text)) or _CODEX_LIMIT in text.lower().replace("\u2019", "'")


def chain(
    primary: str,
    mode: str,
    configured: list[str] | tuple[str, ...] = (),
    providers: Mapping[str, Any] | None = None,
) -> list[str]:
    """Provider names in the order they are tried.

    full: the primary, then `configured` ([fallback].chain). local: the
    primary, then the configured ones marked local. none: the primary only.
    The primary is always first and no provider appears twice.
    """
    if mode == "none":
        rest: tuple[str, ...] = ()
    elif mode == "local":
        known = providers or {}
        rest = tuple(n for n in configured if getattr(known.get(n), "local", False))
    elif mode == "full":
        rest = tuple(configured)
    else:
        raise ValueError(f"unknown fallback {mode!r}")
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
    # Local lanes: the telemetry snapshot taken at dispatch, and whether the
    # admission thresholds would have refused it (telemetry.Admission).
    admission: dict[str, Any] | None = None
    would_refuse: bool | None = None
    admit_reason: str | None = None
    # The health gate found the lane's model not loaded (oMLX models_loaded 0,
    # bppc backend stopped); the run's deadline was extended for the load.
    cold_load: bool = False

    def as_dict(self) -> dict[str, Any]:
        out = {
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
        if self.would_refuse is not None:
            out["would_refuse"] = self.would_refuse
            out["admit_reason"] = self.admit_reason
        if self.cold_load:
            out["cold_load"] = True
        return out


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
            # Grok reports the API error in a list instead of a string.
            if event.get("is_error") and isinstance(event.get("errors"), list):
                parts.extend(e for e in event["errors"] if isinstance(e, str))
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
    "Sep 20th, 2026 1:29 PM" is that date. "Sep 20th 1:29 PM" (no year) is
    this year, or next year when that date has already passed.
    Returned timezone-aware.
    """
    match = _CODEX_AT_RE.search(text or "")
    if match is None:
        return None
    hour = int(match["hour"])
    if not 1 <= hour <= 12 or int(match["minute"]) > 59:
        return None
    hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    minute = int(match["minute"])
    current = now or _local_now()
    if match["month"]:
        month = _MONTHS.get(match["month"].lower())
        if month is None:
            return None
        try:
            if match["year"]:
                when = current.replace(year=int(match["year"]), month=month,
                                       day=int(match["day"]), hour=hour, minute=minute,
                                       second=0, microsecond=0)
            else:
                when = current.replace(month=month, day=int(match["day"]), hour=hour,
                                       minute=minute, second=0, microsecond=0)
                if when <= current:
                    when = when.replace(year=when.year + 1)
        except ValueError:
            return None
        return when.astimezone()
    when = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= current:
        when += timedelta(days=1)
    return when.astimezone()


def zai_reset_at(raw: str | None) -> datetime | None:
    """z.ai's quoted reset time, read as local time. None when absent or unreadable."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")).astimezone()
    except ValueError:
        return None


def classify_refusal(
    vendor: str, events_or_error: list[dict[str, Any]] | str
) -> Refusal | None:
    """The refusal in a run's events (or an error string), or None.

    Each code is read only on its own vendor: z.ai codes on "zai", an empty
    balance on "deepseek", a usage limit on "codex", a spent balance on
    "grok". A local backend never refuses this way; its failures are the run's
    result.

    None means whatever went wrong is not a refusal the router acts on: a
    plain 429 without a vendor code, an auth error, a crash. Those fail the
    run on its provider. A terminal 1313 counts as exhausted because the run
    loop has already spent its throttle retries by the time the events come
    back.
    """
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
        evidence = code_evidence(text, terminal)
    if not text.strip():
        return None
    message = clip(text, 300)
    low = text.lower()

    if vendor == "codex":
        if _CODEX_LIMIT in low.replace("’", "'"):
            return Refusal(CODEX_USAGE_LIMIT, message, codex_reset(text))
        return None
    if vendor == "zai":
        code = zai_code(evidence)
        if code in ZAI_QUOTA_CODES:
            return Refusal(f"zai_{code}", message, zai_reset_at(zai_reset(evidence)))
        if code == ZAI_THROTTLE_CODE:
            return Refusal(ZAI_1313_EXHAUSTED, message, None)
        return None
    if vendor == "deepseek" and _BALANCE_RE.search(text):
        return Refusal(DEEPSEEK_BALANCE, message, None)
    if vendor == "grok" and (_GROK_BALANCE_RE.search(text) or _BALANCE_RE.search(text)):
        return Refusal(GROK_BALANCE, message, None)
    return None


# Codex item types that are the child acting on the machine. A message item
# is not one of them: a child that only answered "I can't, I'm rate limited"
# did no work, however many items the stream carried.
WORK_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


def did_work(events: list[dict[str, Any]]) -> bool:
    """Whether the child did anything in these events.

    Work is a tool call or output tokens above zero, and nothing else. Text
    alone is not: Claude Code reports an API error as a synthetic assistant
    message with zero usage, and a Codex child that answers a usage-limit
    refusal emits an agent_message item with no usage at all. Both are
    exactly the refusal case, whatever the terminal event says.
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
        elif kind in ("result", "turn.completed"):
            usage = event.get("usage")
            if isinstance(usage, dict) and int(usage.get("output_tokens") or 0) > 0:
                return True
        elif kind in ("item.started", "item.completed"):
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") in WORK_ITEMS:
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
    checked again on every dispatch. No closure is longer than MAX_CLOSE.
    """
    now = now or _local_now()
    if refusal.code == HEALTH_FAILED:
        return None
    if refusal.reset_at is not None:
        until = refusal.reset_at if refusal.reset_at > now else None
    elif refusal.code in (DEEPSEEK_BALANCE, GROK_BALANCE):
        until = now + timedelta(hours=balance_close_hours)
    elif refusal.code == ZAI_1313_EXHAUSTED:
        until = now + timedelta(minutes=throttle_close_minutes)
    else:
        until = None
    return min(until, now + MAX_CLOSE) if until is not None else None
