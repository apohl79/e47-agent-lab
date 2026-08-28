# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "fastembed==0.8.0",
#   "qdrant-client==1.19.0",
# ]
# ///
"""Derived context index. Canonical data remains in project, Git, or XDG JSON."""

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
INDEX_SCHEMA_VERSION = 3
LEGACY_RECORD_CATALOG_SCHEMA_VERSION = 1
SOURCE_CATALOG_SCHEMA_VERSIONS = frozenset({2, INDEX_SCHEMA_VERSION})
SEARCH_CANDIDATES = 50
RELATIONSHIP_GRAPH_SCHEMA_VERSION = 1
RELATIONSHIP_GRAPH_MAX_DEPTH = 2
RELATIONSHIP_GRAPH_MIN_TRANSITIVE_CONFIDENCE = 0.7
IGNORE_MARKER = ".no-project-context"
GIT_STORE_MANIFEST = "project-context-store.json"
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
ALL_PROJECTS_HINT = re.compile(
    r"\b((all|every) (projects|repositories|repos)|any (project|repository|repo)|"
    r"across (the )?(workspace|projects|repositories|repos)|cross[- ]project|"
    r"workspace[- ]wide)\b",
    re.IGNORECASE,
)
RELATIONSHIP_PATTERNS = (
    (
        "depends_on",
        re.compile(
            r"\b(depends? on|dependency|dependencies|requires?)\b",
            re.IGNORECASE,
        ),
        0.95,
    ),
    (
        "integrates_with",
        re.compile(
            r"\b(integrat(?:e|es|ed|ing|ion|ions)|calls?|"
            r"connect(?:s|ed|ing)?|routes?|proxies?|forwards?|talks? to)\b",
            re.IGNORECASE,
        ),
        0.9,
    ),
    (
        "owns",
        re.compile(r"\b(owns?|owner|ownership|maintains?|manages?)\b", re.IGNORECASE),
        0.9,
    ),
    (
        "produces",
        re.compile(r"\b(produces?|publishes?|emits?|provides?)\b", re.IGNORECASE),
        0.85,
    ),
    (
        "consumes",
        re.compile(r"\b(consumes?|subscribes?|reads?|uses?)\b", re.IGNORECASE),
        0.85,
    ),
)
DISTINCTIVE_QUERY_TOKEN = re.compile(r"[\w-]{5,}", re.UNICODE)
STRONG_QUERY_STOP_TOKENS = frozenset(
    {
        "about",
        "behavior",
        "context",
        "explain",
        "integration",
        "project",
        "repository",
    }
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


@dataclass(frozen=True)
class ProjectNode:
    project: str
    project_path: str


@dataclass(frozen=True)
class RelationshipEvidence:
    record_id: str
    record_kind: str
    record_label: str
    source_path: str


@dataclass(frozen=True)
class RelationshipEdge:
    source_project: str
    source_project_path: str
    target_project: str
    target_project_path: str
    relation: str
    confidence: float
    evidence: tuple[RelationshipEvidence, ...]


@dataclass(frozen=True)
class RetrievalProject:
    project: str
    project_path: str
    reason: str
    distance: int
    confidence: float


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


def format_diagnostics(failures: Sequence[str], event: str) -> tuple[str, ...]:
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
            if is_git_store_repository(project):
                names[:] = []
                continue
            discovered.setdefault(project, root)
    return tuple(sorted(discovered.items(), key=lambda item: str(item[0]).casefold()))


def context_source(path: Path, root: Path) -> ContextSource:
    return ContextSource(
        source_path=str(path),
        project_path=str(path.parents[2]),
        workspace_root=str(root),
    )


def is_git_store_repository(project: Path) -> bool:
    manifest = project / GIT_STORE_MANIFEST
    return manifest.is_file() and not manifest.is_symlink()


def discovered_sources(
    roots: Sequence[Path],
    external_sources: Sequence[ContextSource] = (),
) -> tuple[ContextSource, ...]:
    sources = {
        source.project_path: source
        for source in (
            context_source(path, root) for path, root in discover_context_files(roots)
        )
        if not is_git_store_repository(Path(source.project_path))
    }
    sources.update((source.project_path, source) for source in external_sources)
    return tuple(
        sorted(sources.values(), key=lambda source: source.project_path.casefold())
    )


def discover_context_candidates(
    roots: Sequence[Path],
    external_sources: Sequence[ContextSource] = (),
) -> tuple[tuple[ContextSource, ...], tuple[ContextSource, ...]]:
    sources = {
        source.project_path: source
        for source in discovered_sources(roots, external_sources)
    }
    requiring_initialization: dict[Path, ContextSource] = {}
    for project, root in discover_primary_git_repositories(roots):
        path = project / "docs/context/context.json"
        if str(project) in sources:
            continue
        directories = (path.parent.parent, path.parent)
        if any(
            directory.is_symlink() or (directory.exists() and not directory.is_dir())
            for directory in directories
        ):
            continue
        if path.exists() or path.is_symlink():
            continue
        candidate = context_source(path, root)
        sources[str(project)] = candidate
        requiring_initialization[path] = candidate
    source_order = sorted(sources, key=str.casefold)
    initialization_order = sorted(
        requiring_initialization,
        key=lambda path: str(path).casefold(),
    )
    return (
        tuple(sources[project] for project in source_order),
        tuple(requiring_initialization[path] for path in initialization_order),
    )


def source_payload(source: ContextSource) -> dict[str, str]:
    return asdict(source)


def parse_external_sources(
    values: Sequence[str],
    roots: Sequence[Path],
) -> tuple[ContextSource, ...]:
    allowed_roots = {str(root.expanduser().resolve()) for root in roots}
    sources: dict[str, ContextSource] = {}
    for raw in values:
        try:
            payload = mapping(json.loads(raw), "external source")
        except json.JSONDecodeError as exc:
            raise GlobalContextError("external source must be valid JSON") from exc
        source_path = Path(one_line(payload.get("source_path"))).expanduser()
        project_path = Path(one_line(payload.get("project_path"))).expanduser()
        workspace_root = Path(one_line(payload.get("workspace_root"))).expanduser()
        if not source_path.is_absolute() or not project_path.is_absolute():
            raise GlobalContextError(
                "external source and project paths must be absolute"
            )
        workspace = workspace_root.resolve()
        project = project_path.resolve()
        if source_path.is_symlink():
            raise GlobalContextError("external canonical source must not be a symlink")
        source = source_path.resolve()
        if str(workspace) not in allowed_roots:
            raise GlobalContextError("external source workspace is not configured")
        try:
            project.relative_to(workspace)
        except ValueError:
            raise GlobalContextError(
                "external source project is outside its workspace"
            ) from None
        if not source.is_file():
            raise GlobalContextError(
                f"external canonical source is unavailable: {source}"
            )
        sources[str(project)] = ContextSource(
            source_path=str(source),
            project_path=str(project),
            workspace_root=str(workspace),
        )
    return tuple(
        sorted(sources.values(), key=lambda source: source.project_path.casefold())
    )


def snapshot_fingerprint(
    sources: Sequence[ContextSource], roots: Sequence[Path] | None = None
) -> str:
    workspace_roots = (
        tuple(roots)
        if roots is not None
        else tuple(
            Path(value) for value in {source.workspace_root for source in sources}
        )
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
        elif kind == "domain":
            if not raw_value or raw_value == "self":
                raise GlobalContextError(
                    "domain applicability requires an explicit selector"
                )
            resolved.add((kind, raw_value))
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


def records_from_context(
    path: Path,
    workspace_root: Path,
    *,
    project_path: str | None = None,
    project_name: str | None = None,
) -> tuple[ContextRecord, ...]:
    data = mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
    repo = path.parents[2].resolve() if project_path is None else Path(project_path)
    project = project_name or repo.name
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
            key = "\x1f".join((str(path), kind, label.casefold()))
            raw_id = one_line(record.get("id"))
            try:
                record_id = str(uuid.UUID(raw_id)) if raw_id else ""
            except ValueError:
                record_id = ""
            content_hash = hashlib.sha256(
                json.dumps(
                    {"text": text, "applicability": applicability},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            records.append(
                ContextRecord(
                    id=record_id or str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
                    key=key,
                    project=project,
                    project_path=project_path
                    if project_path is not None
                    else str(repo),
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


def discover_scope_context_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    resolved_root = root.resolve()
    paths: list[Path] = []
    for candidate in root.rglob("context.json"):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        if resolved_root not in resolved.parents:
            continue
        paths.append(resolved)
    return tuple(sorted(paths, key=str))


def load_scope_records(
    root: Path,
) -> tuple[tuple[ContextRecord, ...], tuple[str, ...], frozenset[str]]:
    records: list[ContextRecord] = []
    failures: list[str] = []
    failed: set[str] = set()
    for path in discover_scope_context_files(root):
        try:
            data = mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
            metadata = mapping(data.get("scope_store"), f"{path}:scope_store")
            applicability = resolved_applicability(
                metadata.get("applicability"),
                project_path=Path("."),
                workspace_root=Path("."),
            )
            default_applicability = resolved_applicability(
                data.get("default_applicability"),
                project_path=Path("."),
                workspace_root=Path("."),
            )
            if default_applicability != applicability:
                raise GlobalContextError(
                    "scope default applicability does not match its canonical store"
                )
            label = ",".join(f"{kind}:{selector}" for kind, selector in applicability)
            scope_records = records_from_context(
                path,
                Path("."),
                project_path="",
                project_name=label,
            )
            if any(record.applicability != applicability for record in scope_records):
                raise GlobalContextError(
                    "scope record applicability does not match its canonical store"
                )
            records.extend(scope_records)
        except (GlobalContextError, OSError, json.JSONDecodeError) as exc:
            failures.append(f"{path}: {exc}")
            failed.add(str(path))
    return sorted_records(records), tuple(failures), frozenset(failed)


def catalog_sources(
    catalog: dict[str, Any],
    roots: Sequence[Path],
    external_sources: Sequence[ContextSource] = (),
) -> tuple[ContextSource, ...]:
    allowed_roots = {
        str(path.expanduser().resolve()): path.expanduser().resolve() for path in roots
    }
    raw_sources = catalog.get("sources")
    candidates = (
        raw_sources if isinstance(raw_sources, list) else catalog.get("records", [])
    )
    external_by_project = {source.project_path: source for source in external_sources}
    sources: dict[str, ContextSource] = {}
    for raw_source in candidates:
        if not isinstance(raw_source, dict):
            continue
        raw_path = raw_source.get("source_path")
        raw_project = str(raw_source.get("project_path", ""))
        replacement = external_by_project.get(raw_project)
        if replacement is not None:
            sources[replacement.project_path] = replacement
            continue
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
        sources[str(project)] = ContextSource(
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
            records.extend(
                records_from_context(
                    path,
                    Path(source.workspace_root),
                    project_path=source.project_path,
                    project_name=project.name,
                )
            )
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


def project_nodes_from_records(
    records: Sequence[ContextRecord],
) -> tuple[ProjectNode, ...]:
    nodes = {
        record.project_path: ProjectNode(record.project, record.project_path)
        for record in records
        if record.project_path
    }
    return tuple(
        nodes[path] for path in sorted(nodes, key=lambda value: value.casefold())
    )


def project_nodes_from_sources(
    sources: Sequence[ContextSource],
) -> tuple[ProjectNode, ...]:
    nodes = {
        source.project_path: ProjectNode(
            Path(source.project_path).name,
            source.project_path,
        )
        for source in sources
        if source.project_path
    }
    return tuple(
        nodes[path] for path in sorted(nodes, key=lambda value: value.casefold())
    )


def relationship_signal(
    text: str,
    start: int,
    end: int,
) -> tuple[str, float]:
    window = text[max(0, start - 120) : min(len(text), end + 120)]
    for relation, pattern, confidence in RELATIONSHIP_PATTERNS:
        if pattern.search(window):
            return relation, confidence
    return "references", 0.6


def project_mention_matcher(
    nodes: Sequence[ProjectNode],
) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    name_counts: dict[str, int] = {}
    for node in nodes:
        key = node.project.casefold()
        name_counts[key] = name_counts.get(key, 0) + 1
    aliases = {
        (
            node.project.casefold()
            if name_counts[node.project.casefold()] == 1
            else node.project_path.casefold()
        ): node.project_path
        for node in nodes
    }
    if not aliases:
        return None, {}
    alternatives = "|".join(
        re.escape(alias)
        for alias in sorted(aliases, key=lambda value: (-len(value), value))
    )
    return (
        re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE),
        aliases,
    )


def record_target_mentions(
    record: ContextRecord,
    matcher: re.Pattern[str] | None,
    aliases: dict[str, str],
) -> dict[str, tuple[tuple[int, int], ...]]:
    if matcher is None:
        return {}
    mentions: dict[str, list[tuple[int, int]]] = {}
    for match in matcher.finditer(record.text):
        target_path = aliases.get(match.group(0).casefold())
        if target_path and target_path != record.project_path:
            mentions.setdefault(target_path, []).append((match.start(), match.end()))
    return {
        target_path: tuple(positions) for target_path, positions in mentions.items()
    }


def relationship_edge_payload(edge: RelationshipEdge) -> dict[str, Any]:
    return {
        "source_project": edge.source_project,
        "source_project_path": edge.source_project_path,
        "target_project": edge.target_project,
        "target_project_path": edge.target_project_path,
        "relation": edge.relation,
        "confidence": edge.confidence,
        "evidence": [asdict(item) for item in edge.evidence],
    }


def derive_relationship_graph(
    records: Sequence[ContextRecord],
) -> dict[str, Any]:
    nodes = project_nodes_from_records(records)
    matcher, aliases = project_mention_matcher(nodes)
    aggregated: dict[
        tuple[str, str, str],
        tuple[ProjectNode, ProjectNode, float, dict[str, RelationshipEvidence]],
    ] = {}
    nodes_by_path = {node.project_path: node for node in nodes}
    for record in records:
        source = nodes_by_path.get(record.project_path)
        if source is None:
            continue
        for target_path, mentions in record_target_mentions(
            record,
            matcher,
            aliases,
        ).items():
            target = nodes_by_path[target_path]
            relation, confidence = max(
                (
                    relationship_signal(record.text, start, end)
                    for start, end in mentions
                ),
                key=lambda item: item[1],
            )
            key = (source.project_path, target.project_path, relation)
            evidence = RelationshipEvidence(
                record.id,
                record.kind,
                record.label,
                record.source_path,
            )
            existing = aggregated.get(key)
            evidence_by_id = dict(existing[3]) if existing is not None else {}
            evidence_by_id[record.id] = evidence
            aggregated[key] = (
                source,
                target,
                max(confidence, existing[2] if existing is not None else 0.0),
                evidence_by_id,
            )
    edges = tuple(
        RelationshipEdge(
            source.project,
            source.project_path,
            target.project,
            target.project_path,
            relation,
            confidence,
            tuple(evidence_by_id[record_id] for record_id in sorted(evidence_by_id)),
        )
        for (source_path, target_path, relation), (
            source,
            target,
            confidence,
            evidence_by_id,
        ) in sorted(aggregated.items())
    )
    return {
        "schema_version": RELATIONSHIP_GRAPH_SCHEMA_VERSION,
        "edges": [relationship_edge_payload(edge) for edge in edges],
    }


def relationships_from_graph(
    graph: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    relationships: dict[str, set[str]] = {}
    for raw_edge in graph["edges"]:
        edge = mapping(raw_edge, "relationship edge")
        source_path = one_line(edge.get("source_project_path"))
        source_name = one_line(edge.get("source_project"))
        target_path = one_line(edge.get("target_project_path"))
        target_name = one_line(edge.get("target_project"))
        relationships.setdefault(source_path, set()).add(target_name)
        relationships.setdefault(target_path, set()).add(source_name)
    return {
        project: tuple(sorted(related, key=str.casefold))
        for project, related in sorted(relationships.items())
    }


def derive_relationships(
    records: Sequence[ContextRecord],
) -> dict[str, tuple[str, ...]]:
    return relationships_from_graph(derive_relationship_graph(records))


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
                if record.project_path
            }.values()
        )
    )
    nodes = project_nodes_from_sources(enrolled)
    graph = derive_relationship_graph(records)
    return {
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "dense_model": DENSE_MODEL,
        "dense_model_revision": DENSE_MODEL_REVISION,
        "sparse_model": SPARSE_MODEL,
        "sparse_model_revision": SPARSE_MODEL_REVISION,
        "enrollment_policy": "snapshot",
        "project_nodes": [asdict(node) for node in nodes],
        "projects": sorted({node.project for node in nodes}, key=str.casefold),
        "project_count": len(enrolled),
        "relationships": relationships_from_graph(graph),
        "relationship_graph": graph,
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

    def search(
        self,
        query: str,
        limit: int,
        project_paths: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        models = self.models_api
        dense = next(iter(self.dense.query_embed([query])))
        sparse = next(iter(self.sparse.query_embed([query])))
        query_filter = (
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="project_path",
                        match=models.MatchAny(any=sorted(set(project_paths))),
                    )
                ]
            )
            if project_paths
            else None
        )
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
            query_filter=query_filter,
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
    schema_version = catalog.get("index_schema_version")
    if type(schema_version) is not int:
        return False
    if schema_version in SOURCE_CATALOG_SCHEMA_VERSIONS:
        return isinstance(catalog.get("sources"), list)
    return schema_version == LEGACY_RECORD_CATALOG_SCHEMA_VERSION and isinstance(
        catalog.get("records"), list
    )


def sync_index(
    roots: Sequence[Path],
    index_dir: Path,
    catalog_path: Path,
    model_cache: Path,
    scope_roots: Sequence[Path] | Path | None = None,
    *,
    external_sources: Sequence[ContextSource] = (),
    enroll_new: bool = False,
    approved_snapshot: str | None = None,
) -> tuple[int, int, int, tuple[str, ...]]:
    with exclusive_lock(index_dir.parent / "index.lock"):
        old_catalog = read_catalog(catalog_path)
        if enroll_new:
            enrolled = discovered_sources(roots, external_sources)
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
            enrolled = catalog_sources(old_catalog, roots, external_sources)
        records, active_sources, failures, failed = load_source_records(enrolled)
        normalized_scope_roots = (
            (scope_roots,)
            if isinstance(scope_roots, Path)
            else tuple(scope_roots or ())
        )
        scope_records: tuple[ContextRecord, ...] = ()
        scope_failures: tuple[str, ...] = ()
        scope_failed: frozenset[str] = frozenset()
        for scope_root in normalized_scope_roots:
            loaded, current_failures, current_failed = load_scope_records(scope_root)
            scope_records = sorted_records((*scope_records, *loaded))
            scope_failures = (*scope_failures, *current_failures)
            scope_failed = frozenset((*scope_failed, *current_failed))
        failed_sources = frozenset((*failed, *scope_failed))
        records = sorted_records(
            (
                *records,
                *scope_records,
                *preserved_records(old_catalog, failed_sources),
            )
        )
        failures = (*failures, *scope_failures)
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


def catalog_project_nodes(catalog: dict[str, Any]) -> tuple[ProjectNode, ...]:
    nodes: dict[str, ProjectNode] = {}
    raw_nodes = catalog.get("project_nodes", [])
    if isinstance(raw_nodes, list):
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                continue
            project = one_line(raw_node.get("project"))
            project_path = one_line(raw_node.get("project_path"))
            if project and project_path:
                nodes[project_path] = ProjectNode(project, project_path)
    if not nodes:
        raw_sources = catalog.get("sources", [])
        if isinstance(raw_sources, list):
            for raw_source in raw_sources:
                if not isinstance(raw_source, dict):
                    continue
                project_path = one_line(raw_source.get("project_path"))
                if project_path:
                    nodes[project_path] = ProjectNode(
                        Path(project_path).name,
                        project_path,
                    )
    return tuple(
        nodes[path] for path in sorted(nodes, key=lambda value: value.casefold())
    )


def relationship_evidence_from_payload(
    raw_evidence: Any,
) -> tuple[RelationshipEvidence, ...]:
    if not isinstance(raw_evidence, list):
        return ()
    evidence: list[RelationshipEvidence] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            continue
        values = tuple(
            one_line(raw_item.get(field))
            for field in ("record_id", "record_kind", "record_label", "source_path")
        )
        if all(values):
            evidence.append(RelationshipEvidence(*values))
    return tuple(sorted(evidence, key=lambda item: item.record_id))


def relationship_edge_from_payload(raw_edge: Any) -> RelationshipEdge | None:
    if not isinstance(raw_edge, dict):
        return None
    values = tuple(
        one_line(raw_edge.get(field))
        for field in (
            "source_project",
            "source_project_path",
            "target_project",
            "target_project_path",
            "relation",
        )
    )
    raw_confidence = raw_edge.get("confidence")
    if not all(values) or not isinstance(raw_confidence, (int, float)):
        return None
    confidence = float(raw_confidence)
    if not 0.0 <= confidence <= 1.0:
        return None
    return RelationshipEdge(
        *values,
        confidence,
        relationship_evidence_from_payload(raw_edge.get("evidence")),
    )


def catalog_relationship_edges(
    catalog: dict[str, Any],
    nodes: Sequence[ProjectNode],
) -> tuple[RelationshipEdge, ...]:
    raw_graph = catalog.get("relationship_graph")
    if (
        isinstance(raw_graph, dict)
        and raw_graph.get("schema_version") == RELATIONSHIP_GRAPH_SCHEMA_VERSION
    ):
        raw_edges = raw_graph.get("edges", [])
        if isinstance(raw_edges, list):
            edges = tuple(
                edge
                for edge in (
                    relationship_edge_from_payload(raw_edge) for raw_edge in raw_edges
                )
                if edge is not None
            )
            if edges or not raw_edges:
                return tuple(
                    sorted(
                        edges,
                        key=lambda edge: (
                            edge.source_project_path.casefold(),
                            edge.target_project_path.casefold(),
                            edge.relation,
                        ),
                    )
                )
    nodes_by_name: dict[str, list[ProjectNode]] = {}
    for node in nodes:
        nodes_by_name.setdefault(node.project.casefold(), []).append(node)
    raw_relationships = catalog.get("relationships", {})
    if not isinstance(raw_relationships, dict):
        return ()
    fallback: list[RelationshipEdge] = []
    for source_path, raw_targets in raw_relationships.items():
        source = next(
            (node for node in nodes if node.project_path == str(source_path)),
            None,
        )
        if source is None or not isinstance(raw_targets, list):
            continue
        for raw_target in raw_targets:
            matches = nodes_by_name.get(one_line(raw_target).casefold(), [])
            if len(matches) != 1:
                continue
            target = matches[0]
            fallback.append(
                RelationshipEdge(
                    source.project,
                    source.project_path,
                    target.project,
                    target.project_path,
                    "references",
                    0.5,
                    (),
                )
            )
    return tuple(
        sorted(
            fallback,
            key=lambda edge: (
                edge.source_project_path.casefold(),
                edge.target_project_path.casefold(),
            ),
        )
    )


def mentioned_projects(query: str, projects: Sequence[str]) -> frozenset[str]:
    haystack = query.casefold()
    return frozenset(
        project
        for project in projects
        if re.search(
            rf"(?<![\w-]){re.escape(project.casefold())}(?![\w-])",
            haystack,
        )
    )


def mentioned_project_paths(
    query: str,
    nodes: Sequence[ProjectNode],
) -> frozenset[str]:
    mentioned_names = mentioned_projects(query, tuple(node.project for node in nodes))
    normalized_query = query.casefold()
    return frozenset(
        node.project_path
        for node in nodes
        if node.project in mentioned_names
        or node.project_path.casefold() in normalized_query
    )


def graph_reach(
    seeds: frozenset[str],
    edges: Sequence[RelationshipEdge],
) -> dict[str, tuple[int, float]]:
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source_project_path, []).append(
            (edge.target_project_path, edge.confidence)
        )
        adjacency.setdefault(edge.target_project_path, []).append(
            (edge.source_project_path, edge.confidence)
        )
    reached = {seed: (0, 1.0) for seed in seeds}
    frontier = frozenset(seeds)
    for distance in range(1, RELATIONSHIP_GRAPH_MAX_DEPTH + 1):
        next_frontier: set[str] = set()
        for project_path in sorted(frontier, key=str.casefold):
            source_confidence = reached[project_path][1]
            for target_path, edge_confidence in adjacency.get(project_path, []):
                confidence = round(source_confidence * edge_confidence, 6)
                if (
                    distance > 1
                    and confidence < RELATIONSHIP_GRAPH_MIN_TRANSITIVE_CONFIDENCE
                ):
                    continue
                existing = reached.get(target_path)
                if existing is not None and (
                    existing[0] < distance
                    or (existing[0] == distance and existing[1] >= confidence)
                ):
                    continue
                reached[target_path] = (distance, confidence)
                next_frontier.add(target_path)
        frontier = frozenset(next_frontier)
        if not frontier:
            break
    return reached


def query_requests_all_projects(query: str) -> bool:
    return ALL_PROJECTS_HINT.search(query) is not None


def retrieval_projects(
    query: str,
    current_repo: Path,
    catalog: dict[str, Any],
) -> tuple[RetrievalProject, ...]:
    current_path = str(current_repo.expanduser().resolve())
    nodes = catalog_project_nodes(catalog)
    nodes_by_path = {node.project_path: node for node in nodes}
    current_node = nodes_by_path.get(
        current_path,
        ProjectNode(current_repo.name, current_path),
    )
    explicit_paths = mentioned_project_paths(query, nodes)
    seeds = frozenset((current_path, *explicit_paths))
    reached = graph_reach(seeds, catalog_relationship_edges(catalog, nodes))
    projects: dict[str, RetrievalProject] = {
        current_path: RetrievalProject(
            current_node.project,
            current_path,
            "current",
            0,
            1.0,
        )
    }
    for project_path in explicit_paths:
        node = nodes_by_path[project_path]
        projects[project_path] = RetrievalProject(
            node.project,
            project_path,
            "explicit",
            0,
            1.0,
        )
    for project_path, (distance, confidence) in reached.items():
        if project_path in projects or distance == 0:
            continue
        node = nodes_by_path.get(project_path)
        if node is not None:
            projects[project_path] = RetrievalProject(
                node.project,
                project_path,
                "related",
                distance,
                confidence,
            )
    if query_requests_all_projects(query):
        for node in nodes:
            projects.setdefault(
                node.project_path,
                RetrievalProject(
                    node.project,
                    node.project_path,
                    "global",
                    RELATIONSHIP_GRAPH_MAX_DEPTH + 1,
                    0.0,
                ),
            )
    reason_order = {"current": 0, "explicit": 1, "related": 2, "global": 3}
    return tuple(
        sorted(
            projects.values(),
            key=lambda item: (
                reason_order[item.reason],
                item.distance,
                item.project.casefold(),
                item.project_path.casefold(),
            ),
        )
    )


def hit_project_path(hit: dict[str, Any]) -> str:
    project_path = one_line(hit.get("project_path"))
    if project_path:
        return project_path
    raw = hit.get("applicability")
    if not isinstance(raw, list):
        return ""
    project_selectors = tuple(
        one_line(item.get("selector"))
        for item in raw
        if isinstance(item, dict)
        and one_line(item.get("kind")).casefold() == "project"
        and one_line(item.get("selector"))
    )
    return project_selectors[0] if len(project_selectors) == 1 else ""


def hit_is_retrievable(
    hit: dict[str, Any],
    active: frozenset[tuple[str, str]],
    project_paths: frozenset[str],
) -> bool:
    raw = hit.get("applicability")
    if not isinstance(raw, list) or not raw:
        return False
    selectors: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return False
        kind = one_line(item.get("kind")).casefold()
        selector = one_line(item.get("selector", "*"))
        if not kind or not selector:
            return False
        selectors.append((kind, selector))
    return all(
        selector in project_paths if kind == "project" else (kind, selector) in active
        for kind, selector in selectors
    )


def strong_query_match(
    hit: dict[str, Any],
    query: str,
    best_score: float,
) -> bool:
    normalized_query = one_line(query).casefold()
    label = one_line(hit.get("label")).casefold()
    if len(label) >= 5 and label in normalized_query:
        return True
    score = float(hit.get("score", 0.0))
    if best_score <= 0.0 or score < best_score * 0.9:
        return False
    tokens = frozenset(
        token.casefold()
        for token in DISTINCTIVE_QUERY_TOKEN.findall(normalized_query)
        if token.casefold() not in STRONG_QUERY_STOP_TOKENS
    )
    haystack = " ".join((label, one_line(hit.get("summary")).casefold()))
    return any(token in haystack for token in tokens)


def merge_hits(
    *groups: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for hit in group:
            identity = one_line(hit.get("id")) or "\x1f".join(
                (
                    one_line(hit.get("source_path")),
                    one_line(hit.get("kind")),
                    one_line(hit.get("label")).casefold(),
                )
            )
            existing = merged.get(identity)
            if existing is None or float(hit.get("score", 0.0)) > float(
                existing.get("score", 0.0)
            ):
                merged[identity] = hit
    return tuple(merged.values())


def retrieval_project_quota(
    project: RetrievalProject | None,
    limit: int,
    has_cross_project_candidates: bool,
) -> int:
    if project is None:
        return max(1, (limit + 3) // 4)
    if project.reason == "explicit":
        return limit
    if project.reason == "current":
        return max(1, (limit + 1) // 2) if has_cross_project_candidates else limit
    if project.reason == "related" and project.distance == 1:
        return max(1, (limit + 2) // 3)
    if project.reason == "related":
        return max(1, (limit + 3) // 4)
    if project.reason == "global":
        return max(1, (limit + 3) // 4)
    return 1


def rerank_hits(
    hits: Sequence[dict[str, Any]],
    query: str,
    projects: Sequence[RetrievalProject],
    limit: int,
) -> tuple[dict[str, Any], ...]:
    projects_by_path = {project.project_path: project for project in projects}
    normalized_query = one_line(query).casefold()

    def score(hit: dict[str, Any]) -> tuple[float, str, str]:
        value = float(hit.get("score", 0.0))
        project_path = hit_project_path(hit)
        project = projects_by_path.get(project_path)
        if project is not None and project.reason == "explicit":
            value += 1.0
        elif project is not None and project.reason == "current":
            value += 0.4
        elif project is not None and project.reason == "related":
            value += (0.35 / project.distance) * project.confidence
        label = one_line(hit.get("label"))
        if label and label.casefold() in normalized_query:
            value += 0.5
        return (
            -value,
            one_line(hit.get("project")).casefold(),
            label.casefold(),
        )

    ranked = tuple(sorted(hits, key=score))
    has_cross_project_candidates = any(
        project.reason != "current" for project in projects
    )
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for hit in ranked:
        project_path = hit_project_path(hit)
        bucket = project_path or one_line(hit.get("source_path"))
        project = projects_by_path.get(project_path)
        quota = retrieval_project_quota(
            project,
            limit,
            has_cross_project_candidates,
        )
        if counts.get(bucket, 0) >= quota:
            deferred.append(hit)
            continue
        selected.append(hit)
        counts[bucket] = counts.get(bucket, 0) + 1
        if len(selected) == limit:
            return tuple(selected)
    return tuple((*selected, *deferred)[:limit])


def parse_active_applicability(values: Sequence[str]) -> frozenset[tuple[str, str]]:
    active: set[tuple[str, str]] = set()
    for value in values:
        kind, separator, selector = value.partition(":")
        if not separator or not kind or not selector:
            raise GlobalContextError(f"invalid active applicability {value!r}")
        active.add((kind.casefold(), selector))
    return frozenset(active)


def hit_is_applicable(
    hit: dict[str, Any],
    active: frozenset[tuple[str, str]],
) -> bool:
    raw = hit.get("applicability")
    if not isinstance(raw, list) or not raw:
        return False
    selectors = {
        (
            one_line(item.get("kind")).casefold(),
            one_line(item.get("selector", "*")),
        )
        for item in raw
        if isinstance(item, dict)
    }
    return len(selectors) == len(raw) and selectors.issubset(active)


def search_index(
    query: str,
    limit: int,
    current_repo: Path,
    index_dir: Path,
    catalog_path: Path,
    model_cache: Path,
    active_applicability: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[dict[str, Any], ...]:
    catalog = read_catalog(catalog_path)
    if not catalog_is_compatible(catalog):
        raise GlobalContextError("global index is not initialized")
    projects = retrieval_projects(query, current_repo, catalog)
    project_paths = frozenset(project.project_path for project in projects)
    filtered_paths = tuple(sorted((*project_paths, ""), key=str.casefold))
    candidate_limit = max(limit, SEARCH_CANDIDATES)
    with exclusive_lock(index_dir.parent / "index.lock"):
        index = QdrantIndex(index_dir, model_cache)
        try:
            candidate_hits = index.search(query, candidate_limit, filtered_paths)
            global_hits = (
                ()
                if query_requests_all_projects(query)
                else index.search(query, candidate_limit)
            )
        finally:
            index.close()
    hits = merge_hits(candidate_hits, global_hits)
    best_score = max((float(hit.get("score", 0.0)) for hit in hits), default=0.0)
    active = active_applicability or frozenset(
        {("project", str(current_repo.expanduser().resolve()))}
    )
    eligible: list[dict[str, Any]] = []
    strong_projects: dict[str, RetrievalProject] = {}
    nodes_by_path = {node.project_path: node for node in catalog_project_nodes(catalog)}
    for hit in hits:
        if hit_is_retrievable(hit, active, project_paths):
            eligible.append(hit)
            continue
        project_path = hit_project_path(hit)
        if (
            not project_path
            or not strong_query_match(hit, query, best_score)
            or not hit_is_retrievable(hit, active, project_paths | {project_path})
        ):
            continue
        eligible.append(hit)
        node = nodes_by_path.get(
            project_path,
            ProjectNode(
                one_line(hit.get("project")) or Path(project_path).name,
                project_path,
            ),
        )
        strong_projects[project_path] = RetrievalProject(
            node.project,
            node.project_path,
            "strong",
            RELATIONSHIP_GRAPH_MAX_DEPTH + 1,
            0.0,
        )
    return rerank_hits(
        eligible,
        query,
        (*projects, *strong_projects.values()),
        limit,
    )


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
    discover.add_argument("--external-source", action="append", default=[])

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
        subparser.add_argument("--scope-root", type=Path, action="append", default=[])
        subparser.add_argument("--external-source", action="append", default=[])

    sync = subparsers.add_parser("sync")
    add_index_arguments(sync)
    sync.add_argument("--enroll-new", action="store_true")
    sync.add_argument("--approved-snapshot")
    search = subparsers.add_parser("search")
    add_index_arguments(search)
    search.add_argument("--current-repo", type=Path, required=True)
    search.add_argument("--active-applicability", action="append", default=[])
    search.add_argument("--query", action="append", required=True)
    search.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "discover":
        roots = tuple(path.expanduser().resolve() for path in args.workspace_root)
        external_sources = parse_external_sources(args.external_source, roots)
        sources, requiring_initialization = discover_context_candidates(
            roots,
            external_sources,
        )
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
    external_sources = parse_external_sources(args.external_source, roots)
    project_count, record_count, changed_count, failures = sync_index(
        roots,
        args.index_dir,
        args.catalog,
        args.model_cache,
        args.scope_root,
        external_sources=external_sources,
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
        parse_active_applicability(args.active_applicability),
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
