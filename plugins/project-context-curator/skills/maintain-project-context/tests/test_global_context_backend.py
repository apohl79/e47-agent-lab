from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / "scripts" / "global_context.py"


def load_backend_module():
    spec = importlib.util.spec_from_file_location("global_context", BACKEND)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_context(
    repo: Path,
    *,
    summary: str,
    applicability: list[dict[str, str]] | None = None,
) -> None:
    context_dir = repo / "docs" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    pattern: dict[str, object] = {"name": "Repository rule", "summary": summary}
    if applicability is not None:
        pattern["applicability"] = applicability
    (context_dir / "context.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_applicability": [{"kind": "project", "selector": "self"}],
                "terms": [],
                "components": [],
                "patterns": [pattern],
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )


def test_discovery_recurses_across_roots_and_skips_dependency_trees(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    expected = workspace / "group" / "conversation-gateway"
    skipped = workspace / "node_modules" / "copied-repo"
    write_context(expected, summary="Owns gateway messages")
    write_context(skipped, summary="Must not be indexed")

    discovered = module.discover_context_files((workspace,))

    assert discovered == ((expected / "docs/context/context.json", workspace),)


def test_discovery_does_not_follow_a_symlinked_context_file(tmp_path: Path) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    context_dir = workspace / "repo/docs/context"
    context_dir.mkdir(parents=True)
    (context_dir / "context.json").symlink_to(outside)

    discovered = module.discover_context_files((workspace,))

    assert discovered == ()


def test_records_inherit_collection_applicability_and_allow_fact_override(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    project = workspace / "repo"
    write_context(
        project,
        summary="A universal Git convention",
        applicability=[{"kind": "universal"}],
    )

    records, failures = module.load_workspace_records((workspace,))

    assert failures == ()
    assert len(records) == 1
    assert records[0].project == "repo"
    assert records[0].applicability == (("universal", "*"),)


def test_invalid_canonical_applicability_is_skipped(tmp_path: Path) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    project = workspace / "repo"
    write_context(
        project,
        summary="Invalid project selector",
        applicability=[{"kind": "project", "selector": ""}],
    )

    records, failures = module.load_workspace_records((workspace,))

    assert records == ()
    assert len(failures) == 1
    assert "project applicability requires a selector" in failures[0]


def test_incremental_diff_changes_only_modified_and_deleted_records(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    first = workspace / "first"
    second = workspace / "second"
    write_context(first, summary="First version")
    write_context(second, summary="Second project")
    old_records, _ = module.load_workspace_records((workspace,))
    old_catalog = module.catalog_from_records(old_records)
    write_context(first, summary="Changed version")
    context_path = second / "docs/context/context.json"
    context_path.unlink()
    new_records, _ = module.load_workspace_records((workspace,))

    changed, removed = module.catalog_diff(old_catalog, new_records)

    assert [record.project for record in changed] == ["first"]
    assert removed == (old_catalog["records"][1]["id"],)


def test_relationships_derive_from_explicit_project_names_only(tmp_path: Path) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    bridge = workspace / "telephony-bridge"
    gateway = workspace / "conversation-gateway"
    unrelated = workspace / "payments"
    write_context(bridge, summary="Calls conversation-gateway for message ownership")
    write_context(gateway, summary="Owns gateway messages")
    write_context(unrelated, summary="Uses a generic gateway")
    records, _ = module.load_workspace_records((workspace,))

    relationships = module.derive_relationships(records)

    assert relationships[str(bridge.resolve())] == ("conversation-gateway",)
    assert str(unrelated.resolve()) not in relationships


def test_relationship_boost_is_gated_by_cross_project_intent(tmp_path: Path) -> None:
    module = load_backend_module()
    hits = (
        {"project": "payments", "label": "Generic owner", "score": 0.9},
        {
            "project": "conversation-gateway",
            "label": "Gateway owner",
            "score": 0.5,
        },
    )

    ordinary = module.rerank_hits(
        hits,
        "who owns this behavior",
        ("payments", "conversation-gateway"),
        ("conversation-gateway",),
        2,
    )
    cross_project = module.rerank_hits(
        hits,
        "which related project owns this behavior",
        ("payments", "conversation-gateway"),
        ("conversation-gateway",),
        2,
    )

    assert ordinary[0]["project"] == "payments"
    assert cross_project[0]["project"] == "conversation-gateway"
