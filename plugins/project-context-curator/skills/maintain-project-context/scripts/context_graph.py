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
WEAK_CONFIDENCE = 0.6
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
            "insights": graph_insights(self),
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


def ranked(values: dict[str, int], labels: dict[str, str], limit: int) -> list[tuple[str, int]]:
    ordered = sorted(values.items(), key=lambda item: (-item[1], labels[item[0]].casefold()))
    return [(labels[node_id], count) for node_id, count in ordered[:limit] if count]


def display_label(node: GraphNode) -> str:
    return node.label if node.kind == "project" else node.id


def graph_insights(graph: KnowledgeGraph) -> dict[str, Any]:
    labels = {node.id: display_label(node) for node in graph.nodes}
    projects = [node for node in graph.nodes if node.kind == "project"]
    statuses: dict[str, int] = {}
    for node in projects:
        statuses[node.status] = statuses.get(node.status, 0) + 1
    relations: dict[str, int] = {}
    degree = {node.id: 0 for node in graph.nodes}
    in_degree = {node.id: 0 for node in graph.nodes}
    weak = 0
    for edge in graph.edges:
        relations[edge.relation] = relations.get(edge.relation, 0) + 1
        if edge.relation == MEMBER_RELATION:
            continue
        degree[edge.source] += 1
        degree[edge.target] += 1
        in_degree[edge.target] += 1
        weak += edge.confidence <= WEAK_CONFIDENCE
    coverage = []
    for domain in (node for node in graph.nodes if node.kind == "domain"):
        members = {
            edge.source
            for edge in graph.edges
            if edge.relation == MEMBER_RELATION and edge.target == domain.id
        }
        member_statuses: dict[str, int] = {}
        for member in members:
            status = graph.node(member).status
            member_statuses[status] = member_statuses.get(status, 0) + 1
        coverage.append(
            {
                "id": domain.label,
                "members": len(members),
                "member_statuses": dict(sorted(member_statuses.items())),
                "records": sum(count for _, count in domain.counts),
                "internal_edges": sum(
                    edge.relation != MEMBER_RELATION
                    and edge.source in members
                    and edge.target in members
                    for edge in graph.edges
                ),
                "isolated_members": sorted(
                    labels[member]
                    for member in members
                    if graph.node(member).status == "initialized" and degree[member] == 0
                ),
            }
        )
    return {
        "projects": len(projects),
        "project_statuses": dict(sorted(statuses.items())),
        "domains": sum(node.kind == "domain" for node in graph.nodes),
        "universal_stores": sum(node.kind == "universal" for node in graph.nodes),
        "records": sum(count for node in graph.nodes for _, count in node.counts),
        "edges": len(graph.edges),
        "relationship_edges": sum(
            count for relation, count in relations.items() if relation != MEMBER_RELATION
        ),
        "relations": dict(sorted(relations.items())),
        "weak_edges": weak,
        "hubs": [
            {"label": label, "degree": count} for label, count in ranked(degree, labels, 8)
        ],
        "most_referenced": [
            {"label": label, "in_degree": count}
            for label, count in ranked(in_degree, labels, 5)
        ],
        "orphans": sorted(
            node.label
            for node in projects
            if node.status == "initialized" and degree[node.id] == 0
        ),
        "domain_coverage": coverage,
    }


def format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{value} {key}" for key, value in counts.items()) or "none"


def format_ranked(items: list[dict[str, Any]], field: str) -> str:
    return ", ".join(f"{item['label']} {item[field]}" for item in items) or "none"


def render_text(graph: KnowledgeGraph) -> str:
    insights = graph_insights(graph)
    view = graph.view
    scope = view.kind if not view.focus else f"{view.kind} {graph.node(view.focus).label}"
    lines = [
        f"Knowledge graph ({scope}, depth {view.depth}, level {view.level}): "
        f"{insights['projects']} projects ({format_counts(insights['project_statuses'])}), "
        f"{insights['domains']} domains, {insights['universal_stores']} universal stores; "
        f"{insights['records']} records; {insights['edges']} edges "
        f"({format_counts(insights['relations'])}).",
        "Hubs: " + format_ranked(insights["hubs"], "degree"),
        "Most referenced: " + format_ranked(insights["most_referenced"], "in_degree"),
        f"Orphans (initialized, no relationship edges): {len(insights['orphans'])}"
        + (": " + ", ".join(insights["orphans"]) if insights["orphans"] else ""),
        f"Weak edges (confidence <= {WEAK_CONFIDENCE}): {insights['weak_edges']} of "
        f"{insights['relationship_edges']}",
    ]
    for domain in insights["domain_coverage"]:
        isolated = domain["isolated_members"]
        lines.append(
            f"Domain {domain['id']}: {domain['members']} members "
            f"({format_counts(domain['member_statuses'])}); {domain['records']} domain "
            f"records; {domain['internal_edges']} edges between members; "
            f"{len(isolated)} initialized members without edges"
            + (": " + ", ".join(isolated) if isolated else "")
        )
    return "\n".join(lines)


def render_json(graph: KnowledgeGraph) -> str:
    return json.dumps(graph.as_dict(), indent=2, sort_keys=True)
