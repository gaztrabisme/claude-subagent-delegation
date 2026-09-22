"""Language-specific date formatting and relative-date ("by Friday") parsing."""

import re
from datetime import date, timedelta

MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Python weekday() numbers: Monday=0 ... Sunday=6.
WEEKDAYS = {
    "en": {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    },
    "vi": {
        "thứ hai": 0, "thứ ba": 1, "thứ tư": 2, "thứ năm": 3,
        "thứ sáu": 4, "thứ bảy": 5, "chủ nhật": 6,
    },
    "ja": {
        "月曜日": 0, "火曜日": 1, "水曜日": 2, "木曜日": 3,
        "金曜日": 4, "土曜日": 5, "日曜日": 6,
    },
}

# The whole matched span is removed from the action text.
RELATIVE_PATTERNS = {
    "en": re.compile(
        r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        re.IGNORECASE,
    ),
    "vi": re.compile(
        r"\bđến\s+(thứ hai|thứ ba|thứ tư|thứ năm|thứ sáu|thứ bảy|chủ nhật)\b",
        re.IGNORECASE,
    ),
    "ja": re.compile(
        r"(月曜日|火曜日|水曜日|木曜日|金曜日|土曜日|日曜日)\s*まで",
    ),
}

# ISO due date, optionally preceded by the word "by".
ISO_PATTERN = re.compile(r"\b(?:by\s+)?(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)


def format_date(d: date, lang: str) -> str:
    if lang == "en":
        return f"{MONTHS_EN[d.month - 1]} {d.day}, {d.year}"
    if lang == "vi":
        return f"ngày {d.day} tháng {d.month} năm {d.year}"
    if lang == "ja":
        return f"{d.year}年{d.month}月{d.day}日"
    raise ValueError(f"unknown language: {lang}")


def find_due(text, meeting_date, lang):
    """Return ``(due_string, (start, end))`` or ``(None, None)``.

    ``due_string`` is an ISO date string; the span covers the matched text
    (including an optional preceding ``by`` for ISO dates) so it can be removed.
    """
    for m in ISO_PATTERN.finditer(text):
        try:
            date.fromisoformat(m.group(1))
        except ValueError:
            continue
        return m.group(1), (m.start(), m.end())

    if meeting_date is not None:
        m = RELATIVE_PATTERNS[lang].search(text)
        if m:
            weekday = WEEKDAYS[lang][m.group(1).lower()]
            delta = (weekday - meeting_date.weekday()) % 7
            due = meeting_date + timedelta(days=delta)
            return due.isoformat(), (m.start(), m.end())

    return None, None
