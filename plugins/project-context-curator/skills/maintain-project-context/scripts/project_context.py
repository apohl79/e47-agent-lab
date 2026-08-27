#!/usr/bin/env python3
"""Maintain canonical repository and XDG context stores."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 3
CONTEXT_DIR = Path("docs/context")
CONTEXT_FILE = CONTEXT_DIR / "context.json"
GIT_EXCLUDE_ENTRY = "docs/context/"
IGNORE_MARKER = ".no-project-context"
CONTEXT_VISIBILITIES = {"local", "versioned"}
APPLICABILITY_KINDS = {
    "domain",
    "machine",
    "project",
    "universal",
    "user",
    "workspace",
}
DEFAULT_APPLICABILITY = [{"kind": "project", "selector": "self"}]
GLOBAL_CONFIG_SCHEMA_VERSION = 3
GLOBAL_CONFIG_FILE = "config.json"
GLOBAL_RUNTIME_FILE = "runtime.json"
GLOBAL_CATALOG_FILE = "catalog.json"
GLOBAL_INDEX_DIR = "qdrant"
GLOBAL_MODEL_DIR = "models"
GLOBAL_CONTEXTS_DIR = "contexts"
GLOBAL_INDEX_SCHEMA_VERSION = 3
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
    "the Project Context Curator skill to confirm workspace roots and one "
    "local-or-versioned policy, preview global-init, request approval for the exact "
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
SCOPE_DIRECTORY_NAMES = {
    "domain": "domains",
    "machine": "machines",
    "user": "users",
    "workspace": "workspaces",
}
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
        if not isinstance(payload, dict) or payload.get("type") != UNTRUSTED_DIAGNOSTIC_TYPE:
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


def validate_domain_id(value: str) -> str:
    domain_id = value.strip().casefold()
    if not DOMAIN_ID_PATTERN.fullmatch(domain_id):
        raise SystemExit(
            "Domain id must contain 1-64 lowercase letters, digits, dots, "
            "underscores, or hyphens."
        )
    return domain_id


def configured_domains(config: dict[str, Any]) -> dict[str, tuple[Path, ...]]:
    raw_domains = config.get("domains", {})
    if not isinstance(raw_domains, dict):
        return {}
    domains: dict[str, tuple[Path, ...]] = {}
    for raw_name, raw_projects in raw_domains.items():
        if not isinstance(raw_name, str) or not isinstance(raw_projects, list):
            continue
        try:
            name = validate_domain_id(raw_name)
        except SystemExit:
            continue
        domains[name] = tuple(
            Path(str(project)).expanduser().resolve()
            for project in raw_projects
            if isinstance(project, str) and project
        )
    return domains


def containing_workspace(repo: Path, roots: tuple[Path, ...]) -> Path | None:
    matches = tuple(
        root for root in roots if repo == root or root in repo.parents
    )
    return max(matches, key=lambda path: len(path.parts), default=None)


def resolve_applicability(
    values: list[dict[str, str]],
    repo: Path,
    *,
    require_domain_membership: bool,
) -> list[dict[str, str]]:
    config = global_config()
    domains = configured_domains(config)
    roots = workspace_roots(config)
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
            if require_domain_membership and repo not in domains[domain_id]:
                raise SystemExit(
                    f"Repository {repo} is not registered in domain {domain_id!r}."
                )
            resolved.append({"kind": kind, "selector": domain_id})
            continue
        if kind == "workspace":
            workspace = (
                containing_workspace(repo, roots)
                if selector == "self"
                else Path(selector).expanduser().resolve()
            )
            if workspace is None:
                raise SystemExit(
                    "workspace:self requires the repository to be inside a configured "
                    "workspace root."
                )
            if workspace not in roots:
                raise SystemExit(
                    f"Unknown workspace {workspace}. Configure it with global-init first."
                )
            if require_domain_membership and repo != workspace and workspace not in repo.parents:
                raise SystemExit(f"Repository {repo} is outside workspace {workspace}.")
            resolved.append({"kind": kind, "selector": str(workspace)})
            continue
        if kind == "project":
            project = repo if selector == "self" else Path(selector).expanduser().resolve()
            if project != repo:
                raise SystemExit(
                    "Project applicability must target --repo; use that repository as --repo."
                )
            resolved.append({"kind": kind, "selector": str(project)})
            continue
        if selector == "self":
            selector = getpass.getuser() if kind == "user" else platform.node()
        resolved.append({"kind": kind, "selector": selector})
    return normalize_applicability(resolved, "resolved applicability")


def active_applicability(repo: Path) -> frozenset[tuple[str, str]]:
    config = global_config()
    active = {
        ("project", str(repo)),
        ("project", "self"),
        ("user", getpass.getuser()),
        ("user", "self"),
        ("machine", platform.node()),
        ("machine", "self"),
        ("universal", "*"),
    }
    matching_workspaces = tuple(
        root
        for root in workspace_roots(config)
        if repo == root or root in repo.parents
    )
    active.update(("workspace", str(root)) for root in matching_workspaces)
    if matching_workspaces:
        active.add(("workspace", "self"))
    active.update(
        ("domain", domain_id)
        for domain_id, projects in configured_domains(config).items()
        if repo in projects
    )
    return frozenset(active)


def applicability_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    normalized = normalize_applicability(value, "applicability")
    return tuple(
        (item["kind"], item.get("selector", "*")) for item in normalized
    )


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
    root = scope_context_root()
    if len(pairs) > 1:
        digest = hashlib.sha256(
            json.dumps(pairs, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        return root / "composite" / digest / "context.json"
    kind, selector = pairs[0]
    if kind == "universal":
        return root / "universal" / "context.json"
    directory = SCOPE_DIRECTORY_NAMES[kind]
    return root / directory / scope_selector_key(kind, selector) / "context.json"


def scope_context_files() -> tuple[Path, ...]:
    root = scope_context_root()
    if not root.is_dir():
        return ()
    resolved_root = root.resolve()
    return tuple(
        sorted(
            (
                path.resolve()
                for path in root.rglob("context.json")
                if path.is_file()
                and not path.is_symlink()
                and resolved_root in path.resolve().parents
            ),
            key=str,
        )
    )


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


def domain_set(args: argparse.Namespace) -> None:
    domain_id = validate_domain_id(args.domain)
    projects = tuple(
        dict.fromkeys(context_repo(path) for path in args.project)
    )
    missing = tuple(project for project in projects if not project.is_dir())
    if missing:
        raise SystemExit(
            "Domain project is not a directory: "
            + ", ".join(str(project) for project in missing)
        )
    config = global_config()
    domains = {
        name: [str(project) for project in members]
        for name, members in configured_domains(config).items()
    }
    domains[domain_id] = [str(project) for project in projects]
    config["domains"] = domains
    write_global_config(config)
    print(f"Configured domain {domain_id}: {len(projects)} projects")


def domain_remove(args: argparse.Namespace) -> None:
    domain_id = validate_domain_id(args.domain)
    config = global_config()
    domains = {
        name: [str(project) for project in members]
        for name, members in configured_domains(config).items()
    }
    if domain_id not in domains:
        raise SystemExit(f"Unknown domain {domain_id!r}.")
    del domains[domain_id]
    config["domains"] = domains
    write_global_config(config)
    print(f"Removed domain membership: {domain_id}")


def domain_list(args: argparse.Namespace) -> None:
    domains = configured_domains(global_config())
    if not domains:
        print("No project domains configured.")
        return
    for domain_id, projects in sorted(domains.items()):
        print(f"{domain_id}: " + ", ".join(str(project) for project in projects))


def global_runtime_state() -> dict[str, Any]:
    return read_json_object(global_data_dir() / GLOBAL_RUNTIME_FILE)


def global_runtime_is_current() -> bool:
    return global_runtime_state().get("fingerprint") == runtime_fingerprint()


def workspace_roots(config: dict[str, Any]) -> tuple[Path, ...]:
    values = config.get("workspace_roots", [])
    if not isinstance(values, list):
        return ()
    return tuple(Path(str(value)).expanduser().resolve() for value in values)


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
            "--scope-root",
            str(scope_context_root()),
        )
    )
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
        raise SystemExit(
            untrusted_diagnostic_line(f"global {command} failed", detail)
        )
    return proc


def discover_global_snapshot(roots: tuple[Path, ...]) -> dict[str, Any]:
    arguments: list[str] = []
    for root in roots:
        arguments.extend(("--workspace-root", str(root)))
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
        "runtime_upgrade_policy": "prompt",
        "created_at": previous.get("created_at", stamp),
        "updated_at": stamp,
    }
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
    return (
        schema_version == GLOBAL_LEGACY_RECORD_CATALOG_SCHEMA_VERSION
        and isinstance(catalog.get("records"), list)
    )


def global_catalog_requires_refresh(catalog: dict[str, Any]) -> bool:
    return catalog.get("index_schema_version") != GLOBAL_INDEX_SCHEMA_VERSION


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
    active_domains = tuple(
        domain_id
        for domain_id, projects_in_domain in sorted(configured_domains(config).items())
        if current_repo in projects_in_domain
    )
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


def context_path(repo: Path) -> Path:
    return repo / CONTEXT_FILE


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
    ensure_record_identities(migrated)
    return migrated


MIGRATIONS: dict[int, Migration] = {
    0: migrate_v0_to_v1,
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
}


def normalize_applicability(
    value: Any,
    label: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SystemExit(f"Invalid {label}: expected a non-empty list")

    normalized: dict[tuple[str, str], dict[str, str]] = {}
    for raw_selector in value:
        if not isinstance(raw_selector, dict):
            raise SystemExit(f"Invalid {label}: each selector must be an object")
        kind = str(raw_selector.get("kind", "")).strip().casefold()
        if kind not in APPLICABILITY_KINDS:
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
        resolved_selector = selector.strip() if separator else "self"
        if not resolved_selector:
            raise SystemExit(f"Applicability {kind!r} requires a non-blank selector")
        selectors.append({"kind": kind, "selector": resolved_selector})
    return normalize_applicability(selectors, "applicability")


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
        raise SystemExit(f"Invalid context visibility {visibility!r}. Allowed: {allowed}")

    stamp = now_iso()
    local = visibility == "local"
    if local and git_initialized:
        decision = (
            "Context stays local to this checkout; docs/context/ is ignored through "
            ".git/info/exclude."
        )
    elif local:
        decision = "Context is local because the target directory is not a Git repository."
    else:
        decision = "Context is intended to be versioned and shared through Git."
    return {
        "context_visibility": visibility,
        "git_initialized": git_initialized,
        "git_exclude_docs_context": local and git_initialized,
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
    write_json_object(path, data)


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

    raise SystemExit(
        "Invalid storage_policy.context_visibility in context.json: "
        f"{visibility!r}. Expected local or versioned."
    )


def save_context(repo: Path, data: dict[str, Any]) -> str:
    ctx_dir = repo / CONTEXT_DIR
    ctx_dir.mkdir(parents=True, exist_ok=True)
    ensure_context_gitignore(ctx_dir)
    normalize(data)
    policy_result = apply_storage_policy(repo, data)

    path = context_path(repo)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    data["components"] = sorted(data.get("components", []), key=lambda item: item["name"].lower())
    data["patterns"] = sorted(data.get("patterns", []), key=lambda item: item["name"].lower())
    data["open_questions"] = sorted(
        data.get("open_questions", []),
        key=lambda item: (item.get("status", "open") != "open", item["question"].lower()),
    )


def find_record(records: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
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
    add_provenance(record, repo, source or str(record.get("source", "unknown")), "recorded")


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
    path, data, _, project_target = context_write_target(
        repo, args.applicability
    )
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
        target_data[collection][target_data[collection].index(target_record)] = source_record
    save_context_target(repo, target_path, target_data, project_target)

    for source_path, source_data, source_is_project, record in sources:
        source_data[collection].remove(record)
        save_context_target(repo, source_path, source_data, source_is_project)
    print(f"Moved {label}: {args.value}")
    print(f"Canonical context: {target_path}")


def init_context(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    git_initialized = is_git_initialized(repo)
    visibility = args.visibility
    if visibility is None and git_initialized:
        raise SystemExit(
            "Context visibility decision required for Git repositories. Ask the user whether "
            "docs/context should stay local or be versioned, then rerun with --visibility local "
            "or --visibility versioned."
        )
    if visibility is None:
        visibility = "local"

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
                "with an explicit --applicability so they use the XDG scope store."
            )
        data["default_applicability"] = default_applicability
    existing_policy = data.get("storage_policy")
    data["storage_policy"] = storage_policy(
        visibility,
        args.source,
        git_initialized,
        existing_policy if isinstance(existing_policy, dict) else None,
    )
    policy_result = save_context(repo, data)
    print(f"Initialized local context: {repo / CONTEXT_DIR}")
    print(f"Context visibility: {visibility}")
    print(f"Git initialized: {git_initialized}")
    if visibility == "local":
        if policy_result == "not-git":
            print("Git exclude: skipped (not a Git repository)")
        else:
            action = "updated" if policy_result == "exclude-added" else "already configured"
            print(f"Git exclude: {action} ({git_exclude_display_path(repo)})")
    else:
        action = (
            "removed docs/context/ entry"
            if policy_result == "exclude-removed"
            else "unchanged"
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
    require_initialized_context(repo)
    data, migrations = load_context_with_migrations(repo)
    save_context(repo, data)
    applied = ", ".join(migrations) if migrations else "none"
    print(f"Updated project context: {repo / CONTEXT_DIR}")
    print(f"Schema version: {SCHEMA_VERSION}")
    print(f"Migrations applied: {applied}")
    print("Generated views: refreshed")


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
        if token not in {"HTTP", "HTTPS", "JSON", "YAML", "XML", "API", "URL", "URI", "SQL"}:
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
    return text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"


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
            applicability = record.get(
                "applicability", data["default_applicability"]
            )
            if not applicability_matches(applicability, active):
                continue
            haystack = "\n".join(one_line(record.get(field)) for field in fields).casefold()
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
    return sorted(results, key=lambda result: (-result[0], result[1], result[2].casefold()))


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


def generated_header() -> str:
    return "<!-- Generated by project-context-curator. Edit docs/context/context.json via project_context.py. -->\n\n"


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
            "2. Run `project_context.py search --query \"<task term>\"` with the updater "
            "path reported by the active session."
        ),
        (
            "3. Read only the matching generated sections; if nothing matches, search "
            "`context.json` with `rg -n -i`."
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
                    f"scope: {one_line(term.get('scope'))}" if term.get("scope") else "",
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
    (ctx_dir / "index.md").write_text(render_index(data), encoding="utf-8")
    (ctx_dir / "glossary.md").write_text(render_glossary(data), encoding="utf-8")
    (ctx_dir / "components.md").write_text(render_components(data), encoding="utf-8")
    (ctx_dir / "architecture.md").write_text(render_architecture(data), encoding="utf-8")
    (ctx_dir / "inbox.md").write_text(render_inbox(data), encoding="utf-8")


def render_index(data: dict[str, Any]) -> str:
    lines = [
        generated_header(),
        "# Project Context",
        "",
        "Read this directory at the start of feature work, research, planning, or review.",
        "",
        "## Files",
        "",
        "- `context.json`: canonical structured text store",
        "- `glossary.md`: terms, abbreviations, aliases, events, APIs, and data stores",
        "- `components.md`: components, responsibilities, paths, and interfaces",
        "- `architecture.md`: architecture patterns and implementation rules",
        "- `inbox.md`: unresolved project-context questions",
        "",
    ]
    policy = data.get("storage_policy")
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
    parser = argparse.ArgumentParser(description="Maintain docs/context project knowledge.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_repo(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo", type=Path, default=Path("."), help="Repository root")

    def add_applicability(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--applicability",
            action="append",
            help=(
                "Override collection applicability with kind[:selector]; repeat for "
                "project, domain, workspace, user, machine, or universal selectors. "
                "A missing selector means self."
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
            "leaves repository .gitignore files unchanged. "
            "Non-Git directories default to local."
        ),
    )
    init_parser.add_argument("--source", default="user-confirmed")
    init_parser.add_argument(
        "--default-applicability",
        action="append",
        help=(
            "Repository collection default. Only project:self is accepted; broader "
            "facts require explicit applicability on add-* and use an XDG store."
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
    update_parser.set_defaults(func=update_context)

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
        help="Create or replace a domain's exact project membership",
    )
    add_repo(domain_set_parser)
    domain_set_parser.add_argument("--domain", required=True)
    domain_set_parser.add_argument(
        "--project",
        action="append",
        type=Path,
        required=True,
        help="Domain project path; repeat for every member",
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

    term_parser = subparsers.add_parser("add-term", help="Add or update a glossary term")
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

    component_parser = subparsers.add_parser("add-component", help="Add or update a component")
    add_repo(component_parser)
    component_parser.add_argument("--name", required=True)
    component_parser.add_argument("--responsibility", required=True)
    component_parser.add_argument("--paths", nargs="*")
    component_parser.add_argument("--interfaces", nargs="*")
    component_parser.add_argument("--notes")
    component_parser.add_argument("--source", default="user-confirmed")
    add_applicability(component_parser)
    component_parser.set_defaults(func=add_component)

    pattern_parser = subparsers.add_parser("add-pattern", help="Add or update an architecture pattern")
    add_repo(pattern_parser)
    pattern_parser.add_argument("--name", required=True)
    pattern_parser.add_argument("--summary", required=True)
    pattern_parser.add_argument("--applies-to", nargs="*")
    pattern_parser.add_argument("--notes")
    pattern_parser.add_argument("--source", default="user-confirmed")
    add_applicability(pattern_parser)
    pattern_parser.set_defaults(func=add_pattern)

    question_parser = subparsers.add_parser("add-question", help="Record an unresolved question")
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

    scan_text_parser = subparsers.add_parser("scan-text", help="Print possible context-gap hints from text")
    add_repo(scan_text_parser)
    scan_text_parser.add_argument("--text", required=True)
    scan_text_parser.set_defaults(func=scan_text)

    scan_file_parser = subparsers.add_parser("scan-file", help="Print possible context-gap hints from a file")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
