"""Redact provider credentials before they reach logs or runtime records."""

from __future__ import annotations

import os
import re

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(\bBearer\s+)[^\s,;\"']+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
)


def redact_secrets(value: str, env_names=()) -> str:
    """Remove configured key values and common bearer/key formats from text."""
    text = str(value)
    values = sorted(
        {os.environ[name] for name in env_names if os.environ.get(name)},
        key=len,
        reverse=True,
    )
    for secret in values:
        text = text.replace(secret, "[REDACTED]")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
