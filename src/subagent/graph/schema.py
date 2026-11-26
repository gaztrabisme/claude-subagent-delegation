"""Graph document model: parse, validate, canonical hash (spec 2). Pure."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import expr

SCHEMA_ID = "subagent.graph/1"
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
KINDS = ("task", "gate", "parallel", "subgraph", "owner_decision", "loop")
JOINS = ("allSettled", "failFast", "any")
ON_FAILURE = ("fail", "continue", "exception")
RETRY_ON = ("failed", "unverified", "interrupted", "lane_refused")
DEFAULT_RESERVE_USD = 0.50
DEFAULT_TIMEOUT_S = 1800
DEFAULT_RETRY_ON = ("interrupted", "lane_refused")

_TOP_KEYS = ("schema", "id", "version", "description", "workspace", "inputs", "budget_usd",
             "defaults", "graphs", "body", "output")
_DEFAULT_KEYS = ("provider", "model", "workspace", "max_steps", "timeout_s", "reserve_usd",
                 "priority", "retries", "retry_on")
_COMMON_KEYS = ("id", "kind", "when", "on_failure", "label")
_KIND_KEYS = {
    "task": ("provider", "model", "prompt", "instructions", "workspace", "verification",
             "max_steps", "timeout_s", "retries", "retry_on", "retry_backoff_s", "reserve_usd",
             "priority", "role"),
    "gate": ("command", "condition", "workspace", "timeout_s", "retries"),
    "parallel": ("join", "branches", "for_each"),
    "subgraph": ("graph", "body", "inputs", "budget_usd", "priority", "output"),
    "owner_decision": ("question", "options", "default", "timeout_s"),
    "loop": ("body", "max_iterations", "until"),
}
# key -> (kind, lo, hi) for the numeric task fields (also used for `defaults`)
_NUMERIC = {
    "max_steps": ("int", 1, 500), "timeout_s": ("num", 60, 14400), "retries": ("int", 0, 3),
    "retry_backoff_s": ("num", 0, 3600), "reserve_usd": ("num", 0, 1_000_000),
    "priority": ("int", 1, 5),
}


@dataclass(frozen=True)
class Problem:
    path: str
    code: str
    message: str


class GraphError(ValueError):
    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(f"{p.path}: {p.message} [{p.code}]" for p in self.problems))


# --- node model ------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Node:
    id: str
    kind: str
    when: Mapping[str, Any] | None = None
    on_failure: str = "fail"
    label: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskNode(Node):
    provider: str | None = None
    model: str | None = None
    prompt: str = ""
    instructions: str | None = None
    workspace: str | None = None
    verification: str = "true"
    max_steps: int | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    retries: int = 1
    retry_on: tuple[str, ...] = DEFAULT_RETRY_ON
    retry_backoff_s: float = 30
    reserve_usd: float = DEFAULT_RESERVE_USD
    priority: int = 3
    role: str | None = None


@dataclass(frozen=True, kw_only=True)
class GateNode(Node):
    command: str | None = None
    condition: Mapping[str, Any] | None = None
    workspace: str | None = None
    timeout_s: float | None = None
    retries: int = 0


@dataclass(frozen=True, kw_only=True)
class ForEach:
    items: str
    as_: str
    node: Node
    max_items: int = 64


@dataclass(frozen=True, kw_only=True)
class ParallelNode(Node):
    branches: tuple[Node, ...] = ()
    for_each: ForEach | None = None
    join: str = "allSettled"


@dataclass(frozen=True, kw_only=True)
class SubgraphNode(Node):
    graph: str | None = None
    body: tuple[Node, ...] | None = None
    raw_body: tuple[Mapping[str, Any], ...] | None = None
    inputs: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    output: Mapping[str, str] = dataclasses.field(default_factory=dict)
    budget_usd: float | None = None
    priority: int | None = None


@dataclass(frozen=True, kw_only=True)
class OwnerDecisionNode(Node):
    question: str = ""
    options: tuple[str, ...] = ()
    default: str | None = None
    timeout_s: float | None = None


@dataclass(frozen=True, kw_only=True)
class LoopNode(Node):
    body: tuple[Node, ...] = ()
    max_iterations: int = 1
    until: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class Body:
    name: str
    inputs: Mapping[str, Any]
    body: tuple[Node, ...]
    output: Mapping[str, str]


@dataclass(frozen=True)
class Graph:
    doc: Mapping[str, Any]
    id: str
    version: int
    body: tuple[Node, ...]
    graphs: Mapping[str, Body]
    inputs: Mapping[str, Any]
    defaults: Mapping[str, Any]
    budget_usd: float | None
    workspace: str | None
    output: Mapping[str, str]

    def node(self, node_id: str) -> Node:
        for found in _walk(self.body):
            if found.id == node_id:
                return found
        for named in self.graphs.values():
            for found in _walk(named.body):
                if found.id == node_id:
                    return found
        raise KeyError(node_id)


def _walk(nodes: Sequence[Node]) -> Iterator[Node]:
    for node in nodes:
        yield node
        if isinstance(node, ParallelNode):
            yield from _walk(node.branches)
            if node.for_each is not None:
                yield from _walk([node.for_each.node])
        elif isinstance(node, LoopNode) or isinstance(node, SubgraphNode) and node.body:
            yield from _walk(node.body)


# --- parse and load ----------------------------------------------------------


def parse(source: str | bytes | Mapping[str, Any], *, name: str = "<graph>") -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        if text.lstrip().startswith(("{", "[")):
            raise GraphError([Problem("$", "parse_error",
                                      f"not valid JSON: {err.msg} (line {err.lineno}, "
                                      f"column {err.colno})")]) from None
        try:
            import yaml
        except ImportError:
            raise GraphError([Problem("$", "yaml_unavailable",
                                      "YAML needs PyYAML; send JSON instead")]) from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as yerr:
            raise GraphError([Problem("$", "parse_error", f"not valid YAML: {yerr}")]) from None
    if not isinstance(data, dict):
        raise GraphError([Problem("$", "type", f"expected object, got {_tn(data)}")])
    return data


def graph_hash(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _names_a_file(text: str) -> bool:
    if "\n" in text or text.lstrip().startswith(("{", "[")) or len(text) > 4096:
        return False
    try:
        return Path(text).is_file()
    except OSError:
        return False


def load_graph(source: str | bytes | Path | Mapping[str, Any], *, max_depth: int = 3) -> Graph:
    if isinstance(source, Path) or (isinstance(source, str) and _names_a_file(source)):
        source = Path(source).read_text(encoding="utf-8")
    document = parse(source)
    problems = validate(document, max_depth=max_depth)
    if problems:
        raise GraphError(problems)
    return _build_graph(document)


# --- validation ----------------------------------------------------------------


def _tn(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _fmt(value: Any) -> str:
    return str(int(value)) if isinstance(value, float) and value == int(value) else str(value)


@dataclass(frozen=True)
class _Ctx:
    visible: frozenset[str]
    inputs: frozenset[str]
    ns: dict[str, str]
    items: frozenset[str] = frozenset()
    loops: frozenset[str] = frozenset()
    main: bool = True
    nested: bool = False


class _Validator:
    def __init__(self, doc: Mapping[str, Any], max_depth: int, max_nodes: int) -> None:
        self.doc = doc
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.problems: list[Problem] = []
        self.count = 0
        graphs = doc.get("graphs")
        self.graphs: Mapping[str, Any] = graphs if isinstance(graphs, Mapping) else {}
        defaults = doc.get("defaults")
        self.defaults: Mapping[str, Any] = defaults if isinstance(defaults, Mapping) else {}

    # -- problem helpers
    def add(self, path: str, code: str, message: str) -> None:
        self.problems.append(Problem(path, code, message))

    def type_(self, path: str, expected: str, value: Any) -> None:
        self.add(path, "type", f"expected {expected}, got {_tn(value)}")

    def range_(self, path: str, key: str, lo: Any, hi: Any, value: Any) -> None:
        self.add(path, "range", f"{key} must be between {_fmt(lo)} and {_fmt(hi)}, got {value}")

    def unknown_keys(self, node: Mapping[str, Any], allowed: Sequence[str], path: str) -> None:
        for key in node:
            if key not in allowed:
                self.add(f"{path}.{key}", "unknown_key", f"unknown key {key!r}")

    def required(self, node: Mapping[str, Any], keys: Sequence[str], path: str) -> None:
        for key in keys:
            if key not in node:
                self.add(path, "required", f"missing required key {key!r}")

    def bad_id(self, value: Any, path: str) -> bool:
        if not isinstance(value, str):
            self.type_(path, "string", value)
            return False
        if not ID_RE.match(value):
            self.add(path, "bad_id", f"invalid id {value!r}: use lowercase letters, digits, - "
                                     "and _, starting with a letter")
            return False
        return True

    def string(self, node: Mapping[str, Any], key: str, path: str, *, lo: int = 0,
               hi: int | None = None) -> None:
        if key not in node:
            return
        value = node[key]
        if not isinstance(value, str):
            self.type_(f"{path}.{key}", "string", value)
        elif len(value) < lo or (hi is not None and len(value) > hi):
            self.range_(f"{path}.{key}", key, lo, hi if hi is not None else "unlimited",
                        len(value))

    def numeric(self, node: Mapping[str, Any], key: str, path: str,
                spec: tuple[str, float, float] | None = None) -> None:
        if key not in node:
            return
        kind, lo, hi = spec or _NUMERIC[key]
        value = node[key]
        ok = _is_int(value) if kind == "int" else _is_num(value)
        if not ok:
            self.type_(f"{path}.{key}", "integer" if kind == "int" else "number", value)
        elif not lo <= value <= hi:
            self.range_(f"{path}.{key}", key, lo, hi, value)

    def retry_on(self, node: Mapping[str, Any], path: str) -> None:
        value = node.get("retry_on")
        if value is None:
            return
        if not isinstance(value, list):
            return self.type_(f"{path}.retry_on", "array", value)
        for i, item in enumerate(value):
            if item not in RETRY_ON:
                self.add(f"{path}.retry_on[{i}]", "type",
                         f"expected one of {', '.join(RETRY_ON)}, got {item!r}")

    # -- references
    def check_path(self, p: str, loc: str, ctx: _Ctx) -> None:
        parts = p.split(".")
        root, name = parts[0], parts[1] if len(parts) > 1 else None
        if root == "nodes" and name is not None:
            if name not in ctx.visible:
                self.add(loc, "unknown_reference", f"{p!r} refers to node {name!r}, which does "
                                                   "not run before this node")
        elif root == "inputs" and name is not None:
            if name not in ctx.inputs:
                self.add(loc, "unknown_input", f"{p!r} refers to input {name!r}, which the "
                                               "graph does not declare")
        elif root == "item" and name is not None:
            if name not in ctx.items:
                self.add(loc, "unknown_reference",
                         f"{p!r} refers to item {name!r}, which no enclosing for_each defines")
        elif root == "loop" and name is not None:
            if name not in ctx.loops:
                self.add(loc, "unknown_reference",
                         f"{p!r} refers to loop {name!r}, which does not enclose this node")
        else:
            self.add(loc, "unknown_reference",
                     f"{p!r} must start with nodes, inputs, item or loop")

    def check_refs(self, value: Any, loc: str, ctx: _Ctx, *, inputs: bool = True) -> None:
        for p in expr.paths(value):
            if not inputs and p.split(".")[0] == "inputs":
                continue
            self.check_path(p, loc, ctx)

    def condition(self, node: Mapping[str, Any], key: str, path: str, ctx: _Ctx) -> None:
        if key not in node:
            return
        problems = expr.check(node[key], f"{path}.{key}")
        self.problems.extend(problems)
        if not problems:
            # `when` may probe an input the graph does not declare (a run-time `exists`)
            self.check_refs(node[key], f"{path}.{key}", ctx, inputs=key != "when")

    def template(self, node: Mapping[str, Any], key: str, path: str, ctx: _Ctx, *,
                 lo: int = 0, hi: int = 100_000) -> None:
        self.string(node, key, path, lo=lo, hi=hi)
        if isinstance(node.get(key), str):
            self.check_refs(node[key], f"{path}.{key}", ctx)

    def output_map(self, value: Any, path: str, ctx: _Ctx) -> None:
        if not isinstance(value, Mapping):
            return self.type_(path, "object", value)
        for key, target in value.items():
            if not isinstance(target, str):
                self.type_(f"{path}.{key}", "string", target)
            else:
                self.check_path(target, f"{path}.{key}", ctx)

    # -- bodies and nodes
    def seq(self, raw: Any, path: str, key: str, ctx: _Ctx, hi: int) -> set[str]:
        if not isinstance(raw, list):
            self.type_(path, "array", raw)
            return set()
        if not 1 <= len(raw) <= hi:
            self.range_(path, key, 1, hi, len(raw))
        visible = set(ctx.visible)
        declared: set[str] = set()
        for i, item in enumerate(raw):
            inner = self.node(item, f"{path}[{i}]", dataclasses.replace(
                ctx, visible=frozenset(visible)))
            visible |= inner
            declared |= inner
        return declared

    def node(self, node: Any, path: str, ctx: _Ctx) -> set[str]:
        if not isinstance(node, Mapping):
            self.type_(path, "object", node)
            return set()
        self.count += 1
        nid = node.get("id")
        exposed: set[str] = set()
        if "id" not in node:
            self.required(node, ["id"], path)
        elif self.bad_id(nid, f"{path}.id"):
            if nid in ctx.ns:
                self.add(path, "duplicate_id",
                         f"duplicate node id {nid!r} (first at {ctx.ns[nid]})")
            else:
                ctx.ns[nid] = path
            exposed.add(nid)
        kind = node.get("kind")
        if "kind" not in node:
            self.required(node, ["kind"], path)
            return exposed
        if kind not in KINDS:
            self.add(f"{path}.kind", "unknown_kind",
                     f"unknown node kind {kind!r}; expected one of {', '.join(KINDS)}")
            return exposed
        self.unknown_keys(node, _COMMON_KEYS + _KIND_KEYS[kind], path)
        self.condition(node, "when", path, ctx)
        if "on_failure" in node and node["on_failure"] not in ON_FAILURE:
            self.add(f"{path}.on_failure", "type",
                     f"expected one of {', '.join(ON_FAILURE)}, got {node['on_failure']!r}")
        self.string(node, "label", path, hi=200)
        handler = getattr(self, f"kind_{kind}")
        return exposed | handler(node, path, ctx)

    def kind_task(self, node: Mapping[str, Any], path: str, ctx: _Ctx) -> set[str]:
        self.required(node, ["prompt", "verification"], path)
        if "provider" not in node and "provider" not in self.defaults:
            self.required(node, ["provider"], path)
        for key in ("provider", "model", "workspace", "verification"):
            self.string(node, key, path, lo=1 if key in ("provider", "verification") else 0)
        self.string(node, "role", path, hi=40)
        self.template(node, "prompt", path, ctx, lo=1)
        self.template(node, "instructions", path, ctx)
        for key in _NUMERIC:
            self.numeric(node, key, path)
        self.retry_on(node, path)
        return set()

    def kind_gate(self, node: Mapping[str, Any], path: str, ctx: _Ctx) -> set[str]:
        if ("command" in node) == ("condition" in node):
            self.add(path, "exactly_one", "give exactly one of 'command' or 'condition'")
        self.string(node, "command", path, lo=1)
        self.string(node, "workspace", path)
        self.condition(node, "condition", path, ctx)
        self.numeric(node, "timeout_s", path, ("num", 1, 3600))
        self.numeric(node, "retries", path)
        return set()

    def kind_parallel(self, node: Mapping[str, Any], path: str, ctx: _Ctx) -> set[str]:
        if ("branches" in node) == ("for_each" in node):
            self.add(path, "exactly_one", "give exactly one of 'branches' or 'for_each'")
        join = node.get("join", "allSettled")
        if join not in JOINS:
            self.add(f"{path}.join", "join_policy",
                     f"unknown join {join!r}; expected allSettled, failFast or any")
        declared: set[str] = set()
        if "branches" in node:
            branches = node["branches"]
            if not isinstance(branches, list):
                self.type_(f"{path}.branches", "array", branches)
            else:
                if not 1 <= len(branches) <= 64:
                    self.range_(f"{path}.branches", "branches", 1, 64, len(branches))
                for i, branch in enumerate(branches):
                    declared |= self.node(branch, f"{path}.branches[{i}]", ctx)
        if "for_each" in node:
            self.for_each(node["for_each"], f"{path}.for_each", ctx)
        return declared

    def for_each(self, spec: Any, path: str, ctx: _Ctx) -> None:
        if not isinstance(spec, Mapping):
            return self.type_(path, "object", spec)
        self.required(spec, ["items", "as", "node"], path)
        self.unknown_keys(spec, ("items", "as", "node", "max_items"), path)
        if isinstance(spec.get("items"), str):
            self.check_path(spec["items"], f"{path}.items", ctx)
        elif "items" in spec:
            self.type_(f"{path}.items", "string", spec["items"])
        self.numeric(spec, "max_items", path, ("int", 1, 64))
        name = spec.get("as")
        valid = "as" in spec and self.bad_id(name, f"{path}.as")
        if "node" in spec:
            inner = dataclasses.replace(
                ctx, items=ctx.items | ({name} if valid else frozenset()))
            self.node(spec["node"], f"{path}.node", inner)

    def kind_subgraph(self, node: Mapping[str, Any], path: str, ctx: _Ctx) -> set[str]:
        if ("graph" in node) == ("body" in node):
            self.add(path, "exactly_one", "give exactly one of 'graph' or 'body'")
        self.numeric(node, "priority", path)
        if "budget_usd" in node:
            value = node["budget_usd"]
            if not _is_num(value):
                self.type_(f"{path}.budget_usd", "number", value)
            elif value <= 0:
                self.range_(f"{path}.budget_usd", "budget_usd", 0.000001, 1_000_000, value)
        inputs = node.get("inputs", {})
        if not isinstance(inputs, Mapping):
            self.type_(f"{path}.inputs", "object", inputs)
            inputs = {}
        else:
            self.check_refs(dict(inputs), f"{path}.inputs", ctx)
        if "graph" in node and "output" in node:
            self.add(f"{path}.output", "unknown_key", "unknown key 'output'")
        if "graph" in node:
            name = node["graph"]
            if not isinstance(name, str):
                self.type_(f"{path}.graph", "string", name)
            elif name not in self.graphs:
                self.add(f"{path}.graph", "subgraph_unknown",
                         f"subgraph {name!r} is not defined in \"graphs\"")
            else:
                declared = self.graphs[name].get("inputs") if isinstance(
                    self.graphs[name], Mapping) else None
                for key in inputs:
                    if isinstance(declared, Mapping) and key not in declared:
                        self.add(f"{path}.inputs.{key}", "unknown_key", f"unknown key {key!r}")
        if "body" in node:
            inner = _Ctx(frozenset(), frozenset(inputs), {}, main=ctx.main, nested=True)
            declared_ids = self.seq(node["body"], f"{path}.body", "body", inner, 200)
            if "output" in node:
                self.output_map(node["output"], f"{path}.output",
                                dataclasses.replace(inner, visible=frozenset(declared_ids)))
        if ctx.main and not ctx.nested:
            nesting = self.nesting(node, ())
            if nesting > self.max_depth:
                self.add(path, "depth_limit",
                         f"subgraph nesting {nesting} exceeds the limit of {self.max_depth}")
        return set()

    def kind_owner_decision(self, node: Mapping[str, Any], path: str, ctx: _Ctx) -> set[str]:
        self.required(node, ["question", "options"], path)
        self.template(node, "question", path, ctx, lo=1, hi=4000)
        self.numeric(node, "timeout_s", path, ("num", 60, 2_592_000))
        options = node.get("options")
        valid = (isinstance(options, list) and 2 <= len(options) <= 10
                 and all(isinstance(o, str) for o in options) and len(set(options)) == len(options))
        if "options" in node and not valid:
            self.add(f"{path}.options", "options", "options must be 2 to 10 distinct strings")
        if "default" in node:
            if not isinstance(node["default"], str):
                self.type_(f"{path}.default", "string", node["default"])
            elif valid and node["default"] not in options:
                self.add(f"{path}.default", "options",
                         f"default {node['default']!r} is not one of the options")
        return set()

    def kind_loop(self, node: Mapping[str, Any], path: str, ctx: _Ctx) -> set[str]:
        self.required(node, ["body", "max_iterations", "until"], path)
        if "max_iterations" in node:
            self.numeric(node, "max_iterations", path, ("int", 1, 10))
        declared: set[str] = set()
        loops = ctx.loops | ({node["id"]} if isinstance(node.get("id"), str) else frozenset())
        if "body" in node:
            declared = self.seq(node["body"], f"{path}.body", "body",
                                dataclasses.replace(ctx, loops=loops), 50)
        self.condition(node, "until", path, dataclasses.replace(
            ctx, loops=loops, visible=ctx.visible | declared))
        return declared

    # -- nesting and cycles
    def subgraph_nodes(self, raw: Any) -> Iterator[Mapping[str, Any]]:
        for node in raw if isinstance(raw, list) else []:
            if not isinstance(node, Mapping):
                continue
            kind = node.get("kind")
            if kind == "subgraph":
                yield node
            elif kind == "parallel":
                yield from self.subgraph_nodes(node.get("branches"))
                spec = node.get("for_each")
                if isinstance(spec, Mapping):
                    yield from self.subgraph_nodes([spec.get("node")])
            elif kind == "loop":
                yield from self.subgraph_nodes(node.get("body"))

    def inner_body(self, node: Mapping[str, Any], stack: tuple[str, ...]) -> Any:
        name = node.get("graph")
        if isinstance(name, str):
            target = self.graphs.get(name)
            if name in stack or not isinstance(target, Mapping):
                return []
            return target.get("body")
        return node.get("body")

    def nesting(self, node: Mapping[str, Any], stack: tuple[str, ...]) -> int:
        name = node.get("graph")
        inner_stack = stack + (name,) if isinstance(name, str) else stack
        depth = 0
        for child in self.subgraph_nodes(self.inner_body(node, stack)):
            depth = max(depth, self.nesting(child, inner_stack))
        return 1 + depth

    def cycles(self) -> None:
        edges = {}
        for name, graph in self.graphs.items():
            body = graph.get("body") if isinstance(graph, Mapping) else None
            edges[name] = [n["graph"] for n in self.all_subgraphs(body)
                           if isinstance(n.get("graph"), str) and n["graph"] in self.graphs]
        reported: set[frozenset[str]] = set()

        def visit(name: str, stack: list[str]) -> None:
            if name in stack:
                loop = stack[stack.index(name):]
                if frozenset(loop) not in reported:
                    reported.add(frozenset(loop))
                    self.add(f"$.graphs.{loop[0]}", "subgraph_cycle",
                             "subgraph cycle: " + " -> ".join(loop + [name]))
                return
            for target in edges.get(name, []):
                visit(target, stack + [name])

        for name in edges:
            visit(name, [])

    def all_subgraphs(self, raw: Any) -> Iterator[Mapping[str, Any]]:
        for node in self.subgraph_nodes(raw):
            yield node
            if "body" in node:
                yield from self.all_subgraphs(node["body"])

    # -- the document
    def run(self) -> list[Problem]:
        doc = self.doc
        if not isinstance(doc, Mapping):
            self.type_("$", "object", doc)
            return self.problems
        if "schema" not in doc:
            self.add("$.schema", "schema_missing", f'missing "schema"; expected "{SCHEMA_ID}"')
        elif doc["schema"] != SCHEMA_ID:
            self.add("$.schema", "schema_unsupported",
                     f"unsupported schema {doc['schema']!r}; this build reads \"{SCHEMA_ID}\"")
        self.required(doc, ["id", "version", "body"], "$")
        self.unknown_keys(doc, _TOP_KEYS, "$")
        if "id" in doc:
            self.bad_id(doc["id"], "$.id")
        if "version" in doc:
            if not _is_int(doc["version"]):
                self.type_("$.version", "integer", doc["version"])
            elif doc["version"] < 1:
                self.range_("$.version", "version", 1, 1_000_000, doc["version"])
        self.string(doc, "description", "$", hi=2000)
        self.string(doc, "workspace", "$")
        inputs = doc.get("inputs", {})
        if not isinstance(inputs, Mapping):
            self.type_("$.inputs", "object", inputs)
            inputs = {}
        if "budget_usd" in doc:
            value = doc["budget_usd"]
            if not _is_num(value):
                self.type_("$.budget_usd", "number", value)
            elif value <= 0:
                self.range_("$.budget_usd", "budget_usd", 0.000001, 1_000_000, value)
        self.check_defaults()
        if "graphs" in doc and not isinstance(doc["graphs"], Mapping):
            self.type_("$.graphs", "object", doc["graphs"])
        ctx = _Ctx(frozenset(), frozenset(inputs), {})
        declared: set[str] = set()
        if "body" in doc:
            declared = self.seq(doc["body"], "$.body", "body", ctx, 200)
        for name, graph in self.graphs.items():
            self.named_graph(name, graph)
        if "output" in doc:
            self.output_map(doc["output"], "$.output",
                            dataclasses.replace(ctx, visible=frozenset(declared)))
        self.cycles()
        if self.count > self.max_nodes:
            self.add("$", "too_many_nodes",
                     f"graph has {self.count} nodes; the limit is {self.max_nodes}")
        return self.problems

    def check_defaults(self) -> None:
        if "defaults" not in self.doc:
            return
        raw = self.doc["defaults"]
        if not isinstance(raw, Mapping):
            return self.type_("$.defaults", "object", raw)
        self.unknown_keys(raw, _DEFAULT_KEYS, "$.defaults")
        for key in ("provider", "model", "workspace"):
            self.string(raw, key, "$.defaults")
        for key in ("max_steps", "timeout_s", "reserve_usd", "priority", "retries"):
            self.numeric(raw, key, "$.defaults")
        self.retry_on(raw, "$.defaults")

    def named_graph(self, name: Any, graph: Any) -> None:
        path = f"$.graphs.{name}"
        self.bad_id(name, path)
        if not isinstance(graph, Mapping):
            return self.type_(path, "object", graph)
        self.required(graph, ["body"], path)
        self.unknown_keys(graph, ("inputs", "body", "output"), path)
        inputs = graph.get("inputs", {})
        if not isinstance(inputs, Mapping):
            self.type_(f"{path}.inputs", "object", inputs)
            inputs = {}
        ctx = _Ctx(frozenset(), frozenset(inputs), {}, main=False)
        declared: set[str] = set()
        if "body" in graph:
            declared = self.seq(graph["body"], f"{path}.body", "body", ctx, 200)
        if "output" in graph:
            self.output_map(graph["output"], f"{path}.output",
                            dataclasses.replace(ctx, visible=frozenset(declared)))


def validate(doc: Mapping[str, Any], *, max_depth: int = 3,
             max_nodes: int = 500) -> list[Problem]:
    """Every problem in `doc`, or an empty list."""
    return _Validator(doc, max_depth, max_nodes).run()


# --- building the model (input is already validated) ---------------------------------


def _pick(raw: Mapping[str, Any], defaults: Mapping[str, Any], key: str, fallback: Any) -> Any:
    if key in raw:
        return raw[key]
    return defaults.get(key, fallback)


def build_body(raw_nodes: Sequence[Mapping[str, Any]], defaults: Mapping[str, Any],
               workspace: str | None) -> tuple[Node, ...]:
    return tuple(_build_node(raw, defaults, workspace) for raw in raw_nodes)


def _build_node(raw: Mapping[str, Any], defaults: Mapping[str, Any],
                workspace: str | None) -> Node:
    common = {"id": raw["id"], "kind": raw["kind"], "when": raw.get("when"),
              "on_failure": raw.get("on_failure", "fail"), "label": raw.get("label")}
    kind = raw["kind"]
    ws = _pick(raw, defaults, "workspace", workspace)
    if kind == "task":
        return TaskNode(
            **common, provider=_pick(raw, defaults, "provider", None),
            model=_pick(raw, defaults, "model", None), prompt=raw["prompt"],
            instructions=raw.get("instructions"), workspace=ws,
            verification=raw["verification"],
            max_steps=_pick(raw, defaults, "max_steps", None),
            timeout_s=_pick(raw, defaults, "timeout_s", DEFAULT_TIMEOUT_S),
            retries=_pick(raw, defaults, "retries", 1),
            retry_on=tuple(_pick(raw, defaults, "retry_on", DEFAULT_RETRY_ON)),
            retry_backoff_s=raw.get("retry_backoff_s", 30),
            reserve_usd=_pick(raw, defaults, "reserve_usd", DEFAULT_RESERVE_USD),
            priority=_pick(raw, defaults, "priority", 3), role=raw.get("role"))
    if kind == "gate":
        return GateNode(**common, command=raw.get("command"), condition=raw.get("condition"),
                        workspace=ws, timeout_s=raw.get("timeout_s"),
                        retries=raw.get("retries", 0))
    if kind == "parallel":
        spec = raw.get("for_each")
        each = None if spec is None else ForEach(
            items=spec["items"], as_=spec["as"], max_items=spec.get("max_items", 64),
            node=_build_node(spec["node"], defaults, workspace))
        return ParallelNode(**common, join=raw.get("join", "allSettled"), for_each=each,
                            branches=build_body(raw.get("branches", ()), defaults, workspace))
    if kind == "subgraph":
        inline = raw.get("body")
        return SubgraphNode(
            **common, graph=raw.get("graph"), inputs=dict(raw.get("inputs", {})),
            output=dict(raw.get("output", {})), budget_usd=raw.get("budget_usd"),
            priority=raw.get("priority"),
            body=None if inline is None else build_body(inline, defaults, workspace),
            raw_body=None if inline is None else tuple(inline))
    if kind == "owner_decision":
        return OwnerDecisionNode(**common, question=raw["question"],
                                 options=tuple(raw["options"]), default=raw.get("default"),
                                 timeout_s=raw.get("timeout_s"))
    return LoopNode(**common, body=build_body(raw["body"], defaults, workspace),
                    max_iterations=raw["max_iterations"], until=raw["until"])


def _build_graph(doc: Mapping[str, Any]) -> Graph:
    defaults = dict(doc.get("defaults", {}))
    workspace = doc.get("workspace")
    graphs = {name: Body(name, dict(g.get("inputs", {})),
                         build_body(g["body"], defaults, workspace), dict(g.get("output", {})))
              for name, g in doc.get("graphs", {}).items()}
    return Graph(
        doc=doc, id=doc["id"], version=doc["version"],
        body=build_body(doc["body"], defaults, workspace), graphs=graphs,
        inputs=dict(doc.get("inputs", {})), defaults=defaults,
        budget_usd=doc.get("budget_usd"), workspace=workspace, output=dict(doc.get("output", {})))
