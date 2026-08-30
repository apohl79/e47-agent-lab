#!/usr/bin/env python3
"""Maintain canonical repository, Git-backed, and private XDG context stores."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 4
CONTEXT_DIR = Path("docs/context")
CONTEXT_FILE = CONTEXT_DIR / "context.json"
GIT_EXCLUDE_ENTRY = "docs/context/"
IGNORE_MARKER = ".no-project-context"
CONTEXT_VISIBILITIES = {"git-store", "local", "versioned"}
STORAGE_RUNTIME_MODES = {"git-store", "local"}
LOCAL_CONTEXT_VISIBILITIES = {"local", "versioned"}
APPLICABILITY_KINDS = {
    "domain",
    "machine",
    "project",
    "universal",
    "user",
}
LEGACY_APPLICABILITY_KINDS = {"workspace"}
SHAREABLE_APPLICABILITY_KINDS = frozenset({"domain", "universal"})
DEFAULT_APPLICABILITY = [{"kind": "project", "selector": "self"}]
GLOBAL_CONFIG_SCHEMA_VERSION = 5
GLOBAL_CONFIG_FILE = "config.json"
GLOBAL_RUNTIME_FILE = "runtime.json"
GLOBAL_CATALOG_FILE = "catalog.json"
GLOBAL_INDEX_DIR = "qdrant"
GLOBAL_MODEL_DIR = "models"
GLOBAL_CONTEXTS_DIR = "contexts"
GIT_STORE_MANIFEST_FILE = "project-context-store.json"
GIT_STORE_SCHEMA_VERSION = 1
GIT_STORE_PROJECTS_DIR = "projects"
GIT_STORE_SCOPES_DIR = "scopes"
GIT_STORE_BRANCH = "main"
GIT_STORE_MANAGED_PATHS = (
    GIT_STORE_MANIFEST_FILE,
    ":(glob)projects/*/context.json",
    ":(glob)scopes/**/context.json",
)
GIT_STORE_STAGE_PATHS = (
    GIT_STORE_MANIFEST_FILE,
    GIT_STORE_PROJECTS_DIR,
    GIT_STORE_SCOPES_DIR,
)
GIT_STORE_UNMANAGED_PATHS = (
    ".",
    f":(exclude){GIT_STORE_MANIFEST_FILE}",
    ":(exclude,glob)projects/*/context.json",
    ":(exclude,glob)scopes/**/context.json",
)
GIT_STORE_MUTATING_COMMANDS = frozenset(
    {
        "add-component",
        "add-pattern",
        "add-question",
        "add-term",
        "domain-remove",
        "domain-set",
        "git-store-bind",
        "init",
        "move",
        "remove",
        "update",
    }
)
GIT_STORE_LOCK_TIMEOUT_SECONDS = 30
GIT_STORE_NETWORK_TIMEOUT_SECONDS = 30
GLOBAL_INDEX_SCHEMA_VERSION = 3
GLOBAL_RELATIONSHIP_GRAPH_SCHEMA_VERSION = 1
GLOBAL_LEGACY_RECORD_CATALOG_SCHEMA_VERSION = 1
GLOBAL_SOURCE_CATALOG_SCHEMA_VERSIONS = frozenset({2, GLOBAL_INDEX_SCHEMA_VERSION})
GLOBAL_RESULT_PREFIX = "UNTRUSTED_CONTEXT_DATA"
UNTRUSTED_SNAPSHOT_TYPE = "UNTRUSTED_SNAPSHOT_DATA"
UNTRUSTED_DIAGNOSTIC_TYPE = "UNTRUSTED_CONTEXT_DIAGNOSTIC"
SNAPSHOT_SOURCE_FIELDS = ("source_path", "project_path", "workspace_root")
SNAPSHOT_PATH_LIMIT = 4096
DIAGNOSTIC_OUTPUT_LIMIT = 1024
DIAGNOSTIC_COUNT_LIMIT = 20
GLOBAL_KIND_OUTPUT_LIMIT = 32
GLOBAL_LABEL_OUTPUT_LIMIT = 200
GLOBAL_PATH_OUTPUT_LIMIT = 1024
LOCAL_SUMMARY_OUTPUT_LIMIT = 500
SNAPSHOT_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
GLOBAL_ONBOARDING_GUIDANCE = (
    "Global context onboarding required. Before normal project work, proactively use "
    "the Project Context Curator skill, honor the storage runtime status, confirm "
    "workspace roots, preview global-init, request approval for the exact "
    "snapshot, bootstrap every listed missing context with verified non-empty records, "
    "and rerun global-init with the approved token."
)
GLOBAL_ENROLLMENT_REPAIR_GUIDANCE = (
    "Before normal project work, proactively use the Project Context Curator "
    "skill to preview global-enroll for the configured workspace roots, show "
    "the exact snapshot, ask the user to approve that snapshot, and rerun "
    "global-enroll with the approved token."
)

TERM_KINDS = {
    "abbreviation",
    "domain-term",
    "event",
    "api",
    "data-store",
    "other",
}

DOMAIN_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
REMOTE_URL_SCHEMES = ("git+ssh://", "ssh://", "https://", "http://", "git://")
SCP_REMOTE_PATTERN = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")
NORMALIZED_REMOTE_PATTERN = re.compile(r"^[^/\s:@]+(?::\d+)?/\S+$")
SCOPE_DIRECTORY_NAMES = {
    "domain": "domains",
    "machine": "machines",
    "user": "users",
    "workspace": "workspaces",
}
DISCOVERY_SKIPPED_DIRECTORIES = frozenset(
    {".git", ".my", ".venv", "build", "dist", "node_modules", "target"}
)
RECORD_KEYS = {
    "terms": "term",
    "components": "name",
    "patterns": "name",
    "open_questions": "question",
}

REMOVE_TARGETS = {
    "term": ("terms", "term", "term"),
    "terms": ("terms", "term", "term"),
    "component": ("components", "name", "component"),
    "components": ("components", "name", "component"),
    "pattern": ("patterns", "name", "pattern"),
    "patterns": ("patterns", "name", "pattern"),
    "question": ("open_questions", "question", "question"),
    "questions": ("open_questions", "question", "question"),
    "open-question": ("open_questions", "question", "question"),
    "open-questions": ("open_questions", "question", "question"),
}

SearchSpec = tuple[str, str, str, str, tuple[str, ...]]
SearchResult = tuple[int, str, str, str, str, str, tuple[str, ...]]


@dataclass(frozen=True)
class GitUpstream:
    remote: str
    branch: str
    push_url: str


@dataclass(frozen=True)
class DomainMembers:
    projects: tuple[Path, ...] = ()
    remotes: tuple[str, ...] = ()

    def to_config(self) -> dict[str, list[str]]:
        return {
            "projects": [str(project) for project in self.projects],
            "remotes": list(self.remotes),
        }


SEARCH_SPECS: tuple[SearchSpec, ...] = (
    (
        "term",
        "terms",
        "term",
        "definition",
        (
            "term",
            "kind",
            "definition",
            "scope",
            "aliases",
            "notes",
            "applicability",
        ),
    ),
    (
        "component",
        "components",
        "name",
        "responsibility",
        ("name", "responsibility", "paths", "interfaces", "notes", "applicability"),
    ),
    (
        "pattern",
        "patterns",
        "name",
        "summary",
        ("name", "summary", "applies_to", "notes", "applicability"),
    ),
    (
        "question",
        "open_questions",
        "question",
        "context",
        ("question", "status", "context", "answer", "applicability"),
    ),
)

SEARCH_FILES = {
    "term": "docs/context/glossary.md",
    "component": "docs/context/components.md",
    "pattern": "docs/context/architecture.md",
    "question": "docs/context/inbox.md",
}


def safe_display_field(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def untrusted_diagnostic_line(event: Any, detail: Any) -> str:
    return json.dumps(
        {
            "type": UNTRUSTED_DIAGNOSTIC_TYPE,
            "event": safe_display_field(event, 80),
            "detail": safe_display_field(detail, DIAGNOSTIC_OUTPUT_LIMIT),
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def forward_global_diagnostics(raw: str) -> None:
    forwarded = 0
    for line in raw.splitlines():
        if forwarded >= DIAGNOSTIC_COUNT_LIMIT:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("type") != UNTRUSTED_DIAGNOSTIC_TYPE
        ):
            continue
        print(
            untrusted_diagnostic_line(payload.get("event"), payload.get("detail")),
            file=sys.stderr,
        )
        forwarded += 1


def snapshot_source(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    source: dict[str, str] = {}
    for field in SNAPSHOT_SOURCE_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value or len(value) > SNAPSHOT_PATH_LIMIT:
            return None
        source[field] = value
    return source


def snapshot_sources(snapshot: dict[str, Any]) -> tuple[dict[str, str], ...]:
    raw_sources = snapshot.get("sources")
    if not isinstance(raw_sources, list):
        raise SystemExit("Global context discovery returned invalid snapshot sources")
    sources = tuple(snapshot_source(raw) for raw in raw_sources)
    if any(source is None for source in sources):
        raise SystemExit(
            "Global context discovery returned an invalid or overlong snapshot path"
        )
    return tuple(source for source in sources if source is not None)


def snapshot_missing_sources(snapshot: dict[str, Any]) -> tuple[dict[str, str], ...]:
    raw_sources = snapshot.get("missing_sources", [])
    if not isinstance(raw_sources, list):
        raise SystemExit("Global context discovery returned invalid missing sources")
    sources = tuple(snapshot_source(raw) for raw in raw_sources)
    if any(source is None for source in sources):
        raise SystemExit(
            "Global context discovery returned an invalid or overlong missing source path"
        )
    return tuple(source for source in sources if source is not None)


def snapshot_workspace_roots(snapshot: dict[str, Any]) -> tuple[str, ...]:
    raw_roots = snapshot.get("workspace_roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise SystemExit("Global context discovery returned invalid workspace roots")
    if any(
        not isinstance(root, str) or not root or len(root) > SNAPSHOT_PATH_LIMIT
        for root in raw_roots
    ):
        raise SystemExit(
            "Global context discovery returned an invalid or overlong workspace root"
        )
    return tuple(dict.fromkeys(raw_roots))


def snapshot_source_key(source: dict[str, str]) -> tuple[str, ...]:
    return tuple(source[field] for field in SNAPSHOT_SOURCE_FIELDS)


def print_snapshot_source(change: str, source: dict[str, str]) -> None:
    print(
        json.dumps(
            {
                "type": UNTRUSTED_SNAPSHOT_TYPE,
                "change": change,
                "source": source,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def environment_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return (
        Path(value).expanduser().resolve() if value else fallback.expanduser().resolve()
    )


def global_config_dir() -> Path:
    return environment_path(
        "PROJECT_CONTEXT_CURATOR_CONFIG_DIR",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "project-context-curator",
    )


def global_cache_dir() -> Path:
    return environment_path(
        "PROJECT_CONTEXT_CURATOR_CACHE_DIR",
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "project-context-curator",
    )


def global_data_dir() -> Path:
    return environment_path(
        "PROJECT_CONTEXT_CURATOR_DATA_DIR",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "project-context-curator",
    )


def scope_context_root() -> Path:
    return global_data_dir() / GLOBAL_CONTEXTS_DIR


def applicability_is_shareable(value: Any) -> bool:
    pairs = applicability_pairs(value)
    return bool(pairs) and all(
        kind in SHAREABLE_APPLICABILITY_KINDS for kind, _selector in pairs
    )


def validate_domain_id(value: str) -> str:
    domain_id = value.strip().casefold()
    if not DOMAIN_ID_PATTERN.fullmatch(domain_id):
        raise SystemExit(
            "Domain id must contain 1-64 lowercase letters, digits, dots, "
            "underscores, or hyphens."
        )
    return domain_id


def configured_domains(config: dict[str, Any]) -> dict[str, DomainMembers]:
    raw_domains = config.get("domains", {})
    if not isinstance(raw_domains, dict):
        return {}
    domains: dict[str, DomainMembers] = {}
    for raw_name, raw_members in raw_domains.items():
        if isinstance(raw_members, list):
            raw_members = {"projects": raw_members}
        if not isinstance(raw_name, str) or not isinstance(raw_members, dict):
            continue
        try:
            name = validate_domain_id(raw_name)
        except SystemExit:
            continue
        raw_projects = raw_members.get("projects", [])
        raw_remotes = raw_members.get("remotes", [])
        domains[name] = DomainMembers(
            projects=tuple(
                Path(project).expanduser().resolve()
                for project in (raw_projects if isinstance(raw_projects, list) else [])
                if isinstance(project, str) and project
            ),
            remotes=tuple(
                remote
                for remote in (raw_remotes if isinstance(raw_remotes, list) else [])
                if isinstance(remote, str) and normalize_remote_url(remote) == remote
            ),
        )
    return domains


def domains_config(domains: dict[str, DomainMembers]) -> dict[str, Any]:
    return {name: members.to_config() for name, members in domains.items()}


def member_domains(repo: Path, domains: dict[str, DomainMembers]) -> tuple[str, ...]:
    remote_url = (
        repo_remote_url(repo)
        if any(members.remotes for members in domains.values())
        else None
    )
    return tuple(
        sorted(
            domain_id
            for domain_id, members in domains.items()
            if repo in members.projects
            or (remote_url is not None and remote_url in members.remotes)
        )
    )


def containing_workspace(repo: Path, roots: tuple[Path, ...]) -> Path | None:
    matches = tuple(root for root in roots if repo == root or root in repo.parents)
    return max(matches, key=lambda path: len(path.parts), default=None)


def resolve_applicability(
    values: list[dict[str, str]],
    repo: Path,
    *,
    require_domain_membership: bool,
) -> list[dict[str, str]]:
    config = global_config()
    domains = configured_domains(config)
    active_domains: tuple[str, ...] | None = None
    resolved: list[dict[str, str]] = []
    for item in values:
        kind = item["kind"]
        selector = item.get("selector", "")
        if kind == "universal":
            resolved.append({"kind": kind})
            continue
        if kind == "domain":
            if selector == "self":
                raise SystemExit("Domain applicability requires an explicit domain id.")
            domain_id = validate_domain_id(selector)
            if domain_id not in domains:
                raise SystemExit(
                    f"Unknown domain {domain_id!r}. Register its projects with domain-set first."
                )
            if require_domain_membership:
                if active_domains is None:
                    active_domains = member_domains(repo, domains)
                if domain_id not in active_domains:
                    raise SystemExit(
                        f"Repository {repo} is not registered in domain {domain_id!r}."
                    )
            resolved.append({"kind": kind, "selector": domain_id})
            continue
        if kind == "project":
            project = (
                repo if selector == "self" else Path(selector).expanduser().resolve()
            )
            if project != repo:
                raise SystemExit(
                    "Project applicability must target --repo; use that repository as --repo."
                )
            resolved.append({"kind": kind, "selector": str(project)})
            continue
        if kind == "machine":
            resolved.append({"kind": kind})
            continue
        if selector == "self":
            selector = getpass.getuser()
        resolved.append({"kind": kind, "selector": selector})
    return normalize_applicability(resolved, "resolved applicability")


def active_applicability(repo: Path) -> frozenset[tuple[str, str]]:
    config = global_config()
    active = {
        ("project", str(repo)),
        ("project", "self"),
        ("user", getpass.getuser()),
        ("user", "self"),
        ("machine", "*"),
        ("universal", "*"),
    }
    active.update(
        ("domain", domain_id)
        for domain_id in member_domains(repo, configured_domains(config))
    )
    return frozenset(active)


@dataclass(frozen=True)
class ScopeSummary:
    label: str
    path: Path
    data: dict[str, Any]

    def counts(self) -> str:
        return (
            f"{len(self.data['terms'])} terms, "
            f"{len(self.data['components'])} components, "
            f"{len(self.data['patterns'])} patterns"
        )


def active_shared_scope_contexts(repo: Path) -> tuple[ScopeSummary, ...]:
    active = active_applicability(repo)
    summaries: list[ScopeSummary] = []
    for path in scope_context_files():
        try:
            data = load_discovered_scope_context(path)
        except (OSError, SystemExit):
            continue
        applicability = data["default_applicability"]
        if not applicability_is_shareable(applicability):
            continue
        if not applicability_matches(applicability, active):
            continue
        if not any(data[collection] for collection in RECORD_KEYS):
            continue
        summaries.append(ScopeSummary(applicability_text(applicability), path, data))
    return tuple(sorted(summaries, key=lambda summary: summary.label))


def applicability_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    normalized = normalize_applicability(value, "applicability")
    return tuple((item["kind"], item.get("selector", "*")) for item in normalized)


def applicability_matches(
    value: Any,
    active: frozenset[tuple[str, str]],
) -> bool:
    return all(pair in active for pair in applicability_pairs(value))


def scope_selector_key(kind: str, selector: str) -> str:
    if kind == "domain":
        return validate_domain_id(selector)
    label = Path(selector).name if kind == "workspace" else selector
    slug = re.sub(r"[^a-z0-9._-]+", "-", label.casefold()).strip("-") or kind
    digest = hashlib.sha256(selector.encode()).hexdigest()[:12]
    return f"{slug[:48]}-{digest}"


def scope_context_path(applicability: list[dict[str, str]]) -> Path:
    pairs = applicability_pairs(applicability)
    git_root = configured_git_store_root()
    root = (
        git_root / GIT_STORE_SCOPES_DIR
        if git_root is not None and applicability_is_shareable(applicability)
        else scope_context_root()
    )
    if len(pairs) > 1:
        digest = hashlib.sha256(
            json.dumps(pairs, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        return root / "composite" / digest / "context.json"
    kind, selector = pairs[0]
    if kind == "universal":
        return root / "universal" / "context.json"
    if kind == "machine":
        return root / SCOPE_DIRECTORY_NAMES[kind] / "context.json"
    directory = SCOPE_DIRECTORY_NAMES[kind]
    return root / directory / scope_selector_key(kind, selector) / "context.json"


def scope_context_files() -> tuple[Path, ...]:
    paths: set[Path] = set()
    for root in scope_context_roots():
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        paths.update(
            path.resolve()
            for path in root.rglob("context.json")
            if path.is_file()
            and not path.is_symlink()
            and resolved_root in path.resolve().parents
        )
    return tuple(sorted(paths, key=str))


def global_backend_script() -> Path:
    return Path(__file__).resolve().with_name("global_context.py")


def runtime_fingerprint() -> str:
    backend = global_backend_script()
    candidates = (
        backend.with_name("global-runtime.json"),
        backend.with_suffix(backend.suffix + ".lock"),
    )
    digest = hashlib.sha256()
    found = False
    for path in candidates:
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
            found = True
    if not found:
        digest.update(backend.read_bytes())
    return digest.hexdigest()


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def global_config() -> dict[str, Any]:
    return read_json_object(global_config_dir() / GLOBAL_CONFIG_FILE)


def write_global_config(config: dict[str, Any]) -> None:
    stamp = now_iso()
    config["schema_version"] = GLOBAL_CONFIG_SCHEMA_VERSION
    config.setdefault("created_at", stamp)
    config["updated_at"] = stamp
    write_json_object(global_config_dir() / GLOBAL_CONFIG_FILE, config)


def configured_storage_runtime(
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    source_config = config if config is not None else global_config()
    raw = source_config.get("storage_runtime")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SystemExit("Invalid storage_runtime in global context configuration.")
    mode = raw.get("mode")
    if mode not in STORAGE_RUNTIME_MODES:
        raise SystemExit(
            "Invalid storage_runtime.mode in global context configuration."
        )
    visibility = raw.get("project_visibility")
    if mode == "local" and visibility not in LOCAL_CONTEXT_VISIBILITIES:
        raise SystemExit(
            "Local storage runtime requires project_visibility local or versioned."
        )
    if mode == "git-store" and visibility is not None:
        raise SystemExit(
            "Git-store storage runtime must not define project_visibility."
        )
    return dict(raw)


def storage_runtime_mode(config: dict[str, Any] | None = None) -> str:
    source_config = config if config is not None else global_config()
    runtime = configured_storage_runtime(source_config)
    git_store = configured_git_store(source_config)
    if runtime is None:
        return "git-store" if git_store is not None else "unconfigured"
    mode = str(runtime["mode"])
    if mode == "git-store" and git_store is None:
        raise SystemExit(
            "Storage runtime is git-store but no Git context store is configured."
        )
    if mode == "local" and git_store is not None:
        raise SystemExit(
            "Storage runtime is local but a Git context store is still configured."
        )
    return mode


def set_storage_runtime(
    config: dict[str, Any],
    mode: str,
    project_visibility: str | None = None,
) -> None:
    if mode not in STORAGE_RUNTIME_MODES:
        raise SystemExit(f"Invalid storage runtime mode: {mode}")
    if mode == "local" and project_visibility not in LOCAL_CONTEXT_VISIBILITIES:
        raise SystemExit(
            "Local storage runtime requires project visibility local or versioned."
        )
    if mode == "git-store" and project_visibility is not None:
        raise SystemExit(
            "Git-store storage runtime does not accept project visibility."
        )
    stamp = now_iso()
    existing = config.get("storage_runtime")
    previous = existing if isinstance(existing, dict) else {}
    runtime = {
        "mode": mode,
        "source": "user-confirmed",
        "created_at": previous.get("created_at", stamp),
        "updated_at": stamp,
    }
    if project_visibility is not None:
        runtime["project_visibility"] = project_visibility
    config["storage_runtime"] = runtime


def configured_git_store(
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    source_config = config if config is not None else global_config()
    raw = source_config.get("git_store")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    raw_path = raw.get("path")
    raw_store_id = raw.get("store_id")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SystemExit("Invalid git_store.path in global context configuration.")
    try:
        store_id = str(uuid.UUID(str(raw_store_id)))
    except ValueError:
        raise SystemExit(
            "Invalid git_store.store_id in global context configuration."
        ) from None
    raw_bindings = raw.get("project_bindings", {})
    if not isinstance(raw_bindings, dict):
        raise SystemExit(
            "Invalid git_store.project_bindings in global context configuration."
        )
    bindings: dict[str, str] = {}
    for raw_repo, raw_project_id in raw_bindings.items():
        if not isinstance(raw_repo, str) or not raw_repo:
            raise SystemExit("Invalid Git context store project binding path.")
        try:
            project_id = str(uuid.UUID(str(raw_project_id)))
        except ValueError:
            raise SystemExit(
                f"Invalid Git context project store id {raw_project_id!r}."
            ) from None
        bindings[str(Path(raw_repo).expanduser().resolve())] = project_id
    return {
        **raw,
        "path": str(Path(raw_path).expanduser().resolve()),
        "store_id": store_id,
        "project_bindings": bindings,
    }


def configured_git_store_root(
    config: dict[str, Any] | None = None,
) -> Path | None:
    store = configured_git_store(config)
    return Path(store["path"]) if store is not None else None


def scope_context_roots() -> tuple[Path, ...]:
    roots = [scope_context_root()]
    git_root = configured_git_store_root()
    if git_root is not None:
        roots.append(git_root / GIT_STORE_SCOPES_DIR)
    return tuple(dict.fromkeys(root.resolve() for root in roots))


def git_store_project_binding(
    repo: Path,
    config: dict[str, Any] | None = None,
) -> str | None:
    store = configured_git_store(config)
    if store is None:
        return None
    return store["project_bindings"].get(str(repo.expanduser().resolve()))


def git_store_project_path(root: Path, project_store_id: str) -> Path:
    try:
        canonical_id = str(uuid.UUID(project_store_id))
    except ValueError:
        raise SystemExit(
            f"Invalid project context store id {project_store_id!r}."
        ) from None
    return root / GIT_STORE_PROJECTS_DIR / canonical_id / "context.json"


def validate_git_store_target(root: Path, path: Path) -> None:
    canonical_root = root.resolve()
    candidate = path.absolute()
    try:
        relative = candidate.relative_to(canonical_root)
    except ValueError:
        raise SystemExit(
            f"Git context target is outside its store: {candidate}"
        ) from None
    current = canonical_root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise SystemExit(f"Git context target traverses a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise SystemExit(f"Git context target parent is not a directory: {current}")
    if candidate.is_symlink():
        raise SystemExit(f"Git context target is a symlink: {candidate}")
    if candidate.exists() and not candidate.is_file():
        raise SystemExit(f"Git context target is not a file: {candidate}")


def write_git_json(root: Path, path: Path, value: dict[str, Any]) -> None:
    validate_git_store_target(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def persist_git_store_domain(
    domain_id: str,
    members: DomainMembers | None,
) -> None:
    configured = configured_git_store()
    if configured is None:
        return
    root = validate_git_store_root(Path(configured["path"]))
    manifest = load_git_store_manifest(root)
    if manifest["store_id"] != configured["store_id"]:
        raise SystemExit(
            "Configured Git context store identity does not match its manifest."
        )
    domains = manifest["domains"]
    domain_remotes = manifest["domain_remotes"]
    if members is None:
        domains.pop(domain_id, None)
        domain_remotes.pop(domain_id, None)
    else:
        missing = tuple(
            project
            for project in members.projects
            if str(project.resolve()) not in configured["project_bindings"]
        )
        if missing:
            raise SystemExit(
                "Git-backed domains require every member project to be initialized or "
                "bound first: " + ", ".join(str(project) for project in missing)
            )
        domains[domain_id] = sorted(
            {
                configured["project_bindings"][str(project.resolve())]
                for project in members.projects
            }
        )
        if members.remotes:
            domain_remotes[domain_id] = list(members.remotes)
        else:
            domain_remotes.pop(domain_id, None)
    manifest["updated_at"] = now_iso()
    write_git_json(root, git_store_manifest_path(root), manifest)


def domain_set(args: argparse.Namespace) -> None:
    domain_id = validate_domain_id(args.domain)
    if not args.project and not args.remote:
        raise SystemExit("Domain membership requires at least one --project or --remote.")
    projects = tuple(dict.fromkeys(context_repo(path) for path in args.project or ()))
    missing = tuple(project for project in projects if not project.is_dir())
    if missing:
        raise SystemExit(
            "Domain project is not a directory: "
            + ", ".join(str(project) for project in missing)
        )
    remotes: dict[str, None] = {}
    for raw in args.remote or ():
        remote_url = normalize_remote_url(raw)
        if remote_url is None:
            raise SystemExit(f"Domain remote is not a Git remote URL: {raw!r}")
        remotes[remote_url] = None
    members = DomainMembers(projects=projects, remotes=tuple(sorted(remotes)))
    config = global_config()
    domains = configured_domains(config)
    domains[domain_id] = members
    persist_git_store_domain(domain_id, members)
    config["domains"] = domains_config(domains)
    write_global_config(config)
    print(
        f"Configured domain {domain_id}: {len(projects)} projects, "
        f"{len(members.remotes)} remotes"
    )


def domain_remove(args: argparse.Namespace) -> None:
    domain_id = validate_domain_id(args.domain)
    config = global_config()
    domains = configured_domains(config)
    if domain_id not in domains:
        raise SystemExit(f"Unknown domain {domain_id!r}.")
    del domains[domain_id]
    persist_git_store_domain(domain_id, None)
    config["domains"] = domains_config(domains)
    write_global_config(config)
    print(f"Removed domain membership: {domain_id}")


def domain_list(args: argparse.Namespace) -> None:
    domains = configured_domains(global_config())
    if not domains:
        print("No project domains configured.")
        return
    for domain_id, members in sorted(domains.items()):
        entries = [*(str(project) for project in members.projects), *members.remotes]
        print(f"{domain_id}: " + ", ".join(entries))


def global_runtime_state() -> dict[str, Any]:
    return read_json_object(global_data_dir() / GLOBAL_RUNTIME_FILE)


def global_runtime_is_current() -> bool:
    return global_runtime_state().get("fingerprint") == runtime_fingerprint()


def workspace_roots(config: dict[str, Any]) -> tuple[Path, ...]:
    values = config.get("workspace_roots", [])
    if not isinstance(values, list):
        return ()
    return tuple(Path(str(value)).expanduser().resolve() for value in values)


def append_external_source_arguments(
    arguments: list[str],
    roots: tuple[Path, ...],
) -> None:
    store = configured_git_store()
    git_root = configured_git_store_root()
    if store is not None and git_root is not None:
        for raw_repo, project_id in sorted(store["project_bindings"].items()):
            project = Path(raw_repo)
            workspace = containing_workspace(project, roots)
            source = git_store_project_path(git_root, project_id)
            if (
                workspace is None
                or not source.is_file()
                or (project / IGNORE_MARKER).exists()
            ):
                continue
            payload = {
                "source_path": str(source),
                "project_path": str(project),
                "workspace_root": str(workspace),
            }
            arguments.extend(("--external-source", json.dumps(payload, sort_keys=True)))


def backend_base_arguments(roots: tuple[Path, ...]) -> list[str]:
    cache = global_cache_dir()
    arguments: list[str] = []
    for root in roots:
        arguments.extend(("--workspace-root", str(root)))
    arguments.extend(
        (
            "--index-dir",
            str(cache / GLOBAL_INDEX_DIR),
            "--catalog",
            str(cache / GLOBAL_CATALOG_FILE),
            "--model-cache",
            str(cache / GLOBAL_MODEL_DIR),
        )
    )
    for root in scope_context_roots():
        arguments.extend(("--scope-root", str(root)))
    append_external_source_arguments(arguments, roots)
    return arguments


def invoke_global_backend(
    command: str,
    arguments: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    uv = os.environ.get("PROJECT_CONTEXT_CURATOR_UV") or shutil.which("uv")
    if not uv:
        raise SystemExit(
            "Global context requires uv. Install uv, then rerun the deterministic "
            "global-init or global-upgrade command."
        )
    backend = global_backend_script()
    if not backend.exists():
        raise SystemExit(f"Global context backend is missing: {backend}")
    try:
        proc = subprocess.run(
            [uv, "run", "--frozen", "--script", str(backend), command, *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            untrusted_diagnostic_line(f"global {command} failed", exc)
        ) from exc
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout).split()) or (
            f"backend exited with status {proc.returncode}"
        )
        raise SystemExit(untrusted_diagnostic_line(f"global {command} failed", detail))
    return proc


def discover_global_snapshot(roots: tuple[Path, ...]) -> dict[str, Any]:
    arguments: list[str] = []
    for root in roots:
        arguments.extend(("--workspace-root", str(root)))
    append_external_source_arguments(arguments, roots)
    backend = global_backend_script()
    try:
        proc = subprocess.run(
            [sys.executable, str(backend), "discover", *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            untrusted_diagnostic_line("global discovery failed", exc)
        ) from exc
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout).split())
        raise SystemExit(untrusted_diagnostic_line("global discovery failed", detail))
    try:
        snapshot = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("Global context discovery returned invalid JSON") from exc
    if not isinstance(snapshot, dict) or not snapshot.get("snapshot"):
        raise SystemExit("Global context discovery returned no snapshot token")
    roots = snapshot_workspace_roots(snapshot)
    sources = snapshot_sources(snapshot)
    missing_sources = snapshot_missing_sources(snapshot)
    if any(source["workspace_root"] not in roots for source in sources):
        raise SystemExit("Global context discovery returned a source outside its roots")
    source_keys = {snapshot_source_key(source) for source in sources}
    if any(
        source["workspace_root"] not in roots
        or snapshot_source_key(source) not in source_keys
        for source in missing_sources
    ):
        raise SystemExit(
            "Global context discovery returned a missing source outside its snapshot"
        )
    return snapshot


def print_snapshot_preview(
    snapshot: dict[str, Any], previous_catalog: dict[str, Any]
) -> None:
    for root in snapshot_workspace_roots(snapshot):
        print(
            json.dumps(
                {
                    "type": UNTRUSTED_SNAPSHOT_TYPE,
                    "change": "workspace_root",
                    "workspace_root": root,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    missing_sources = snapshot_missing_sources(snapshot)
    print(f"Projects requiring context initialization: {len(missing_sources)}")
    for source in missing_sources:
        print_snapshot_source("initialize", source)
    current_sources = {
        snapshot_source_key(source): source for source in snapshot_sources(snapshot)
    }
    raw_previous = previous_catalog.get("sources", [])
    previous_values = (
        tuple(snapshot_source(raw) for raw in raw_previous)
        if isinstance(raw_previous, list)
        else ()
    )
    previous_sources = {
        snapshot_source_key(source): source
        for source in previous_values
        if source is not None
    }
    added = sorted(set(current_sources) - set(previous_sources))
    removed = sorted(set(previous_sources) - set(current_sources))
    print(f"Projects to enroll: {len(added)}")
    for key in added:
        print_snapshot_source("add", current_sources[key])
    print(f"Projects to remove: {len(removed)}")
    for key in removed:
        print_snapshot_source("remove", previous_sources[key])
    print(f"Projects retained: {len(set(current_sources) & set(previous_sources))}")
    print(f"Snapshot token: {snapshot['snapshot']}")
    print("No changes made. Ask the user to approve this exact snapshot token.")


def validate_snapshot_approval(snapshot: dict[str, Any], approved: str) -> None:
    current = str(snapshot.get("snapshot", ""))
    if not SNAPSHOT_TOKEN_PATTERN.fullmatch(approved):
        raise SystemExit(
            "Approved snapshot token must be 64 lowercase hexadecimal characters."
        )
    if not hmac.compare_digest(current, approved):
        raise SystemExit(
            "Discovered project snapshot changed. Preview it again before approval."
        )


def validate_workspace_roots(values: list[Path]) -> tuple[Path, ...]:
    roots = tuple(dict.fromkeys(path.expanduser().resolve() for path in values))
    missing = tuple(path for path in roots if not path.is_dir())
    if missing:
        raise SystemExit(
            "Workspace root is not a directory: "
            + ", ".join(str(path) for path in missing)
        )
    return roots


def record_validated_runtime() -> None:
    write_json_object(
        global_data_dir() / GLOBAL_RUNTIME_FILE,
        {"fingerprint": runtime_fingerprint(), "validated_at": now_iso()},
    )


def validate_global_runtime() -> str:
    cache = global_cache_dir()
    proc = invoke_global_backend(
        "doctor",
        [
            "--index-dir",
            str(cache / GLOBAL_INDEX_DIR),
            "--model-cache",
            str(cache / GLOBAL_MODEL_DIR),
        ],
        timeout=1800,
    )
    record_validated_runtime()
    return proc.stdout.strip()


def global_upgrade(args: argparse.Namespace) -> None:
    detail = validate_global_runtime()
    print(detail or "Qdrant runtime ready")
    print(f"Runtime fingerprint: {runtime_fingerprint()}")


def global_init(args: argparse.Namespace) -> None:
    roots = validate_workspace_roots(args.workspace_root)
    snapshot = discover_global_snapshot(roots)
    if not args.approve_snapshot:
        print_snapshot_preview(
            snapshot, read_json_object(global_cache_dir() / GLOBAL_CATALOG_FILE)
        )
        return
    validate_snapshot_approval(snapshot, args.approve_snapshot)
    detail = (
        "Qdrant runtime already current"
        if global_runtime_is_current()
        else validate_global_runtime()
    )
    arguments = backend_base_arguments(roots)
    arguments.extend(("--enroll-new", "--approved-snapshot", args.approve_snapshot))
    proc = invoke_global_backend("sync", arguments, timeout=1800)
    stamp = now_iso()
    previous = global_config()
    config = {
        "schema_version": GLOBAL_CONFIG_SCHEMA_VERSION,
        "enabled": True,
        "enrollment_policy": "snapshot",
        "workspace_roots": [str(root) for root in roots],
        "domains": previous.get("domains", {}),
        "git_store": previous.get("git_store"),
        "storage_runtime": previous.get("storage_runtime"),
        "runtime_upgrade_policy": "prompt",
        "created_at": previous.get("created_at", stamp),
        "updated_at": stamp,
    }
    if config["git_store"] is None:
        del config["git_store"]
    if config["storage_runtime"] is None:
        del config["storage_runtime"]
    write_global_config(config)
    print(detail or "Qdrant runtime ready")
    print(proc.stdout.strip())
    print(f"Global context configured: {global_config_dir() / GLOBAL_CONFIG_FILE}")


def require_global_configuration() -> tuple[dict[str, Any], tuple[Path, ...]]:
    config = global_config()
    roots = workspace_roots(config)
    if not config.get("enabled") or not roots:
        raise SystemExit(
            "Global context is not configured. Run global-init with at least one "
            "--workspace-root."
        )
    return config, roots


def require_current_global_runtime() -> None:
    if global_runtime_is_current():
        return
    raise SystemExit(
        "Global context runtime update required. Ask the user before running "
        f"python3 {Path(__file__).resolve()} global-upgrade."
    )


def global_update(args: argparse.Namespace) -> None:
    _, roots = require_global_configuration()
    require_current_global_runtime()
    proc = invoke_global_backend(
        "sync",
        backend_base_arguments(roots),
        timeout=1800,
    )
    print(proc.stdout.strip())


def global_enroll(args: argparse.Namespace) -> None:
    _, roots = require_global_configuration()
    snapshot = discover_global_snapshot(roots)
    catalog = read_json_object(global_cache_dir() / GLOBAL_CATALOG_FILE)
    if not args.approve_snapshot:
        print_snapshot_preview(snapshot, catalog)
        return
    validate_snapshot_approval(snapshot, args.approve_snapshot)
    require_current_global_runtime()
    arguments = backend_base_arguments(roots)
    arguments.extend(("--enroll-new", "--approved-snapshot", args.approve_snapshot))
    proc = invoke_global_backend("sync", arguments, timeout=1800)
    print(proc.stdout.strip())


def global_catalog_has_enrollment_state(catalog: dict[str, Any]) -> bool:
    schema_version = catalog.get("index_schema_version")
    if type(schema_version) is not int:
        return False
    if schema_version in GLOBAL_SOURCE_CATALOG_SCHEMA_VERSIONS:
        return isinstance(catalog.get("sources"), list)
    return schema_version == GLOBAL_LEGACY_RECORD_CATALOG_SCHEMA_VERSION and isinstance(
        catalog.get("records"), list
    )


def global_catalog_requires_refresh(catalog: dict[str, Any]) -> bool:
    graph = catalog.get("relationship_graph")
    return (
        catalog.get("index_schema_version") != GLOBAL_INDEX_SCHEMA_VERSION
        or not isinstance(graph, dict)
        or graph.get("schema_version") != GLOBAL_RELATIONSHIP_GRAPH_SCHEMA_VERSION
    )


def global_status(args: argparse.Namespace) -> None:
    config = global_config()
    roots = workspace_roots(config)
    if not config.get("enabled") or not roots:
        print("Global context index: disabled.")
        if args.format == "hook":
            print(GLOBAL_ONBOARDING_GUIDANCE)
        return
    if not global_runtime_is_current():
        print(
            "Global context runtime update required. Ask the user before running: "
            f"python3 {Path(__file__).resolve()} global-upgrade"
        )
        return

    catalog = read_json_object(global_cache_dir() / GLOBAL_CATALOG_FILE)
    if not global_catalog_has_enrollment_state(catalog):
        print(
            "Global context enrollment repair required: catalog is missing or invalid."
        )
        print("Workspace roots: " + ", ".join(str(root) for root in roots))
        if args.format == "hook":
            print(GLOBAL_ENROLLMENT_REPAIR_GUIDANCE)
        return
    projects = catalog.get("projects", [])
    project_count = catalog.get("project_count", len(projects))
    records = catalog.get("records", [])
    print(
        f"Global context index: active across {project_count} projects and "
        f"{len(records)} records."
    )
    print("Workspace roots: " + ", ".join(str(root) for root in roots))
    current_repo = context_repo(args.repo)
    active_domains = member_domains(current_repo, configured_domains(config))
    if active_domains:
        print("Active context domains: " + ", ".join(active_domains))
    relationships = catalog.get("relationships", {})
    related = (
        relationships.get(str(current_repo), [])
        if isinstance(relationships, dict)
        else []
    )
    if related:
        print(f"Related projects for {current_repo.name}: {', '.join(related)}")
    if args.format == "hook":
        print("The standard search command queries this global index automatically.")


def try_global_search(
    repo: Path,
    queries: tuple[str, ...],
    limit: int,
) -> tuple[str, ...] | None:
    config = global_config()
    roots = workspace_roots(config)
    if not config.get("enabled") or not roots:
        return None
    if not global_runtime_is_current():
        print(
            "Global context runtime update required; using local context search. "
            f"Ask before running: python3 {Path(__file__).resolve()} global-upgrade",
            file=sys.stderr,
        )
        return None
    catalog = read_json_object(global_cache_dir() / GLOBAL_CATALOG_FILE)
    if not global_catalog_has_enrollment_state(catalog):
        print(
            "Global context enrollment repair required: catalog is missing or invalid.",
            file=sys.stderr,
        )
        print(GLOBAL_ENROLLMENT_REPAIR_GUIDANCE, file=sys.stderr)
        print("Using repository-local context search.", file=sys.stderr)
        return None
    if global_catalog_requires_refresh(catalog):
        try:
            invoke_global_backend(
                "sync",
                backend_base_arguments(roots),
                timeout=1800,
            )
        except SystemExit as exc:
            print(
                untrusted_diagnostic_line("global index refresh failed", exc),
                file=sys.stderr,
            )
            print("Using repository-local context search.", file=sys.stderr)
            return None
    arguments = backend_base_arguments(roots)
    arguments.extend(("--current-repo", str(repo)))
    for kind, selector in sorted(active_applicability(repo)):
        arguments.extend(("--active-applicability", f"{kind}:{selector}"))
    for query in queries:
        arguments.extend(("--query", query))
    arguments.extend(("--limit", str(limit)))
    try:
        proc = invoke_global_backend("search", arguments, timeout=300)
    except SystemExit as exc:
        print(
            untrusted_diagnostic_line("global search unavailable", exc),
            file=sys.stderr,
        )
        print("Using repository-local context search.", file=sys.stderr)
        return None
    if proc.stderr:
        forward_global_diagnostics(proc.stderr)
    return tuple(
        line
        for line in proc.stdout.splitlines()
        if line and not line.startswith("No global context matches for:")
    )


def local_context_path(repo: Path) -> Path:
    return repo / CONTEXT_FILE


def context_path(repo: Path) -> Path:
    project_store_id = git_store_project_binding(repo)
    git_root = configured_git_store_root()
    if project_store_id is not None and git_root is not None:
        return git_store_project_path(git_root, project_store_id)
    return local_context_path(repo)


def ignore_marker_path(repo: Path) -> Path:
    return repo / IGNORE_MARKER


def git_output(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = proc.stdout.strip()
    return output if proc.returncode == 0 and output else None


def normalize_remote_url(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    lowered = value.casefold()
    scheme = next((item for item in REMOTE_URL_SCHEMES if lowered.startswith(item)), None)
    if scheme is not None:
        value = value[len(scheme) :]
    elif (match := SCP_REMOTE_PATTERN.match(value)) is not None:
        value = f"{match.group(1)}/{match.group(2)}"
    elif NORMALIZED_REMOTE_PATTERN.match(value) is None:
        return None
    host, _, path = value.partition("/")
    host = host.rpartition("@")[2].casefold()
    path = path.strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4].rstrip("/")
    if not host or not path:
        return None
    return f"{host}/{path}"


def repo_remote_url(repo: Path) -> str | None:
    remotes = git_output(repo, "remote")
    if remotes is None:
        return None
    names = remotes.splitlines()
    name = "origin" if "origin" in names else names[0]
    return normalize_remote_url(git_output(repo, "remote", "get-url", name))


def git_process(
    cwd: Path,
    *arguments: str,
    timeout: int = GIT_STORE_NETWORK_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=str(cwd),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Git context store command failed: {exc}") from exc


def git_error_detail(proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout).strip()
    return safe_display_field(
        detail or f"git exited with status {proc.returncode}",
        DIAGNOSTIC_OUTPUT_LIMIT,
    )


def git_checked(
    cwd: Path,
    *arguments: str,
    action: str,
) -> subprocess.CompletedProcess[str]:
    proc = git_process(cwd, *arguments)
    if proc.returncode != 0:
        raise SystemExit(f"Git context store {action} failed: {git_error_detail(proc)}")
    return proc


def git_config_value(root: Path, key: str) -> str | None:
    proc = git_process(root, "config", "--get", key)
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise SystemExit(
            f"Git context store configuration read failed: {git_error_detail(proc)}"
        )
    value = proc.stdout.strip()
    return value or None


def git_store_upstream(root: Path) -> GitUpstream:
    branch_proc = git_process(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = branch_proc.stdout.strip()
    if branch_proc.returncode != 0 or not branch:
        raise SystemExit(
            "Git context store must use an attached branch for automatic push."
        )
    if branch != GIT_STORE_BRANCH:
        raise SystemExit(
            f"Git context store must be checked out on {GIT_STORE_BRANCH} for "
            "automatic push."
        )
    remote = git_config_value(root, f"branch.{branch}.remote")
    merge = git_config_value(root, f"branch.{branch}.merge")
    target = merge.removeprefix("refs/heads/") if merge else None
    if remote in (None, ".") or not target or target == merge:
        remotes = tuple(
            item
            for item in git_checked(
                root, "remote", action="remote lookup"
            ).stdout.splitlines()
            if item
        )
        if len(remotes) != 1:
            raise SystemExit(
                "Git context store requires one configured push remote or an exact "
                "branch upstream."
            )
        remote = remotes[0]
        target = branch
    if target != GIT_STORE_BRANCH:
        raise SystemExit(f"Git context store must push directly to {GIT_STORE_BRANCH}.")
    push_url = git_checked(
        root, "remote", "get-url", "--push", remote, action="push remote lookup"
    ).stdout.strip()
    return GitUpstream(remote=remote, branch=target, push_url=push_url)


def git_head_exists(root: Path) -> bool:
    proc = git_process(root, "rev-parse", "--verify", "HEAD")
    if proc.returncode not in (0, 128):
        raise SystemExit(
            f"Git context store HEAD lookup failed: {git_error_detail(proc)}"
        )
    return proc.returncode == 0


def git_remote_branch_exists(root: Path, upstream: GitUpstream) -> bool:
    proc = git_process(
        root,
        "ls-remote",
        "--exit-code",
        "--heads",
        upstream.remote,
        f"refs/heads/{upstream.branch}",
    )
    if proc.returncode not in (0, 2):
        raise SystemExit(
            f"Git context store remote lookup failed: {git_error_detail(proc)}"
        )
    return proc.returncode == 0


def git_status_for_paths(root: Path, paths: tuple[str, ...]) -> str:
    proc = git_checked(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *paths,
        action="status inspection",
    )
    return proc.stdout.strip()


def require_no_unmanaged_git_store_changes(root: Path) -> None:
    status = git_status_for_paths(root, GIT_STORE_UNMANAGED_PATHS)
    if status:
        raise SystemExit(
            "Git context store has unmanaged changes outside curator paths: "
            + safe_display_field(status, DIAGNOSTIC_OUTPUT_LIMIT)
        )


def commit_git_store_changes(root: Path, message: str) -> bool:
    if not git_status_for_paths(root, GIT_STORE_MANAGED_PATHS):
        return False
    paths = tuple(
        path
        for path in GIT_STORE_STAGE_PATHS
        if (root / path).exists() or git_process(root, "ls-files", "--", path).stdout
    )
    git_checked(root, "add", "-A", "--", *paths, action="staging")
    diff = git_process(root, "diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        raise SystemExit(
            f"Git context store staged diff failed: {git_error_detail(diff)}"
        )
    git_checked(root, "commit", "-m", message, action="commit")
    return True


def rebase_git_store(root: Path, upstream: GitUpstream) -> None:
    proc = git_process(
        root,
        "pull",
        "--rebase",
        "--no-autostash",
        upstream.remote,
        upstream.branch,
    )
    if proc.returncode == 0:
        return
    git_process(root, "rebase", "--abort")
    raise SystemExit(
        f"Git context store upstream sync failed: {git_error_detail(proc)}"
    )


def push_git_store(
    root: Path, upstream: GitUpstream
) -> subprocess.CompletedProcess[str]:
    return git_process(
        root,
        "push",
        "--set-upstream",
        upstream.remote,
        f"HEAD:refs/heads/{upstream.branch}",
    )


def push_git_store_commit(root: Path, upstream: GitUpstream) -> None:
    proc = push_git_store(root, upstream)
    if proc.returncode == 0:
        return
    first_error = git_error_detail(proc)
    try:
        if git_remote_branch_exists(root, upstream):
            rebase_git_store(root, upstream)
            retry = push_git_store(root, upstream)
            if retry.returncode == 0:
                return
            first_error = git_error_detail(retry)
    except SystemExit as exc:
        first_error = safe_display_field(exc, DIAGNOSTIC_OUTPUT_LIMIT)
    raise SystemExit(
        "Git context update was committed locally but push failed: " + first_error
    )


def prepare_git_store(root: Path, upstream: GitUpstream) -> None:
    require_no_unmanaged_git_store_changes(root)
    remote_exists = git_remote_branch_exists(root, upstream)
    local_exists = git_head_exists(root)
    if remote_exists and not local_exists:
        raise SystemExit(
            "Git context store upstream has history but the local branch has no commit."
        )
    commit_git_store_changes(root, "chore(context): persist pending updates")
    if remote_exists:
        rebase_git_store(root, upstream)
    if git_head_exists(root):
        push_git_store_commit(root, upstream)


@contextmanager
def git_store_lock(root: Path) -> Iterator[None]:
    git_dir = git_path(root, "--git-dir")
    if git_dir is None:
        raise SystemExit(f"Git context store has no Git directory: {root}")
    lock_path = git_dir / "project-context-curator.lock"
    deadline = time.monotonic() + GIT_STORE_LOCK_TIMEOUT_SECONDS
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SystemExit(
                        "Timed out waiting for the Git context store lock."
                    )
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def git_path(cwd: Path, option: str) -> Path | None:
    raw = git_output(cwd, "rev-parse", option)
    if raw is None:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def worktree_root(path: Path) -> Path:
    root = git_output(path, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else path


def main_worktree_root(worktree: Path) -> Path:
    git_dir = git_path(worktree, "--git-dir")
    common_dir = git_path(worktree, "--git-common-dir")
    if git_dir is None or common_dir is None or git_dir == common_dir:
        return worktree

    if common_dir.name != ".git":
        return worktree

    candidate = common_dir.parent.resolve()
    if candidate == worktree or not candidate.exists():
        return worktree

    candidate_common_dir = git_path(candidate, "--git-common-dir")
    if candidate_common_dir == common_dir:
        return candidate

    return worktree


def context_repo(repo: Path) -> Path:
    resolved = repo.expanduser().resolve()
    return main_worktree_root(worktree_root(resolved))


def is_git_initialized(repo: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def ignore_file_has_context_entry(existing: str) -> bool:
    ignored_forms = {"docs/context", "docs/context/", "/docs/context", "/docs/context/"}
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped in ignored_forms:
            return True
    return False


def git_exclude_path(repo: Path) -> Path | None:
    git_dir = git_path(repo, "--git-dir")
    if git_dir is None:
        return None
    return git_dir / "info" / "exclude"


def git_exclude_display_path(repo: Path) -> Path:
    return git_exclude_path(repo) or repo / ".git" / "info" / "exclude"


def ensure_git_exclude_entry(repo: Path) -> bool:
    path = git_exclude_path(repo)
    if path is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if ignore_file_has_context_entry(existing):
        return False

    prefix = ""
    if existing and not existing.endswith("\n"):
        prefix = "\n"
    path.write_text(existing + prefix + GIT_EXCLUDE_ENTRY + "\n", encoding="utf-8")
    return True


def remove_git_exclude_entry(repo: Path) -> bool:
    path = git_exclude_path(repo)
    if path is None or not path.exists():
        return False

    existing = path.read_text(encoding="utf-8")
    ignored_forms = {"docs/context", "docs/context/", "/docs/context", "/docs/context/"}
    kept_lines = []
    removed = False
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped in ignored_forms and not stripped.startswith("#"):
            removed = True
            continue
        kept_lines.append(line)
    if not removed:
        return False

    updated = "\n".join(kept_lines)
    if updated:
        updated += "\n"

    path.write_text(updated, encoding="utf-8")
    return True


def ensure_record_identities(data: dict[str, Any]) -> None:
    raw_store_id = str(data.get("store_id", ""))
    try:
        namespace = uuid.UUID(raw_store_id)
    except ValueError:
        if raw_store_id:
            raise SystemExit(f"Invalid context store_id {raw_store_id!r}") from None
        namespace = uuid.uuid4()
        data["store_id"] = str(namespace)
    for collection, key in RECORD_KEYS.items():
        records = data.get(collection, [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            label = str(record.get(key, "")).strip()
            if not label:
                continue
            raw_id = str(record.get("id", ""))
            try:
                uuid.UUID(raw_id)
            except ValueError:
                if raw_id:
                    raise SystemExit(
                        f"Invalid {collection} record id {raw_id!r} for {label!r}"
                    ) from None
                record["id"] = str(
                    uuid.uuid5(namespace, f"{collection}:{label.casefold()}")
                )
            provenance = record.setdefault("provenance", [])
            if not isinstance(provenance, list):
                raise SystemExit(
                    f"Invalid {collection} provenance for {label!r}: expected list"
                )


def legacy_context_store_id(path: Path) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"project-context:{path.resolve()}"))


def default_context() -> dict[str, Any]:
    data = {
        "schema_version": SCHEMA_VERSION,
        "store_id": str(uuid.uuid4()),
        "default_applicability": deepcopy(DEFAULT_APPLICABILITY),
        "terms": [],
        "components": [],
        "patterns": [],
        "open_questions": [],
    }
    return data


Migration = Callable[[dict[str, Any]], dict[str, Any]]


def migrate_v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    migrated: dict[str, Any] = {
        "schema_version": 1,
        "terms": [],
        "components": [],
        "patterns": [],
        "open_questions": [],
    }
    migrated.update(deepcopy(data))
    policy = migrated.get("storage_policy")
    if isinstance(policy, dict) and "gitignore_docs_context" in policy:
        updated_policy = dict(policy)
        updated_policy["git_exclude_docs_context"] = updated_policy.pop(
            "gitignore_docs_context"
        )
        migrated["storage_policy"] = updated_policy
    migrated["schema_version"] = 1
    return migrated


def migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(data)
    migrated.setdefault("default_applicability", deepcopy(DEFAULT_APPLICABILITY))
    migrated["schema_version"] = 2
    return migrated


def migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(data)
    migrated["schema_version"] = 3
    return migrated


def migrate_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(data)

    def migrate_applicability(value: Any) -> None:
        if not isinstance(value, list):
            return
        for selector in value:
            if isinstance(selector, dict) and selector.get("kind") == "machine":
                selector.pop("selector", None)

    migrate_applicability(migrated.get("default_applicability"))
    scope_store = migrated.get("scope_store")
    if isinstance(scope_store, dict):
        migrate_applicability(scope_store.get("applicability"))
    for collection in RECORD_KEYS:
        records = migrated.get(collection)
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict):
                migrate_applicability(record.get("applicability"))
    migrated["schema_version"] = 4
    return migrated


MIGRATIONS: dict[int, Migration] = {
    0: migrate_v0_to_v1,
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
    3: migrate_v3_to_v4,
}


def normalize_applicability(
    value: Any,
    label: str,
    *,
    allow_legacy: bool = True,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SystemExit(f"Invalid {label}: expected a non-empty list")

    normalized: dict[tuple[str, str], dict[str, str]] = {}
    for raw_selector in value:
        if not isinstance(raw_selector, dict):
            raise SystemExit(f"Invalid {label}: each selector must be an object")
        kind = str(raw_selector.get("kind", "")).strip().casefold()
        allowed_kinds = APPLICABILITY_KINDS | (
            LEGACY_APPLICABILITY_KINDS if allow_legacy else set()
        )
        if kind not in allowed_kinds:
            allowed = ", ".join(sorted(APPLICABILITY_KINDS))
            raise SystemExit(
                f"Invalid applicability kind {kind!r} in {label}. Allowed: {allowed}"
            )
        raw_value = str(raw_selector.get("selector", "")).strip()
        if kind == "universal":
            if raw_value:
                raise SystemExit(
                    f"Invalid {label}: universal applicability has no selector"
                )
            selector = "*"
            item = {"kind": kind}
        elif kind == "machine":
            if raw_value:
                raise SystemExit(
                    f"Invalid {label}: machine applicability has no selector"
                )
            selector = "*"
            item = {"kind": kind}
        else:
            if not raw_value:
                raise SystemExit(
                    f"Invalid {label}: {kind} applicability requires a selector"
                )
            selector = raw_value
            item = {"kind": kind, "selector": selector}
        normalized[(kind, selector.casefold())] = item
    return [normalized[key] for key in sorted(normalized)]


def parse_applicability(values: list[str] | None) -> list[dict[str, str]] | None:
    if values is None:
        return None
    selectors: list[dict[str, str]] = []
    for value in values:
        raw = value.strip()
        kind, separator, selector = raw.partition(":")
        kind = kind.casefold()
        if kind not in APPLICABILITY_KINDS:
            allowed = ", ".join(sorted(APPLICABILITY_KINDS))
            raise SystemExit(f"Invalid applicability kind {kind!r}. Allowed: {allowed}")
        if kind == "universal":
            if separator:
                raise SystemExit("Universal applicability does not accept a selector")
            selectors.append({"kind": kind})
            continue
        if kind == "machine":
            if separator and selector.strip() not in {"", "self"}:
                raise SystemExit(
                    "Machine applicability has no selector; XDG storage is the machine boundary."
                )
            selectors.append({"kind": kind})
            continue
        resolved_selector = selector.strip() if separator else "self"
        if not resolved_selector:
            raise SystemExit(f"Applicability {kind!r} requires a non-blank selector")
        selectors.append({"kind": kind, "selector": resolved_selector})
    return normalize_applicability(
        selectors,
        "applicability",
        allow_legacy=False,
    )


def context_schema_version(data: dict[str, Any]) -> int:
    version = data.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise SystemExit(
            f"Invalid context schema_version {version!r}: expected a non-negative integer."
        )
    if version > SCHEMA_VERSION:
        raise SystemExit(
            f"Unsupported context schema_version {version}; this updater supports up to "
            f"schema_version {SCHEMA_VERSION}. Upgrade Project Context Curator first."
        )
    return version


def migrate_context(data: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    version = context_schema_version(data)
    migrated = deepcopy(data)
    applied: list[str] = []
    while version < SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise SystemExit(
                f"No migration registered from schema_version {version} to "
                f"schema_version {version + 1}."
            )
        migrated = migration(migrated)
        next_version = context_schema_version(migrated)
        if next_version != version + 1:
            raise SystemExit(
                f"Migration from schema_version {version} produced schema_version "
                f"{next_version}; expected {version + 1}."
            )
        applied.append(f"{version} -> {next_version}")
        version = next_version
    return migrated, tuple(applied)


def storage_policy(
    visibility: str,
    source: str,
    git_initialized: bool,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if visibility not in CONTEXT_VISIBILITIES:
        allowed = ", ".join(sorted(CONTEXT_VISIBILITIES))
        raise SystemExit(
            f"Invalid context visibility {visibility!r}. Allowed: {allowed}"
        )

    stamp = now_iso()
    local = visibility == "local"
    if visibility == "git-store":
        decision = (
            "Canonical project context is stored in the configured Git context "
            "repository; docs/context contains generated views only."
        )
    elif local and git_initialized:
        decision = (
            "Context stays local to this checkout; docs/context/ is ignored through "
            ".git/info/exclude."
        )
    elif local:
        decision = (
            "Context is local because the target directory is not a Git repository."
        )
    else:
        decision = "Context is intended to be versioned and shared through Git."
    return {
        "context_visibility": visibility,
        "git_initialized": git_initialized,
        "git_exclude_docs_context": visibility in {"git-store", "local"}
        and git_initialized,
        "decision": decision,
        "source": source,
        "created_at": existing.get("created_at", stamp) if existing else stamp,
        "updated_at": stamp,
    }


def load_context_file(path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not path.exists():
        return default_context(), ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit(f"Invalid context root in {path}: expected object")

    migrated, applied = migrate_context(data)
    merged = default_context()
    merged.update(migrated)
    if not str(migrated.get("store_id", "")):
        merged["store_id"] = legacy_context_store_id(path)
    for key in ("terms", "components", "patterns", "open_questions"):
        if not isinstance(merged.get(key), list):
            raise SystemExit(f"Invalid context field {key}: expected list")
    merged["default_applicability"] = normalize_applicability(
        merged.get("default_applicability"),
        "default_applicability",
    )
    for collection in ("terms", "components", "patterns", "open_questions"):
        for index, record in enumerate(merged[collection]):
            if not isinstance(record, dict):
                raise SystemExit(
                    f"Invalid context field {collection}[{index}]: expected object"
                )
            if "applicability" in record:
                record["applicability"] = normalize_applicability(
                    record["applicability"],
                    f"{collection}[{index}].applicability",
                )
    ensure_record_identities(merged)
    return merged, applied


def load_context_with_migrations(
    repo: Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    return load_context_file(context_path(repo))


def load_context(repo: Path) -> dict[str, Any]:
    data, _ = load_context_with_migrations(repo)
    return data


def require_initialized_context(repo: Path) -> None:
    if context_path(repo).exists():
        return
    raise SystemExit(
        "Context is not initialized. Ask the user whether project context should be "
        f"initialized. If no, run ignore to create {IGNORE_MARKER}. If yes, read the "
        "repository README/CLAUDE.md/AGENTS.md, top-level layout, and main manifests, "
        "then run init and the add-component/add-term/add-pattern commands for the "
        "verified findings in the same turn."
    )


def load_scope_context(
    path: Path,
    applicability: list[dict[str, str]],
) -> dict[str, Any]:
    data, _ = load_context_file(path)
    expected_pairs = applicability_pairs(applicability)
    if path.exists():
        if applicability_pairs(validate_scope_context(data, path)) != expected_pairs:
            raise SystemExit(f"Scope context applicability mismatch in {path}")
        return data
    stamp = now_iso()
    data["default_applicability"] = deepcopy(applicability)
    data["scope_store"] = {
        "applicability": deepcopy(applicability),
        "created_at": stamp,
        "updated_at": stamp,
    }
    return data


def validate_scope_context(
    data: dict[str, Any],
    path: Path,
) -> list[dict[str, str]]:
    metadata = data.get("scope_store")
    if not isinstance(metadata, dict):
        raise SystemExit(f"Invalid scope context metadata in {path}")
    boundary = normalize_applicability(
        metadata.get("applicability"),
        f"{path}:scope_store.applicability",
    )
    expected = applicability_pairs(boundary)
    if applicability_pairs(data.get("default_applicability")) != expected:
        raise SystemExit(f"Scope context default applicability mismatch in {path}")
    for collection in RECORD_KEYS:
        for record in data[collection]:
            effective = record.get("applicability", data["default_applicability"])
            if applicability_pairs(effective) != expected:
                raise SystemExit(
                    f"Scope context record applicability mismatch in {path}"
                )
    return boundary


def load_discovered_scope_context(path: Path) -> dict[str, Any]:
    data, _ = load_context_file(path)
    validate_scope_context(data, path)
    return data


def save_scope_context(path: Path, data: dict[str, Any]) -> None:
    normalize(data)
    metadata = data.get("scope_store")
    if not isinstance(metadata, dict):
        raise SystemExit(f"Invalid scope context metadata in {path}")
    metadata["updated_at"] = now_iso()
    git_root = configured_git_store_root()
    if git_root is not None and git_root.resolve() in path.resolve().parents:
        _configured, validated_root, _manifest = require_configured_git_store()
        write_git_json(validated_root, path, data)
    else:
        write_json_object(path, data)


def merge_scope_context_data(
    target: dict[str, Any],
    source: dict[str, Any],
    path: Path,
) -> None:
    target_boundary = applicability_pairs(validate_scope_context(target, path))
    source_boundary = applicability_pairs(validate_scope_context(source, path))
    if source_boundary != target_boundary:
        raise SystemExit(f"Legacy scope migration boundary mismatch: {path}")
    for collection in RECORD_KEYS:
        records = {
            str(record["id"]): record
            for record in target[collection]
            if isinstance(record, dict)
        }
        for record in source[collection]:
            if not isinstance(record, dict):
                continue
            record_id = str(record["id"])
            existing = records.get(record_id)
            if existing is None:
                target[collection].append(deepcopy(record))
                records[record_id] = record
            elif existing != record:
                raise SystemExit(
                    f"Legacy scope migration record conflict: {collection}:{record_id}"
                )


def migrate_private_scope_contexts() -> tuple[str, ...]:
    root = scope_context_root()
    if not root.is_dir():
        return ()
    migrations: list[tuple[Path, Path, dict[str, Any], tuple[str, ...]]] = []
    for source in sorted(root.rglob("context.json"), key=str):
        if not source.is_file() or source.is_symlink():
            continue
        data, applied = load_context_file(source)
        if not applied:
            continue
        boundary = validate_scope_context(data, source)
        migrations.append((source.resolve(), scope_context_path(boundary), data, applied))

    sources_by_target: dict[Path, list[tuple[Path, dict[str, Any], tuple[str, ...]]]] = {}
    for source, target, data, applied in migrations:
        sources_by_target.setdefault(target, []).append((source, data, applied))

    plans: list[tuple[Path, dict[str, Any], tuple[Path, ...], tuple[str, ...]]] = []
    for target, sources in sources_by_target.items():
        target_source = next(
            (data for source, data, _steps in sources if source == target.resolve()),
            None,
        )
        if target_source is not None:
            merged = deepcopy(target_source)
        elif target.exists():
            merged, _ = load_context_file(target)
        else:
            merged = deepcopy(sources[0][1])
        for source, data, _steps in sources:
            if source != target.resolve() or data != target_source:
                merge_scope_context_data(merged, data, target)
        legacy_sources = tuple(source for source, _data, _steps in sources if source != target.resolve())
        steps = tuple(step for _source, _data, source_steps in sources for step in source_steps)
        plans.append((target, merged, legacy_sources, steps))

    for target, data, _sources, _steps in plans:
        save_scope_context(target, data)
    applied_steps: list[str] = []
    for _target, _data, sources, steps in plans:
        for source in sources:
            source.unlink()
            parent = source.parent
            while parent != root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        applied_steps.extend(steps)
    return tuple(applied_steps)


def context_write_target(
    repo: Path,
    values: list[str] | None,
) -> tuple[Path, dict[str, Any], list[dict[str, str]] | None, bool]:
    require_initialized_context(repo)
    parsed = parse_applicability(values)
    if parsed is None:
        return context_path(repo), load_context(repo), None, True
    resolved = resolve_applicability(
        parsed,
        repo,
        require_domain_membership=True,
    )
    if any(item["kind"] == "machine" for item in resolved):
        migrate_private_scope_contexts()
    if applicability_pairs(resolved) == (("project", str(repo)),):
        return context_path(repo), load_context(repo), parsed, True
    path = scope_context_path(resolved)
    return path, load_scope_context(path, resolved), resolved, False


def save_context_target(
    repo: Path,
    path: Path,
    data: dict[str, Any],
    project_target: bool,
) -> None:
    if project_target:
        save_context(repo, data)
        return
    save_scope_context(path, data)


def policy_git_initialized(repo: Path, policy: dict[str, Any]) -> bool:
    raw = policy.get("git_initialized")
    if isinstance(raw, bool):
        return raw
    return is_git_initialized(repo)


def apply_storage_policy(repo: Path, data: dict[str, Any]) -> str:
    policy = data.get("storage_policy")
    if not isinstance(policy, dict):
        return "missing"

    visibility = policy.get("context_visibility")
    if visibility == "local":
        if not policy_git_initialized(repo, policy):
            return "not-git"
        return "exclude-added" if ensure_git_exclude_entry(repo) else "exclude-present"
    if visibility == "versioned":
        return "exclude-removed" if remove_git_exclude_entry(repo) else "exclude-absent"
    if visibility == "git-store":
        if not policy_git_initialized(repo, policy):
            return "not-git"
        return "exclude-added" if ensure_git_exclude_entry(repo) else "exclude-present"

    raise SystemExit(
        "Invalid storage_policy.context_visibility in context.json: "
        f"{visibility!r}. Expected git-store, local, or versioned."
    )


def save_context(repo: Path, data: dict[str, Any]) -> str:
    ctx_dir = repo / CONTEXT_DIR
    ctx_dir.mkdir(parents=True, exist_ok=True)
    ensure_context_gitignore(ctx_dir)
    normalize(data)
    policy_result = apply_storage_policy(repo, data)

    path = context_path(repo)
    git_root = configured_git_store_root()
    if git_root is not None and git_root.resolve() in path.resolve().parents:
        _configured, validated_root, _manifest = require_configured_git_store()
        write_git_json(validated_root, path, data)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    render_markdown(repo, data)
    return policy_result


def ensure_context_gitignore(ctx_dir: Path) -> None:
    # The session hook keeps transient state (turn counter) in
    # docs/context/.hook-state.json; it must never be committed in
    # versioned mode. Self-heals existing installs on any save.
    gitignore = ctx_dir / ".gitignore"
    entry = ".hook-state.json"
    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        if entry in lines:
            return
        lines.append(entry)
        gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    gitignore.write_text(entry + "\n", encoding="utf-8")


def normalize(data: dict[str, Any]) -> None:
    data["schema_version"] = SCHEMA_VERSION
    data["default_applicability"] = normalize_applicability(
        data.get("default_applicability", deepcopy(DEFAULT_APPLICABILITY)),
        "default_applicability",
    )
    policy = data.get("storage_policy")
    if isinstance(policy, dict) and "gitignore_docs_context" in policy:
        policy["git_exclude_docs_context"] = policy.pop("gitignore_docs_context")
    ensure_record_identities(data)
    data["terms"] = sorted(data.get("terms", []), key=lambda item: item["term"].lower())
    data["components"] = sorted(
        data.get("components", []), key=lambda item: item["name"].lower()
    )
    data["patterns"] = sorted(
        data.get("patterns", []), key=lambda item: item["name"].lower()
    )
    data["open_questions"] = sorted(
        data.get("open_questions", []),
        key=lambda item: (
            item.get("status", "open") != "open",
            item["question"].lower(),
        ),
    )


def find_record(
    records: list[dict[str, Any]], key: str, value: str
) -> dict[str, Any] | None:
    needle = value.casefold()
    for record in records:
        if str(record.get(key, "")).casefold() == needle:
            return record
    return None


def split_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    for value in values:
        for part in value.split(","):
            cleaned = part.strip()
            if cleaned:
                result.append(cleaned)
    return sorted(dict.fromkeys(result), key=str.casefold)


def set_if_present(record: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, list) and not value:
        return
    if isinstance(value, str) and not value.strip():
        return
    record[key] = value


def add_provenance(
    record: dict[str, Any],
    repo: Path,
    source: str,
    action: str,
) -> None:
    stamp = now_iso()
    entry = {
        "action": action,
        "recorded_at": stamp,
        "repo": str(repo),
        "source": source,
    }
    commit = git_output(repo, "rev-parse", "HEAD")
    if commit:
        entry["commit"] = commit
    provenance = record.setdefault("provenance", [])
    identity = tuple(
        entry.get(field) for field in ("action", "repo", "source", "commit")
    )
    if any(
        tuple(item.get(field) for field in ("action", "repo", "source", "commit"))
        == identity
        for item in provenance
        if isinstance(item, dict)
    ):
        return
    provenance.append(entry)


def upsert_common(
    record: dict[str, Any],
    source: str | None,
    repo: Path,
) -> None:
    stamp = now_iso()
    record.setdefault("created_at", stamp)
    record["updated_at"] = stamp
    if source:
        record["source"] = source
    add_provenance(
        record, repo, source or str(record.get("source", "unknown")), "recorded"
    )


def set_applicability(
    record: dict[str, Any],
    applicability: list[dict[str, str]] | None,
) -> None:
    if applicability is not None:
        record["applicability"] = deepcopy(applicability)


def add_term(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    if args.kind not in TERM_KINDS:
        allowed = ", ".join(sorted(TERM_KINDS))
        raise SystemExit(f"Invalid term kind {args.kind!r}. Allowed: {allowed}")

    path, data, applicability, project_target = context_write_target(
        repo, args.applicability
    )
    record = find_record(data["terms"], "term", args.term)
    if record is None:
        record = {"term": args.term}
        data["terms"].append(record)

    record["kind"] = args.kind
    record["definition"] = args.definition
    record["scope"] = args.scope
    set_if_present(record, "aliases", split_values(args.aliases))
    set_if_present(record, "notes", args.notes)
    set_applicability(record, applicability)
    upsert_common(record, args.source, repo)
    save_context_target(repo, path, data, project_target)
    print(f"Updated term: {args.term}")
    print(f"Canonical context: {path}")


def add_component(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    path, data, applicability, project_target = context_write_target(
        repo, args.applicability
    )
    record = find_record(data["components"], "name", args.name)
    if record is None:
        record = {"name": args.name}
        data["components"].append(record)

    record["responsibility"] = args.responsibility
    set_if_present(record, "paths", split_values(args.paths))
    set_if_present(record, "interfaces", split_values(args.interfaces))
    set_if_present(record, "notes", args.notes)
    set_applicability(record, applicability)
    upsert_common(record, args.source, repo)
    save_context_target(repo, path, data, project_target)
    print(f"Updated component: {args.name}")
    print(f"Canonical context: {path}")


def add_pattern(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    path, data, applicability, project_target = context_write_target(
        repo, args.applicability
    )
    record = find_record(data["patterns"], "name", args.name)
    if record is None:
        record = {"name": args.name}
        data["patterns"].append(record)

    record["summary"] = args.summary
    set_if_present(record, "applies_to", split_values(args.applies_to))
    set_if_present(record, "notes", args.notes)
    set_applicability(record, applicability)
    upsert_common(record, args.source, repo)
    save_context_target(repo, path, data, project_target)
    print(f"Updated pattern: {args.name}")
    print(f"Canonical context: {path}")


def add_question(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    path, data, applicability, project_target = context_write_target(
        repo, args.applicability
    )
    record = find_record(data["open_questions"], "question", args.question)
    if record is None:
        record = {"question": args.question, "status": "open"}
        data["open_questions"].append(record)

    set_if_present(record, "context", args.context)
    set_applicability(record, applicability)
    stamp = now_iso()
    record.setdefault("created_at", stamp)
    record["updated_at"] = stamp
    add_provenance(record, repo, "user-confirmed", "recorded")
    save_context_target(repo, path, data, project_target)
    print(f"Recorded question: {args.question}")
    print(f"Canonical context: {path}")


def remove_entry(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    collection_name, key, label = REMOVE_TARGETS[args.type]
    if args.applicability is None and not context_path(repo).exists():
        raise SystemExit(f"No {label} found for {args.value!r}")
    path, data, _, project_target = context_write_target(repo, args.applicability)
    records = data[collection_name]
    record = find_record(records, key, args.value)
    if record is None:
        raise SystemExit(f"No {label} found for {args.value!r}")

    records.remove(record)
    save_context_target(repo, path, data, project_target)
    print(f"Removed {label}: {record.get(key, args.value)}")


def context_record_locations(
    repo: Path,
    collection: str,
    key: str,
    value: str,
) -> list[tuple[Path, dict[str, Any], bool, dict[str, Any]]]:
    locations: list[tuple[Path, dict[str, Any], bool, dict[str, Any]]] = []
    project_data = load_context(repo)
    project_record = find_record(project_data[collection], key, value)
    if project_record is not None:
        locations.append((context_path(repo), project_data, True, project_record))
    for path in scope_context_files():
        data = load_discovered_scope_context(path)
        record = find_record(data[collection], key, value)
        if record is not None:
            locations.append((path, data, False, record))
    return locations


def move_entry(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    target_path, target_data, applicability, project_target = context_write_target(
        repo, args.applicability
    )
    if applicability is None:
        raise SystemExit("move requires an explicit --applicability target.")
    collection, key, label = REMOVE_TARGETS[args.type]
    locations = context_record_locations(repo, collection, key, args.value)
    target_record = find_record(target_data[collection], key, args.value)
    sources = tuple(location for location in locations if location[0] != target_path)
    if not sources:
        if target_record is not None:
            print(f"{label.title()} already stored at: {target_path}")
            return
        raise SystemExit(f"No {label} found for {args.value!r}")

    source_record = deepcopy(sources[0][3])
    source_ids = {str(location[3].get("id", "")) for location in sources}
    if len(source_ids) != 1:
        raise SystemExit(
            f"Conflicting copies of {label} {args.value!r}; consolidate them before moving."
        )
    if target_record is not None and target_record.get("id") != source_record.get("id"):
        raise SystemExit(
            f"Target already contains a different {label} named {args.value!r}."
        )

    set_applicability(source_record, applicability)
    add_provenance(source_record, repo, args.source, "moved")
    if target_record is None:
        target_data[collection].append(source_record)
    else:
        target_data[collection][target_data[collection].index(target_record)] = (
            source_record
        )
    save_context_target(repo, target_path, target_data, project_target)

    for source_path, source_data, source_is_project, record in sources:
        source_data[collection].remove(record)
        save_context_target(repo, source_path, source_data, source_is_project)
    print(f"Moved {label}: {args.value}")
    print(f"Canonical context: {target_path}")


def git_store_manifest_path(root: Path) -> Path:
    return root / GIT_STORE_MANIFEST_FILE


def validate_git_store_root(value: Path) -> Path:
    root = value.expanduser().resolve()
    if not root.is_dir() or not is_git_initialized(root):
        raise SystemExit(f"Git context store must be a Git checkout root: {root}")
    checkout = worktree_root(root)
    if checkout != root:
        raise SystemExit(f"Git context store must be a Git checkout root: {root}")
    return root


def load_git_store_manifest(root: Path) -> dict[str, Any]:
    path = git_store_manifest_path(root)
    validate_git_store_target(root, path)
    if not path.exists():
        return {
            "schema_version": GIT_STORE_SCHEMA_VERSION,
            "store_id": str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"project-context-store:{root}")
            ),
            "projects": {},
            "domains": {},
            "domain_remotes": {},
        }
    manifest = read_json_object(path)
    if manifest.get("schema_version") != GIT_STORE_SCHEMA_VERSION:
        raise SystemExit(f"Unsupported Git context store manifest: {path}")
    try:
        manifest["store_id"] = str(uuid.UUID(str(manifest.get("store_id"))))
    except ValueError:
        raise SystemExit(f"Invalid Git context store id in {path}") from None
    projects = manifest.get("projects")
    if not isinstance(projects, dict):
        raise SystemExit(f"Invalid Git context store project catalog in {path}")
    for project_id, metadata in projects.items():
        try:
            uuid.UUID(str(project_id))
        except ValueError:
            raise SystemExit(
                f"Invalid project store id {project_id!r} in {path}"
            ) from None
        if not isinstance(metadata, dict):
            raise SystemExit(f"Invalid project metadata for {project_id!r} in {path}")
        remote_url = metadata.get("remote_url")
        if remote_url is not None and normalize_remote_url(remote_url) != remote_url:
            raise SystemExit(
                f"Invalid project remote_url for {project_id!r} in {path}"
            )
    domains = manifest.setdefault("domains", {})
    if not isinstance(domains, dict):
        raise SystemExit(f"Invalid Git context store domain catalog in {path}")
    for domain_id, members in domains.items():
        validate_domain_id(str(domain_id))
        if not isinstance(members, list) or any(
            str(member) not in projects for member in members
        ):
            raise SystemExit(f"Invalid domain membership for {domain_id!r} in {path}")
    domain_remotes = manifest.setdefault("domain_remotes", {})
    if not isinstance(domain_remotes, dict):
        raise SystemExit(f"Invalid Git context store domain remotes in {path}")
    for domain_id, remotes in domain_remotes.items():
        validate_domain_id(str(domain_id))
        if not isinstance(remotes, list) or any(
            normalize_remote_url(remote) != remote for remote in remotes
        ):
            raise SystemExit(f"Invalid domain remotes for {domain_id!r} in {path}")
    return manifest


def path_content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def context_record_count(data: dict[str, Any]) -> int:
    return sum(len(data.get(collection, [])) for collection in RECORD_KEYS)


def record_applicability_values(data: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = [data.get("default_applicability")]
    for collection in RECORD_KEYS:
        values.extend(
            record.get("applicability", data.get("default_applicability"))
            for record in data.get(collection, [])
            if isinstance(record, dict)
        )
    return tuple(values)


def legacy_workspace_records(data: dict[str, Any]) -> tuple[str, ...]:
    labels: list[str] = []
    for collection, key in RECORD_KEYS.items():
        for record in data.get(collection, []):
            if not isinstance(record, dict):
                continue
            applicability = record.get(
                "applicability", data.get("default_applicability")
            )
            if any(
                kind == "workspace" for kind, _ in applicability_pairs(applicability)
            ):
                labels.append(f"{collection}:{record.get(key, '')}")
    return tuple(labels)


def contexts_have_same_records(
    source: dict[str, Any],
    target: dict[str, Any],
) -> bool:
    return source.get("store_id") == target.get("store_id") and all(
        source.get(collection, []) == target.get(collection, [])
        for collection in RECORD_KEYS
    )


def discover_local_project_contexts(
    roots: tuple[Path, ...],
    current_repo: Path,
    store_root: Path,
) -> tuple[tuple[Path, Path], ...]:
    discovered: dict[Path, Path] = {}
    scan_roots = tuple(dict.fromkeys((*roots, current_repo)))
    for raw_root in scan_roots:
        root = raw_root.expanduser().resolve()
        if not root.is_dir():
            continue
        if root == current_repo and local_context_path(current_repo).is_file():
            discovered[local_context_path(current_repo).resolve()] = current_repo
        for directory, names, files in os.walk(root, followlinks=False):
            current = Path(directory)
            try:
                current.resolve().relative_to(store_root)
            except ValueError:
                pass
            else:
                names[:] = []
                continue
            names[:] = sorted(
                name for name in names if name not in DISCOVERY_SKIPPED_DIRECTORIES
            )
            if (
                current.name != "context"
                or current.parent.name != "docs"
                or "context.json" not in files
            ):
                continue
            candidate = current / "context.json"
            if candidate.is_symlink():
                names[:] = []
                continue
            resolved = candidate.resolve()
            project = resolved.parents[2]
            if (project / IGNORE_MARKER).exists():
                names[:] = []
                continue
            discovered.setdefault(resolved, project.resolve())
            names[:] = []
    return tuple(sorted(discovered.items(), key=lambda item: str(item[0]).casefold()))


def git_store_migration_plan(
    repo: Path,
    store_root: Path,
    roots: tuple[Path, ...],
) -> dict[str, Any]:
    config = global_config()
    configured = configured_git_store(config)
    if configured is not None and Path(configured["path"]) != store_root:
        raise SystemExit(
            f"A different Git context store is already configured: {configured['path']}"
        )
    upstream = git_store_upstream(store_root)
    manifest = load_git_store_manifest(store_root)
    project_moves: list[dict[str, Any]] = []
    blockers: list[str] = []
    seen_project_ids: dict[str, Path] = {}
    for source, project in discover_local_project_contexts(roots, repo, store_root):
        data, _ = load_context_file(source)
        workspace_labels = legacy_workspace_records(data)
        if workspace_labels:
            blockers.append(f"{source}: {', '.join(workspace_labels)}")
            continue
        invalid_values = tuple(
            value
            for value in record_applicability_values(data)
            if applicability_pairs(value)
            not in (
                (("project", "self"),),
                (("project", str(project)),),
            )
        )
        if invalid_values:
            blockers.append(
                f"{source}: contains non-project records; move them to an explicit scope first"
            )
            continue
        project_id = str(data["store_id"])
        previous = seen_project_ids.get(project_id)
        if previous is not None and previous != source:
            raise SystemExit(
                f"Project context store id {project_id} is duplicated in {previous} and {source}."
            )
        seen_project_ids[project_id] = source
        target = git_store_project_path(store_root, project_id)
        validate_git_store_target(store_root, target)
        if target.exists():
            target_data, _ = load_context_file(target)
            if not contexts_have_same_records(data, target_data):
                raise SystemExit(
                    f"Git context target conflicts with {source}: {target}"
                )
        project_moves.append(
            {
                "source": source,
                "target": target,
                "project": project,
                "project_id": project_id,
                "data": data,
            }
        )

    scope_moves: list[dict[str, Any]] = []
    private_count = 0
    xdg_root = scope_context_root().resolve()
    if xdg_root.is_dir():
        for source in sorted(xdg_root.rglob("context.json"), key=str):
            if not source.is_file() or source.is_symlink():
                continue
            data = load_discovered_scope_context(source)
            boundary = validate_scope_context(data, source)
            if context_record_count(data) == 0:
                continue
            workspace_labels = legacy_workspace_records(data)
            if workspace_labels:
                blockers.append(f"{source}: {', '.join(workspace_labels)}")
                continue
            if not applicability_is_shareable(boundary):
                private_count += 1
                continue
            target = (
                store_root
                / GIT_STORE_SCOPES_DIR
                / source.resolve().relative_to(xdg_root)
            )
            validate_git_store_target(store_root, target)
            if target.exists():
                target_data = load_discovered_scope_context(target)
                if not contexts_have_same_records(data, target_data):
                    raise SystemExit(
                        f"Git context target conflicts with {source}: {target}"
                    )
            scope_moves.append({"source": source, "target": target, "data": data})

    if blockers:
        raise SystemExit(
            "Reclassify legacy workspace applicability with move before Git "
            "store migration:\n" + "\n".join(blockers)
        )
    token_payload = {
        "source_mode": storage_runtime_mode(config),
        "target_mode": "git-store",
        "config_hash": path_content_hash(global_config_dir() / GLOBAL_CONFIG_FILE),
        "store": str(store_root),
        "store_id": manifest["store_id"],
        "manifest_hash": path_content_hash(git_store_manifest_path(store_root)),
        "upstream": {
            "remote": upstream.remote,
            "branch": upstream.branch,
            "push_url_hash": hashlib.sha256(upstream.push_url.encode()).hexdigest(),
        },
        "projects": [
            {
                "project": str(item["project"]),
                "source": str(item["source"]),
                "source_hash": path_content_hash(item["source"]),
                "target": str(item["target"]),
                "target_hash": path_content_hash(item["target"]),
            }
            for item in project_moves
        ],
        "scopes": [
            {
                "source": str(item["source"]),
                "source_hash": path_content_hash(item["source"]),
                "target": str(item["target"]),
                "target_hash": path_content_hash(item["target"]),
            }
            for item in scope_moves
        ],
    }
    token = hashlib.sha256(
        json.dumps(token_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "source_mode": storage_runtime_mode(config),
        "target_mode": "git-store",
        "store_root": store_root,
        "manifest": manifest,
        "project_moves": tuple(project_moves),
        "scope_moves": tuple(scope_moves),
        "private_count": private_count,
        "upstream": upstream,
        "token": token,
    }


def validate_local_storage_target(root: Path, path: Path) -> None:
    canonical_root = root.resolve()
    candidate = path.absolute()
    try:
        relative = candidate.relative_to(canonical_root)
    except ValueError:
        raise SystemExit(
            f"Local context target is outside its root: {candidate}"
        ) from None
    current = canonical_root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise SystemExit(f"Local context target traverses a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise SystemExit(
                f"Local context target parent is not a directory: {current}"
            )
    if candidate.is_symlink():
        raise SystemExit(f"Local context target is a symlink: {candidate}")
    if candidate.exists() and not candidate.is_file():
        raise SystemExit(f"Local context target is not a file: {candidate}")


def bound_projects_by_store_id(
    configured: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Path]:
    candidates: dict[str, set[Path]] = {
        project_id: set() for project_id in manifest["projects"]
    }
    for raw_project, project_id in configured["project_bindings"].items():
        project = Path(raw_project)
        if project_id in candidates and project.is_dir():
            candidates[project_id].add(context_repo(project))
    invalid = {
        project_id: tuple(sorted(projects, key=str))
        for project_id, projects in candidates.items()
        if len(projects) != 1
    }
    if invalid:
        details = ", ".join(
            f"{project_id}={len(projects)}"
            for project_id, projects in sorted(invalid.items())
        )
        raise SystemExit(
            "Every Git-store project requires exactly one local checkout binding "
            f"before migration to local mode: {details}. Use git-store-bind on this machine."
        )
    return {
        project_id: next(iter(projects)) for project_id, projects in candidates.items()
    }


def local_storage_migration_plan(
    project_visibility: str,
) -> dict[str, Any]:
    config = global_config()
    source_mode = storage_runtime_mode(config)
    if source_mode != "git-store":
        payload = {
            "source_mode": source_mode,
            "target_mode": "local",
            "project_visibility": project_visibility,
            "config_hash": path_content_hash(global_config_dir() / GLOBAL_CONFIG_FILE),
            "projects": [],
            "scopes": [],
        }
        token = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        return {
            "source_mode": source_mode,
            "target_mode": "local",
            "project_visibility": project_visibility,
            "project_moves": (),
            "scope_moves": (),
            "store_root": None,
            "token": token,
        }

    configured, root, manifest = require_configured_git_store()
    upstream = git_store_upstream(root)
    projects_by_id = bound_projects_by_store_id(configured, manifest)
    project_moves: list[dict[str, Any]] = []
    blockers: list[str] = []
    target_projects: set[Path] = set()
    for project_id in sorted(manifest["projects"]):
        project = projects_by_id[project_id]
        if project in target_projects:
            raise SystemExit(
                f"Multiple Git-store projects resolve to one local checkout: {project}"
            )
        target_projects.add(project)
        if project_visibility == "versioned" and not is_git_initialized(project):
            raise SystemExit(
                f"Versioned project context requires a Git checkout: {project}"
            )
        source = git_store_project_path(root, project_id)
        validate_git_store_target(root, source)
        if not source.is_file():
            raise SystemExit(f"Canonical project context is missing: {source}")
        data, _ = load_context_file(source)
        workspace_labels = legacy_workspace_records(data)
        if workspace_labels:
            blockers.append(f"{source}: {', '.join(workspace_labels)}")
            continue
        target = local_context_path(project)
        validate_local_storage_target(project, target)
        if target.exists():
            target_data, _ = load_context_file(target)
            if not contexts_have_same_records(data, target_data):
                raise SystemExit(
                    f"Local context target conflicts with {source}: {target}"
                )
        project_moves.append(
            {
                "source": source,
                "target": target,
                "project": project,
                "project_id": project_id,
                "data": data,
            }
        )

    scope_moves: list[dict[str, Any]] = []
    git_scopes = root / GIT_STORE_SCOPES_DIR
    if git_scopes.is_dir():
        resolved_scopes = git_scopes.resolve()
        for source in sorted(git_scopes.rglob("context.json"), key=str):
            if (
                not source.is_file()
                or source.is_symlink()
                or resolved_scopes not in source.resolve().parents
            ):
                continue
            data = load_discovered_scope_context(source)
            workspace_labels = legacy_workspace_records(data)
            if workspace_labels:
                blockers.append(f"{source}: {', '.join(workspace_labels)}")
                continue
            target = scope_context_root() / source.resolve().relative_to(
                resolved_scopes
            )
            validate_local_storage_target(scope_context_root(), target)
            if target.exists():
                target_data = load_discovered_scope_context(target)
                if not contexts_have_same_records(data, target_data):
                    raise SystemExit(
                        f"Local scope target conflicts with {source}: {target}"
                    )
            scope_moves.append({"source": source, "target": target, "data": data})

    if blockers:
        raise SystemExit(
            "Reclassify legacy workspace applicability with move before local "
            "storage migration:\n" + "\n".join(blockers)
        )
    token_payload = {
        "source_mode": source_mode,
        "target_mode": "local",
        "project_visibility": project_visibility,
        "config_hash": path_content_hash(global_config_dir() / GLOBAL_CONFIG_FILE),
        "store": str(root),
        "manifest_hash": path_content_hash(git_store_manifest_path(root)),
        "upstream": {
            "remote": upstream.remote,
            "branch": upstream.branch,
            "push_url_hash": hashlib.sha256(upstream.push_url.encode()).hexdigest(),
        },
        "projects": [
            {
                "project": str(item["project"]),
                "source": str(item["source"]),
                "source_hash": path_content_hash(item["source"]),
                "target": str(item["target"]),
                "target_hash": path_content_hash(item["target"]),
            }
            for item in project_moves
        ],
        "scopes": [
            {
                "source": str(item["source"]),
                "source_hash": path_content_hash(item["source"]),
                "target": str(item["target"]),
                "target_hash": path_content_hash(item["target"]),
            }
            for item in scope_moves
        ],
    }
    token = hashlib.sha256(
        json.dumps(token_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "source_mode": source_mode,
        "target_mode": "local",
        "project_visibility": project_visibility,
        "project_moves": tuple(project_moves),
        "scope_moves": tuple(scope_moves),
        "store_root": root,
        "upstream": upstream,
        "token": token,
    }


def snapshot_path_value(path: Path) -> str:
    value = str(path)
    if not value or len(value) > SNAPSHOT_PATH_LIMIT:
        raise SystemExit("Git context migration path is empty or too long.")
    return value


def print_git_store_preview(root: Path, plan: dict[str, Any]) -> None:
    upstream = plan["upstream"]
    print(
        json.dumps(
            {
                "type": UNTRUSTED_SNAPSHOT_TYPE,
                "change": "git_store",
                "store_path": snapshot_path_value(root),
                "push_remote": safe_display_field(
                    upstream.remote, GLOBAL_LABEL_OUTPUT_LIMIT
                ),
                "push_branch": safe_display_field(
                    upstream.branch, GLOBAL_LABEL_OUTPUT_LIMIT
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    print(f"Project contexts to move: {len(plan['project_moves'])}")
    for item in plan["project_moves"]:
        print(
            json.dumps(
                {
                    "type": UNTRUSTED_SNAPSHOT_TYPE,
                    "change": "move",
                    "kind": "project",
                    "source_path": snapshot_path_value(item["source"]),
                    "target_path": snapshot_path_value(item["target"]),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    print(f"Shareable scope contexts to move: {len(plan['scope_moves'])}")
    for item in plan["scope_moves"]:
        print(
            json.dumps(
                {
                    "type": UNTRUSTED_SNAPSHOT_TYPE,
                    "change": "move",
                    "kind": "scope",
                    "source_path": snapshot_path_value(item["source"]),
                    "target_path": snapshot_path_value(item["target"]),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    print(f"Private XDG scope contexts retained: {plan['private_count']}")
    print(f"Snapshot token: {plan['token']}")
    print("No changes made. Ask the user to approve this exact snapshot token.")


def print_storage_migration_preview(plan: dict[str, Any]) -> None:
    event: dict[str, Any] = {
        "type": UNTRUSTED_SNAPSHOT_TYPE,
        "change": "storage_runtime",
        "source_mode": plan["source_mode"],
        "target_mode": plan["target_mode"],
    }
    if plan.get("project_visibility") is not None:
        event["project_visibility"] = plan["project_visibility"]
    store_root = plan.get("store_root")
    if isinstance(store_root, Path):
        event["store_path"] = snapshot_path_value(store_root)
    upstream = plan.get("upstream")
    if isinstance(upstream, GitUpstream):
        event["push_remote"] = safe_display_field(
            upstream.remote, GLOBAL_LABEL_OUTPUT_LIMIT
        )
        event["push_branch"] = safe_display_field(
            upstream.branch, GLOBAL_LABEL_OUTPUT_LIMIT
        )
    print(json.dumps(event, ensure_ascii=True, sort_keys=True))
    print(f"Project contexts to move: {len(plan['project_moves'])}")
    for item in plan["project_moves"]:
        print(
            json.dumps(
                {
                    "type": UNTRUSTED_SNAPSHOT_TYPE,
                    "change": "move",
                    "kind": "project",
                    "source_path": snapshot_path_value(item["source"]),
                    "target_path": snapshot_path_value(item["target"]),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    print(f"Shareable scope contexts to move: {len(plan['scope_moves'])}")
    for item in plan["scope_moves"]:
        print(
            json.dumps(
                {
                    "type": UNTRUSTED_SNAPSHOT_TYPE,
                    "change": "move",
                    "kind": "scope",
                    "source_path": snapshot_path_value(item["source"]),
                    "target_path": snapshot_path_value(item["target"]),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    if "private_count" in plan:
        print(f"Private XDG scope contexts retained: {plan['private_count']}")
    print(f"Snapshot token: {plan['token']}")
    print("No changes made. Ask the user to approve this exact snapshot token.")


def git_project_metadata(
    repo: Path, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    stamp = now_iso()
    metadata = {
        "name": repo.name,
        "created_at": existing.get("created_at", stamp) if existing else stamp,
        "updated_at": stamp,
    }
    remote_url = repo_remote_url(repo)
    if remote_url is None and existing is not None:
        remote_url = existing.get("remote_url")
    if isinstance(remote_url, str) and remote_url:
        metadata["remote_url"] = remote_url
    return metadata


def git_store_projects_by_remote(
    manifest: dict[str, Any], remote_url: str | None
) -> tuple[str, ...]:
    if remote_url is None:
        return ()
    return tuple(
        sorted(
            project_id
            for project_id, metadata in manifest["projects"].items()
            if metadata.get("remote_url") == remote_url
        )
    )


def refresh_git_store_project_metadata(
    repo: Path,
    project_id: str,
    root: Path,
    manifest: dict[str, Any],
) -> bool:
    existing = manifest["projects"].get(project_id)
    if not isinstance(existing, dict):
        return False
    remote_url = repo_remote_url(repo)
    if remote_url is None or existing.get("remote_url") == remote_url:
        return False
    duplicates = tuple(
        other
        for other in git_store_projects_by_remote(manifest, remote_url)
        if other != project_id
    )
    if duplicates:
        print(
            f"WARNING: remote {remote_url} is also registered as project context "
            + ", ".join(duplicates)
            + ". Consider consolidating with git-store-bind --match-remote."
        )
    manifest["projects"][project_id] = git_project_metadata(repo, existing)
    manifest["updated_at"] = now_iso()
    write_git_json(root, git_store_manifest_path(root), manifest)
    return True


def refresh_bound_git_store_project_metadata(repo: Path) -> None:
    configured = configured_git_store()
    if configured is None:
        return
    project_id = configured["project_bindings"].get(str(repo.resolve()))
    if project_id is None:
        return
    root = validate_git_store_root(Path(configured["path"]))
    manifest = load_git_store_manifest(root)
    refresh_git_store_project_metadata(repo, project_id, root, manifest)


def restore_git_store_domains(
    repo: Path, project_id: str, manifest: dict[str, Any]
) -> None:
    config = global_config()
    domains = configured_domains(config)
    remote_url = repo_remote_url(repo)
    for domain_id in sorted({*manifest["domains"], *manifest["domain_remotes"]}):
        member_ids = manifest["domains"].get(domain_id, [])
        remotes = manifest["domain_remotes"].get(domain_id, [])
        if project_id not in member_ids and remote_url not in remotes:
            continue
        current = domains.get(domain_id, DomainMembers())
        projects = set(current.projects)
        if project_id in member_ids:
            projects.add(repo.resolve())
        domains[domain_id] = DomainMembers(
            projects=tuple(sorted(projects, key=str)),
            remotes=tuple(sorted({*current.remotes, *remotes})),
        )
    config["domains"] = domains_config(domains)
    write_global_config(config)


def configure_git_store(
    root: Path,
    manifest: dict[str, Any],
    bindings: dict[str, str],
) -> None:
    stamp = now_iso()
    config = global_config()
    previous = configured_git_store(config)
    config["git_store"] = {
        "enabled": True,
        "path": str(root),
        "store_id": manifest["store_id"],
        "project_bindings": dict(sorted(bindings.items())),
        "created_at": previous.get("created_at", stamp) if previous else stamp,
        "updated_at": stamp,
    }
    set_storage_runtime(config, "git-store")
    write_global_config(config)


def apply_git_store_migration(root: Path, plan: dict[str, Any]) -> None:
    manifest = deepcopy(plan["manifest"])
    manifest.setdefault("created_at", now_iso())
    manifest["updated_at"] = now_iso()
    projects = manifest["projects"]
    config = global_config()
    current = configured_git_store(config)
    bindings = dict(current["project_bindings"]) if current else {}
    rendered: list[tuple[Path, dict[str, Any]]] = []
    for item in plan["project_moves"]:
        project = item["project"]
        data = deepcopy(item["data"])
        existing_policy = data.get("storage_policy")
        data["storage_policy"] = storage_policy(
            "git-store",
            "user-confirmed",
            is_git_initialized(project),
            existing_policy if isinstance(existing_policy, dict) else None,
        )
        normalize(data)
        write_git_json(root, item["target"], data)
        project_id = item["project_id"]
        existing_metadata = projects.get(project_id)
        projects[project_id] = git_project_metadata(
            project,
            existing_metadata if isinstance(existing_metadata, dict) else None,
        )
        bindings[str(project.resolve())] = project_id
        rendered.append((project, data))
    for item in plan["scope_moves"]:
        data = deepcopy(item["data"])
        normalize(data)
        metadata = data.get("scope_store")
        if isinstance(metadata, dict):
            metadata["updated_at"] = now_iso()
        write_git_json(root, item["target"], data)
    for domain_id, members in configured_domains(config).items():
        member_ids = tuple(
            bindings.get(str(member.resolve())) for member in members.projects
        )
        if member_ids and all(member_ids):
            manifest["domains"][domain_id] = sorted(set(member_ids))
        if members.remotes:
            manifest["domain_remotes"][domain_id] = list(members.remotes)
    write_git_json(root, git_store_manifest_path(root), manifest)
    configure_git_store(root, manifest, bindings)
    for item in (*plan["project_moves"], *plan["scope_moves"]):
        source = item["source"]
        if source != item["target"] and source.exists():
            source.unlink()
    for project, data in rendered:
        ensure_context_gitignore(project / CONTEXT_DIR)
        apply_storage_policy(project, data)
        render_markdown(project, data)


def write_local_project_context(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def apply_local_storage_migration(plan: dict[str, Any]) -> None:
    visibility = str(plan["project_visibility"])
    rendered: list[tuple[Path, dict[str, Any]]] = []
    for item in plan["project_moves"]:
        project = item["project"]
        data = deepcopy(item["data"])
        existing_policy = data.get("storage_policy")
        data["storage_policy"] = storage_policy(
            visibility,
            "user-confirmed",
            is_git_initialized(project),
            existing_policy if isinstance(existing_policy, dict) else None,
        )
        normalize(data)
        write_local_project_context(item["target"], data)
        rendered.append((project, data))
    for item in plan["scope_moves"]:
        data = deepcopy(item["data"])
        normalize(data)
        metadata = data.get("scope_store")
        if isinstance(metadata, dict):
            metadata["updated_at"] = now_iso()
        write_json_object(item["target"], data)

    config = global_config()
    config.pop("git_store", None)
    set_storage_runtime(config, "local", visibility)
    write_global_config(config)
    for project, data in rendered:
        ensure_context_gitignore(project / CONTEXT_DIR)
        apply_storage_policy(project, data)
        render_markdown(project, data)
    for item in (*plan["project_moves"], *plan["scope_moves"]):
        source = item["source"]
        if source != item["target"] and source.exists():
            source.unlink()
    store_root = plan.get("store_root")
    if isinstance(store_root, Path):
        manifest = git_store_manifest_path(store_root)
        if manifest.exists():
            manifest.unlink()


def storage_status(args: argparse.Namespace) -> None:
    config = global_config()
    mode = storage_runtime_mode(config)
    runtime = configured_storage_runtime(config)
    payload: dict[str, Any] = {
        "configured": mode != "unconfigured",
        "mode": mode,
    }
    if mode == "local" and runtime is not None:
        payload["project_visibility"] = runtime["project_visibility"]
    if mode == "git-store":
        store = configured_git_store(config)
        if store is not None:
            payload["store_path"] = store["path"]
            payload["store_id"] = store["store_id"]
    if args.format == "json":
        print(json.dumps(payload, sort_keys=True))
        return
    if mode == "unconfigured":
        print("Storage runtime mode: unconfigured (local compatibility mode).")
        if args.format == "hook":
            print(
                "Storage runtime selection required before initial setup. Invoke "
                "$configure-context-storage and ask the user to choose local or "
                "git-store mode."
            )
        return
    if mode == "local":
        print(
            "Storage runtime mode: local "
            f"(new project visibility: {payload['project_visibility']})."
        )
        return
    print(
        f"Storage runtime mode: git-store (canonical store: {payload['store_path']})."
    )


def storage_migrate(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    if args.target == "git-store":
        if args.store is None:
            raise SystemExit("Git-store migration requires --store.")
        if args.project_visibility is not None:
            raise SystemExit(
                "--project-visibility applies only when the target mode is local."
            )
        root = validate_git_store_root(args.store)
        configured_roots = workspace_roots(global_config())
        roots = (
            validate_workspace_roots(args.workspace_root)
            if args.workspace_root
            else configured_roots
        )
        plan = git_store_migration_plan(repo, root, roots)
        if not args.approve_snapshot:
            print_storage_migration_preview(plan)
            return
        validate_snapshot_approval(
            {"snapshot": plan["token"]},
            args.approve_snapshot,
        )
        apply_git_store_migration(root, plan)
    else:
        if args.store is not None:
            raise SystemExit("Local storage migration does not accept --store.")
        if args.workspace_root:
            raise SystemExit(
                "Local storage migration uses configured Git-store bindings and does "
                "not accept --workspace-root."
            )
        if args.project_visibility is None:
            raise SystemExit(
                "Local storage migration requires --project-visibility local or versioned."
            )
        plan = local_storage_migration_plan(args.project_visibility)
        if not args.approve_snapshot:
            print_storage_migration_preview(plan)
            return
        validate_snapshot_approval(
            {"snapshot": plan["token"]},
            args.approve_snapshot,
        )
        apply_local_storage_migration(plan)
    print(f"Storage runtime configured: {args.target}")
    print(f"Project contexts moved: {len(plan['project_moves'])}")
    print(f"Shareable scope contexts moved: {len(plan['scope_moves'])}")


def git_store_init(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    root = validate_git_store_root(args.store)
    configured_roots = workspace_roots(global_config())
    roots = (
        validate_workspace_roots(args.workspace_root)
        if args.workspace_root
        else configured_roots
    )
    plan = git_store_migration_plan(repo, root, roots)
    if not args.approve_snapshot:
        print_git_store_preview(root, plan)
        return
    validate_snapshot_approval(
        {"snapshot": plan["token"]},
        args.approve_snapshot,
    )
    apply_git_store_migration(root, plan)
    print(f"Git context store configured: {root}")
    print(f"Project contexts moved: {len(plan['project_moves'])}")
    print(f"Shareable scope contexts moved: {len(plan['scope_moves'])}")


def require_configured_git_store() -> tuple[dict[str, Any], Path, dict[str, Any]]:
    configured = configured_git_store()
    if configured is None:
        raise SystemExit(
            "No Git context store is configured. Invoke $configure-context-storage "
            "and approve the exact storage-migrate snapshot first."
        )
    root = validate_git_store_root(Path(configured["path"]))
    manifest = load_git_store_manifest(root)
    if manifest["store_id"] != configured["store_id"]:
        raise SystemExit(
            "Configured Git context store identity does not match its manifest."
        )
    return configured, root, manifest


def git_store_bind(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    configured, root, manifest = require_configured_git_store()
    if args.match_remote:
        remote_url = repo_remote_url(repo)
        if remote_url is None:
            raise SystemExit("Repository has no Git remote URL to match.")
        matches = git_store_projects_by_remote(manifest, remote_url)
        if len(matches) != 1:
            detail = f": {', '.join(matches)}" if matches else ""
            raise SystemExit(
                f"Remote {remote_url} matches {len(matches)} project contexts{detail}. "
                "Use --project-store-id."
            )
        project_id = matches[0]
    else:
        try:
            project_id = str(uuid.UUID(args.project_store_id))
        except ValueError:
            raise SystemExit(
                f"Invalid project context store id {args.project_store_id!r}."
            ) from None
    if project_id not in manifest["projects"]:
        raise SystemExit(f"Unknown project context store id {project_id}.")
    target = git_store_project_path(root, project_id)
    if not target.is_file():
        raise SystemExit(f"Canonical project context is missing: {target}")
    existing = git_store_project_binding(repo)
    if existing is not None and existing != project_id:
        raise SystemExit(
            f"Repository is already bound to a different project context store: {existing}"
        )
    if local_context_path(repo).exists():
        raise SystemExit(
            "A repository-local context already exists. Use storage-migrate preview and "
            "exact approval to migrate it."
        )
    data, _ = load_context_file(target)
    bindings = dict(configured["project_bindings"])
    bindings[str(repo.resolve())] = project_id
    configure_git_store(root, manifest, bindings)
    restore_git_store_domains(repo, project_id, manifest)
    refresh_git_store_project_metadata(repo, project_id, root, manifest)
    context_dir = repo / CONTEXT_DIR
    context_dir.mkdir(parents=True, exist_ok=True)
    ensure_context_gitignore(context_dir)
    apply_storage_policy(repo, data)
    render_markdown(repo, data)
    print(f"Bound project context: {repo} -> {project_id}")
    print(f"Canonical context: {target}")


def git_store_status(args: argparse.Namespace) -> None:
    configured, root, manifest = require_configured_git_store()
    upstream = git_store_upstream(root)
    print(f"Git context store: {root}")
    print(f"Store id: {manifest['store_id']}")
    print(f"Push upstream: {upstream.remote}/{upstream.branch}")
    print(f"Configured project bindings: {len(configured['project_bindings'])}")
    for project_id, metadata in sorted(manifest["projects"].items()):
        name = safe_display_field(metadata.get("name", ""), GLOBAL_LABEL_OUTPUT_LIMIT)
        remote = safe_display_field(
            metadata.get("remote_url") or "-", GLOBAL_LABEL_OUTPUT_LIMIT
        )
        print(
            f"{project_id} | {name} | {remote} | "
            f"{git_store_project_path(root, project_id)}"
        )
    for domain_id, members in sorted(manifest["domains"].items()):
        print(f"Domain {domain_id}: {', '.join(members)}")
    for domain_id, remotes in sorted(manifest["domain_remotes"].items()):
        print(f"Domain {domain_id} remotes: {', '.join(remotes)}")
    dirty = git_output(root, "status", "--short")
    print(f"Git working tree: {'dirty' if dirty else 'clean'}")


def enroll_empty_git_store_project(repo: Path, data: dict[str, Any]) -> None:
    configured, root, manifest = require_configured_git_store()
    project_id = str(data["store_id"])
    target = git_store_project_path(root, project_id)
    if local_context_path(repo).exists():
        raise SystemExit(
            "A repository-local context already exists. Use storage-migrate preview and "
            "exact approval to migrate it."
        )
    if target.exists():
        raise SystemExit(f"Git context target already exists: {target}")
    remote_url = repo_remote_url(repo)
    duplicates = git_store_projects_by_remote(manifest, remote_url)
    if duplicates:
        raise SystemExit(
            f"Remote {remote_url} is already registered as project context "
            f"{', '.join(duplicates)}. Attach this checkout with "
            "git-store-bind --match-remote instead of initializing a new project."
        )
    existing_policy = data.get("storage_policy")
    data["storage_policy"] = storage_policy(
        "git-store",
        "user-confirmed",
        is_git_initialized(repo),
        existing_policy if isinstance(existing_policy, dict) else None,
    )
    normalize(data)
    write_git_json(root, target, data)
    manifest.setdefault("created_at", now_iso())
    manifest["updated_at"] = now_iso()
    manifest["projects"][project_id] = git_project_metadata(repo)
    write_git_json(root, git_store_manifest_path(root), manifest)
    bindings = dict(configured["project_bindings"])
    bindings[str(repo.resolve())] = project_id
    configure_git_store(root, manifest, bindings)
    restore_git_store_domains(repo, project_id, manifest)


def init_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    git_initialized = is_git_initialized(repo)
    config = global_config()
    runtime_mode = storage_runtime_mode(config)
    runtime = configured_storage_runtime(config)
    visibility = args.visibility
    git_store = configured_git_store(config)
    record_local_runtime = False
    if runtime_mode == "git-store" and visibility not in (None, "git-store"):
        raise SystemExit(
            "Storage runtime mode is git-store; project initialization must use its "
            "canonical Git context repository."
        )
    if runtime_mode == "local" and visibility == "git-store":
        raise SystemExit(
            "Storage runtime mode is local. Use storage-migrate with exact preview "
            "approval before initializing a Git-store project."
        )
    if visibility is None and runtime_mode == "git-store":
        visibility = "git-store"
    if visibility is None and runtime_mode == "local" and runtime is not None:
        visibility = str(runtime["project_visibility"])
    if visibility is None and git_initialized:
        raise SystemExit(
            "Storage runtime decision required for initial setup. Invoke "
            "$configure-context-storage so the user can choose local or git-store mode."
        )
    if visibility is None:
        visibility = "local"
    if runtime_mode == "unconfigured" and visibility in LOCAL_CONTEXT_VISIBILITIES:
        record_local_runtime = args.visibility is not None
    if visibility == "git-store" and git_store is None:
        raise SystemExit(
            "No Git context store is configured. Invoke $configure-context-storage "
            "and approve the exact storage-migrate snapshot first."
        )

    data = load_context(repo)
    default_applicability = parse_applicability(args.default_applicability)
    if default_applicability is not None:
        resolved_default = resolve_applicability(
            default_applicability,
            repo,
            require_domain_membership=True,
        )
        if applicability_pairs(resolved_default) != (("project", str(repo)),):
            raise SystemExit(
                "Repository context defaults to project:self. Store broader facts "
                "with an explicit --applicability so the updater selects its canonical store."
            )
        data["default_applicability"] = default_applicability
    existing_policy = data.get("storage_policy")
    data["storage_policy"] = storage_policy(
        visibility,
        args.source,
        git_initialized,
        existing_policy if isinstance(existing_policy, dict) else None,
    )
    if visibility == "git-store" and git_store_project_binding(repo) is None:
        enroll_empty_git_store_project(repo, data)
    policy_result = save_context(repo, data)
    if record_local_runtime:
        set_storage_runtime(config, "local", visibility)
        write_global_config(config)
    location_label = "local context" if visibility != "git-store" else "project context"
    print(f"Initialized {location_label}: {repo / CONTEXT_DIR}")
    print(f"Context visibility: {visibility}")
    print(f"Git initialized: {git_initialized}")
    if visibility == "local":
        if policy_result == "not-git":
            print("Git exclude: skipped (not a Git repository)")
        else:
            action = (
                "updated" if policy_result == "exclude-added" else "already configured"
            )
            print(f"Git exclude: {action} ({git_exclude_display_path(repo)})")
    elif visibility == "versioned":
        action = (
            "removed docs/context/ entry"
            if policy_result == "exclude-removed"
            else "unchanged"
        )
        print(f"Git exclude: {action} ({git_exclude_display_path(repo)})")
    else:
        print(f"Canonical context: {context_path(repo)}")
        if policy_result != "not-git":
            action = (
                "updated" if policy_result == "exclude-added" else "already configured"
            )
            print(f"Git exclude: {action} ({git_exclude_display_path(repo)})")
    if not (data["terms"] or data["components"] or data["patterns"]):
        print(
            "WARNING: context is empty (0 terms, 0 components, 0 patterns). "
            "The bootstrap repository analysis has not been recorded. Read the "
            "repository README/CLAUDE.md/AGENTS.md, top-level layout, and main "
            "manifests now, then run add-component/add-term/add-pattern for the "
            "verified findings in this same turn. An init left at all-zero counts "
            "means the bootstrap step was skipped."
        )


def update_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    if getattr(args, "if_initialized", False) and not context_path(repo).exists():
        return
    require_initialized_context(repo)
    data, migrations = load_context_with_migrations(repo)
    save_context(repo, data)
    refresh_bound_git_store_project_metadata(repo)
    scope_migrations = migrate_private_scope_contexts()
    applied = ", ".join(migrations) if migrations else "none"
    print(f"Updated project context: {repo / CONTEXT_DIR}")
    print(f"Schema version: {SCHEMA_VERSION}")
    print(f"Migrations applied: {applied}")
    if scope_migrations:
        print(f"Scoped migrations applied: {', '.join(scope_migrations)}")
    print("Generated views: refreshed")


def status_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    path = context_path(repo)
    if not path.exists():
        print("No docs/context/context.json exists yet.")
        git_root = configured_git_store_root()
        if git_root is not None:
            print(
                f"Git context store configured: {git_root}. New project context "
                "initialization uses it automatically."
            )
        return
    data = load_context(repo)
    open_questions = [
        question
        for question in data.get("open_questions", [])
        if isinstance(question, dict) and question.get("status", "open") == "open"
    ]
    print(
        f"Existing context counts: {len(data.get('terms', []))} terms, "
        f"{len(data.get('components', []))} components, "
        f"{len(data.get('patterns', []))} patterns, "
        f"{len(open_questions)} open questions."
    )
    if path != local_context_path(repo):
        print(f"Canonical context: {path}")
    for summary in active_shared_scope_contexts(repo):
        print(
            f"Scoped context {summary.label}: {summary.counts()} "
            f"(canonical: {summary.path}; not in docs/context views, use search)."
        )


def ignore_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    marker = ignore_marker_path(repo)
    if context_path(repo).exists():
        raise SystemExit(
            f"Context is already initialized at {context_path(repo)}. Remove docs/context first "
            f"if this repository should be ignored with {IGNORE_MARKER}."
        )
    marker.write_text(
        "Project Context Curator is disabled for this repository.\n",
        encoding="utf-8",
    )
    print(f"Project context disabled: {marker}")


def known_terms(data: dict[str, Any]) -> set[str]:
    known: set[str] = set()
    for term in data["terms"]:
        known.add(str(term["term"]).casefold())
        for alias in term.get("aliases", []):
            known.add(str(alias).casefold())
    for component in data["components"]:
        known.add(str(component["name"]).casefold())
    return known


def extract_candidates(text: str) -> list[str]:
    candidates: set[str] = set()

    for match in re.finditer(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)?\b", text):
        token = match.group(0)
        if token not in {
            "HTTP",
            "HTTPS",
            "JSON",
            "YAML",
            "XML",
            "API",
            "URL",
            "URI",
            "SQL",
        }:
            candidates.add(token)

    for match in re.finditer(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){1,}\b", text):
        candidates.add(match.group(0))

    return sorted(candidates, key=str.casefold)


def scan_text(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    data = load_context(repo)
    known = known_terms(data)
    candidates = [
        candidate
        for candidate in extract_candidates(args.text)
        if candidate.casefold() not in known
    ]
    for candidate in candidates:
        print(candidate)


def scan_file(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    file_path = args.file
    if not file_path.is_absolute():
        file_path = repo / file_path
    if not file_path.exists():
        raise SystemExit(f"File not found: {file_path}")
    args.text = file_path.read_text(encoding="utf-8", errors="replace")
    args.repo = repo
    scan_text(args)


def one_line(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(one_line(item) for item in value)
    return " ".join(str(value or "").split()).replace("|", "\\|")


def compact_summary(value: Any, limit: int = 180) -> str:
    text = one_line(value)
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def normalized_queries(values: list[str]) -> tuple[str, ...]:
    queries = (one_line(value).casefold() for value in values)
    return tuple(dict.fromkeys(query for query in queries if query))


def context_search_results(
    data: dict[str, Any],
    queries: tuple[str, ...],
    *,
    canonical_source: Path,
    active: frozenset[tuple[str, str]],
    scoped_store: bool = False,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for kind, collection, label_key, summary_key, fields in SEARCH_SPECS:
        for record in data[collection]:
            applicability = record.get("applicability", data["default_applicability"])
            if not applicability_matches(applicability, active):
                continue
            haystack = "\n".join(
                one_line(record.get(field)) for field in fields
            ).casefold()
            matched = tuple(query for query in queries if query in haystack)
            if not matched:
                continue
            results.append(
                (
                    len(matched),
                    kind,
                    one_line(record.get(label_key)),
                    str(canonical_source) if scoped_store else SEARCH_FILES[kind],
                    str(canonical_source),
                    one_line(record.get(summary_key)),
                    matched,
                )
            )
    return sorted(
        results, key=lambda result: (-result[0], result[1], result[2].casefold())
    )


def search_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    queries = normalized_queries(args.query)
    if not queries:
        raise SystemExit("Search query must contain at least one non-blank term.")
    if args.limit <= 0:
        raise SystemExit("Search limit must be greater than zero.")

    require_initialized_context(repo)
    global_lines = try_global_search(repo, queries, args.limit)
    active = active_applicability(repo)
    try:
        local_results = context_search_results(
            load_context(repo),
            queries,
            canonical_source=context_path(repo),
            active=active,
        )
    except (OSError, SystemExit) as exc:
        print(
            untrusted_diagnostic_line("invalid local context", exc),
            file=sys.stderr,
        )
        local_results = []
    for path in scope_context_files():
        try:
            data = load_discovered_scope_context(path)
            local_results.extend(
                context_search_results(
                    data,
                    queries,
                    canonical_source=path,
                    active=active,
                    scoped_store=True,
                )
            )
        except (OSError, SystemExit) as exc:
            print(
                untrusted_diagnostic_line(f"invalid scope context {path}", exc),
                file=sys.stderr,
            )
    local_results.sort(key=lambda result: (-result[0], result[1], result[2].casefold()))
    local_lines = [
        " | ".join(
            (
                safe_display_field(kind, GLOBAL_KIND_OUTPUT_LIMIT),
                safe_display_field(label, GLOBAL_LABEL_OUTPUT_LIMIT),
                safe_display_field(path, GLOBAL_PATH_OUTPUT_LIMIT),
                safe_display_field(summary, LOCAL_SUMMARY_OUTPUT_LIMIT),
                "matched: "
                + safe_display_field(", ".join(matched), LOCAL_SUMMARY_OUTPUT_LIMIT),
            )
        )
        for _, kind, label, path, _, summary, matched in local_results
    ]
    local_identities = {
        (
            safe_display_field(source, GLOBAL_PATH_OUTPUT_LIMIT)
            .replace("|", "\\|")
            .casefold(),
            safe_display_field(kind, GLOBAL_KIND_OUTPUT_LIMIT).casefold(),
            safe_display_field(label, GLOBAL_LABEL_OUTPUT_LIMIT).casefold(),
        )
        for _, kind, label, _, source, _, _ in local_results
    }
    remaining_global: list[str] = []
    for line in global_lines or ():
        parts = line.split(" | ")
        identity = (
            (
                parts[4].casefold(),
                parts[2].casefold(),
                parts[3].casefold(),
            )
            if len(parts) >= 5 and parts[0] == GLOBAL_RESULT_PREFIX
            else None
        )
        if identity not in local_identities:
            remaining_global.append(line)
    combined = (*local_lines, *remaining_global)[: args.limit]
    if not combined:
        print(f"No context matches for: {', '.join(queries)}")
        return
    for line in combined:
        print(line)


AUDIT_KINDS = (("term", "terms"), ("component", "components"), ("pattern", "patterns"))
AUDIT_TEXT_FIELDS = ("term", "name", "definition", "summary", "responsibility", "notes")
AUDIT_TIME_BOUND = re.compile(
    r"\b(?:superseded|deprecated|obsolete|no longer|fixed in|as of 20\d\d|temporar\w*|"
    r"not yet|for now|wip|work in progress|in progress|installed-version|checkout state)\b"
    r"|\(20\d\d-\d\d\)|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* 20\d\d\b",
    re.IGNORECASE,
)
AUDIT_MAX_PATTERNS = 200
AUDIT_MAX_INDEX_BYTES = 64 * 1024


@dataclass(frozen=True)
class AuditFinding:
    check: str
    kind: str
    name: str
    store: str
    detail: str
    suggestion: str

    def as_dict(self) -> dict[str, str]:
        return {
            "check": self.check,
            "kind": self.kind,
            "name": self.name,
            "store": self.store,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


def audit_days_since(value: Any, now: datetime) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).days


def audit_record_text(record: dict[str, Any]) -> str:
    return " ".join(one_line(record.get(field)) for field in AUDIT_TEXT_FIELDS)


def audit_store(
    label: str,
    data: dict[str, Any],
    *,
    now: datetime,
    stale_days: int,
    question_days: int,
    burst: int,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if len(data["patterns"]) > AUDIT_MAX_PATTERNS:
        findings.append(
            AuditFinding(
                "oversized",
                "store",
                label,
                label,
                f"{len(data['patterns'])} patterns exceed {AUDIT_MAX_PATTERNS}; "
                "index reading and search precision degrade",
                "consolidate or remove implementation-detail patterns",
            )
        )
    by_day: dict[str, list[str]] = {}
    for kind, collection in AUDIT_KINDS:
        key = RECORD_KEYS[collection]
        for record in data[collection]:
            name = one_line(record.get(key))
            created = str(record.get("created_at", ""))[:10]
            if created:
                by_day.setdefault(created, []).append(f"{kind} {name}")
            marker = AUDIT_TIME_BOUND.search(audit_record_text(record))
            if marker:
                findings.append(
                    AuditFinding(
                        "time-bound",
                        kind,
                        name,
                        label,
                        f"contains time-bound wording {marker.group(0)!r}",
                        f"rewrite as a durable invariant with add-{kind} or remove "
                        f'--type {kind} --value "{name}"',
                    )
                )
            age = audit_days_since(record.get("updated_at"), now)
            if age is not None and age > stale_days:
                findings.append(
                    AuditFinding(
                        "aged",
                        kind,
                        name,
                        label,
                        f"not confirmed for {age} days",
                        f"verify against the repository, then re-add or remove "
                        f'--type {kind} --value "{name}"',
                    )
                )
    for day, names in sorted(by_day.items()):
        if len(names) >= burst:
            findings.append(
                AuditFinding(
                    "burst",
                    "store",
                    label,
                    label,
                    f"{len(names)} records created on {day}: {', '.join(names[:5])}"
                    + (", …" if len(names) > 5 else ""),
                    "review the batch for implementation detail that fails the admission gate",
                )
            )
    for question in data["open_questions"]:
        if question.get("status", "open") != "open":
            continue
        age = audit_days_since(question.get("created_at"), now)
        if age is not None and age > question_days:
            name = one_line(question.get("question"))
            findings.append(
                AuditFinding(
                    "stale-question",
                    "question",
                    name,
                    label,
                    f"open for {age} days",
                    f'answer it with the user or remove --type question --value "{name}"',
                )
            )
    return findings


def audit_shadowed(
    project: dict[str, Any],
    scopes: tuple[ScopeSummary, ...],
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for kind, collection in AUDIT_KINDS:
        key = RECORD_KEYS[collection]
        for record in project[collection]:
            name = one_line(record.get(key))
            for scope in scopes:
                if find_record(scope.data[collection], key, name) is None:
                    continue
                findings.append(
                    AuditFinding(
                        "shadowed",
                        kind,
                        name,
                        "project",
                        f"also defined in {scope.label}; the project record wins on read",
                        f'keep one: remove --type {kind} --value "{name}" here or '
                        f"re-add the corrected definition in {scope.label}",
                    )
                )
    return findings


def audit_divergent(repo: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    domains = configured_domains(global_config())
    for domain_id in member_domains(repo, domains):
        members = tuple(
            member
            for member in domains[domain_id].projects
            if member != repo and member.is_dir() and context_path(member).exists()
        )
        contexts: list[tuple[Path, dict[str, Any]]] = []
        for member in members:
            try:
                contexts.append((member, load_context(member)))
            except (OSError, SystemExit):
                continue
        project = load_context(repo)
        for kind, collection in (("term", "terms"), ("component", "components")):
            key = RECORD_KEYS[collection]
            for record in project[collection]:
                name = one_line(record.get(key))
                others = [
                    (member, hit)
                    for member, data in contexts
                    for hit in (find_record(data[collection], key, name),)
                    if hit is not None
                ]
                if not others:
                    continue
                definitions = {
                    audit_record_text(hit).casefold() for _, hit in others
                } | {audit_record_text(record).casefold()}
                findings.append(
                    AuditFinding(
                        "divergent",
                        kind,
                        name,
                        f"domain:{domain_id}",
                        f"defined in {len(others) + 1} member projects with "
                        f"{len(definitions)} distinct definitions: "
                        + ", ".join(member.name for member, _ in others),
                        f'agree on one definition, then move --type {kind} --value "{name}" '
                        f"--applicability domain:{domain_id} and remove the member copies",
                    )
                )
    return findings


def audit_dead_paths(repo: Path, project: dict[str, Any]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for component in project["components"]:
        for raw in component.get("paths", []) or []:
            path = str(raw)
            if any(token in path for token in "*?[") or Path(path).is_absolute():
                continue
            if not (repo / path).exists():
                name = one_line(component.get("name"))
                findings.append(
                    AuditFinding(
                        "dead-path",
                        "component",
                        name,
                        "project",
                        f"path {path} no longer exists in the repository",
                        f'update paths with add-component --name "{name}" or remove it',
                    )
                )
    return findings


def audit_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    require_initialized_context(repo)
    now = datetime.now(timezone.utc)
    project = load_context(repo)
    scopes = active_shared_scope_contexts(repo)
    options = {
        "now": now,
        "stale_days": args.stale_days,
        "question_days": args.question_days,
        "burst": args.burst,
    }
    findings = audit_store("project", project, **options)
    for scope in scopes:
        findings.extend(audit_store(scope.label, scope.data, **options))
    findings.extend(audit_shadowed(project, scopes))
    findings.extend(audit_divergent(repo))
    findings.extend(audit_dead_paths(repo, project))
    index = repo / CONTEXT_DIR / "index.md"
    if index.exists() and index.stat().st_size > AUDIT_MAX_INDEX_BYTES:
        findings.append(
            AuditFinding(
                "oversized",
                "view",
                "docs/context/index.md",
                "project",
                f"{index.stat().st_size} bytes exceed {AUDIT_MAX_INDEX_BYTES}; every session "
                "is told to read this file",
                "reduce record count or shorten summaries",
            )
        )
    stores = ("project", *(scope.label for scope in scopes))
    if args.format == "json":
        print(
            json.dumps(
                {
                    "repo": str(repo),
                    "stores": list(stores),
                    "findings": [finding.as_dict() for finding in findings],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.check] = counts.get(finding.check, 0) + 1
    summary = ", ".join(f"{count} {check}" for check, count in sorted(counts.items()))
    if args.format == "hook":
        if findings:
            print(
                f"Context audit: {len(findings)} findings ({summary}); "
                "run $curate-project-context to review them."
            )
        return
    print(
        f"Context audit for {repo} across {', '.join(stores)}: "
        + (f"{len(findings)} findings ({summary})." if findings else "no findings.")
    )
    for finding in findings:
        print(
            " | ".join(
                (
                    finding.check,
                    finding.kind,
                    compact_summary(finding.name, 80),
                    finding.store,
                    compact_summary(finding.detail, 240),
                    f"suggest: {compact_summary(finding.suggestion, 240)}",
                )
            )
        )


GRAPH_FORMATS = ("json", "text", "mermaid", "dot", "html")
GRAPH_LEVELS = ("projects", "records")


def load_graph_backend(name: str = "context_graph") -> Any:
    import importlib

    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


def knowledge_graph_inputs(repo: Path) -> tuple[tuple[Any, ...], tuple[Any, ...], list[Any]]:
    backend = load_graph_backend()
    global_backend = sys.modules["global_context"]
    config = global_config()
    roots = workspace_roots(config) or (repo,)
    arguments: list[str] = []
    append_external_source_arguments(arguments, roots)
    external = global_backend.parse_external_sources(arguments[1::2], roots)
    sources = global_backend.discovered_sources(roots, external)
    records, active_sources, _, _ = global_backend.load_source_records(sources)
    for scope_root in scope_context_roots():
        loaded, _, _ = global_backend.load_scope_records(scope_root)
        records = global_backend.sorted_records((*records, *loaded))
    domains = [
        backend.DomainSpec(
            domain_id,
            tuple(str(project) for project in members.projects),
            tuple(members.remotes),
        )
        for domain_id, members in configured_domains(config).items()
    ]
    return records, active_sources, domains


def graph_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    backend = load_graph_backend()
    records, sources, domains = knowledge_graph_inputs(repo)
    graph = backend.build_project_graph(records, sources, domains, repo_remote_url)
    if args.domain:
        view = backend.GraphView("domain", args.domain, args.depth)
    elif args.project is not None:
        view = backend.GraphView("project", args.project or str(repo), args.depth)
    else:
        view = backend.GraphView("overview", "", args.depth)
    view = backend.GraphView(
        view.kind,
        view.focus,
        view.depth,
        "projects",
        args.min_confidence,
        tuple(args.relation or ()),
    )
    try:
        graph = backend.apply_view(graph, view)
    except ValueError as exc:
        raise SystemExit(f"Knowledge graph view failed: {exc}") from exc
    if args.level == backend.RECORD_LEVEL or args.format == "html":
        graph = backend.add_record_level(graph, records)
        graph = replace(graph, view=replace(graph.view, level=args.level))
    exports = load_graph_backend("context_graph_export")
    renderers = {
        "json": backend.render_json,
        "text": backend.render_text,
        "mermaid": exports.render_mermaid,
        "dot": exports.render_dot,
        "html": exports.render_html,
    }
    output = renderers[args.format](graph)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
        print(f"Knowledge graph written to {args.output}")
        return
    print(output)


def generated_header() -> str:
    return "<!-- Generated by project-context-curator. Edit canonical context via project_context.py. -->\n\n"


def md_escape(value: Any) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def markdown_anchor(value: Any) -> str:
    text = re.sub(r"[^\w\s-]", "", str(value).casefold())
    return re.sub(r"[\s-]+", "-", text).strip("-")


def applicability_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return ", ".join(
        (
            str(selector.get("kind", ""))
            if selector.get("kind") == "universal"
            else f"{selector.get('kind', '')}:{selector.get('selector', '')}"
        )
        for selector in value
        if isinstance(selector, dict)
    )


def topical_applicability(record: dict[str, Any]) -> str:
    value = applicability_text(record.get("applicability"))
    return f" [applies: {value}]" if value else ""


def render_topical_index(data: dict[str, Any]) -> list[str]:
    lines = [
        "## Retrieval",
        "",
        "1. Scan the topical index below for task-specific names and concepts.",
        (
            '2. Run `project_context.py search --query "<task term>"` with the updater '
            "path reported by the active session."
        ),
        (
            "3. Read only the matching generated sections; if nothing matches, run "
            "`project_context.py status` to locate canonical JSON before using `rg -n -i`."
        ),
        "",
        "## Topical Index",
        "",
        "### Terms and APIs",
        "",
    ]
    if data["terms"]:
        for term in data["terms"]:
            details = "; ".join(
                value
                for value in (
                    one_line(term.get("kind")),
                    f"scope: {one_line(term.get('scope'))}"
                    if term.get("scope")
                    else "",
                )
                if value
            )
            suffix = f" ({details})" if details else ""
            lines.append(
                f"- [{one_line(term.get('term'))}](glossary.md){suffix} — "
                f"{compact_summary(term.get('definition'))}{topical_applicability(term)}"
            )
    else:
        lines.append("- None recorded.")

    lines.extend(["", "### Components", ""])
    if data["components"]:
        for component in data["components"]:
            name = one_line(component.get("name"))
            lines.append(
                f"- [{name}](components.md#{markdown_anchor(name)}) — "
                f"{compact_summary(component.get('responsibility'))}"
                f"{topical_applicability(component)}"
            )
    else:
        lines.append("- None recorded.")

    lines.extend(["", "### Architecture and conventions", ""])
    if data["patterns"]:
        for pattern in data["patterns"]:
            name = one_line(pattern.get("name"))
            lines.append(
                f"- [{name}](architecture.md#{markdown_anchor(name)}) — "
                f"{compact_summary(pattern.get('summary'))}{topical_applicability(pattern)}"
            )
    else:
        lines.append("- None recorded.")

    lines.extend(["", "### Open questions", ""])
    if data["open_questions"]:
        for question in data["open_questions"]:
            label = one_line(question.get("question"))
            context = compact_summary(question.get("context") or question.get("answer"))
            suffix = f" — {context}" if context else ""
            lines.append(
                f"- [{one_line(question.get('status', 'open'))}] "
                f"[{label}](inbox.md){suffix}{topical_applicability(question)}"
            )
    else:
        lines.append("- None recorded.")
    lines.append("")
    return lines


def render_markdown(repo: Path, data: dict[str, Any]) -> None:
    ctx_dir = repo / CONTEXT_DIR
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "index.md").write_text(
        render_index(data, active_shared_scope_contexts(repo)), encoding="utf-8"
    )
    (ctx_dir / "glossary.md").write_text(render_glossary(data), encoding="utf-8")
    (ctx_dir / "components.md").write_text(render_components(data), encoding="utf-8")
    (ctx_dir / "architecture.md").write_text(
        render_architecture(data), encoding="utf-8"
    )
    (ctx_dir / "inbox.md").write_text(render_inbox(data), encoding="utf-8")


def render_scoped_context(scopes: tuple[ScopeSummary, ...]) -> list[str]:
    lines = [
        "## Scoped Context",
        "",
        (
            "Domain and universal records live outside this directory and are not in the "
            "topical index; retrieve them with `project_context.py search`. On conflict, a "
            "project record overrides a domain record, which overrides a universal record."
        ),
        "",
    ]
    if not scopes:
        lines.extend(["- No domain or universal context applies to this project.", ""])
        return lines
    for summary in scopes:
        lines.extend(
            [
                f"### {summary.label} — {summary.counts()}",
                "",
                f"- Canonical: `{summary.path}`",
            ]
        )
        for heading, collection in (("Terms", "terms"), ("Components", "components")):
            names = ", ".join(
                one_line(record.get(RECORD_KEYS[collection]))
                for record in summary.data[collection]
            )
            if names:
                lines.append(f"- {heading}: {names}")
        lines.append("")
    return lines


def render_index(data: dict[str, Any], scopes: tuple[ScopeSummary, ...] = ()) -> str:
    policy = data.get("storage_policy")
    canonical_file = (
        "- Canonical JSON: configured Git context store (run `project_context.py status`)"
        if isinstance(policy, dict) and policy.get("context_visibility") == "git-store"
        else "- `context.json`: canonical structured text store"
    )
    lines = [
        generated_header(),
        "# Project Context",
        "",
        "Read this directory at the start of feature work, research, planning, or review.",
        "",
        "## Files",
        "",
        canonical_file,
        "- `glossary.md`: terms, abbreviations, aliases, events, APIs, and data stores",
        "- `components.md`: components, responsibilities, paths, and interfaces",
        "- `architecture.md`: architecture patterns and implementation rules",
        "- `inbox.md`: unresolved project-context questions",
        "",
    ]
    if isinstance(policy, dict):
        lines.extend(
            [
                "## Storage Policy",
                "",
                f"- Visibility: {policy.get('context_visibility', '')}",
                f"- Git exclude docs/context: {policy.get('git_exclude_docs_context', '')}",
                f"- Decision: {policy.get('decision', '')}",
                f"- Source: {policy.get('source', '')}",
                "",
            ]
        )
    lines.extend(
        [
            "## Applicability",
            "",
            f"- Collection default: {applicability_text(data['default_applicability'])}",
            "- Individual records may override this default.",
            "",
        ]
    )
    lines.extend(
        [
            "## Counts",
            "",
            f"- Terms: {len(data['terms'])}",
            f"- Components: {len(data['components'])}",
            f"- Patterns: {len(data['patterns'])}",
            f"- Open questions: {sum(1 for q in data['open_questions'] if q.get('status') == 'open')}",
            "",
        ]
    )
    lines.extend(render_scoped_context(scopes))
    lines.extend(render_topical_index(data))
    return "\n".join(lines)


def render_glossary(data: dict[str, Any]) -> str:
    lines = [
        generated_header(),
        "# Glossary",
        "",
    ]
    if not data["terms"]:
        lines.extend(["No terms recorded yet.", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "| Term | Kind | Definition | Scope | Applicability | Aliases | Source |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for term in data["terms"]:
        aliases = ", ".join(term.get("aliases", []))
        lines.append(
            "| {term} | {kind} | {definition} | {scope} | {applicability} | {aliases} | {source} |".format(
                term=md_escape(term.get("term", "")),
                kind=md_escape(term.get("kind", "")),
                definition=md_escape(term.get("definition", "")),
                scope=md_escape(term.get("scope", "")),
                applicability=md_escape(applicability_text(term.get("applicability"))),
                aliases=md_escape(aliases),
                source=md_escape(term.get("source", "")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def render_components(data: dict[str, Any]) -> str:
    lines = [
        generated_header(),
        "# Components",
        "",
    ]
    if not data["components"]:
        lines.extend(["No components recorded yet.", ""])
        return "\n".join(lines)

    for component in data["components"]:
        lines.extend(
            [
                f"## {component.get('name', '')}",
                "",
                f"Responsibility: {component.get('responsibility', '')}",
                "",
            ]
        )
        if component.get("paths"):
            lines.extend(["Paths:", ""])
            lines.extend(f"- `{path}`" for path in component["paths"])
            lines.append("")
        if component.get("interfaces"):
            lines.extend(["Interfaces:", ""])
            lines.extend(f"- {interface}" for interface in component["interfaces"])
            lines.append("")
        if component.get("notes"):
            lines.extend([f"Notes: {component['notes']}", ""])
        if component.get("applicability"):
            lines.extend(
                [f"Applicability: {applicability_text(component['applicability'])}", ""]
            )
        if component.get("source"):
            lines.extend([f"Source: {component['source']}", ""])
    return "\n".join(lines)


def render_architecture(data: dict[str, Any]) -> str:
    lines = [
        generated_header(),
        "# Architecture Patterns",
        "",
    ]
    if not data["patterns"]:
        lines.extend(["No architecture patterns recorded yet.", ""])
        return "\n".join(lines)

    for pattern in data["patterns"]:
        lines.extend(
            [
                f"## {pattern.get('name', '')}",
                "",
                pattern.get("summary", ""),
                "",
            ]
        )
        if pattern.get("applies_to"):
            lines.extend(["Applies to:", ""])
            lines.extend(f"- `{item}`" for item in pattern["applies_to"])
            lines.append("")
        if pattern.get("notes"):
            lines.extend([f"Notes: {pattern['notes']}", ""])
        if pattern.get("applicability"):
            lines.extend(
                [f"Applicability: {applicability_text(pattern['applicability'])}", ""]
            )
        if pattern.get("source"):
            lines.extend([f"Source: {pattern['source']}", ""])
    return "\n".join(lines)


def render_inbox(data: dict[str, Any]) -> str:
    lines = [
        generated_header(),
        "# Context Inbox",
        "",
    ]
    questions = data["open_questions"]
    if not questions:
        lines.extend(["No open questions.", ""])
        return "\n".join(lines)

    for question in questions:
        status = question.get("status", "open")
        lines.extend(
            [
                f"## [{status}] {question.get('question', '')}",
                "",
            ]
        )
        if question.get("context"):
            lines.extend([f"Context: {question['context']}", ""])
        if question.get("answer"):
            lines.extend([f"Answer: {question['answer']}", ""])
        if question.get("applicability"):
            lines.extend(
                [f"Applicability: {applicability_text(question['applicability'])}", ""]
            )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain docs/context project knowledge."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_repo(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--repo", type=Path, default=Path("."), help="Repository root"
        )

    def add_applicability(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--applicability",
            action="append",
            help=(
                "Override collection applicability with kind[:selector]; repeat for "
                "project, domain, user, machine, or universal selectors. "
                "A missing project or user selector means self; machine has no selector."
            ),
        )

    init_parser = subparsers.add_parser("init", help="Create docs/context files")
    add_repo(init_parser)
    init_parser.add_argument(
        "--visibility",
        choices=sorted(CONTEXT_VISIBILITIES),
        help=(
            "User-confirmed storage policy for Git repositories: local updates .git/info/exclude "
            "to keep docs/context local; versioned removes that local exclude entry and "
            "leaves repository .gitignore files unchanged; git-store uses the configured "
            "canonical Git context repository. "
            "Non-Git directories default to local."
        ),
    )
    init_parser.add_argument("--source", default="user-confirmed")
    init_parser.add_argument(
        "--default-applicability",
        action="append",
        help=(
            "Repository collection default. Only project:self is accepted; broader "
            "facts require explicit applicability on add-* and use their canonical store."
        ),
    )
    init_parser.set_defaults(func=init_context)

    ignore_parser = subparsers.add_parser(
        "ignore",
        help=f"Create {IGNORE_MARKER} to disable project context for this repository",
    )
    add_repo(ignore_parser)
    ignore_parser.set_defaults(func=ignore_context)

    update_parser = subparsers.add_parser(
        "update",
        help="Apply schema migrations and regenerate all context views",
    )
    add_repo(update_parser)
    update_parser.add_argument(
        "--if-initialized", action="store_true", help=argparse.SUPPRESS
    )
    update_parser.set_defaults(func=update_context)

    status_parser = subparsers.add_parser(
        "status",
        help="Report project context counts and its canonical path",
    )
    add_repo(status_parser)
    status_parser.set_defaults(func=status_context)

    storage_status_parser = subparsers.add_parser(
        "storage-status",
        help="Report the configured canonical storage runtime mode",
    )
    add_repo(storage_status_parser)
    storage_status_parser.add_argument(
        "--format", choices=("text", "json", "hook"), default="text"
    )
    storage_status_parser.set_defaults(func=storage_status)

    storage_migrate_parser = subparsers.add_parser(
        "storage-migrate",
        help=(
            "Preview or approve deterministic migration between local and Git-store "
            "canonical storage"
        ),
    )
    add_repo(storage_migrate_parser)
    storage_migrate_parser.add_argument(
        "--target", choices=sorted(STORAGE_RUNTIME_MODES), required=True
    )
    storage_migrate_parser.add_argument(
        "--store",
        type=Path,
        help="Existing Git checkout root required for a git-store target",
    )
    storage_migrate_parser.add_argument(
        "--workspace-root",
        type=Path,
        action="append",
        help=(
            "Root to scan for local project contexts when targeting git-store; "
            "repeat as needed"
        ),
    )
    storage_migrate_parser.add_argument(
        "--project-visibility",
        choices=sorted(LOCAL_CONTEXT_VISIBILITIES),
        help="Default and migrated project visibility required for a local target",
    )
    storage_migrate_parser.add_argument(
        "--approve-snapshot",
        help="User-approved token from an unchanged storage-migrate preview",
    )
    storage_migrate_parser.set_defaults(func=storage_migrate)

    git_store_init_parser = subparsers.add_parser(
        "git-store-init",
        help=(
            "Preview or approve migration of project, domain, and universal context "
            "to one canonical Git repository"
        ),
    )
    add_repo(git_store_init_parser)
    git_store_init_parser.add_argument(
        "--store",
        type=Path,
        required=True,
        help="Existing Git checkout root that will hold canonical context",
    )
    git_store_init_parser.add_argument(
        "--workspace-root",
        type=Path,
        action="append",
        help=(
            "Root to scan for repository-local contexts; repeat as needed. "
            "Configured global roots are used when omitted."
        ),
    )
    git_store_init_parser.add_argument(
        "--approve-snapshot",
        help="User-approved token from an unchanged git-store-init preview",
    )
    git_store_init_parser.set_defaults(func=git_store_init)

    git_store_bind_parser = subparsers.add_parser(
        "git-store-bind",
        help=(
            "Bind a checkout to an existing canonical project context by stable "
            "store id or by its Git remote URL"
        ),
    )
    add_repo(git_store_bind_parser)
    bind_selector = git_store_bind_parser.add_mutually_exclusive_group(required=True)
    bind_selector.add_argument("--project-store-id")
    bind_selector.add_argument(
        "--match-remote",
        action="store_true",
        help="Resolve the project by the checkout's normalized Git remote URL",
    )
    git_store_bind_parser.set_defaults(func=git_store_bind)

    git_store_status_parser = subparsers.add_parser(
        "git-store-status",
        help="List the configured canonical Git store and its project ids",
    )
    add_repo(git_store_status_parser)
    git_store_status_parser.set_defaults(func=git_store_status)

    global_init_parser = subparsers.add_parser(
        "global-init",
        help="Provision the pinned Qdrant runtime and index configured workspace roots",
    )
    add_repo(global_init_parser)
    global_init_parser.add_argument(
        "--workspace-root",
        type=Path,
        action="append",
        required=True,
        help="Root to scan recursively for docs/context/context.json; repeat as needed",
    )
    global_init_parser.add_argument(
        "--approve-snapshot",
        help="User-approved token from an unchanged global-init preview",
    )
    global_init_parser.set_defaults(func=global_init)

    global_upgrade_parser = subparsers.add_parser(
        "global-upgrade",
        help="Provision or update the pinned Qdrant runtime after user approval",
    )
    add_repo(global_upgrade_parser)
    global_upgrade_parser.set_defaults(func=global_upgrade)

    global_update_parser = subparsers.add_parser(
        "global-update",
        help="Incrementally synchronize configured canonical contexts into Qdrant",
    )
    add_repo(global_update_parser)
    global_update_parser.set_defaults(func=global_update)

    global_enroll_parser = subparsers.add_parser(
        "global-enroll",
        help="Preview or approve a replacement snapshot of enrolled projects",
    )
    add_repo(global_enroll_parser)
    global_enroll_parser.add_argument(
        "--approve-snapshot",
        help="User-approved token from an unchanged global-enroll preview",
    )
    global_enroll_parser.set_defaults(func=global_enroll)

    global_status_parser = subparsers.add_parser(
        "global-status",
        help="Report global-index configuration and runtime compatibility",
    )
    add_repo(global_status_parser)
    global_status_parser.add_argument(
        "--format", choices=("text", "hook"), default="text"
    )
    global_status_parser.set_defaults(func=global_status)

    domain_set_parser = subparsers.add_parser(
        "domain-set",
        help="Create or replace a domain's exact membership (project paths and remote URLs)",
    )
    add_repo(domain_set_parser)
    domain_set_parser.add_argument("--domain", required=True)
    domain_set_parser.add_argument(
        "--project",
        action="append",
        type=Path,
        help="Domain project path; repeat for every member",
    )
    domain_set_parser.add_argument(
        "--remote",
        action="append",
        help="Domain member Git remote URL (matched against checkouts' origin); repeatable",
    )
    domain_set_parser.set_defaults(func=domain_set)

    domain_remove_parser = subparsers.add_parser(
        "domain-remove",
        help="Remove domain membership without deleting its canonical context",
    )
    add_repo(domain_remove_parser)
    domain_remove_parser.add_argument("--domain", required=True)
    domain_remove_parser.set_defaults(func=domain_remove)

    domain_list_parser = subparsers.add_parser(
        "domain-list",
        help="List configured domains and their project members",
    )
    add_repo(domain_list_parser)
    domain_list_parser.set_defaults(func=domain_list)

    term_parser = subparsers.add_parser(
        "add-term", help="Add or update a glossary term"
    )
    add_repo(term_parser)
    term_parser.add_argument("--term", required=True)
    term_parser.add_argument("--kind", default="domain-term")
    term_parser.add_argument("--definition", required=True)
    term_parser.add_argument("--scope", default="project")
    term_parser.add_argument("--aliases", nargs="*")
    term_parser.add_argument("--notes")
    term_parser.add_argument("--source", default="user-confirmed")
    add_applicability(term_parser)
    term_parser.set_defaults(func=add_term)

    component_parser = subparsers.add_parser(
        "add-component", help="Add or update a component"
    )
    add_repo(component_parser)
    component_parser.add_argument("--name", required=True)
    component_parser.add_argument("--responsibility", required=True)
    component_parser.add_argument("--paths", nargs="*")
    component_parser.add_argument("--interfaces", nargs="*")
    component_parser.add_argument("--notes")
    component_parser.add_argument("--source", default="user-confirmed")
    add_applicability(component_parser)
    component_parser.set_defaults(func=add_component)

    pattern_parser = subparsers.add_parser(
        "add-pattern", help="Add or update an architecture pattern"
    )
    add_repo(pattern_parser)
    pattern_parser.add_argument("--name", required=True)
    pattern_parser.add_argument("--summary", required=True)
    pattern_parser.add_argument("--applies-to", nargs="*")
    pattern_parser.add_argument("--notes")
    pattern_parser.add_argument("--source", default="user-confirmed")
    add_applicability(pattern_parser)
    pattern_parser.set_defaults(func=add_pattern)

    question_parser = subparsers.add_parser(
        "add-question", help="Record an unresolved question"
    )
    add_repo(question_parser)
    question_parser.add_argument("--question", required=True)
    question_parser.add_argument("--context")
    add_applicability(question_parser)
    question_parser.set_defaults(func=add_question)

    remove_parser = subparsers.add_parser(
        "remove",
        help="Remove a term, component, pattern, or open question",
    )
    add_repo(remove_parser)
    remove_parser.add_argument(
        "--type",
        choices=sorted(REMOVE_TARGETS),
        required=True,
        help="Context entry type to remove",
    )
    remove_parser.add_argument(
        "--value",
        required=True,
        help="Entry key value: term, component name, pattern name, or question text",
    )
    add_applicability(remove_parser)
    remove_parser.set_defaults(func=remove_entry)

    move_parser = subparsers.add_parser(
        "move",
        help="Move one canonical record to a verified applicability scope",
    )
    add_repo(move_parser)
    move_parser.add_argument(
        "--type",
        choices=sorted(REMOVE_TARGETS),
        required=True,
    )
    move_parser.add_argument("--value", required=True)
    move_parser.add_argument("--source", default="user-confirmed")
    add_applicability(move_parser)
    move_parser.set_defaults(func=move_entry)

    scan_text_parser = subparsers.add_parser(
        "scan-text", help="Print possible context-gap hints from text"
    )
    add_repo(scan_text_parser)
    scan_text_parser.add_argument("--text", required=True)
    scan_text_parser.set_defaults(func=scan_text)

    scan_file_parser = subparsers.add_parser(
        "scan-file", help="Print possible context-gap hints from a file"
    )
    add_repo(scan_file_parser)
    scan_file_parser.add_argument("--file", type=Path, required=True)
    scan_file_parser.set_defaults(func=scan_file)

    search_parser = subparsers.add_parser(
        "search",
        help="Search durable context by one or more task terms",
    )
    add_repo(search_parser)
    search_parser.add_argument(
        "--query",
        action="append",
        required=True,
        help="Case-insensitive term or phrase; repeat to rank records matching more terms first",
    )
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.set_defaults(func=search_context)

    audit_parser = subparsers.add_parser(
        "audit",
        help="Report stale, time-bound, duplicated, or oversized context without changing it",
    )
    add_repo(audit_parser)
    audit_parser.add_argument("--format", choices=("text", "json", "hook"), default="text")
    audit_parser.add_argument(
        "--stale-days",
        type=int,
        default=180,
        help="Flag records not updated for more than this many days",
    )
    audit_parser.add_argument(
        "--question-days",
        type=int,
        default=60,
        help="Flag open questions older than this many days",
    )
    audit_parser.add_argument(
        "--burst",
        type=int,
        default=25,
        help="Flag stores where at least this many records were created on one day",
    )
    audit_parser.set_defaults(func=audit_context)

    graph_parser = subparsers.add_parser(
        "graph",
        help="Export the knowledge graph of domains, projects, and their relationships",
    )
    add_repo(graph_parser)
    graph_parser.add_argument("--domain", help="Focus on one domain and its members")
    graph_parser.add_argument(
        "--project",
        nargs="?",
        const="",
        help="Focus on one project by name or path; defaults to --repo when no value is given",
    )
    graph_parser.add_argument(
        "--depth", type=int, default=1, help="Hops to include around the focus node"
    )
    graph_parser.add_argument(
        "--level",
        choices=GRAPH_LEVELS,
        default="projects",
        help="Stop at project and domain nodes or attach every record of the shown stores",
    )
    graph_parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Drop relationship edges below this confidence (membership edges always stay)",
    )
    graph_parser.add_argument(
        "--relation",
        action="append",
        help="Keep only these relation kinds; repeatable",
    )
    graph_parser.add_argument("--format", choices=GRAPH_FORMATS, default="text")
    graph_parser.add_argument("--output", type=Path, help="Write the export to this file")
    graph_parser.set_defaults(func=graph_context)

    return parser


def git_store_mutation_root(args: argparse.Namespace) -> Path | None:
    if args.command == "storage-migrate":
        if not args.approve_snapshot:
            return None
        if args.target == "git-store":
            if args.store is None or args.project_visibility is not None:
                return None
            candidate = args.store
        else:
            if (
                args.store is not None
                or args.workspace_root
                or not args.project_visibility
            ):
                return None
            candidate = configured_git_store_root()
        return validate_git_store_root(candidate) if candidate is not None else None
    if args.command == "git-store-init":
        return validate_git_store_root(args.store) if args.approve_snapshot else None
    if args.command not in GIT_STORE_MUTATING_COMMANDS:
        return None
    candidate = configured_git_store_root()
    return validate_git_store_root(candidate) if candidate is not None else None


def storage_plan_workspace_roots(args: argparse.Namespace) -> tuple[Path, ...]:
    configured_roots = workspace_roots(global_config())
    return (
        validate_workspace_roots(args.workspace_root)
        if args.workspace_root
        else configured_roots
    )


def validate_git_store_approval(args: argparse.Namespace, root: Path) -> None:
    if args.command not in {"storage-migrate", "git-store-init"}:
        return
    repo = context_repo(args.repo)
    if args.command == "storage-migrate" and args.target == "local":
        plan = local_storage_migration_plan(args.project_visibility)
    else:
        plan = git_store_migration_plan(
            repo,
            root,
            storage_plan_workspace_roots(args),
        )
    validate_snapshot_approval({"snapshot": plan["token"]}, args.approve_snapshot)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = git_store_mutation_root(args)
    if root is None:
        args.func(args)
        return 0
    with git_store_lock(root):
        validate_git_store_approval(args, root)
        upstream = git_store_upstream(root)
        prepare_git_store(root, upstream)
        args.func(args)
        changed = commit_git_store_changes(
            root,
            f"chore(context): apply {args.command}",
        )
        if changed:
            push_git_store_commit(root, upstream)
    remote = safe_display_field(upstream.remote, GLOBAL_LABEL_OUTPUT_LIMIT)
    branch = safe_display_field(upstream.branch, GLOBAL_LABEL_OUTPUT_LIMIT)
    print(f"Git context store synchronized: {remote}/{branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
