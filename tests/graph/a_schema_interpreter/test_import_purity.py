"""Spec section 1: the pure modules import without temporalio; the others need it."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PURE = ("schema", "expr", "ids", "interpreter", "local", "dispatch", "ledger", "admission",
        "exceptions", "server")
LAZY = ("service", "mcp_tools", "cli")
TEMPORAL_MODULES = ("workflow", "activities", "worker")


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-c", textwrap.dedent(code)],
                          capture_output=True, text=True, timeout=60)


def test_pure_modules_import_without_temporalio_and_workflow_does_not():
    code = f"""
    import importlib, sys
    sys.modules["temporalio"] = None
    for name in [m for m in sys.modules if m == "subagent.graph" or m.startswith("subagent.graph.")]:
        del sys.modules[name]
    import subagent.graph as g
    for mod in {PURE!r}:
        importlib.import_module("subagent.graph." + mod)
    assert g.temporal_available() is False
    try:
        importlib.import_module("subagent.graph.workflow")
    except ImportError:
        pass
    else:
        raise SystemExit("workflow imported without temporalio")
    assert sys.modules["temporalio"] is None
    print("ok")
    """
    proc = _run(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_package_exports():
    import subagent.graph as g

    for name in ("SCHEMA_ID", "Graph", "GraphError", "Problem", "GraphUnavailable",
                 "load_graph", "validate", "graph_hash", "temporal_available"):
        assert hasattr(g, name), name
    assert g.SCHEMA_ID == "subagent.graph/1"
    assert issubclass(g.GraphError, ValueError)
    assert issubclass(g.GraphUnavailable, RuntimeError)


def test_temporal_available_true_when_installed():
    pytest.importorskip("temporalio")
    import subagent.graph as g

    assert g.temporal_available() is True


def _package_dir() -> Path:
    import subagent.graph as g

    return Path(g.__file__).parent


def _toplevel_imports(path: Path) -> set[str]:
    """Names imported at module level (not inside a function), as dotted module names."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        for sub in ast.walk(node) if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ():
            if isinstance(sub, ast.Import):
                names.update(a.name for a in sub.names)
            elif isinstance(sub, ast.ImportFrom):
                base = "." * sub.level + (sub.module or "")
                names.add(base)
                names.update(f"{base}.{a.name}".lstrip(".") for a in sub.names)
    return names


@pytest.mark.parametrize("mod", ("__init__",) + PURE + ("ledger", "journal", "store", "trace"))
def test_pure_modules_never_import_temporalio_or_temporal_modules_at_top(mod):
    path = _package_dir() / f"{mod}.py"
    assert path.exists(), path
    names = _toplevel_imports(path)
    assert not any(n == "temporalio" or n.startswith("temporalio.") for n in names), names
    for banned in TEMPORAL_MODULES:
        assert not any(n.endswith(f".{banned}") or n == banned or n.endswith(f"graph.{banned}")
                       for n in names), (mod, names)


@pytest.mark.parametrize("mod", LAZY)
def test_service_tool_and_cli_modules_import_workflow_inside_functions_only(mod):
    path = _package_dir() / f"{mod}.py"
    assert path.exists(), path
    names = _toplevel_imports(path)
    assert not any(n == "temporalio" or n.startswith("temporalio.") for n in names), names
    for banned in ("workflow", "activities", "worker"):
        assert not any(n.endswith(f".{banned}") or n == banned for n in names), (mod, names)
    # and importing them with temporalio hidden works
    code = f"""
    import sys
    sys.modules["temporalio"] = None
    import importlib
    importlib.import_module("subagent.graph.{mod}")
    print("ok")
    """
    proc = _run(code)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("mod", ("interpreter", "workflow"))
def test_determinism_no_forbidden_imports_or_calls(mod):
    """Spec 3.2: no random, uuid, time, sqlite, subprocess, network or datetime.now in workflow code."""
    path = _package_dir() / f"{mod}.py"
    assert path.exists(), path
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("random", "uuid", "time", "sqlite3", "subprocess", "socket", "secrets"):
        assert banned not in imported, (mod, banned)
    for text in ("datetime.now", "datetime.utcnow", "time.time(", "os.environ", "os.getenv"):
        assert text not in source, (mod, text)


def test_importing_module_by_name_works():
    for mod in PURE:
        importlib.import_module(f"subagent.graph.{mod}")
