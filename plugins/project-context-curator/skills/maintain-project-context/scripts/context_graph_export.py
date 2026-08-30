"""Mermaid and Graphviz DOT exports of a knowledge graph."""

from __future__ import annotations

from context_graph import (
    MEMBER_RELATION,
    STORE_KINDS,
    STORED_IN_RELATION,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
)

MERMAID_SHAPES = {
    "project": ('["', '"]'),
    "domain": ('{{"', '"}}'),
    "universal": ('(("', '"))'),
    "term": ('(["', '"])'),
    "component": ('[["', '"]]'),
    "pattern": ('>"', '"]'),
    "question": ('{"', '"}'),
}
DOT_SHAPES = {
    "project": "box",
    "domain": "hexagon",
    "universal": "doublecircle",
    "term": "ellipse",
    "component": "component",
    "pattern": "note",
    "question": "diamond",
}
DASHED_STATUSES = frozenset({"uninitialized", "missing", "remote-only"})
MAX_LABEL = 72


def node_label(node: GraphNode) -> str:
    text = node.label if node.kind == "project" else f"{node.kind}: {node.label}"
    if len(text) > MAX_LABEL:
        text = text[: MAX_LABEL - 1] + "…"
    if node.status in DASHED_STATUSES:
        text = f"{text} ({node.status})"
    return text


def mermaid_label(node: GraphNode) -> str:
    return node_label(node).replace('"', "#quot;")


def render_mermaid(graph: KnowledgeGraph) -> str:
    ids = {node.id: f"n{index}" for index, node in enumerate(graph.nodes)}
    lines = ["graph LR"]
    for node in graph.nodes:
        opening, closing = MERMAID_SHAPES.get(node.kind, MERMAID_SHAPES["pattern"])
        lines.append(f"    {ids[node.id]}{opening}{mermaid_label(node)}{closing}")
    for edge in graph.edges:
        label = edge.relation.replace('"', "#quot;")
        if edge.relation in (MEMBER_RELATION, STORED_IN_RELATION):
            lines.append(f"    {ids[edge.source]} -.->|{label}| {ids[edge.target]}")
        else:
            lines.append(
                f"    {ids[edge.source]} -->|{label} {edge.confidence:.2f}| {ids[edge.target]}"
            )
    for status in sorted(DASHED_STATUSES):
        members = [ids[node.id] for node in graph.nodes if node.status == status]
        if members:
            lines.append(f"    classDef {status.replace('-', '_')} stroke-dasharray: 5 5")
            lines.append(f"    class {','.join(members)} {status.replace('-', '_')}")
    return "\n".join(lines)


def dot_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dot_node(node: GraphNode) -> str:
    attributes = [
        f"label={dot_quote(node_label(node))}",
        f"shape={DOT_SHAPES.get(node.kind, 'note')}",
    ]
    if node.status in DASHED_STATUSES:
        attributes.append("style=dashed")
    if node.summary:
        attributes.append(f"tooltip={dot_quote(node.summary)}")
    return f"{dot_quote(node.id)} [{', '.join(attributes)}];"


def dot_edge(edge: GraphEdge) -> str:
    attributes = [f"label={dot_quote(f'{edge.relation} {edge.confidence:.2f}')}"]
    if edge.relation == MEMBER_RELATION:
        attributes = ['label="member_of"', "style=dashed", "arrowhead=empty"]
    elif edge.confidence <= 0.6:
        attributes.append("style=dotted")
    return f"{dot_quote(edge.source)} -> {dot_quote(edge.target)} [{', '.join(attributes)}];"


def store_cluster(
    store: GraphNode,
    records: list[GraphNode],
    indent: str,
) -> list[str]:
    if not records:
        return [f"{indent}{dot_node(store)}"]
    lines = [
        f"{indent}subgraph {dot_quote('cluster_' + store.id)} {{",
        f"{indent}    label={dot_quote(store.label)};",
        f"{indent}    {dot_node(store)}",
    ]
    lines.extend(f"{indent}    {dot_node(record)}" for record in records)
    lines.append(f"{indent}}}")
    return lines


def render_dot(graph: KnowledgeGraph) -> str:
    records_of: dict[str, list[GraphNode]] = {}
    for node in graph.nodes:
        if node.kind not in STORE_KINDS:
            records_of.setdefault(node.store, []).append(node)
    domains_of: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.relation == MEMBER_RELATION:
            domains_of.setdefault(edge.source, []).append(edge.target)
    lines = ["digraph knowledge {", "    rankdir=LR;", "    node [fontname=Helvetica];"]
    placed: set[str] = set()
    for domain in (node for node in graph.nodes if node.kind == "domain"):
        lines.append(f"    subgraph {dot_quote('cluster_' + domain.id)} {{")
        lines.append(f"        label={dot_quote('domain ' + domain.label)};")
        lines.append(f"        {dot_node(domain)}")
        lines.extend(f"        {dot_node(record)}" for record in records_of.get(domain.id, []))
        for node in graph.nodes:
            if node.kind == "project" and domains_of.get(node.id) == [domain.id]:
                lines.extend(store_cluster(node, records_of.get(node.id, []), "        "))
                placed.add(node.id)
        lines.append("    }")
        placed.add(domain.id)
    for node in graph.nodes:
        if node.kind in STORE_KINDS and node.id not in placed:
            lines.extend(store_cluster(node, records_of.get(node.id, []), "    "))
    lines.extend(
        f"    {dot_edge(edge)}"
        for edge in graph.edges
        if edge.relation != STORED_IN_RELATION
    )
    lines.append("}")
    return "\n".join(lines)
