"""Run ids, scopes and request ids (spec 3.4). Pure functions with exact formats."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

_MAX_REQUEST_ID = 200


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_root_run_id(request_id: str | None = None) -> str:
    if request_id is not None:
        return "gr-" + _sha("graph-start:" + request_id)[:12]
    return "gr-" + uuid.uuid4().hex[:12]


def node_scope(node_id: str, enclosing: Sequence[tuple[str, int]] = (), retry: int = 0) -> str:
    prefix = "/".join(f"{name}[{index}]" for name, index in enclosing)
    scope = f"{prefix}/{node_id}" if prefix else node_id
    return f"{scope}~r{retry}" if retry > 0 else scope


def child_run_path(parent_run_path: str, scope: str) -> str:
    return f"{parent_run_path}/{scope}"


def task_request_id(run_path: str, scope: str) -> str:
    full = f"{run_path}/{scope}"
    if len(full) <= _MAX_REQUEST_ID:
        return full
    return full[:180] + "~" + _sha(full)[:16]


def attempt_request_id(request_id: str, attempt: int) -> str:
    return f"{request_id}#a{attempt}"


def exception_id(key: str) -> str:
    return "gx-" + _sha(key)[:12]
