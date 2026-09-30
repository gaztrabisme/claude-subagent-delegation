"""Source tags for the prompt text visible to this process."""

from __future__ import annotations

import math
from typing import Any

INPUT_SOURCES = ("harness_prompt", "plan", "tool_results", "test_output")
SOURCE_NAMES = (*INPUT_SOURCES, "model_output")


def make_source_usage(
    source_chars: dict[str, int] | None,
    *,
    output_tokens: int,
    reasoning_tokens: int,
    chars_per_token: float = 3.5,
    unmeasured: tuple[str, ...] | list[str] = (),
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Return source counts and estimates without letting observability fail a run."""
    try:
        return _make_source_usage(
            source_chars, output_tokens, reasoning_tokens, chars_per_token, unmeasured
        )
    except Exception:  # noqa: BLE001 - accounting must never fail a worker thread
        model_output = {
            "output_tokens": _nonnegative_int(output_tokens),
            "reasoning_tokens": _nonnegative_int(reasoning_tokens),
        }
        unmeasured = list(INPUT_SOURCES)
        if type(output_tokens) is not int or output_tokens < 0 or (
            type(reasoning_tokens) is not int or reasoning_tokens < 0
        ):
            unmeasured.append("model_output")
        return (
            {
                **{
                    name: {"input_chars": 0, "input_tokens_est": 0}
                    for name in INPUT_SOURCES
                },
                "model_output": model_output,
            },
            unmeasured,
        )


def _make_source_usage(source_chars, output_tokens, reasoning_tokens, chars_per_token, unknown):
    try:
        rate = float(chars_per_token)
        if not math.isfinite(rate) or rate <= 0:
            rate = None
    except (TypeError, ValueError, OverflowError):
        rate = None
    unmeasured = set(unknown) if isinstance(unknown, (tuple, list, set)) else set()
    chars = source_chars if isinstance(source_chars, dict) else {}
    result: dict[str, dict[str, int]] = {}
    for name in INPUT_SOURCES:
        value = chars.get(name)
        if type(value) is not int or value < 0:
            value = 0
            unmeasured.add(name)
        if name in unmeasured:
            value = 0
        estimate = round(value / rate) if rate is not None else 0
        if rate is None:
            unmeasured.add(name)
        result[name] = {"input_chars": value, "input_tokens_est": estimate}
    result["model_output"] = {
        "output_tokens": 0 if "model_output" in unmeasured else _nonnegative_int(output_tokens),
        "reasoning_tokens": (
            0 if "model_output" in unmeasured else _nonnegative_int(reasoning_tokens)
        ),
    }
    if type(output_tokens) is not int or output_tokens < 0 or (
        type(reasoning_tokens) is not int or reasoning_tokens < 0
    ):
        unmeasured.add("model_output")
    return result, [name for name in SOURCE_NAMES if name in unmeasured]


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0
