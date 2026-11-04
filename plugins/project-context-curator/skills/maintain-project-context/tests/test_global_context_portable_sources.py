from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PROJECT_ID = "41f37478-9525-4590-9b2e-e521735e1fc9"
OTHER_ID = "61e21f89-1f79-4249-b616-c28f6bf6aa6d"
PROJECT_URI = f"pcc://project/{PROJECT_ID}"
REMOTE = "github.com/acme/payments"


@pytest.fixture
def backend() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("global_context")


def portable_payload(
    root: Path,
    project_id: str = PROJECT_ID,
    *,
    project: Path | None = None,
) -> dict[str, object]:
    canonical = root / "store/projects" / project_id / "context.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(
        json.dumps(
            {
                "patterns": [
                    {"name": "Capture", "summary": "Keep payment knowledge current"}
                ],
            }
        ),
        encoding="utf-8",
    )
    workspace = root / "workspace" if project else root / "store"
    workspace.mkdir(exist_ok=True)
    return {
        "source_path": str(canonical),
        "project_path": str(project) if project else f"pcc://project/{project_id}",
        "workspace_root": str(workspace),
        "project_id": project_id,
        "remote_url": REMOTE,
        "portable": True,
    }


def parse_source(
    backend: ModuleType,
    payload: dict[str, object],
    workspace: Path,
) -> object:
    return backend.parse_external_sources((json.dumps(payload),), (workspace,))[0]


def test_discover_cli_previews_remote_only_source_without_writing(
    tmp_path: Path,
) -> None:
    payload = portable_payload(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "global_context.py"),
            "discover",
            "--workspace-root",
            str(workspace),
            "--external-source",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    preview = json.loads(result.stdout)
    assert preview["projects"] == [PROJECT_URI]
    assert preview["sources"] == [payload]
    assert preview["missing_sources"] == []
    assert len(preview["snapshot"]) == 64
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_portable_records_preserve_identity_across_store_and_checkout_moves(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    remote_payload = portable_payload(tmp_path / "first")
    first = parse_source(backend, remote_payload, tmp_path / "first/workspace")
    local = tmp_path / "second/workspace/custom-name"
    local.mkdir(parents=True)
    bound_payload = portable_payload(tmp_path / "second", project=local)
    bound = parse_source(backend, bound_payload, tmp_path / "second/workspace")

    remote_records, _, failures, _ = backend.load_source_records((first,))
    bound_records, _, bound_failures, _ = backend.load_source_records((bound,))

    assert failures == bound_failures == ()
    assert remote_records[0].id == bound_records[0].id
    assert remote_records[0].key == bound_records[0].key
    assert remote_records[0].project == "payments"
    assert bound_records[0].project == "payments"
    assert remote_records[0].project_path == PROJECT_URI
    assert remote_records[0].applicability == (("project", PROJECT_URI),)
    assert bound_records[0].project_path == str(local)
    assert bound_records[0].applicability == (("project", str(local)),)
    catalog = backend.catalog_from_records(bound_records, (bound,))
    current = backend.retrieval_projects("capture", local, catalog)
    assert current[0].project_path == str(local)
    assert current[0].reason == "current"


def test_sync_cli_rejects_changed_portable_snapshot_before_index_writes(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    payload = portable_payload(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = parse_source(backend, payload, workspace)
    approved = backend.snapshot_fingerprint((source,), (workspace,))
    extra_payload = portable_payload(tmp_path, OTHER_ID)
    index_dir = tmp_path / "derived/index"
    catalog = tmp_path / "derived/catalog.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "global_context.py"),
            "sync",
            "--workspace-root",
            str(workspace),
            "--external-source",
            json.dumps(payload),
            "--external-source",
            json.dumps(extra_payload),
            "--index-dir",
            str(index_dir),
            "--catalog",
            str(catalog),
            "--model-cache",
            str(tmp_path / "models"),
            "--enroll-new",
            "--approved-snapshot",
            approved,
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "snapshot changed" in result.stderr
    assert not catalog.exists()
    assert not index_dir.exists()


def test_refresh_rebinds_only_enrolled_project_ids(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    old_payload = portable_payload(tmp_path / "old")
    old = parse_source(backend, old_payload, tmp_path / "old/workspace")
    new_payload = portable_payload(tmp_path / "new")
    new = parse_source(backend, new_payload, tmp_path / "new/workspace")
    extra_payload = portable_payload(tmp_path / "new", OTHER_ID)
    extra = parse_source(backend, extra_payload, tmp_path / "new/workspace")
    catalog = backend.catalog_from_records((), (old,))

    refreshed = backend.catalog_sources(
        catalog, (tmp_path / "new/workspace",), (new, extra)
    )

    assert refreshed == (new,)
    assert backend.catalog_sources(
        catalog, (tmp_path / "new/workspace",), (extra,)
    ) == ()
    changed_source_id = replace(extra, project_path=old.project_path)
    assert backend.catalog_sources(
        catalog, (tmp_path / "new/workspace",), (changed_source_id,)
    ) == ()


def test_legacy_catalog_and_source_payload_stay_compatible(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    project = tmp_path / "workspace/payments"
    project.mkdir(parents=True)
    payload = portable_payload(tmp_path, project=project)
    portable = parse_source(backend, payload, tmp_path / "workspace")
    legacy = backend.ContextSource(
        portable.source_path, str(project), str(tmp_path / "workspace")
    )
    legacy_payload = {
        "source_path": legacy.source_path,
        "project_path": legacy.project_path,
        "workspace_root": legacy.workspace_root,
    }

    assert backend.source_payload(legacy) == legacy_payload
    assert backend.parse_external_sources(
        (json.dumps(legacy_payload),), (tmp_path / "workspace",)
    ) == (legacy,)
    assert backend.catalog_sources(
        {"sources": [legacy_payload]}, (tmp_path / "workspace",), (portable,)
    ) == (portable,)


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": ""},
        {"project_id": "not-a-uuid"},
        {"project_path": "pcc://project/another-project"},
        {"project_id": OTHER_ID, "project_path": f"pcc://project/{OTHER_ID}"},
    ],
)
def test_portable_sources_reject_invalid_or_mismatched_identity(
    backend: ModuleType,
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    payload = portable_payload(tmp_path)
    payload.update(change)

    with pytest.raises(backend.GlobalContextError):
        parse_source(backend, payload, tmp_path / "workspace")


def test_portable_source_rejects_escape_from_store_and_symlinks(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    payload = portable_payload(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    payload["workspace_root"] = str(outside)
    with pytest.raises(backend.GlobalContextError, match="outside"):
        parse_source(backend, payload, tmp_path / "workspace")

    payload["workspace_root"] = str(tmp_path / "store")
    canonical = Path(str(payload["source_path"]))
    real = canonical.with_name("real.json")
    canonical.rename(real)
    canonical.symlink_to(real)
    with pytest.raises(backend.GlobalContextError, match="symlink"):
        parse_source(backend, payload, tmp_path / "workspace")


def test_portable_opt_out_drops_bound_source(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    project = tmp_path / "workspace/payments"
    project.mkdir(parents=True)
    payload = portable_payload(tmp_path, project=project)
    source = parse_source(backend, payload, tmp_path / "workspace")
    (project / ".no-project-context").write_text("disabled\n", encoding="utf-8")

    assert backend.load_source_records((source,)) == ((), (), (), frozenset())


def test_portable_graph_keeps_uuid_identity_and_remote_membership_without_checkout(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    graph = importlib.import_module("context_graph")
    payload = portable_payload(tmp_path)
    source = parse_source(backend, payload, tmp_path / "workspace")
    records, _, _, _ = backend.load_source_records((source,))
    domains = (graph.DomainSpec("billing", (), (REMOTE,)),)

    built = graph.build_project_graph(records, (source,), domains, lambda _: None)

    node_id = f"project:{PROJECT_ID}"
    node = built.node(node_id)
    assert node.path == ""
    assert node.label == "payments"
    assert node.store == source.source_path
    assert dict(node.counts)["pattern"] == 1
    assert [(edge.source, edge.target) for edge in built.edges] == [
        (node_id, "domain:billing")
    ]
    assert graph.resolve_focus(built, graph.GraphView("project", PROJECT_ID)) == node_id
    local = tmp_path / "workspace/custom-name"
    local.mkdir(parents=True)
    bound = replace(source, project_path=str(local))
    bound_records, _, _, _ = backend.load_source_records((bound,))
    rebound = graph.build_project_graph(
        bound_records, (bound,), domains, lambda _: None
    )
    assert rebound.node(node_id).path == str(local)
    assert graph.resolve_focus(rebound, graph.GraphView("project", str(local))) == node_id


def test_catalog_diff_reindexes_changed_canonical_provenance(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    source = parse_source(
        backend, portable_payload(tmp_path), tmp_path / "workspace"
    )
    records, _, _, _ = backend.load_source_records((source,))
    moved_record = replace(records[0], source_path="/new-store/context.json")
    catalog = backend.catalog_from_records(records, (source,))

    assert backend.catalog_diff(catalog, (moved_record,)) == ((moved_record,), ())


def test_portable_graph_preserves_relationships_between_remote_only_projects(
    backend: ModuleType,
    tmp_path: Path,
) -> None:
    graph = importlib.import_module("context_graph")
    first_payload = portable_payload(tmp_path)
    second_payload = portable_payload(tmp_path, OTHER_ID)
    second_payload["remote_url"] = "github.com/acme/worker"
    first = parse_source(backend, first_payload, tmp_path / "workspace")
    second = parse_source(backend, second_payload, tmp_path / "workspace")
    canonical = Path(first.source_path)
    data = json.loads(canonical.read_text(encoding="utf-8"))
    data["patterns"][0]["summary"] = "payments depends on worker"
    canonical.write_text(json.dumps(data), encoding="utf-8")
    records, _, failures, _ = backend.load_source_records((first, second))

    built = graph.build_project_graph(
        records, (first, second), (), lambda _: None
    )

    assert failures == ()
    assert [(edge.source, edge.target, edge.relation) for edge in built.edges] == [
        (f"project:{PROJECT_ID}", f"project:{OTHER_ID}", "depends_on")
    ]
