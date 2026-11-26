"""Conditions, paths and templates (spec 2.4). Pure: no clock, no I/O, no randomness."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # schema imports this module; the import back is lazy
    from .schema import Problem


class _Missing:
    """The sentinel for an absent path: falsy, one instance, never equal to data."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: Any) -> _Missing:
        return self


MISSING: Any = _Missing()

COMPARISONS = ("eq", "ne", "in", "gt", "gte", "lt", "lte")
UNARY = ("exists", "truthy")
OPS = COMPARISONS + UNARY
ROOTS = ("nodes", "inputs", "item", "loop")
_HOLE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


class ExprError(ValueError):
    """A template or condition could not be evaluated; `code` is the machine name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve(path: str, scope: Mapping[str, Any]) -> Any:
    """Walk a dotted path through mappings and lists; MISSING when anything is absent."""
    current: Any = scope
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part in current:
                current = current[part]
            else:
                return MISSING
        elif isinstance(current, (list, tuple)):
            if not part.isdigit() or int(part) >= len(current):
                return MISSING
            current = current[int(part)]
        else:
            return MISSING
    return current


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is MISSING:
        return False
    if op == "in":
        return isinstance(right, (list, tuple)) and any(
            _kind(left) == _kind(item) and left == item for item in right)
    if _kind(left) != _kind(right):
        return False
    if op == "eq":
        return bool(left == right)
    if op == "ne":
        return bool(left != right)
    if _kind(left) not in ("number", "str"):
        return False
    return bool({"gt": left > right, "gte": left >= right,
                 "lt": left < right, "lte": left <= right}[op])


def evaluate(condition: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    """Evaluate a condition against `scope`; mismatched types and MISSING are false."""
    if not isinstance(condition, Mapping):
        raise ExprError("bad_condition", "invalid condition: expected object")
    if "all" in condition:
        return all(evaluate(c, scope) for c in condition["all"])
    if "any" in condition:
        return any(evaluate(c, scope) for c in condition["any"])
    if "not" in condition:
        return not evaluate(condition["not"], scope)
    op = condition.get("op")
    if op not in OPS or not isinstance(condition.get("path"), str):
        raise ExprError("bad_condition", f"invalid condition: {dict(condition)!r}")
    value = resolve(condition["path"], scope)
    if op == "exists":
        return value is not MISSING
    if op == "truthy":
        return value is not MISSING and bool(value)
    return _compare(op, value, condition.get("value"))


def _problem(where: str, detail: str) -> Problem:
    from .schema import Problem

    return Problem(where, "bad_condition", f"invalid condition: {detail}")


def check(condition: Any, where: str) -> list[Problem]:
    """Static shape check of a condition; problems are located at or under `where`."""
    if not isinstance(condition, Mapping):
        return [_problem(where, f"expected object, got {_kind(condition)}")]
    keys = set(condition)
    if keys & {"all", "any", "not"}:
        if len(keys) != 1:
            return [_problem(where, "a combinator takes no other keys")]
        key = next(iter(keys))
        inner = condition[key]
        if key == "not":
            return check(inner, f"{where}.not")
        if not isinstance(inner, list):
            return [_problem(f"{where}.{key}", f"{key!r} takes a list of conditions")]
        out: list[Problem] = []
        for i, item in enumerate(inner):
            out.extend(check(item, f"{where}.{key}[{i}]"))
        return out
    extra = keys - {"path", "op", "value"}
    if extra:
        return [_problem(where, f"unknown key {sorted(extra)[0]!r}")]
    if not isinstance(condition.get("path"), str):
        return [_problem(where, "'path' must be a string")]
    op = condition.get("op")
    if op not in OPS:
        return [_problem(where, f"unknown op {op!r}; expected one of {', '.join(OPS)}")]
    if op in COMPARISONS and "value" not in condition:
        return [_problem(where, f"op {op!r} needs a 'value'")]
    return []


def _template_paths(text: str) -> Iterator[str]:
    for match in _HOLE.finditer(text):
        yield match.group(1)


def paths(value: Any) -> list[str]:
    """Every full path a condition or template (or a list/object of them) reads."""
    found: list[str] = []
    if isinstance(value, str):
        found.extend(_template_paths(value))
    elif isinstance(value, Mapping):
        if isinstance(value.get("path"), str) and "op" in value:
            found.append(value["path"])
        else:
            for item in value.values():
                found.extend(paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(paths(item))
    return found


def ref_key(path: str) -> str | None:
    """`nodes.plan.output.x` -> `nodes.plan`; the root and the id a path depends on."""
    parts = path.split(".")
    if parts[0] in ROOTS and len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return None


def references(value: Any) -> set[str]:
    """The `root.id` pairs a condition or template reads."""
    return {key for key in (ref_key(p) for p in paths(value)) if key}


def render(template: str, scope: Mapping[str, Any], *, limit: int = 100_000) -> str:
    """Fill `{{ path }}` holes; strings as-is, everything else as compact JSON."""

    def fill(match: re.Match[str]) -> str:
        path = match.group(1)
        value = resolve(path, scope)
        if value is MISSING:
            raise ExprError("template_unresolved", f"template path {path!r} did not resolve")
        if isinstance(value, str):
            return value
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    out = _HOLE.sub(fill, template)
    if len(out) > limit:
        raise ExprError("template_too_long",
                        f"rendered text is {len(out)} chars; the limit is {limit}")
    return out
