from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_graph_module() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("context_graph")


def record(
    project: str,
    project_path: str,
    kind: str,
    label: str,
    text: str,
    applicability: tuple[tuple[str, str], ...] = (("project", "self"),),
) -> object:
    backend = importlib.import_module("global_context")
    return backend.ContextRecord(
        f"{project}:{kind}:{label}",
        f"{kind}:{label}",
        project,
        project_path,
        "",
        kind,
        label,
        text,
        f"project: {project}\nrecord type: {kind}\n{text}",
        f"{project_path or project}/context.json",
        applicability,
        "hash",
    )


def fixture_graph(tmp_path: Path) -> tuple[ModuleType, object, dict[str, str]]:
    graph = load_graph_module()
    backend = importlib.import_module("global_context")
    alpha, beta, gamma, delta = (
        tmp_path / name for name in ("alpha", "beta", "gamma", "delta")
    )
    for project in (alpha, beta, gamma):
        project.mkdir()
    sources = tuple(
        backend.ContextSource(str(path / "docs/context/context.json"), str(path), "")
        for path in (alpha, beta)
    )
    domain, universal = (("domain", "billing"),), (("universal", "*"),)
    records = (
        record("alpha", str(alpha), "pattern", "Payments", "alpha calls beta for payments"),
        record("alpha", str(alpha), "term", "Ledger", "Ledger is the book of record"),
        record("beta", str(beta), "component", "API", "Ledger API; see alpha for terms"),
        record("domain:billing", "", "term", "Invoice", "alpha owns invoices", domain),
        record("universal:*", "", "pattern", "Retries", "Retry with backoff", universal),
        record("machine:*", "", "term", "Laptop", "alpha lives here", (("machine", "*"),)),
    )
    domains = (
        graph.DomainSpec(
            "billing",
            (str(alpha), str(gamma), str(delta)),
            ("github.com/acme/beta", "github.com/acme/worker"),
        ),
    )
    built = graph.build_project_graph(
        records, sources, domains, {beta: "github.com/acme/beta"}.get
    )
    ids = {path.name: f"project:{path}" for path in (alpha, beta, gamma, delta)}
    return graph, built, ids


def edge_keys(built: object) -> list[tuple[str, str, str, float]]:
    return [(edge.source, edge.target, edge.relation, edge.confidence) for edge in built.edges]


def counts(**values: int) -> dict[str, int]:
    return {"component": 0, "pattern": 0, "question": 0, "term": 0, **values}


def test_project_graph_builds_store_nodes_membership_and_relationship_edges(
    tmp_path: Path,
) -> None:
    graph, built, ids = fixture_graph(tmp_path)

    assert (
        [(n.id, n.kind, n.label, n.status, dict(n.counts)) for n in built.nodes],
        edge_keys(built),
        built.edges[0].evidence,
    ) == (
        [
            ("domain:billing", "domain", "billing", "declared", counts(term=1)),
            (ids["alpha"], "project", "alpha", "initialized", counts(pattern=1, term=1)),
            (ids["beta"], "project", "beta", "initialized", counts(component=1)),
            (ids["delta"], "project", "delta", "missing", {}),
            (ids["gamma"], "project", "gamma", "uninitialized", {}),
            ("remote:github.com/acme/worker", "project", "worker", "remote-only", {}),
            ("universal", "universal", "universal", "declared", counts(pattern=1)),
        ],
        [
            ("domain:billing", ids["alpha"], "owns", 0.9),
            (ids["alpha"], "domain:billing", "member_of", 1.0),
            (ids["alpha"], ids["beta"], "integrates_with", 0.9),
            (ids["beta"], "domain:billing", "member_of", 1.0),
            (ids["beta"], ids["alpha"], "references", 0.6),
            (ids["delta"], "domain:billing", "member_of", 1.0),
            (ids["gamma"], "domain:billing", "member_of", 1.0),
            ("remote:github.com/acme/worker", "domain:billing", "member_of", 1.0),
        ],
        (("domain:billing:term:Invoice", "term", "Invoice"),),
    )


def test_views_focus_by_domain_or_project_and_filter_edges(tmp_path: Path) -> None:
    graph, built, ids = fixture_graph(tmp_path)
    members = ["billing", "alpha", "beta", "delta", "gamma", "worker"]
    shallow = graph.apply_view(
        built, graph.GraphView("domain", "billing", 1, min_confidence=0.8)
    )
    deep = graph.apply_view(
        built, graph.GraphView("domain", "billing", 2, relations=("member_of", "references"))
    )
    by_name = graph.apply_view(built, graph.GraphView("project", "BETA"))
    by_path = graph.apply_view(built, graph.GraphView("project", str(tmp_path / "alpha")))
    overview = graph.apply_view(built, graph.GraphView())
    with pytest.raises(ValueError, match="unknown domain 'ops'"):
        graph.apply_view(built, graph.GraphView("domain", "ops"))
    with pytest.raises(ValueError, match="unknown project 'omega'"):
        graph.apply_view(built, graph.GraphView("project", "omega"))

    assert (
        [node.label for node in shallow.nodes],
        edge_keys(shallow),
        shallow.view.focus,
        [node.label for node in deep.nodes],
        [edge.relation for edge in deep.edges],
        (by_name.view.focus, [node.label for node in by_name.nodes]),
        (by_path.view.focus, [node.label for node in by_path.nodes]),
        (overview.view.focus, len(overview.nodes), len(overview.edges)),
        graph.render_json(by_name).count('"schema_version": 1'),
    ) == (
        members,
        [edge for edge in edge_keys(built) if edge[2] != "references"],
        "domain:billing",
        members,
        ["member_of", "member_of", "references", "member_of", "member_of", "member_of"],
        (ids["beta"], ["billing", "alpha", "beta"]),
        (ids["alpha"], ["billing", "alpha", "beta"]),
        ("", 7, 8),
        1,
    )
