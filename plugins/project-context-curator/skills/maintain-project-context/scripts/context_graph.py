"""Knowledge graph derived from canonical context records. Read-only."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from global_context import (
    ContextRecord,
    ContextSource,
    derive_relationship_graph,
    sorted_records,
)

GRAPH_SCHEMA_VERSION = 1
PRIVATE_SCOPE_KINDS = frozenset({"user", "machine", "workspace"})
MEMBER_RELATION = "member_of"
RECORD_KINDS = ("term", "component", "pattern", "question")
EVIDENCE_FIELDS = ("record_id", "kind", "label")


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    label: str
    status: str = ""
    path: str = ""
    store: str = ""
    counts: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "counts": dict(self.counts)}


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    confidence: float
    level: str
    evidence: tuple[tuple[str, str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        evidence = [dict(zip(EVIDENCE_FIELDS, item)) for item in self.evidence]
        return {**asdict(self), "evidence": evidence}


@dataclass(frozen=True)
class DomainSpec:
    id: str
    projects: tuple[str, ...]
    remotes: tuple[str, ...]


@dataclass(frozen=True)
class GraphView:
    kind: str = "overview"
    focus: str = ""
    depth: int = 1
    level: str = "projects"
    min_confidence: float = 0.0
    relations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "relations": list(self.relations)}


@dataclass(frozen=True)
class KnowledgeGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    view: GraphView = GraphView()

    def node(self, node_id: str) -> GraphNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "view": self.view.as_dict(),
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
        }


def project_node_id(path: str) -> str:
    return f"project:{path}"


def domain_node_id(domain_id: str) -> str:
    return f"domain:{domain_id}"


def remote_name(remote: str) -> str:
    name = remote.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def is_private(record: ContextRecord) -> bool:
    return any(kind in PRIVATE_SCOPE_KINDS for kind, _ in record.applicability)


def scope_store_id(record: ContextRecord) -> str | None:
    kinds = {kind for kind, _ in record.applicability}
    if kinds == {"universal"}:
        return "universal"
    domains = [selector for kind, selector in record.applicability if kind == "domain"]
    if len(domains) == 1 and kinds == {"domain"}:
        return domain_node_id(domains[0])
    return None


def store_of(record: ContextRecord) -> str:
    if record.project_path:
        return project_node_id(record.project_path)
    return scope_store_id(record) or ""


def record_counts(records: Sequence[ContextRecord]) -> tuple[tuple[str, int], ...]:
    counts = {kind: 0 for kind in RECORD_KINDS}
    for record in records:
        counts[record.kind] = counts.get(record.kind, 0) + 1
    return tuple(sorted(counts.items()))


def derivation_record(record: ContextRecord, store: str) -> ContextRecord:
    return replace(record, project_path=record.project_path or store)


def resolve_remote_members(
    spec: DomainSpec,
    initialized: dict[str, GraphNode],
    remote_url_of: Callable[[Path], str | None],
) -> dict[str, str | None]:
    by_name: dict[str, list[GraphNode]] = {}
    for node in initialized.values():
        by_name.setdefault(node.label.casefold(), []).append(node)
    resolved: dict[str, str | None] = {}
    for remote in spec.remotes:
        resolved[remote] = next(
            (
                node.path
                for node in by_name.get(remote_name(remote).casefold(), ())
                if remote_url_of(Path(node.path)) == remote
            ),
            None,
        )
    return resolved


def dedupe_edges(edges: Sequence[GraphEdge]) -> tuple[GraphEdge, ...]:
    seen: dict[tuple[str, str, str], GraphEdge] = {}
    for edge in edges:
        key = (edge.source, edge.target, edge.relation)
        current = seen.get(key)
        if current is None or edge.confidence > current.confidence:
            seen[key] = edge
    return tuple(seen[key] for key in sorted(seen))


def store_node(
    node_id: str,
    kind: str,
    label: str,
    status: str,
    records: Sequence[ContextRecord],
    path: str = "",
) -> GraphNode:
    store = records[0].source_path if records else ""
    return GraphNode(node_id, kind, label, status, path, store, record_counts(records))


def build_project_graph(
    records: Sequence[ContextRecord],
    sources: Sequence[ContextSource],
    domains: Sequence[DomainSpec],
    remote_url_of: Callable[[Path], str | None],
) -> KnowledgeGraph:
    by_store: dict[str, list[ContextRecord]] = {}
    for record in records:
        store = "" if is_private(record) else store_of(record)
        if store:
            by_store.setdefault(store, []).append(record)
    nodes: dict[str, GraphNode] = {}
    for source in sources:
        node_id = project_node_id(source.project_path)
        nodes[node_id] = replace(
            store_node(
                node_id,
                "project",
                Path(source.project_path).name,
                "initialized",
                by_store.get(node_id, ()),
                source.project_path,
            ),
            store=source.source_path,
        )
    initialized = dict(nodes)
    ordered_domains = sorted(domains, key=lambda item: item.id)
    for spec in ordered_domains:
        node_id = domain_node_id(spec.id)
        nodes[node_id] = store_node(
            node_id, "domain", spec.id, "declared", by_store.get(node_id, ())
        )
    for store, store_records in by_store.items():
        if store not in nodes and store.startswith("domain:"):
            label = store.split(":", 1)[1]
            nodes[store] = store_node(store, "domain", label, "unconfigured", store_records)
    if "universal" in by_store:
        nodes["universal"] = store_node(
            "universal", "universal", "universal", "declared", by_store["universal"]
        )
    edges: list[GraphEdge] = []
    for spec in ordered_domains:
        domain_id = domain_node_id(spec.id)
        for path in spec.projects:
            node_id = project_node_id(path)
            if node_id not in nodes:
                status = "uninitialized" if Path(path).is_dir() else "missing"
                nodes[node_id] = GraphNode(node_id, "project", Path(path).name, status, path)
            edges.append(GraphEdge(node_id, domain_id, MEMBER_RELATION, 1.0, "project"))
        for remote, path in resolve_remote_members(spec, initialized, remote_url_of).items():
            node_id = project_node_id(path) if path is not None else f"remote:{remote}"
            nodes.setdefault(
                node_id,
                GraphNode(node_id, "project", remote_name(remote), "remote-only", remote),
            )
            edges.append(GraphEdge(node_id, domain_id, MEMBER_RELATION, 1.0, "project"))
    derived = derive_relationship_graph(
        sorted_records(
            tuple(
                derivation_record(record, store)
                for store, store_records in by_store.items()
                for record in store_records
            )
        )
    )
    for raw in derived["edges"]:
        endpoints = []
        for path in (raw["source_project_path"], raw["target_project_path"]):
            endpoints.append(path if path in nodes else project_node_id(path))
        if any(endpoint not in nodes for endpoint in endpoints):
            continue
        edges.append(
            GraphEdge(
                endpoints[0],
                endpoints[1],
                raw["relation"],
                float(raw["confidence"]),
                "project",
                tuple(
                    (item["record_id"], item["record_kind"], item["record_label"])
                    for item in raw["evidence"]
                ),
            )
        )
    ordered = tuple(nodes[node_id] for node_id in sorted(nodes, key=str.casefold))
    return KnowledgeGraph(ordered, dedupe_edges(edges))


def resolve_focus(graph: KnowledgeGraph, view: GraphView) -> str:
    if view.kind == "domain":
        node_id = domain_node_id(view.focus)
        if graph.node(node_id) is None:
            raise ValueError(f"unknown domain {view.focus!r}")
        return node_id
    if view.kind == "project":
        resolved = project_node_id(str(Path(view.focus).expanduser().resolve()))
        if graph.node(resolved) is not None:
            return resolved
        matches = [
            node.id
            for node in graph.nodes
            if node.kind == "project" and node.label.casefold() == view.focus.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"{'ambiguous' if matches else 'unknown'} project {view.focus!r}")
    return ""


def apply_view(graph: KnowledgeGraph, view: GraphView) -> KnowledgeGraph:
    edges = [
        edge
        for edge in graph.edges
        if (edge.relation == MEMBER_RELATION or edge.confidence >= view.min_confidence)
        and (not view.relations or edge.relation in view.relations)
    ]
    focus = resolve_focus(graph, view)
    if not focus:
        return KnowledgeGraph(graph.nodes, tuple(edges), view)
    neighbours: dict[str, set[str]] = {}
    for edge in edges:
        neighbours.setdefault(edge.source, set()).add(edge.target)
        neighbours.setdefault(edge.target, set()).add(edge.source)
    included = {focus}
    frontier = {focus}
    for _ in range(view.depth):
        frontier = {
            neighbour for node_id in frontier for neighbour in neighbours.get(node_id, ())
        } - included
        included |= frontier
    return KnowledgeGraph(
        tuple(node for node in graph.nodes if node.id in included),
        tuple(
            edge for edge in edges if edge.source in included and edge.target in included
        ),
        replace(view, focus=focus),
    )


def render_json(graph: KnowledgeGraph) -> str:
    return json.dumps(graph.as_dict(), indent=2, sort_keys=True)
