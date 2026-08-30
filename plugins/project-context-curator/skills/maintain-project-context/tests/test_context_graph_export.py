from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

from test_context_graph import fixture_graph, record


def test_mermaid_export_shapes_nodes_by_kind_and_dashes_unavailable_members(
    tmp_path: Path,
) -> None:
    graph, built, _, _ = fixture_graph(tmp_path)
    exports = importlib.import_module("context_graph_export")
    focused = graph.apply_view(
        built, graph.GraphView("domain", "billing", 1, min_confidence=0.8)
    )

    assert exports.render_mermaid(focused) == "\n".join(
        [
            "graph LR",
            '    n0{{"domain: billing"}}',
            '    n1["alpha"]',
            '    n2["beta"]',
            '    n3["delta (missing)"]',
            '    n4["gamma (uninitialized)"]',
            '    n5["worker (remote-only)"]',
            "    n0 -->|owns 0.90| n1",
            "    n1 -.->|member_of| n0",
            "    n1 -->|integrates_with 0.90| n2",
            "    n2 -.->|member_of| n0",
            "    n3 -.->|member_of| n0",
            "    n4 -.->|member_of| n0",
            "    n5 -.->|member_of| n0",
            "    classDef missing stroke-dasharray: 5 5",
            "    class n3 missing",
            "    classDef remote_only stroke-dasharray: 5 5",
            "    class n5 remote_only",
            "    classDef uninitialized stroke-dasharray: 5 5",
            "    class n4 uninitialized",
        ]
    )


def test_dot_export_clusters_domain_members_with_their_records(tmp_path: Path) -> None:
    graph, built, ids, records = fixture_graph(tmp_path)
    exports = importlib.import_module("context_graph_export")
    detailed = graph.add_record_level(
        graph.apply_view(built, graph.GraphView("project", "beta", 1)), records
    )
    alpha, beta = ids["alpha"], ids["beta"]

    assert exports.render_dot(detailed) == "\n".join(
        [
            "digraph knowledge {",
            "    rankdir=LR;",
            "    node [fontname=Helvetica];",
            '    subgraph "cluster_domain:billing" {',
            '        label="domain billing";',
            '        "domain:billing" [label="domain: billing", shape=hexagon];',
            '        "record:domain:billing:term:Invoice" [label="term: Invoice", '
            'shape=ellipse, tooltip="alpha owns invoices"];',
            '        "record:domain:billing:term:Ledger" [label="term: Ledger", '
            'shape=ellipse, tooltip="Shared book of record"];',
            f'        subgraph "cluster_{alpha}" {{',
            '            label="alpha";',
            f'            "{alpha}" [label="alpha", shape=box];',
            '            "record:alpha:pattern:Payments" [label="pattern: Payments", '
            'shape=note, tooltip="alpha calls beta for payments"];',
            '            "record:alpha:term:Ledger" [label="term: Ledger", shape=ellipse, '
            'tooltip="Book of record"];',
            "        }",
            f'        subgraph "cluster_{beta}" {{',
            '            label="beta";',
            f'            "{beta}" [label="beta", shape=box];',
            '            "record:beta:component:API" [label="component: API", '
            'shape=component, tooltip="Ledger API; see alpha for terms"];',
            '            "record:beta:term:Ledger" [label="term: Ledger", shape=ellipse, '
            'tooltip="Ledger service"];',
            "        }",
            "    }",
            f'    "domain:billing" -> "{alpha}" [label="owns 0.90"];',
            f'    "{alpha}" -> "domain:billing" [label="member_of", style=dashed, arrowhead=empty];',
            f'    "{alpha}" -> "{beta}" [label="integrates_with 0.90"];',
            f'    "{beta}" -> "domain:billing" [label="member_of", style=dashed, arrowhead=empty];',
            f'    "{beta}" -> "{alpha}" [label="references 0.60", style=dotted];',
            f'    "record:alpha:pattern:Payments" -> "{beta}" [label="integrates_with 0.90"];',
            '    "record:alpha:term:Ledger" -> "record:beta:term:Ledger" [label="diverges 1.00"];',
            '    "record:alpha:term:Ledger" -> "record:domain:billing:term:Ledger" '
            '[label="shadows 1.00"];',
            f'    "record:beta:component:API" -> "{alpha}" [label="references 0.60", style=dotted];',
            '    "record:beta:component:API" -> "record:beta:term:Ledger" [label="mentions 0.70"];',
            '    "record:beta:component:API" -> "record:domain:billing:term:Ledger" '
            '[label="mentions 0.70"];',
            '    "record:beta:term:Ledger" -> "record:domain:billing:term:Ledger" '
            '[label="shadows 1.00"];',
            f'    "record:domain:billing:term:Invoice" -> "{alpha}" [label="owns 0.90"];',
            "}",
        ]
    )


def test_html_export_embeds_the_graph_and_viewer_without_remote_assets(
    tmp_path: Path,
) -> None:
    graph, built, ids, records = fixture_graph(tmp_path)
    exports = importlib.import_module("context_graph_export")
    detailed = graph.add_record_level(
        graph.apply_view(built, graph.GraphView("domain", "billing", 1)), records
    )
    html = exports.render_html(detailed)
    embedded = re.search(
        r'<script id="graph-data" type="application/json">(.*?)</script>', html, re.S
    )
    payload = json.loads(embedded.group(1)) if embedded else {}
    head, viewer = html.split('id="graph-data"')

    assert (
        html.startswith("<!DOCTYPE html>"),
        "<title>Knowledge graph: domain billing</title>" in html,
        'id="graph-canvas"' in html,
        ("http://" in head or "https://" in head or 'src="' in viewer),
        payload["schema_version"],
        payload["view"],
        sorted(node["id"] for node in payload["nodes"] if not node["id"].startswith("record:")),
        len([node for node in payload["nodes"] if node["id"].startswith("record:")]),
        "--bg:" in head and "getElementById(\"graph-data\")" in viewer,
    ) == (
        True,
        True,
        True,
        False,
        1,
        {
            "kind": "domain",
            "focus": "domain:billing",
            "depth": 1,
            "level": "records",
            "min_confidence": 0.0,
            "relations": [],
        },
        sorted(
            [
                "domain:billing",
                ids["alpha"],
                ids["beta"],
                ids["delta"],
                ids["gamma"],
                "remote:github.com/acme/worker",
            ]
        ),
        6,
        True,
    )


def test_html_export_neutralizes_markup_inside_record_text(tmp_path: Path) -> None:
    graph, built, _, _ = fixture_graph(tmp_path)
    exports = importlib.import_module("context_graph_export")
    hostile = '</script><!--<script id="x"> a & b <br/>'
    records = (record("alpha", str(tmp_path / "alpha"), "pattern", "Markup", hostile),)
    detailed = graph.add_record_level(
        graph.apply_view(built, graph.GraphView("domain", "billing", 1)), records
    )
    html = exports.render_html(detailed)
    embedded = re.search(
        r'<script id="graph-data" type="application/json">(.*?)</script>', html, re.S
    )
    payload = json.loads(embedded.group(1)) if embedded else {"nodes": []}
    markup = [node for node in payload["nodes"] if node["label"] == "Markup"]

    assert (
        html.count("<script"),
        html.count("</script"),
        html.count("<!--"),
        embedded is not None and re.search(r"[<>&]", embedded.group(1)) is None,
        markup[0]["summary"] if markup else None,
    ) == (2, 2, 0, True, hostile)
