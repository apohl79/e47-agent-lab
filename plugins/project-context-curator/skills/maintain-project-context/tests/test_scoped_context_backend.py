from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType


BACKEND = Path(__file__).resolve().parents[1] / "scripts" / "global_context.py"
RECORD_ID = "48e7278a-8b98-4ee5-a119-e1ae40c794ee"


def load_backend_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scoped_global_context", BACKEND)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_scope_context(
    root: Path,
    applicability: list[dict[str, str]],
    *,
    record_id: str = RECORD_ID,
) -> Path:
    path = root / "domains/billing/context.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "store_id": "f0b9cb7c-2cc4-4eb2-907f-b69ec16d3702",
                "scope_store": {"applicability": applicability},
                "default_applicability": applicability,
                "terms": [],
                "components": [],
                "patterns": [
                    {
                        "id": record_id,
                        "name": "Billing ownership",
                        "summary": "Billing services belong to the revenue domain",
                        "applicability": applicability,
                        "provenance": [],
                    }
                ],
                "open_questions": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_project_context(repo: Path) -> None:
    path = repo / "docs/context/context.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "store_id": "b7e8f44a-518d-440b-b4b7-c6f05ba127b5",
                "default_applicability": [
                    {"kind": "project", "selector": "self"}
                ],
                "terms": [],
                "components": [],
                "patterns": [],
                "open_questions": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def install_fake_index(module: ModuleType, hits: Sequence[dict[str, object]]) -> None:
    class FakeIndex:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def recreate(self) -> None:
            pass

        def delete(self, _values: Sequence[object]) -> None:
            pass

        def upsert(self, _values: Sequence[object]) -> None:
            pass

        def search(self, _query: str, _limit: int) -> tuple[dict[str, object], ...]:
            return tuple(hits)

        def close(self) -> None:
            pass

    module.QdrantIndex = FakeIndex


def test_scope_loader_preserves_canonical_identity_and_source(tmp_path: Path) -> None:
    module = load_backend_module()
    root = tmp_path / "contexts"
    path = write_scope_context(
        root, [{"kind": "domain", "selector": "billing"}]
    )

    records, failures, failed = module.load_scope_records(root)

    assert (
        failures,
        failed,
        len(records),
        records[0].id,
        records[0].project,
        records[0].project_path,
        records[0].source_path,
        records[0].applicability,
    ) == (
        (),
        frozenset(),
        1,
        RECORD_ID,
        "domain:billing",
        "",
        str(path.resolve()),
        (("domain", "billing"),),
    )


def test_scope_loader_rejects_record_outside_store_boundary(tmp_path: Path) -> None:
    module = load_backend_module()
    root = tmp_path / "contexts"
    path = write_scope_context(
        root, [{"kind": "domain", "selector": "billing"}]
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    data["patterns"][0]["applicability"] = [{"kind": "universal"}]
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    records, failures, failed = module.load_scope_records(root)

    assert (
        records,
        len(failures),
        "does not match its canonical store" in failures[0],
        failed,
    ) == ((), 1, True, frozenset({str(path.resolve())}))


def test_scope_record_is_indexed_without_becoming_an_enrolled_project(
    tmp_path: Path,
) -> None:
    module = load_backend_module()
    workspace = tmp_path / "workspace"
    project = workspace / "repo"
    scope_root = tmp_path / "contexts"
    write_project_context(project)
    write_scope_context(scope_root, [{"kind": "domain", "selector": "billing"}])
    install_fake_index(module, ())
    module.collection_is_available = lambda _path: False
    sources = module.discovered_sources((workspace,))
    catalog_path = tmp_path / "catalog.json"

    result = module.sync_index(
        (workspace,),
        tmp_path / "index",
        catalog_path,
        tmp_path / "models",
        scope_root,
        enroll_new=True,
        approved_snapshot=module.snapshot_fingerprint(sources, (workspace,)),
    )
    catalog = module.read_catalog(catalog_path)

    assert (
        result[:3],
        catalog["project_count"],
        catalog["projects"],
        len(catalog["sources"]),
        [(record["project"], record["id"]) for record in catalog["records"]],
    ) == (
        (1, 1, 1),
        1,
        ["repo"],
        1,
        [("domain:billing", RECORD_ID)],
    )


def test_applicability_filter_requires_every_selector() -> None:
    module = load_backend_module()
    hit = {
        "applicability": [
            {"kind": "domain", "selector": "billing"},
            {"kind": "user", "selector": "andreas"},
        ]
    }

    assert (
        module.hit_is_applicable(
            hit, frozenset({("domain", "billing"), ("user", "andreas")})
        ),
        module.hit_is_applicable(hit, frozenset({("domain", "billing")})),
        module.hit_is_applicable(hit, frozenset({("user", "andreas")})),
        module.hit_is_applicable(hit, frozenset({("domain", "other")})),
    ) == (True, False, False, False)


def test_search_discards_inapplicable_hits_before_limit(tmp_path: Path) -> None:
    module = load_backend_module()
    current_repo = tmp_path / "current"
    inapplicable = {
        "project": "other",
        "label": "Other rule",
        "score": 0.99,
        "applicability": [
            {"kind": "project", "selector": str(tmp_path / "other")}
        ],
    }
    applicable = {
        "project": "domain:billing",
        "label": "Billing rule",
        "score": 0.5,
        "applicability": [{"kind": "domain", "selector": "billing"}],
    }
    install_fake_index(module, (inapplicable, applicable))
    catalog_path = tmp_path / "catalog.json"
    module.write_json(
        catalog_path,
        {
            "index_schema_version": module.INDEX_SCHEMA_VERSION,
            "dense_model": module.DENSE_MODEL,
            "dense_model_revision": module.DENSE_MODEL_REVISION,
            "sparse_model": module.SPARSE_MODEL,
            "sparse_model_revision": module.SPARSE_MODEL_REVISION,
            "projects": ["current", "other"],
            "relationships": {},
        },
    )

    hits = module.search_index(
        "billing",
        1,
        current_repo,
        tmp_path / "index",
        catalog_path,
        tmp_path / "models",
        frozenset({("domain", "billing")} ),
    )

    assert hits == (applicable,)
