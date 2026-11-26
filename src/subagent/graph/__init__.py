"""Durable workflow graph. This module is pure: it never imports temporalio."""

from __future__ import annotations

import importlib.util
from functools import lru_cache

from .schema import SCHEMA_ID, Graph, GraphError, Problem, graph_hash, load_graph, validate

__all__ = ["SCHEMA_ID", "Graph", "GraphError", "GraphUnavailable", "Problem", "graph_hash",
           "load_graph", "temporal_available", "validate"]

INSTALL_HINT = "install the graph extra: uv sync --extra graph"
CLI_HINT = "Temporal CLI not found: put temporal on PATH or set TEMPORAL_BIN"


class GraphUnavailable(RuntimeError):
    """A call needs Temporal and the extra or the CLI is missing; the message names the fix."""


@lru_cache(maxsize=1)
def temporal_available() -> bool:
    try:
        return importlib.util.find_spec("temporalio") is not None
    except (ImportError, ValueError):
        return False
