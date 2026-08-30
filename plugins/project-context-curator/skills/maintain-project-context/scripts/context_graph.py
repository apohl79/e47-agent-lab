"""Knowledge graph derived from canonical context records. Read-only."""

from __future__ import annotations

import json
import re
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
STORE_KINDS = frozenset({"project", "domain", "universal"})
RECORD_LEVEL = "records"
STORED_IN_RELATION = "stored_in"
MENTIONS_RELATION = "mentions"
SHADOWS_RELATION = "shadows"
DIVERGES_RELATION = "diverges"
MENTIONS_CONFIDENCE = 0.7
MENTION_LABEL_KINDS = ("term", "component")
MIN_MENTION_LABEL = 3


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    label: str
    status: str = ""
    path: str = ""
    store: str = ""
    counts: tuple[tuple[str, int], ...] = ()
    summary: str = ""

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


def record_node_id(record: ContextRecord) -> str:
    return f"record:{record.id}"


def record_key(record: ContextRecord) -> tuple[str, str]:
    return record.kind, record.label.casefold()


def applicable_stores(graph: KnowledgeGraph, store: str) -> tuple[str, ...]:
    stores = [store]
    stores.extend(
        edge.target
        for edge in graph.edges
        if edge.relation == MEMBER_RELATION and edge.source == store
    )
    if graph.node("universal") is not None:
        stores.append("universal")
    return tuple(dict.fromkeys(stores))


def mention_matcher(
    records: Sequence[ContextRecord],
) -> tuple[re.Pattern[str] | None, dict[str, list[ContextRecord]]]:
    targets: dict[str, list[ContextRecord]] = {}
    for record in records:
        if record.kind in MENTION_LABEL_KINDS and len(record.label) >= MIN_MENTION_LABEL:
            targets.setdefault(record.label.casefold(), []).append(record)
    if not targets:
        return None, {}
    alternatives = "|".join(
        re.escape(label) for label in sorted(targets, key=lambda value: (-len(value), value))
    )
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE), targets


def mention_edges(
    by_store: dict[str, list[ContextRecord]],
    graph: KnowledgeGraph,
) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    matchers: dict[tuple[str, ...], Any] = {}
    for store, records in by_store.items():
        stores = applicable_stores(graph, store)
        if stores not in matchers:
            matchers[stores] = mention_matcher(
                [record for scope in stores for record in by_store.get(scope, ())]
            )
        matcher, targets = matchers[stores]
        if matcher is None:
            continue
        for record in records:
            mentioned = {match.group(0).casefold() for match in matcher.finditer(record.text)}
            for label in sorted(mentioned):
                for target in targets[label]:
                    if record_key(target) != record_key(record):
                        edges.append(
                            GraphEdge(
                                record_node_id(record),
                                record_node_id(target),
                                MENTIONS_RELATION,
                                MENTIONS_CONFIDENCE,
                                RECORD_LEVEL,
                            )
                        )
    return edges


def shadow_edges(
    by_store: dict[str, list[ContextRecord]],
    graph: KnowledgeGraph,
) -> list[GraphEdge]:
    by_key = {
        store: {record_key(record): record for record in records}
        for store, records in by_store.items()
    }
    return [
        GraphEdge(
            record_node_id(record),
            record_node_id(by_key[scope][record_key(record)]),
            SHADOWS_RELATION,
            1.0,
            RECORD_LEVEL,
        )
        for store, records in by_store.items()
        if store.startswith("project:")
        for scope in applicable_stores(graph, store)[1:]
        for record in records
        if record_key(record) in by_key.get(scope, {})
    ]


def divergence_edges(
    by_store: dict[str, list[ContextRecord]],
    graph: KnowledgeGraph,
) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    for domain in (node for node in graph.nodes if node.kind == "domain"):
        definitions: dict[tuple[str, str], list[ContextRecord]] = {}
        for edge in graph.edges:
            if edge.relation != MEMBER_RELATION or edge.target != domain.id:
                continue
            for record in by_store.get(edge.source, ()):
                if record.kind in MENTION_LABEL_KINDS:
                    definitions.setdefault(record_key(record), []).append(record)
        for records in definitions.values():
            ordered = sorted(records, key=record_node_id)
            for index, first in enumerate(ordered):
                for second in ordered[index + 1 :]:
                    if first.summary.casefold() != second.summary.casefold():
                        edges.append(
                            GraphEdge(
                                record_node_id(first),
                                record_node_id(second),
                                DIVERGES_RELATION,
                                1.0,
                                RECORD_LEVEL,
                            )
                        )
    return edges


def add_record_level(graph: KnowledgeGraph, records: Sequence[ContextRecord]) -> KnowledgeGraph:
    by_store: dict[str, list[ContextRecord]] = {}
    for record in records:
        store = "" if is_private(record) else store_of(record)
        if graph.node(store) is not None:
            by_store.setdefault(store, []).append(record)
    nodes = list(graph.nodes)
    edges = list(graph.edges)
    known: set[str] = set()
    for store, store_records in by_store.items():
        for record in store_records:
            node_id = record_node_id(record)
            known.add(node_id)
            nodes.append(
                GraphNode(
                    node_id, record.kind, record.label, "", "", store, summary=record.summary
                )
            )
            edges.append(GraphEdge(node_id, store, STORED_IN_RELATION, 1.0, RECORD_LEVEL))
    for edge in graph.edges:
        for record_id, _, _ in edge.evidence:
            if f"record:{record_id}" in known:
                edges.append(
                    GraphEdge(
                        f"record:{record_id}",
                        edge.target,
                        edge.relation,
                        edge.confidence,
                        RECORD_LEVEL,
                    )
                )
    edges.extend(mention_edges(by_store, graph))
    edges.extend(shadow_edges(by_store, graph))
    edges.extend(divergence_edges(by_store, graph))
    ordered = tuple(sorted(nodes, key=lambda node: node.id.casefold()))
    return KnowledgeGraph(ordered, dedupe_edges(edges), replace(graph.view, level=RECORD_LEVEL))


def ranked(values: dict[str, int], labels: dict[str, str], limit: int) -> list[tuple[str, int]]:
    ordered = sorted(values.items(), key=lambda item: (-item[1], labels[item[0]].casefold()))
    return [(labels[node_id], count) for node_id, count in ordered[:limit] if count]


def display_label(node: GraphNode) -> str:
    return node.label if node.kind == "project" else node.id


def record_insights(graph: KnowledgeGraph) -> dict[str, Any]:
    records = [node for node in graph.nodes if node.kind not in STORE_KINDS]
    if not records:
        return {}
    stores = {node.id: display_label(node) for node in graph.nodes if node.kind in STORE_KINDS}
    labels = {
        node.id: f"{node.kind} {node.label} ({stores.get(node.store, node.store)})"
        for node in records
    }
    connected: set[str] = set()
    mentioned = {node.id: 0 for node in records}
    relations: dict[str, int] = {}
    for edge in graph.edges:
        if edge.level != RECORD_LEVEL or edge.relation == STORED_IN_RELATION:
            continue
        relations[edge.relation] = relations.get(edge.relation, 0) + 1
        connected.update((edge.source, edge.target))
        if edge.relation == MENTIONS_RELATION:
            mentioned[edge.target] += 1
    return {
        "records": len(records),
        "relations": dict(sorted(relations.items())),
        "unconnected": sum(node.id not in connected for node in records),
        "most_mentioned": [
            {"label": label, "mentions": count}
            for label, count in ranked(mentioned, labels, 5)
        ],
    }


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
        if edge.relation == MEMBER_RELATION or edge.level == RECORD_LEVEL:
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
            edge.relation != MEMBER_RELATION and edge.level != RECORD_LEVEL
            for edge in graph.edges
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
        "record_level": record_insights(graph),
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
    records = insights["record_level"]
    if records:
        lines.append(
            f"Records: {records['records']} nodes; record edges "
            f"({format_counts(records['relations'])}); {records['unconnected']} without "
            "record edges; most mentioned: "
            + format_ranked(records["most_mentioned"], "mentions")
        )
    return "\n".join(lines)


def render_json(graph: KnowledgeGraph) -> str:
    return json.dumps(graph.as_dict(), indent=2, sort_keys=True)
