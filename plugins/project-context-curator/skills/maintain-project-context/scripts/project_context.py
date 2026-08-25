#!/usr/bin/env python3
"""Maintain repository-local project context files."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 2
CONTEXT_DIR = Path("docs/context")
CONTEXT_FILE = CONTEXT_DIR / "context.json"
GIT_EXCLUDE_ENTRY = "docs/context/"
IGNORE_MARKER = ".no-project-context"
CONTEXT_VISIBILITIES = {"local", "versioned"}
APPLICABILITY_KINDS = {"project", "workspace", "user", "machine", "universal"}
DEFAULT_APPLICABILITY = [{"kind": "project", "selector": "self"}]
GLOBAL_CONFIG_SCHEMA_VERSION = 2
GLOBAL_CONFIG_FILE = "config.json"
GLOBAL_RUNTIME_FILE = "runtime.json"
GLOBAL_CATALOG_FILE = "catalog.json"
GLOBAL_INDEX_DIR = "qdrant"
GLOBAL_MODEL_DIR = "models"
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

TERM_KINDS = {
    "abbreviation",
    "domain-term",
    "event",
    "api",
    "data-store",
    "other",
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
SearchResult = tuple[int, str, str, str, str, tuple[str, ...]]

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
    if any(source["workspace_root"] not in roots for source in snapshot_sources(snapshot)):
        raise SystemExit("Global context discovery returned a source outside its roots")
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
        "runtime_upgrade_policy": "prompt",
        "created_at": previous.get("created_at", stamp),
        "updated_at": stamp,
    }
    write_json_object(global_config_dir() / GLOBAL_CONFIG_FILE, config)
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


def global_status(args: argparse.Namespace) -> None:
    config = global_config()
    roots = workspace_roots(config)
    if not config.get("enabled") or not roots:
        print("Global context index: disabled.")
        return
    if not global_runtime_is_current():
        print(
            "Global context runtime update required. Ask the user before running: "
            f"python3 {Path(__file__).resolve()} global-upgrade"
        )
        return

    catalog = read_json_object(global_cache_dir() / GLOBAL_CATALOG_FILE)
    projects = catalog.get("projects", [])
    project_count = catalog.get("project_count", len(projects))
    records = catalog.get("records", [])
    print(
        f"Global context index: active across {project_count} projects and "
        f"{len(records)} records."
    )
    print("Workspace roots: " + ", ".join(str(root) for root in roots))
    current_repo = context_repo(args.repo)
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
    arguments = backend_base_arguments(roots)
    arguments.extend(("--current-repo", str(repo)))
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


def default_context() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "default_applicability": deepcopy(DEFAULT_APPLICABILITY),
        "terms": [],
        "components": [],
        "patterns": [],
        "open_questions": [],
    }


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


MIGRATIONS: dict[int, Migration] = {
    0: migrate_v0_to_v1,
    1: migrate_v1_to_v2,
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


def load_context_with_migrations(
    repo: Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    path = context_path(repo)
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
    return merged, applied


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


def upsert_common(record: dict[str, Any], source: str | None) -> None:
    stamp = now_iso()
    record.setdefault("created_at", stamp)
    record["updated_at"] = stamp
    if source:
        record["source"] = source


def set_applicability(record: dict[str, Any], values: list[str] | None) -> None:
    applicability = parse_applicability(values)
    if applicability is not None:
        record["applicability"] = applicability


def add_term(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    require_initialized_context(repo)
    if args.kind not in TERM_KINDS:
        allowed = ", ".join(sorted(TERM_KINDS))
        raise SystemExit(f"Invalid term kind {args.kind!r}. Allowed: {allowed}")

    data = load_context(repo)
    record = find_record(data["terms"], "term", args.term)
    if record is None:
        record = {"term": args.term}
        data["terms"].append(record)

    record["kind"] = args.kind
    record["definition"] = args.definition
    record["scope"] = args.scope
    set_if_present(record, "aliases", split_values(args.aliases))
    set_if_present(record, "notes", args.notes)
    set_applicability(record, args.applicability)
    upsert_common(record, args.source)
    save_context(repo, data)
    print(f"Updated term: {args.term}")


def add_component(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    require_initialized_context(repo)
    data = load_context(repo)
    record = find_record(data["components"], "name", args.name)
    if record is None:
        record = {"name": args.name}
        data["components"].append(record)

    record["responsibility"] = args.responsibility
    set_if_present(record, "paths", split_values(args.paths))
    set_if_present(record, "interfaces", split_values(args.interfaces))
    set_if_present(record, "notes", args.notes)
    set_applicability(record, args.applicability)
    upsert_common(record, args.source)
    save_context(repo, data)
    print(f"Updated component: {args.name}")


def add_pattern(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    require_initialized_context(repo)
    data = load_context(repo)
    record = find_record(data["patterns"], "name", args.name)
    if record is None:
        record = {"name": args.name}
        data["patterns"].append(record)

    record["summary"] = args.summary
    set_if_present(record, "applies_to", split_values(args.applies_to))
    set_if_present(record, "notes", args.notes)
    set_applicability(record, args.applicability)
    upsert_common(record, args.source)
    save_context(repo, data)
    print(f"Updated pattern: {args.name}")


def add_question(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    require_initialized_context(repo)
    data = load_context(repo)
    record = find_record(data["open_questions"], "question", args.question)
    if record is None:
        record = {"question": args.question, "status": "open"}
        data["open_questions"].append(record)

    set_if_present(record, "context", args.context)
    set_applicability(record, args.applicability)
    stamp = now_iso()
    record.setdefault("created_at", stamp)
    record["updated_at"] = stamp
    save_context(repo, data)
    print(f"Recorded question: {args.question}")


def remove_entry(args: argparse.Namespace) -> None:
    repo = context_repo(args.repo)
    data = load_context(repo)
    collection_name, key, label = REMOVE_TARGETS[args.type]
    records = data[collection_name]
    record = find_record(records, key, args.value)
    if record is None:
        raise SystemExit(f"No {label} found for {args.value!r}")

    records.remove(record)
    save_context(repo, data)
    print(f"Removed {label}: {record.get(key, args.value)}")


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
    data: dict[str, Any], queries: tuple[str, ...]
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for kind, collection, label_key, summary_key, fields in SEARCH_SPECS:
        for record in data[collection]:
            haystack = "\n".join(one_line(record.get(field)) for field in fields).casefold()
            matched = tuple(query for query in queries if query in haystack)
            if not matched:
                continue
            results.append(
                (
                    len(matched),
                    kind,
                    one_line(record.get(label_key)),
                    SEARCH_FILES[kind],
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
    try:
        local_results = context_search_results(load_context(repo), queries)
    except (OSError, SystemExit) as exc:
        print(
            untrusted_diagnostic_line("invalid local context", exc),
            file=sys.stderr,
        )
        local_results = []
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
        for _, kind, label, path, summary, matched in local_results
    ]
    local_identities = {
        (
            safe_display_field(str(context_path(repo)), GLOBAL_PATH_OUTPUT_LIMIT)
            .replace("|", "\\|")
            .casefold(),
            safe_display_field(kind, GLOBAL_KIND_OUTPUT_LIMIT).casefold(),
            safe_display_field(label, GLOBAL_LABEL_OUTPUT_LIMIT).casefold(),
        )
        for _, kind, label, _, _, _ in local_results
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
                "project, workspace, user, machine, or universal selectors. "
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
            "Collection default as kind[:selector]; repeat for project, workspace, "
            "user, machine, or universal selectors. Defaults to project:self."
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
    remove_parser.set_defaults(func=remove_entry)

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
