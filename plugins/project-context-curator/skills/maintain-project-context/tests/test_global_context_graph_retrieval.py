from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType


BACKEND = Path(__file__).resolve().parents[1] / "scripts" / "global_context.py"


def load_backend_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("graph_global_context", BACKEND)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_context(repo: Path, summary: str) -> None:
    path = repo / "docs/context/context.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "default_applicability": [
                    {"kind": "project", "selector": "self"}
                ],
                "terms": [],
                "components": [],
                "patterns": [{"name": "Repository rule", "summary": summary}],
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )


def node(name: str, path: Path) -> dict[str, str]:
    return {"project": name, "project_path": str(path)}


def edge(
    source_name: str,
    source_path: Path,
    target_name: str,
    target_path: Path,
    confidence: float,
) -> dict[str, object]:
    return {
        "source_project": source_name,
        "source_project_path": str(source_path),
        "target_project": target_name,
        "target_project_path": str(target_path),
        "relation": "integrates_with",
        "confidence": confidence,
        "evidence": [],
    }


def catalog(
    module: ModuleType,
    nodes: Sequence[dict[str, str]],
    edges: Sequence[dict[str, object]],
) -> dict[str, object]:
    return {
        "index_schema_version": module.INDEX_SCHEMA_VERSION,
        "dense_model": module.DENSE_MODEL,
        "dense_model_revision": module.DENSE_MODEL_REVISION,
        "sparse_model": module.SPARSE_MODEL,
        "sparse_model_revision": module.SPARSE_MODEL_REVISION,
        "project_nodes": list(nodes),
        "projects": [item["project"] for item in nodes],
        "relationships": {},
        "relationship_graph": {
            "schema_version": module.RELATIONSHIP_GRAPH_SCHEMA_VERSION,
            "edges": list(edges),
        },
    }


def hit(
    record_id: str,
    project: str,
    project_path: Path | None,
    label: str,
    score: float,
    applicability: Sequence[dict[str, str]],
) -> dict[str, object]:
    return {
        "id": record_id,
        "project": project,
        "project_path": str(project_path) if project_path is not None else "",
        "kind": "pattern",
        "label": label,
        "score": score,
        "source_path": f"/{project}/docs/context/context.json",
        "summary": f"{label} summary",
        "applicability": list(applicability),
    }


def install_fake_index(
    module: ModuleType, hits: Sequence[dict[str, object]]
) -> None:
    class FakeIndex:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def search(
            self,
            _query: str,
            _limit: int,
            project_paths: Sequence[str] | None = None,
        ) -> tuple[dict[str, object], ...]:
            allowed = frozenset(project_paths or ())
            return tuple(
                item
                for item in hits
                if not allowed or item.get("project_path") in allowed
            )

        def close(self) -> None:
            pass

    module.QdrantIndex = FakeIndex


def search(
    module: ModuleType,
    tmp_path: Path,
    query: str,
    current: Path,
    catalog_data: dict[str, object],
    hits: Sequence[dict[str, object]],
    active: frozenset[tuple[str, str]],
    limit: int = 20,
) -> tuple[dict[str, object], ...]:
    install_fake_index(module, hits)
    catalog_path = tmp_path / "catalog.json"
    module.write_json(catalog_path, catalog_data)
    return module.search_index(
        query,
        limit,
        current,
        tmp_path / "index",
        catalog_path,
        tmp_path / "models",
        active,
    )


def test_relationship_graph_derives_typed_edge_with_canonical_evidence(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    bridge = workspace / "telephony-bridge"
    gateway = workspace / "conversation-gateway"
    write_context(bridge, "Calls conversation-gateway for message ownership")
    write_context(gateway, "Owns gateway messages")
    records, failures = module.load_workspace_records((workspace,))
    source_record = next(record for record in records if record.project == bridge.name)

    graph = module.derive_relationship_graph(records)

    assert (failures, graph) == (
        (),
        {
            "schema_version": module.RELATIONSHIP_GRAPH_SCHEMA_VERSION,
            "edges": [
                {
                    "source_project": bridge.name,
                    "source_project_path": str(bridge.resolve()),
                    "target_project": gateway.name,
                    "target_project_path": str(gateway.resolve()),
                    "relation": "integrates_with",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "record_id": source_record.id,
                            "record_kind": "pattern",
                            "record_label": "Repository rule",
                            "source_path": source_record.source_path,
                        }
                    ],
                }
            ],
        },
    )


def test_relationship_graph_is_independent_of_record_order(tmp_path: Path) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    bridge = workspace / "telephony-bridge"
    gateway = workspace / "conversation-gateway"
    write_context(bridge, "Calls conversation-gateway")
    write_context(gateway, "References telephony-bridge")
    records, _ = module.load_workspace_records((workspace,))

    result = (
        module.derive_relationship_graph(records),
        module.derive_relationship_graph(tuple(reversed(records))),
    )

    assert result[0] == result[1]


def test_retrieval_projects_stop_after_two_graph_hops(tmp_path: Path) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    nodes = tuple(
        node(path.name, path) for path in (current, first, second, third)
    )
    edges = (
        edge(current.name, current, first.name, first, 0.9),
        edge(first.name, first, second.name, second, 0.8),
        edge(second.name, second, third.name, third, 0.7),
    )

    projects = module.retrieval_projects(
        "integration boundary", current, catalog(module, nodes, edges)
    )

    assert tuple(
        (
            item.project,
            item.project_path,
            item.reason,
            item.distance,
            item.confidence,
        )
        for item in projects
    ) == (
        (current.name, str(current), "current", 0, 1.0),
        (first.name, str(first), "related", 1, 0.9),
        (second.name, str(second), "related", 2, 0.72),
    )


def test_cross_project_query_considers_every_enrolled_repository(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    unrelated = tmp_path / "unrelated"
    nodes = (node(current.name, current), node(unrelated.name, unrelated))

    projects = module.retrieval_projects(
        "cross-project ownership",
        current,
        catalog(module, nodes, ()),
    )

    assert tuple((item.project, item.reason) for item in projects) == (
        (current.name, "current"),
        (unrelated.name, "global"),
    )


def test_transitive_relationship_requires_minimum_path_confidence(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    first = tmp_path / "first"
    accepted = tmp_path / "accepted"
    rejected = tmp_path / "rejected"
    nodes = tuple(
        node(path.name, path) for path in (current, first, accepted, rejected)
    )
    edges = (
        edge(current.name, current, first.name, first, 1.0),
        edge(first.name, first, accepted.name, accepted, 0.7),
        edge(first.name, first, rejected.name, rejected, 0.699999),
    )

    projects = module.retrieval_projects(
        "ownership",
        current,
        catalog(module, nodes, edges),
    )

    assert tuple(item.project for item in projects) == (
        current.name,
        first.name,
        accepted.name,
    )


def test_search_with_named_repository_returns_its_project_fact(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    target = tmp_path / "codex-providers"
    unrelated = tmp_path / "unrelated"
    nodes = tuple(node(path.name, path) for path in (current, target, unrelated))
    target_hit = hit(
        "target",
        target.name,
        target,
        "Provider integrations",
        0.5,
        ({"kind": "project", "selector": str(target)},),
    )
    unrelated_hit = hit(
        "unrelated",
        unrelated.name,
        unrelated,
        "Unrelated behavior",
        0.9,
        ({"kind": "project", "selector": str(unrelated)},),
    )

    results = search(
        module,
        tmp_path,
        "what do you know about codex-providers",
        current,
        catalog(module, nodes, ()),
        (unrelated_hit, target_hit),
        frozenset({("project", str(current))}),
        limit=1,
    )

    assert tuple(item["id"] for item in results) == ("target",)


def test_search_returns_one_and_two_hop_facts_but_not_third_hop(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    nodes = tuple(
        node(path.name, path) for path in (current, first, second, third)
    )
    edges = (
        edge(current.name, current, first.name, first, 0.9),
        edge(first.name, first, second.name, second, 0.8),
        edge(second.name, second, third.name, third, 0.7),
    )
    hits = tuple(
        hit(
            path.name,
            path.name,
            path,
            f"{path.name} integration",
            score,
            ({"kind": "project", "selector": str(path)},),
        )
        for path, score in ((third, 0.9), (second, 0.6), (first, 0.5))
    )

    results = search(
        module,
        tmp_path,
        "integration behavior",
        current,
        catalog(module, nodes, edges),
        hits,
        frozenset({("project", str(current))}),
    )

    assert tuple(item["id"] for item in results) == (first.name, second.name)


def test_search_keeps_broader_scope_applicability_strict(tmp_path: Path) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    universal = hit(
        "universal",
        "universal:*",
        None,
        "Universal convention",
        0.5,
        ({"kind": "universal", "selector": "*"},),
    )
    billing = hit(
        "billing",
        "domain:billing",
        None,
        "Billing convention",
        0.9,
        ({"kind": "domain", "selector": "billing"},),
    )

    results = search(
        module,
        tmp_path,
        "convention",
        current,
        catalog(module, (node(current.name, current),), ()),
        (billing, universal),
        frozenset(
            {("project", str(current)), ("universal", "*")}
        ),
    )

    assert tuple(item["id"] for item in results) == ("universal",)


def test_search_returns_strong_exact_label_from_unrelated_repository(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    unrelated = tmp_path / "unrelated"
    strong = hit(
        "strong",
        unrelated.name,
        unrelated,
        "Gateway ownership",
        0.7,
        ({"kind": "project", "selector": str(unrelated)},),
    )

    results = search(
        module,
        tmp_path,
        "explain Gateway ownership",
        current,
        catalog(
            module,
            (node(current.name, current), node(unrelated.name, unrelated)),
            (),
        ),
        (strong,),
        frozenset({("project", str(current))}),
    )

    assert tuple(item["id"] for item in results) == ("strong",)


def test_related_project_quota_preserves_multiple_projects(tmp_path: Path) -> None:
    module = load_backend_module()
    current = tmp_path / "current"
    first = tmp_path / "first"
    second = tmp_path / "second"
    nodes = tuple(node(path.name, path) for path in (current, first, second))
    edges = (
        edge(current.name, current, first.name, first, 0.9),
        edge(current.name, current, second.name, second, 0.9),
    )
    hits = tuple(
        hit(
            f"{path.name}-{index}",
            path.name,
            path,
            f"Shared integration {index}",
            score,
            ({"kind": "project", "selector": str(path)},),
        )
        for path, base in ((first, 0.9), (second, 0.5))
        for index, score in enumerate((base, base - 0.01, base - 0.02, base - 0.03))
    )

    results = search(
        module,
        tmp_path,
        "shared integration",
        current,
        catalog(module, nodes, edges),
        hits,
        frozenset({("project", str(current))}),
        limit=6,
    )

    assert tuple(item["project"] for item in results) == (
        first.name,
        first.name,
        second.name,
        second.name,
        first.name,
        first.name,
    )
