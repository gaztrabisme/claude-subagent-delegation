"""Redact provider credentials before they reach logs or runtime records."""

from __future__ import annotations

import os
import re

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(\bBearer\s+)[^\s,;\"']+(?:\r?\n[ \t]*[^\s,;\"']+)*"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-(?:[A-Za-z0-9_-]\s*){11,}[A-Za-z0-9_-]\b"), "[REDACTED]"),
    (re.compile(r"\bAIza(?:[0-9A-Za-z_-]\s*){19,}[0-9A-Za-z_-]\b"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_(?:[A-Za-z0-9]\s*){19,}[A-Za-z0-9]\b"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-(?:[A-Za-z0-9-]\s*){9,}[A-Za-z0-9-]\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA(?:[0-9A-Z]\s*){15,}[0-9A-Z]\b"), "[REDACTED]"),
)


def redact_secrets(value: str, env_names=()) -> str:
    """Remove configured key values and common bearer/key formats from text."""
    text = str(value)
    values = sorted(
        {
            value.strip()
            for name in env_names
            if (value := os.environ.get(name)) and len(value.strip()) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in values:
        wrapped = re.compile(r"\s*".join(re.escape(char) for char in secret))
        text = wrapped.sub("[REDACTED]", text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
