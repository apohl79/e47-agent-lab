# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "fastembed==0.8.0",
#   "qdrant-client==1.19.0",
# ]
# ///
"""Derived cross-project context index. Canonical data remains in context.json."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import platform
import re
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence


DENSE_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_MODEL_REPOSITORY = "qdrant/all-MiniLM-L6-v2-onnx"
DENSE_MODEL_REVISION = "5f1b8cd78bc4fb444dd171e59b18f3a3af89a079"
SPARSE_MODEL_REPOSITORY = "Qdrant/bm25"
SPARSE_MODEL_REVISION = "22b8d2af71a76161e18dd432d2cee0eefa66e412"
DENSE_DIMENSION = 384
SPARSE_AVERAGE_LENGTH = 128.0
COLLECTION = "project_context"
INDEX_SCHEMA_VERSION = 2
SEARCH_CANDIDATES = 50
IGNORE_MARKER = ".no-project-context"
UNTRUSTED_RESULT_PREFIX = "UNTRUSTED_CONTEXT_DATA"
UNTRUSTED_DIAGNOSTIC_TYPE = "UNTRUSTED_CONTEXT_DIAGNOSTIC"
PROJECT_OUTPUT_LIMIT = 120
KIND_OUTPUT_LIMIT = 32
LABEL_OUTPUT_LIMIT = 200
PATH_OUTPUT_LIMIT = 1024
SUMMARY_OUTPUT_LIMIT = 500
APPLICABILITY_OUTPUT_LIMIT = 500
DIAGNOSTIC_OUTPUT_LIMIT = 1024
DIAGNOSTIC_COUNT_LIMIT = 20
SNAPSHOT_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
CROSS_PROJECT_HINT = re.compile(
    r"\b(cross[- ]project|other (project|repository|repo)|related (project|repository|repo)|upstream|downstream|sibling)\b",
    re.IGNORECASE,
)
SKIPPED_DIRECTORIES = frozenset(
    {".git", ".my", ".venv", "build", "dist", "node_modules", "target"}
)
RECORD_SPECS = (
    (
        "terms",
        "term",
        "term",
        "definition",
        ("term", "kind", "definition", "scope", "aliases", "notes"),
    ),
    (
        "components",
        "component",
        "name",
        "responsibility",
        ("name", "responsibility", "paths", "interfaces", "notes"),
    ),
    (
        "patterns",
        "pattern",
        "name",
        "summary",
        ("name", "summary", "applies_to", "notes"),
    ),
    (
        "open_questions",
        "question",
        "question",
        "context",
        ("question", "status", "context", "answer"),
    ),
)


class GlobalContextError(RuntimeError):
    """Expected global-index failure suitable for a concise CLI message."""


@dataclass(frozen=True)
class ContextRecord:
    id: str
    key: str
    project: str
    project_path: str
    workspace_root: str
    kind: str
    label: str
    summary: str
    text: str
    source_path: str
    applicability: tuple[tuple[str, str], ...]
    content_hash: str


@dataclass(frozen=True)
class ContextSource:
    source_path: str
    project_path: str
    workspace_root: str


def one_line(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(one_line(item) for item in value)
    return " ".join(str(value or "").split())


def safe_output_field(value: Any, limit: int) -> str:
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in one_line(value)
    )
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_diagnostics(
    failures: Sequence[str], event: str
) -> tuple[str, ...]:
    shown_count = (
        len(failures)
        if len(failures) <= DIAGNOSTIC_COUNT_LIMIT
        else DIAGNOSTIC_COUNT_LIMIT - 1
    )
    lines = tuple(
        json.dumps(
            {
                "type": UNTRUSTED_DIAGNOSTIC_TYPE,
                "event": safe_output_field(event, 80),
                "detail": safe_output_field(failure, DIAGNOSTIC_OUTPUT_LIMIT),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        for failure in failures[:shown_count]
    )
    remaining = len(failures) - len(lines)
    if remaining <= 0:
        return lines
    summary = json.dumps(
        {
            "type": UNTRUSTED_DIAGNOSTIC_TYPE,
            "event": safe_output_field(event, 80),
            "detail": f"{remaining} additional diagnostics omitted",
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return (*lines, summary)


def mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GlobalContextError(f"{label} must be an object")
    return value


def sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GlobalContextError(f"{label} must be an array")
    return value


def discover_context_files(
    roots: Sequence[Path],
) -> tuple[tuple[Path, Path], ...]:
    discovered: dict[Path, Path] = {}
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.is_dir():
            continue
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if name not in SKIPPED_DIRECTORIES)
            current = Path(directory)
            if (
                current.name == "context"
                and current.parent.name == "docs"
                and "context.json" in files
            ):
                candidate = current / "context.json"
                if candidate.is_symlink():
                    names[:] = []
                    continue
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    names[:] = []
                    continue
                project = resolved.parents[2]
                if (project / IGNORE_MARKER).exists():
                    names[:] = []
                    continue
                discovered.setdefault(resolved, root)
                names[:] = []
    return tuple(sorted(discovered.items(), key=lambda item: str(item[0]).casefold()))


def discover_primary_git_repositories(
    roots: Sequence[Path],
) -> tuple[tuple[Path, Path], ...]:
    discovered: dict[Path, Path] = {}
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.is_dir():
            continue
        for directory, names, _files in os.walk(root, followlinks=False):
            current = Path(directory)
            git_directory = current / ".git"
            has_git_directory = ".git" in names and not git_directory.is_symlink()
            names[:] = sorted(name for name in names if name not in SKIPPED_DIRECTORIES)
            if not has_git_directory:
                continue
            if current.is_symlink():
                names[:] = []
                continue
            project = current.resolve()
            try:
                project.relative_to(root)
            except ValueError:
                names[:] = []
                continue
            if (project / IGNORE_MARKER).exists():
                continue
            discovered.setdefault(project, root)
    return tuple(sorted(discovered.items(), key=lambda item: str(item[0]).casefold()))


def context_source(path: Path, root: Path) -> ContextSource:
    return ContextSource(
        source_path=str(path),
        project_path=str(path.parents[2]),
        workspace_root=str(root),
    )


def discovered_sources(roots: Sequence[Path]) -> tuple[ContextSource, ...]:
    return tuple(
        context_source(path, root) for path, root in discover_context_files(roots)
    )


def discover_context_candidates(
    roots: Sequence[Path],
) -> tuple[tuple[ContextSource, ...], tuple[ContextSource, ...]]:
    sources = {
        Path(source.source_path): source for source in discovered_sources(roots)
    }
    requiring_initialization: dict[Path, ContextSource] = {}
    for project, root in discover_primary_git_repositories(roots):
        path = project / "docs/context/context.json"
        if path in sources:
            continue
        directories = (path.parent.parent, path.parent)
        if any(
            directory.is_symlink()
            or (directory.exists() and not directory.is_dir())
            for directory in directories
        ):
            continue
        if path.exists() or path.is_symlink():
            continue
        candidate = context_source(path, root)
        sources[path] = candidate
        requiring_initialization[path] = candidate
    source_order = sorted(sources, key=lambda path: str(path).casefold())
    initialization_order = sorted(
        requiring_initialization,
        key=lambda path: str(path).casefold(),
    )
    return (
        tuple(sources[path] for path in source_order),
        tuple(requiring_initialization[path] for path in initialization_order),
    )


def source_payload(source: ContextSource) -> dict[str, str]:
    return asdict(source)


def snapshot_fingerprint(
    sources: Sequence[ContextSource], roots: Sequence[Path] | None = None
) -> str:
    workspace_roots = (
        tuple(roots)
        if roots is not None
        else tuple(Path(value) for value in {source.workspace_root for source in sources})
    )
    payload = {
        "workspace_roots": sorted(
            {str(root.expanduser().resolve()) for root in workspace_roots},
            key=str.casefold,
        ),
        "sources": [source_payload(source) for source in sources],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def resolved_applicability(
    value: Any,
    *,
    project_path: Path,
    workspace_root: Path,
) -> tuple[tuple[str, str], ...]:
    selectors = sequence(value, "applicability")
    resolved: set[tuple[str, str]] = set()
    for raw_selector in selectors:
        selector = mapping(raw_selector, "applicability selector")
        kind = one_line(selector.get("kind")).casefold()
        raw_value = one_line(selector.get("selector"))
        if kind == "universal":
            if raw_value:
                raise GlobalContextError(
                    "universal applicability must not contain a selector"
                )
            resolved.add((kind, "*"))
        elif kind == "project":
            if not raw_value:
                raise GlobalContextError("project applicability requires a selector")
            resolved.add(
                (kind, str(project_path) if raw_value == "self" else raw_value)
            )
        elif kind == "workspace":
            if not raw_value:
                raise GlobalContextError("workspace applicability requires a selector")
            resolved.add(
                (kind, str(workspace_root) if raw_value == "self" else raw_value)
            )
        elif kind == "user":
            if not raw_value:
                raise GlobalContextError("user applicability requires a selector")
            resolved.add(
                (kind, getpass.getuser() if raw_value == "self" else raw_value)
            )
        elif kind == "machine":
            if not raw_value:
                raise GlobalContextError("machine applicability requires a selector")
            resolved.add((kind, platform.node() if raw_value == "self" else raw_value))
        else:
            raise GlobalContextError(f"unsupported applicability kind {kind!r}")
    if not resolved:
        raise GlobalContextError("applicability must contain at least one selector")
    return tuple(sorted(resolved))


def record_text(
    project: str,
    kind: str,
    record: dict[str, Any],
    fields: Sequence[str],
    applicability: Sequence[tuple[str, str]],
) -> str:
    values = [f"project: {project}", f"record type: {kind}"]
    values.extend(
        f"{field.replace('_', ' ')}: {one_line(record.get(field))}"
        for field in fields
        if one_line(record.get(field))
    )
    values.append(
        "applicability: "
        + " ".join(f"{kind}:{selector}" for kind, selector in applicability)
    )
    return "\n".join(values)


def records_from_context(path: Path, workspace_root: Path) -> tuple[ContextRecord, ...]:
    data = mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
    repo = path.parents[2].resolve()
    project = repo.name
    default = data.get(
        "default_applicability", [{"kind": "project", "selector": "self"}]
    )
    records: list[ContextRecord] = []
    for collection, kind, label_field, summary_field, fields in RECORD_SPECS:
        for raw_record in sequence(data.get(collection, []), f"{path}:{collection}"):
            record = mapping(raw_record, f"{path}:{collection} record")
            label = one_line(record.get(label_field))
            if not label:
                raise GlobalContextError(
                    f"{path}:{collection} record has no {label_field}"
                )
            applicability = resolved_applicability(
                record.get("applicability", default),
                project_path=repo,
                workspace_root=workspace_root,
            )
            text = record_text(project, kind, record, fields, applicability)
            key = "\x1f".join((str(repo), kind, label.casefold()))
            content_hash = hashlib.sha256(
                json.dumps(
                    {"text": text, "applicability": applicability},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            records.append(
                ContextRecord(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
                    key=key,
                    project=project,
                    project_path=str(repo),
                    workspace_root=str(workspace_root),
                    kind=kind,
                    label=label,
                    summary=one_line(record.get(summary_field)),
                    text=text,
                    source_path=str(path),
                    applicability=applicability,
                    content_hash=content_hash,
                )
            )
    keys = [record.key for record in records]
    if len(keys) != len(set(keys)):
        raise GlobalContextError(f"{path} contains duplicate record labels")
    return tuple(records)


def catalog_sources(
    catalog: dict[str, Any], roots: Sequence[Path]
) -> tuple[ContextSource, ...]:
    allowed_roots = {
        str(path.expanduser().resolve()): path.expanduser().resolve() for path in roots
    }
    raw_sources = catalog.get("sources")
    candidates = (
        raw_sources if isinstance(raw_sources, list) else catalog.get("records", [])
    )
    sources: dict[str, ContextSource] = {}
    for raw_source in candidates:
        if not isinstance(raw_source, dict):
            continue
        raw_path = raw_source.get("source_path")
        raw_workspace = raw_source.get("workspace_root")
        workspace = allowed_roots.get(str(raw_workspace))
        if not raw_path or workspace is None:
            continue
        source = Path(os.path.normpath(str(Path(str(raw_path)).expanduser())))
        if not source.is_absolute():
            continue
        try:
            source.relative_to(workspace)
        except ValueError:
            continue
        if (
            source.name != "context.json"
            or source.parent.name != "context"
            or source.parent.parent.name != "docs"
        ):
            continue
        project = source.parents[2]
        sources[str(source)] = ContextSource(
            source_path=str(source),
            project_path=str(project),
            workspace_root=str(workspace),
        )
    return tuple(sources[key] for key in sorted(sources, key=str.casefold))


def record_from_payload(raw: dict[str, Any]) -> ContextRecord | None:
    try:
        applicability = tuple(
            (str(item["kind"]), str(item["selector"]))
            for item in raw["applicability"]
            if isinstance(item, dict)
        )
        return ContextRecord(
            id=str(raw["id"]),
            key=str(raw["key"]),
            project=str(raw["project"]),
            project_path=str(raw["project_path"]),
            workspace_root=str(raw["workspace_root"]),
            kind=str(raw["kind"]),
            label=str(raw["label"]),
            summary=str(raw["summary"]),
            text=str(raw["text"]),
            source_path=str(raw["source_path"]),
            applicability=applicability,
            content_hash=str(raw["content_hash"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def sorted_records(records: Sequence[ContextRecord]) -> tuple[ContextRecord, ...]:
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item.project_path.casefold(),
                item.kind,
                item.label.casefold(),
            ),
        )
    )


def load_source_records(
    sources: Sequence[ContextSource],
) -> tuple[
    tuple[ContextRecord, ...],
    tuple[ContextSource, ...],
    tuple[str, ...],
    frozenset[str],
]:
    records: list[ContextRecord] = []
    active_sources: list[ContextSource] = []
    failures: list[str] = []
    failed_sources: set[str] = set()
    for source in sources:
        path = Path(source.source_path)
        project = Path(source.project_path)
        if (project / IGNORE_MARKER).exists():
            continue
        active_sources.append(source)
        try:
            safe_file = (
                path.is_file()
                and not path.is_symlink()
                and path.resolve(strict=True) == path
            )
        except OSError:
            safe_file = False
        if not safe_file:
            failures.append(f"{path}: canonical context is missing or unsafe")
            failed_sources.add(source.source_path)
            continue
        try:
            records.extend(records_from_context(path, Path(source.workspace_root)))
        except (OSError, json.JSONDecodeError, GlobalContextError) as exc:
            failures.append(f"{path}: {exc}")
            failed_sources.add(source.source_path)
    return (
        sorted_records(records),
        tuple(active_sources),
        tuple(failures),
        frozenset(failed_sources),
    )


def preserved_records(
    catalog: dict[str, Any], failed_sources: frozenset[str]
) -> tuple[ContextRecord, ...]:
    records: list[ContextRecord] = []
    for raw_record in catalog.get("records", []):
        if not isinstance(raw_record, dict):
            continue
        if str(raw_record.get("source_path")) not in failed_sources:
            continue
        record = record_from_payload(raw_record)
        if record is not None:
            records.append(record)
    return sorted_records(records)


def load_workspace_records(
    roots: Sequence[Path],
) -> tuple[tuple[ContextRecord, ...], tuple[str, ...]]:
    records, _, failures, _ = load_source_records(discovered_sources(roots))
    return records, failures


def load_known_records(
    catalog: dict[str, Any],
    roots: Sequence[Path],
) -> tuple[tuple[ContextRecord, ...], tuple[str, ...]]:
    records, _, failures, failed = load_source_records(catalog_sources(catalog, roots))
    combined = sorted_records((*records, *preserved_records(catalog, failed)))
    return combined, failures


def derive_relationships(
    records: Sequence[ContextRecord],
) -> dict[str, tuple[str, ...]]:
    projects = sorted({record.project for record in records}, key=len, reverse=True)
    relationships: dict[str, set[str]] = {}
    for record in records:
        haystack = record.text.casefold()
        for project in projects:
            if project == record.project:
                continue
            pattern = rf"(?<![\w-]){re.escape(project.casefold())}(?![\w-])"
            if re.search(pattern, haystack):
                relationships.setdefault(record.project_path, set()).add(project)
    return {
        project: tuple(sorted(related, key=str.casefold))
        for project, related in sorted(relationships.items())
    }


def record_payload(record: ContextRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["applicability"] = [
        {"kind": kind, "selector": selector} for kind, selector in record.applicability
    ]
    return payload


def catalog_from_records(
    records: Sequence[ContextRecord],
    sources: Sequence[ContextSource] | None = None,
) -> dict[str, Any]:
    enrolled = (
        tuple(sources)
        if sources is not None
        else tuple(
            {
                record.source_path: ContextSource(
                    source_path=record.source_path,
                    project_path=record.project_path,
                    workspace_root=record.workspace_root,
                )
                for record in records
            }.values()
        )
    )
    projects = {Path(source.project_path).name for source in enrolled}
    projects.update(record.project for record in records)
    return {
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "dense_model": DENSE_MODEL,
        "dense_model_revision": DENSE_MODEL_REVISION,
        "sparse_model": SPARSE_MODEL,
        "sparse_model_revision": SPARSE_MODEL_REVISION,
        "enrollment_policy": "snapshot",
        "projects": sorted(projects, key=str.casefold),
        "project_count": len(enrolled),
        "relationships": derive_relationships(records),
        "sources": [source_payload(source) for source in enrolled],
        "records": [record_payload(record) for record in records],
    }


def catalog_diff(
    old_catalog: dict[str, Any],
    new_records: Sequence[ContextRecord],
) -> tuple[tuple[ContextRecord, ...], tuple[str, ...]]:
    old_records = {
        str(record.get("id")): record
        for record in old_catalog.get("records", [])
        if isinstance(record, dict) and record.get("id")
    }
    new_ids = {record.id for record in new_records}
    changed = tuple(
        record
        for record in new_records
        if old_records.get(record.id, {}).get("content_hash") != record.content_hash
    )
    removed = tuple(sorted(set(old_records) - new_ids))
    return changed, removed


def read_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield


def pinned_model_path(
    repository: str,
    revision: str,
    cache: Path,
    *,
    allow_download: bool,
) -> str:
    from huggingface_hub import snapshot_download

    try:
        return snapshot_download(
            repo_id=repository,
            revision=revision,
            cache_dir=cache,
            local_files_only=not allow_download,
        )
    except Exception as exc:
        if allow_download:
            raise GlobalContextError(
                f"failed to download pinned model {repository}@{revision}: {exc}"
            ) from exc
        raise GlobalContextError(
            "pinned embedding models are not cached; ask before running global-upgrade"
        ) from exc


class QdrantIndex:
    def __init__(
        self,
        index_dir: Path,
        model_cache: Path,
        *,
        allow_model_download: bool = False,
    ) -> None:
        from fastembed import SparseTextEmbedding, TextEmbedding
        from qdrant_client import QdrantClient, models

        model_cache.mkdir(parents=True, exist_ok=True)
        model_cache.chmod(0o700)
        index_dir.parent.mkdir(parents=True, exist_ok=True)
        index_dir.parent.chmod(0o700)
        dense_path = pinned_model_path(
            DENSE_MODEL_REPOSITORY,
            DENSE_MODEL_REVISION,
            model_cache,
            allow_download=allow_model_download,
        )
        sparse_path = pinned_model_path(
            SPARSE_MODEL_REPOSITORY,
            SPARSE_MODEL_REVISION,
            model_cache,
            allow_download=allow_model_download,
        )
        self.models_api = models
        self.client = QdrantClient(path=index_dir)
        self.dense = TextEmbedding(
            model_name=DENSE_MODEL,
            cache_dir=str(model_cache),
            specific_model_path=dense_path,
        )
        self.sparse = SparseTextEmbedding(
            model_name=SPARSE_MODEL,
            cache_dir=str(model_cache),
            avg_len=SPARSE_AVERAGE_LENGTH,
            specific_model_path=sparse_path,
        )

    def doctor(self) -> None:
        next(iter(self.dense.query_embed(["project context runtime check"])))
        next(iter(self.sparse.query_embed(["project context runtime check"])))

    def close(self) -> None:
        self.client.close()

    def recreate(self) -> None:
        models = self.models_api
        if self.client.collection_exists(COLLECTION):
            self.client.delete_collection(COLLECTION)
        self.client.create_collection(
            collection_name=COLLECTION,
            vectors_config={
                "dense": models.VectorParams(
                    size=DENSE_DIMENSION,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(COLLECTION):
            self.recreate()

    def upsert(self, records: Sequence[ContextRecord]) -> None:
        if not records:
            return
        models = self.models_api
        texts = [record.text for record in records]
        dense_vectors = tuple(self.dense.embed(texts))
        sparse_vectors = tuple(self.sparse.embed(texts))
        points = [
            models.PointStruct(
                id=record.id,
                vector={
                    "dense": dense.tolist(),
                    "sparse": models.SparseVector(
                        indices=sparse.indices.tolist(),
                        values=sparse.values.tolist(),
                    ),
                },
                payload=record_payload(record),
            )
            for record, dense, sparse in zip(records, dense_vectors, sparse_vectors)
        ]
        for start in range(0, len(points), 128):
            self.client.upsert(
                collection_name=COLLECTION,
                points=points[start : start + 128],
                wait=True,
            )

    def delete(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        self.client.delete(
            collection_name=COLLECTION,
            points_selector=self.models_api.PointIdsList(points=list(record_ids)),
            wait=True,
        )

    def search(self, query: str, limit: int) -> tuple[dict[str, Any], ...]:
        models = self.models_api
        dense = next(iter(self.dense.query_embed([query])))
        sparse = next(iter(self.sparse.query_embed([query])))
        response = self.client.query_points(
            COLLECTION,
            prefetch=[
                models.Prefetch(
                    query=dense.tolist(), using="dense", limit=SEARCH_CANDIDATES
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse.indices.tolist(), values=sparse.values.tolist()
                    ),
                    using="sparse",
                    limit=SEARCH_CANDIDATES,
                ),
            ],
            query=models.RrfQuery(rrf=models.Rrf(k=60)),
            limit=max(limit * 3, limit),
        )
        return tuple(
            {**(point.payload or {}), "score": float(point.score)}
            for point in response.points
        )


def collection_is_available(index_dir: Path) -> bool:
    if not index_dir.exists():
        return False
    from qdrant_client import QdrantClient

    client = None
    try:
        client = QdrantClient(path=index_dir)
        return client.collection_exists(COLLECTION)
    except Exception:
        return False
    finally:
        if client is not None:
            client.close()


def catalog_is_compatible(catalog: dict[str, Any]) -> bool:
    return (
        catalog.get("index_schema_version") == INDEX_SCHEMA_VERSION
        and catalog.get("dense_model") == DENSE_MODEL
        and catalog.get("dense_model_revision") == DENSE_MODEL_REVISION
        and catalog.get("sparse_model") == SPARSE_MODEL
        and catalog.get("sparse_model_revision") == SPARSE_MODEL_REVISION
    )


def catalog_has_enrollment_state(catalog: dict[str, Any]) -> bool:
    if catalog_is_compatible(catalog):
        return isinstance(catalog.get("sources"), list)
    return (
        catalog.get("index_schema_version") == 1
        and isinstance(catalog.get("records"), list)
    )


def sync_index(
    roots: Sequence[Path],
    index_dir: Path,
    catalog_path: Path,
    model_cache: Path,
    *,
    enroll_new: bool = False,
    approved_snapshot: str | None = None,
) -> tuple[int, int, int, tuple[str, ...]]:
    with exclusive_lock(index_dir.parent / "index.lock"):
        old_catalog = read_catalog(catalog_path)
        if enroll_new:
            enrolled = discovered_sources(roots)
            current_snapshot = snapshot_fingerprint(enrolled, roots)
            if not approved_snapshot or not SNAPSHOT_TOKEN_PATTERN.fullmatch(
                approved_snapshot
            ):
                raise GlobalContextError(
                    "approved snapshot token must be 64 lowercase hexadecimal characters"
                )
            if not hmac.compare_digest(current_snapshot, approved_snapshot):
                raise GlobalContextError(
                    "discovered project snapshot changed; preview it again before approval"
                )
        else:
            if not catalog_has_enrollment_state(old_catalog):
                raise GlobalContextError(
                    "global enrollment catalog is missing or invalid; preview "
                    "global-enroll and request approval again"
                )
            enrolled = catalog_sources(old_catalog, roots)
        records, active_sources, failures, failed = load_source_records(enrolled)
        records = sorted_records((*records, *preserved_records(old_catalog, failed)))
        reset = not catalog_is_compatible(old_catalog) or not collection_is_available(
            index_dir
        )
        changed, removed = (
            (records, ()) if reset else catalog_diff(old_catalog, records)
        )
        if not reset and not changed and not removed:
            write_json(catalog_path, catalog_from_records(records, active_sources))
            return (
                len(active_sources),
                len(records),
                0,
                failures,
            )
        index = QdrantIndex(index_dir, model_cache)
        try:
            if reset:
                index.recreate()
            else:
                index.delete(removed)
            index.upsert(changed)
        finally:
            index.close()
        write_json(catalog_path, catalog_from_records(records, active_sources))
    return (
        len(active_sources),
        len(records),
        len(changed),
        failures,
    )


def mentioned_projects(query: str, projects: Sequence[str]) -> frozenset[str]:
    haystack = query.casefold()
    return frozenset(
        project
        for project in projects
        if re.search(rf"(?<![\w-]){re.escape(project.casefold())}(?![\w-])", haystack)
    )


def rerank_hits(
    hits: Sequence[dict[str, Any]],
    query: str,
    projects: Sequence[str],
    related_projects: Sequence[str],
    limit: int,
) -> tuple[dict[str, Any], ...]:
    mentioned = mentioned_projects(query, projects)
    relationship_targets = (
        frozenset(related_projects) if CROSS_PROJECT_HINT.search(query) else frozenset()
    )
    normalized_query = query.casefold()

    def score(hit: dict[str, Any]) -> tuple[float, str, str]:
        value = float(hit.get("score", 0.0))
        project = one_line(hit.get("project"))
        label = one_line(hit.get("label"))
        if project in mentioned:
            value += 1.0
        if project in relationship_targets:
            value += 0.5
        if label and label.casefold() in normalized_query:
            value += 0.25
        return (-value, project.casefold(), label.casefold())

    return tuple(sorted(hits, key=score)[:limit])


def search_index(
    query: str,
    limit: int,
    current_repo: Path,
    index_dir: Path,
    catalog_path: Path,
    model_cache: Path,
) -> tuple[dict[str, Any], ...]:
    catalog = read_catalog(catalog_path)
    if not catalog_is_compatible(catalog):
        raise GlobalContextError("global index is not initialized")
    with exclusive_lock(index_dir.parent / "index.lock"):
        index = QdrantIndex(index_dir, model_cache)
        try:
            hits = index.search(query, limit)
        finally:
            index.close()
    relationships = catalog.get("relationships", {})
    current_project_path = str(current_repo.expanduser().resolve())
    related = (
        relationships.get(current_project_path, [])
        if isinstance(relationships, dict)
        else []
    )
    return rerank_hits(hits, query, catalog.get("projects", []), related, limit)


def format_hit(hit: dict[str, Any]) -> str:
    applicability = ", ".join(
        f"{safe_output_field(item.get('kind'), KIND_OUTPUT_LIMIT)}:"
        f"{safe_output_field(item.get('selector', '*'), PROJECT_OUTPUT_LIMIT)}"
        for item in hit.get("applicability", [])
        if isinstance(item, dict)
    )
    return " | ".join(
        (
            UNTRUSTED_RESULT_PREFIX,
            safe_output_field(hit.get("project"), PROJECT_OUTPUT_LIMIT),
            safe_output_field(hit.get("kind"), KIND_OUTPUT_LIMIT),
            safe_output_field(hit.get("label"), LABEL_OUTPUT_LIMIT),
            safe_output_field(hit.get("source_path"), PATH_OUTPUT_LIMIT),
            safe_output_field(hit.get("summary"), SUMMARY_OUTPUT_LIMIT),
            "applies: " + safe_output_field(applicability, APPLICABILITY_OUTPUT_LIMIT),
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain a derived global context index."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover")
    discover.add_argument("--workspace-root", type=Path, action="append", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--index-dir", type=Path, required=True)
    doctor.add_argument("--model-cache", type=Path, required=True)

    def add_index_arguments(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--workspace-root", type=Path, action="append", required=True
        )
        subparser.add_argument("--index-dir", type=Path, required=True)
        subparser.add_argument("--catalog", type=Path, required=True)
        subparser.add_argument("--model-cache", type=Path, required=True)

    sync = subparsers.add_parser("sync")
    add_index_arguments(sync)
    sync.add_argument("--enroll-new", action="store_true")
    sync.add_argument("--approved-snapshot")
    search = subparsers.add_parser("search")
    add_index_arguments(search)
    search.add_argument("--current-repo", type=Path, required=True)
    search.add_argument("--query", action="append", required=True)
    search.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "discover":
        roots = tuple(path.expanduser().resolve() for path in args.workspace_root)
        sources, requiring_initialization = discover_context_candidates(roots)
        print(
            json.dumps(
                {
                    "snapshot": snapshot_fingerprint(sources, roots),
                    "workspace_roots": [str(root) for root in roots],
                    "projects": [source.project_path for source in sources],
                    "sources": [source_payload(source) for source in sources],
                    "missing_sources": [
                        source_payload(source) for source in requiring_initialization
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "doctor":
        with exclusive_lock(args.index_dir.parent / "index.lock"):
            index = QdrantIndex(
                args.index_dir,
                args.model_cache,
                allow_model_download=True,
            )
            try:
                index.doctor()
            finally:
                index.close()
        print("Qdrant runtime ready")
        return 0

    roots = tuple(path.expanduser().resolve() for path in args.workspace_root)
    project_count, record_count, changed_count, failures = sync_index(
        roots,
        args.index_dir,
        args.catalog,
        args.model_cache,
        enroll_new=args.command == "sync" and args.enroll_new,
        approved_snapshot=(args.approved_snapshot if args.command == "sync" else None),
    )
    if args.command == "sync":
        print(
            f"Indexed {project_count} projects and {record_count} records "
            f"({changed_count} changed)."
        )
        for line in format_diagnostics(failures, "skipped invalid context"):
            print(line)
        return 0

    for line in format_diagnostics(failures, "retained invalid context"):
        print(line, file=sys.stderr)

    query = " ".join(one_line(value) for value in args.query if one_line(value))
    if not query:
        raise GlobalContextError("search query must not be blank")
    hits = search_index(
        query,
        args.limit,
        args.current_repo,
        args.index_dir,
        args.catalog,
        args.model_cache,
    )
    if not hits:
        print(f"No global context matches for: {query.casefold()}")
    else:
        for hit in hits:
            print(format_hit(hit))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GlobalContextError as exc:
        raise SystemExit(str(exc)) from exc
