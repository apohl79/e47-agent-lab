"""Mermaid, Graphviz DOT, and self-contained HTML exports of a knowledge graph."""

from __future__ import annotations

import json
from pathlib import Path

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


ASSETS = Path(__file__).resolve().parent.parent / "assets"
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <h1>__TITLE__</h1>
    <p id="summary" class="muted"></p>
    <h2>Search</h2>
    <input id="search" type="search" placeholder="Filter nodes by label">
    <h2>Minimum confidence <span id="confidence-value">0.00</span></h2>
    <input id="confidence" type="range" min="0" max="1" step="0.05" value="0">
    <h2>Relations</h2>
    <div id="relations"></div>
    <h2>Kinds</h2>
    <div id="kinds"></div>
    <h2>Layout</h2>
    <div id="buttons">
      <button id="fit">Fit</button><button id="relayout">Re-layout</button>
      <button id="expand-all">Expand all</button><button id="collapse-all">Collapse all</button>
    </div>
    <h2>Selection</h2>
    <div id="details"></div>
  </aside>
  <main id="stage">
    <canvas id="graph-canvas"></canvas>
    <div id="tooltip"></div>
    <div id="hint">drag: pan · wheel: zoom · click: select · double-click: expand or collapse a store</div>
  </main>
</div>
<script id="graph-data" type="application/json">__DATA__</script>
<script>
__JS__
</script>
</body>
</html>
"""


def html_title(graph: KnowledgeGraph) -> str:
    view = graph.view
    focus = graph.node(view.focus)
    if focus is None:
        return "Knowledge graph: overview"
    return f"Knowledge graph: {view.kind} {focus.label}"


def render_html(graph: KnowledgeGraph) -> str:
    payload = (
        json.dumps(graph.as_dict(), sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return (
        HTML_TEMPLATE.replace("__TITLE__", html_title(graph).replace("<", "&lt;"))
        .replace("__CSS__", (ASSETS / "graph-viewer.css").read_text(encoding="utf-8"))
        .replace("__JS__", (ASSETS / "graph-viewer.js").read_text(encoding="utf-8"))
        .replace("__DATA__", payload)
    )
