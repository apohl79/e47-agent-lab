"""Optional domain repositories and approval-bound whole-bundle migrations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SLUG = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?")
GITHUB_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
)
JOURNAL = "project-context-domain-migration.json"


def _slug(value: str) -> str:
    if not isinstance(value, str) or value == "main" or not SLUG.fullmatch(value):
        raise SystemExit("Domain store names must be safe lowercase slugs other than 'main'.")
    return value


def _remote(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise SystemExit("A Git remote must be a non-empty URL or path without whitespace.")
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.-]*)://", value)
    if "::" in value or (
        scheme and scheme.group(1).lower() not in {"ssh", "https", "http", "git", "file", "git+ssh"}
    ):
        raise SystemExit("External Git remote helpers are not supported for domain stores.")
    if scheme and scheme.group(1).lower() in {"https", "http"} and "@" in urlsplit(value).netloc:
        raise SystemExit("Use a Git credential helper, not credentials embedded in a domain store URL.")
    return value


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(f"Unsafe domain store path: {relative!r}")
    candidate = root
    for part in path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise SystemExit(f"Domain store path traverses a symlink: {candidate}")
    return candidate


def validate_routes(pc: Any, manifest: dict[str, Any]) -> None:
    stores = manifest.get("stores", {})
    if not isinstance(stores, dict):
        raise SystemExit("Invalid domain store registry.")
    identities = {manifest["store_id"]}
    for name, entry in stores.items():
        _slug(name)
        if not isinstance(entry, dict) or entry.get("path") != f"stores/{name}":
            raise SystemExit(f"Invalid registered path for domain store {name!r}.")
        _remote(entry.get("remote_url"))
        try:
            identity = str(uuid.UUID(str(entry.get("store_id"))))
        except ValueError:
            raise SystemExit(f"Invalid identity for domain store {name!r}.") from None
        if identity in identities:
            raise SystemExit(f"Duplicate domain store identity for {name!r}.")
        identities.add(identity)
    for key, catalog in (
        ("project_owners", manifest["projects"]),
        ("domain_owners", manifest["domains"]),
    ):
        owners = manifest.get(key, {})
        if not isinstance(owners, dict):
            raise SystemExit(f"Invalid {key} registry.")
        for identity, owner in owners.items():
            if identity not in catalog or owner not in {"main", *stores}:
                raise SystemExit(f"Invalid {key} entry for {identity!r}.")


def _manifest(pc: Any, root: Path) -> dict[str, Any]:
    manifest = pc.load_git_store_manifest(root)
    validate_routes(pc, manifest)
    return manifest


def managed_paths(pc: Any, root: Path) -> tuple[str, ...]:
    manifest = _manifest(pc, root)
    return (
        (".gitmodules", *(entry["path"] for entry in manifest.get("stores", {}).values()))
        if manifest.get("stores")
        else ()
    )


def _gitlink(pc: Any, root: Path, relative: str) -> str:
    output = pc.git_checked(
        root, "ls-files", "--stage", "--", relative, action="submodule pointer inspection"
    ).stdout.strip()
    if not output:
        raise SystemExit(f"Registered domain store has no submodule pointer: {relative}")
    entries = output.splitlines()
    fields = entries[0].split(None, 3)
    if len(entries) != 1 or len(fields) != 4 or fields[0] != "160000" or fields[2] != "0":
        raise SystemExit(f"Registered domain store is not an unconflicted submodule: {relative}")
    return fields[1]


def _submodule_name(pc: Any, root: Path, relative: str, remote: str) -> str:
    modules = _safe_path(root, ".gitmodules")
    if not modules.is_file():
        raise SystemExit("Registered domain stores require a .gitmodules file.")
    result = pc.git_process(root, "config", "-f", str(modules), "--get-regexp", r"^submodule\..*\.path$")
    matches = [
        line.rsplit(None, 1)[0].removesuffix(".path")
        for line in result.stdout.splitlines()
        if len(line.rsplit(None, 1)) == 2 and line.rsplit(None, 1)[1] == relative
    ]
    if result.returncode not in (0, 1) or len(matches) != 1:
        raise SystemExit(f"Domain store has no unique .gitmodules registration: {relative}")
    key = matches[0]
    configured = pc.git_checked(
        root, "config", "-f", str(modules), "--get", f"{key}.url",
        action="submodule URL inspection",
    ).stdout.strip()
    if configured != remote:
        raise SystemExit(f"Domain store .gitmodules URL differs from its registry: {relative}")
    return key.removeprefix("submodule.")


def submodule_name(pc: Any, root: Path, relative: str, remote: str) -> str:
    return _submodule_name(pc, root, relative, remote)


def validate_registry(pc: Any, root: Path, manifest: dict[str, Any]) -> None:
    validate_routes(pc, manifest)
    for entry in manifest.get("stores", {}).values():
        _safe_path(root, entry["path"])
        _gitlink(pc, root, entry["path"])
        _submodule_name(pc, root, entry["path"], entry["remote_url"])


def _child(pc: Any, root: Path, name: str, entry: dict[str, Any]) -> Path | None:
    child = _safe_path(root, entry["path"])
    pointer = _gitlink(pc, root, entry["path"])
    _submodule_name(pc, root, entry["path"], entry["remote_url"])
    if not (child / ".git").exists():
        if child.exists() and (not child.is_dir() or any(child.iterdir())):
            raise SystemExit(f"Domain store path is occupied but not a submodule checkout: {child}")
        return None
    pc.validate_git_store_root(child)
    actual = pc.git_checked(
        child, "config", "--get-all", "remote.origin.url", action="domain store remote inspection"
    ).stdout.splitlines()
    if actual != [entry["remote_url"]]:
        raise SystemExit(f"Domain store remote differs from its registry: {name}")
    ancestor = pc.git_process(child, "merge-base", "--is-ancestor", pointer, "HEAD")
    if ancestor.returncode != 0:
        raise SystemExit(f"Domain store checkout does not contain its registered Git pointer: {name}")
    manifest = pc.load_git_store_manifest(child)
    if manifest["store_id"] != entry["store_id"]:
        raise SystemExit(f"Domain store identity differs from its registry: {name}")
    if manifest.get("stores") or manifest.get("project_owners") or manifest.get("domain_owners"):
        raise SystemExit(f"Nested domain stores are not supported: {name}")
    return child


def roots(
    pc: Any, root: Path, require_available: bool = False
) -> dict[str, Path]:
    manifest = _manifest(pc, root)
    result = {"main": root}
    for name, entry in sorted(manifest.get("stores", {}).items()):
        child = _child(pc, root, name, entry)
        if child is None:
            if require_available:
                raise SystemExit(
                    f"Domain store {name!r} is unavailable; initialize its submodule explicitly."
                )
        else:
            result[name] = child
    return result


def _owned_root(pc: Any, root: Path, field: str, identity: str) -> Path:
    manifest = _manifest(pc, root)
    owner = manifest.get(field, {}).get(identity, "main")
    if owner == "main":
        return root
    entry = manifest["stores"][owner]
    child = _child(pc, root, owner, entry)
    if child is None:
        raise SystemExit(
            f"Canonical context belongs to unavailable domain store {owner!r}; "
            "initialize its submodule explicitly. Refusing a main-store fallback."
        )
    return child


def project_root(pc: Any, root: Path, project_id: str) -> Path:
    return _owned_root(pc, root, "project_owners", project_id)


def scope_root(pc: Any, root: Path, domain: str) -> Path:
    return _owned_root(pc, root, "domain_owners", domain)


def attach_branch(pc: Any, child: Path) -> None:
    branch = pc.git_process(child, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode == 0:
        if branch.stdout.strip() != pc.GIT_STORE_BRANCH:
            raise SystemExit(f"Domain store must use the {pc.GIT_STORE_BRANCH} branch: {child}")
        return
    head = _head(pc, child)
    existing = pc.git_process(child, "rev-parse", "--verify", f"refs/heads/{pc.GIT_STORE_BRANCH}")
    if existing.returncode == 0 and existing.stdout.strip() != head:
        main_head = existing.stdout.strip()
        if pc.git_process(child, "merge-base", "--is-ancestor", head, main_head).returncode == 0:
            pc.git_checked(child, "checkout", pc.GIT_STORE_BRANCH, action="domain store branch attachment")
            return
        if pc.git_process(child, "merge-base", "--is-ancestor", main_head, head).returncode == 0:
            pc.git_checked(child, "checkout", pc.GIT_STORE_BRANCH, action="domain store branch attachment")
            pc.git_checked(child, "merge", "--ff-only", head, action="domain store branch fast-forward")
            return
        raise SystemExit(f"Detached domain store has divergent local {pc.GIT_STORE_BRANCH}: {child}")
    arguments = ("checkout", pc.GIT_STORE_BRANCH) if existing.returncode == 0 else (
        "checkout", "-b", pc.GIT_STORE_BRANCH
    )
    pc.git_checked(child, *arguments, action="domain store branch attachment")


def _head(pc: Any, root: Path) -> str:
    result = pc.git_process(root, "rev-parse", "--verify", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _remote_head(pc: Any, root: Path, remote: str) -> str:
    result = pc.git_checked(
        root, "ls-remote", "--", remote, "refs/heads/main", action="remote main inspection"
    )
    entries = result.stdout.splitlines()
    if len(entries) > 1:
        raise SystemExit("Git remote returned multiple main branch pointers.")
    return entries[0].split()[0] if entries else ""


def _push_urls(pc: Any, root: Path, remote: str) -> list[str]:
    result = pc.git_checked(
        root, "remote", "get-url", "--push", "--all", remote,
        action="complete push URL inspection",
    ).stdout.splitlines()
    if len(result) != 1:
        raise SystemExit("Domain stores require exactly one unambiguous push URL.")
    return [_remote(value) for value in result]


def _new_push_urls(pc: Any, remote: str) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="pcc-domain-target-") as directory:
        temporary = Path(directory).resolve()
        pc.git_checked(temporary, "init", "--quiet", action="push preview initialization")
        pc.git_checked(
            temporary, "remote", "add", "origin", remote, action="push preview remote setup"
        )
        return _push_urls(pc, temporary, "origin")


def _snapshot(pc: Any, root: Path) -> dict[str, Any]:
    branch = pc.git_process(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode == 0:
        upstream = pc.git_store_upstream(root)
    else:
        push_url = pc.git_checked(
            root, "remote", "get-url", "--push", "origin",
            action="detached domain store push URL inspection",
        ).stdout.strip()
        upstream = pc.GitUpstream(remote="origin", branch="main", push_url=push_url)
    return {
        "path": str(root),
        "head": _head(pc, root),
        "remote_head": _remote_head(pc, root, upstream.remote),
        "push_url": _remote(upstream.push_url),
        "push_urls": _push_urls(pc, root, upstream.remote),
        "remote": upstream.remote,
        "branch": upstream.branch,
        "manifest_hash": _hash(pc.git_store_manifest_path(root)),
        "status": pc.git_status_for_paths(root, (".",)),
        "local_main_head": pc.git_process(
            root, "rev-parse", "--verify", "refs/heads/main"
        ).stdout.strip(),
    }


def _inspect_remote(pc: Any, root: Path, remote: str) -> dict[str, Any]:
    head = _remote_head(pc, root, remote)
    push_urls = _new_push_urls(pc, remote)
    if not head:
        refs = pc.git_checked(root, "ls-remote", "--", remote, action="empty remote inspection")
        if refs.stdout.strip():
            raise SystemExit("A domain repository with history must have a main branch.")
        return {
            "head": "", "files": {}, "manifest": None, "push_url": remote,
            "push_urls": push_urls,
        }
    with tempfile.TemporaryDirectory(prefix="pcc-domain-preview-") as directory:
        temporary = Path(directory).resolve()
        pc.git_checked(temporary, "init", "--quiet", action="temporary preview initialization")
        pc.git_checked(
            temporary, "fetch", "--quiet", "--no-tags", "--depth=1", "--", remote,
            "refs/heads/main", action="domain repository preview fetch",
        )
        fetched = pc.git_checked(
            temporary, "rev-parse", "FETCH_HEAD", action="preview revision inspection"
        ).stdout.strip()
        if fetched != head:
            raise SystemExit("Domain repository changed during preview; run the preview again.")
        files: dict[str, str] = {}
        scope_domains: dict[str, list[str]] = {}
        project_contexts: dict[str, str] = {}
        raw_manifest: bytes | None = None
        tree = pc.git_checked(
            temporary, "ls-tree", "-rz", "--full-tree", fetched, action="preview tree inspection"
        ).stdout
        for record in tree.split("\0"):
            if not record:
                continue
            metadata, relative = record.split("\t", 1)
            mode, kind, object_id = metadata.split()
            if (
                relative == pc.GIT_STORE_MANIFEST_FILE
                or relative.startswith("projects/")
                or relative.startswith("scopes/")
            ):
                if mode not in {"100644", "100755"} or kind != "blob":
                    raise SystemExit(f"Domain repository contains an unsafe canonical path: {relative}")
                try:
                    result = subprocess.run(
                        ["git", "cat-file", "blob", object_id], cwd=str(temporary),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                        timeout=pc.GIT_STORE_NETWORK_TIMEOUT_SECONDS,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise SystemExit(f"Domain preview content inspection failed: {exc}") from exc
                if result.returncode != 0:
                    raise SystemExit("Domain preview content inspection failed.")
                content = result.stdout
                files[relative] = hashlib.sha256(content).hexdigest()
                if relative == pc.GIT_STORE_MANIFEST_FILE:
                    raw_manifest = content
                elif relative.endswith("/context.json"):
                    context_path = _safe_path(temporary, relative)
                    context_path.parent.mkdir(parents=True, exist_ok=True)
                    context_path.write_bytes(content)
                    if relative.startswith("scopes/"):
                        data = pc.load_discovered_scope_context(context_path)
                        if not pc.applicability_is_shareable(data["default_applicability"]):
                            raise SystemExit(f"Destination contains a private or legacy scope: {relative}")
                        scope_domains[relative] = sorted(_scope_domains(pc, data))
                    elif len(Path(relative).parts) == 3:
                        data, _ = pc.load_context_file(context_path)
                        project_id = Path(relative).parts[1]
                        if data.get("store_id") != project_id:
                            raise SystemExit(f"Destination project context identity differs from its path: {relative}")
                        project_contexts[project_id] = relative
        manifest = None
        if raw_manifest is not None:
            try:
                manifest = json.loads(raw_manifest)
            except (ValueError, UnicodeError):
                raise SystemExit("Domain repository contains an invalid store manifest.") from None
            pc.write_git_json(temporary, pc.git_store_manifest_path(temporary), manifest)
            manifest = pc.load_git_store_manifest(temporary)
            validate_routes(pc, manifest)
            if manifest.get("stores") or manifest.get("project_owners") or manifest.get("domain_owners"):
                raise SystemExit("A domain repository cannot contain nested store routing.")
        elif files:
            raise SystemExit("Domain repository contains canonical context without a store manifest.")
        return {
            "head": head, "files": files, "manifest": manifest, "push_url": remote,
            "push_urls": push_urls, "scope_domains": scope_domains,
            "project_contexts": project_contexts,
        }


def _request(pc: Any, args: Any) -> dict[str, Any]:
    domain = pc.validate_domain_id(args.domain)
    store = args.store
    if store != "main":
        _slug(store)
    remote = getattr(args, "remote", None)
    create = getattr(args, "create_repo", None)
    visibility = getattr(args, "visibility", None)
    if remote and remote.startswith(("./", "../")):
        raise SystemExit("Use an absolute local path or Git URL for --remote, not a relative submodule URL.")
    if store == "main" and (remote or create or visibility):
        raise SystemExit("The main store does not accept repository creation or remote options.")
    if create:
        if remote or not GITHUB_REPOSITORY.fullmatch(create) or create.split("/")[1] in {".", ".."}:
            raise SystemExit("--create-repo requires an exact owner/name and cannot be combined with --remote.")
        if visibility not in {"private", "public"}:
            raise SystemExit("Creating a GitHub repository requires explicit --visibility private|public.")
        remote = f"https://github.com/{create}.git"
    elif visibility:
        raise SystemExit("--visibility is only valid with --create-repo.")
    return {
        "domain": domain, "store": store, "remote": _remote(remote) if remote else None,
        "create_repo": create, "visibility": visibility,
    }


def _scope_domains(pc: Any, data: dict[str, Any]) -> set[str]:
    return {
        selector
        for value in pc.record_applicability_values(data)
        if value is not None
        for kind, selector in pc.applicability_pairs(value)
        if kind == "domain"
    }


def _github_identity() -> str:
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "user", "--jq", ".login"],
            text=True, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"GitHub account inspection failed: {exc}") from exc
    identity = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", identity):
        raise SystemExit("Cannot identify the authenticated github.com account for repository creation.")
    return identity


def _github_repository(request: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["gh", "repo", "view", request["create_repo"], "--json", "id,nameWithOwner,isPrivate"],
            env={**os.environ, "GH_HOST": "github.com"},
            text=True, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"GitHub repository inspection failed: {exc}") from exc
    if result.returncode != 0:
        if "could not resolve to a repository" in result.stderr.lower() or "not found" in result.stderr.lower():
            return None
        raise SystemExit("Cannot determine whether the requested GitHub repository already exists.")
    try:
        repository = json.loads(result.stdout)
    except ValueError:
        raise SystemExit("GitHub returned invalid repository metadata.") from None
    if (
        not isinstance(repository, dict)
        or str(repository.get("nameWithOwner", "")).casefold() != request["create_repo"].casefold()
        or not isinstance(repository.get("isPrivate"), bool)
        or not repository.get("id")
    ):
        raise SystemExit("GitHub returned unexpected repository metadata.")
    return repository


def _plan(pc: Any, root: Path, request: dict[str, Any]) -> dict[str, Any]:
    manifest = _manifest(pc, root)
    available = roots(pc, root)
    domain, destination = request["domain"], request["store"]
    stores = manifest.get("stores", {})
    blockers: list[str] = []
    remote_info = None
    remote = request["remote"]
    if destination in stores:
        entry = stores[destination]
        if request["create_repo"] or (remote and remote != entry["remote_url"]):
            raise SystemExit("An existing store cannot be recreated or assigned a different remote.")
        remote = entry["remote_url"]
        if destination not in available:
            raise SystemExit("Initialize the destination submodule before migrating into it.")
    elif destination != "main":
        if not remote:
            raise SystemExit("A new domain store requires --remote or --create-repo.")
        target = _safe_path(root, f"stores/{destination}")
        if target.exists():
            raise SystemExit(f"New domain store destination already exists: {target}")
        if not request["create_repo"]:
            remote_info = _inspect_remote(pc, root, remote)
        else:
            if _github_repository(request) is not None:
                raise SystemExit("The requested GitHub repository already exists; use --remote to attach it.")
            remote_info = {
                "head": "", "files": {}, "manifest": None, "push_url": remote,
                "push_urls": _new_push_urls(pc, remote),
            }
        if remote_info["manifest"] and remote_info["manifest"]["store_id"] in {
            manifest["store_id"], *(entry["store_id"] for entry in stores.values())
        }:
            raise SystemExit("Destination repository identity is already registered in this store.")
    target_root = available.get(destination, root / "stores" / destination)
    imports: dict[str, Any] = {}
    imported_manifest = (remote_info or {}).get("manifest")
    if imported_manifest:
        imports = {
            "projects": deepcopy(imported_manifest["projects"]),
            "domains": deepcopy(imported_manifest["domains"]),
            "domain_remotes": deepcopy(imported_manifest["domain_remotes"]),
            "files": deepcopy(remote_info["files"]),
        }
        for imported_domain in imports["domain_remotes"]:
            imports["domains"].setdefault(imported_domain, [])
        seen_remotes = {
            metadata["remote_url"]: project_id
            for project_id, metadata in manifest["projects"].items()
            if metadata.get("remote_url")
        }
        for project_id, metadata in imports["projects"].items():
            if project_id in manifest["projects"]:
                blockers.append(f"Imported project identity already has a canonical owner: {project_id}")
            if project_id not in remote_info.get("project_contexts", {}):
                blockers.append(f"Imported project has no canonical context: {project_id}")
            project_remote = metadata.get("remote_url")
            if project_remote:
                previous = seen_remotes.get(project_remote)
                if previous is not None and previous != project_id:
                    blockers.append(
                        f"Imported project remote {project_remote} already identifies {previous}, not {project_id}."
                    )
                seen_remotes[project_remote] = project_id
        for project_id in remote_info.get("project_contexts", {}):
            if project_id not in imports["projects"]:
                blockers.append(f"Destination has uncatalogued project context: {project_id}")
        for imported_domain, members in imports["domains"].items():
            if imported_domain in manifest["domains"]:
                if imported_domain != domain or members != manifest["domains"][imported_domain]:
                    blockers.append(f"Imported domain conflicts with the main catalog: {imported_domain}")
                if (
                    imports["domain_remotes"].get(imported_domain, [])
                    != manifest["domain_remotes"].get(imported_domain, [])
                ):
                    blockers.append(f"Imported domain remotes conflict with the main catalog: {imported_domain}")
        for relative, domains in remote_info.get("scope_domains", {}).items():
            if not domains:
                blockers.append(
                    f"Destination has a non-domain scope that cannot be owned by a domain store: {relative}"
                )
            for scoped_domain in domains:
                if scoped_domain not in imports["domains"]:
                    blockers.append(f"Destination scope has an uncatalogued domain: {scoped_domain}")
    project_ids = sorted(
        manifest["domains"].get(domain, imports.get("domains", {}).get(domain, []))
    )
    moves: list[dict[str, str]] = []
    sources: set[str] = set()
    for project_id in project_ids:
        if project_id not in manifest["projects"] and project_id in imports.get("projects", {}):
            relative = f"projects/{project_id}/context.json"
            moves.append({
                "owner": destination, "relative": relative,
                "source_hash": remote_info["files"].get(relative, ""),
            })
            continue
        owner = manifest.get("project_owners", {}).get(project_id, "main")
        if owner not in available:
            raise SystemExit(f"Project {project_id} belongs to unavailable domain store {owner!r}.")
        relative = f"projects/{project_id}/context.json"
        source = _safe_path(available[owner], relative)
        pc.validate_git_store_target(available[owner], source)
        if not source.is_file():
            blockers.append(f"Canonical project context is missing: {source}")
        else:
            moves.append({"owner": owner, "relative": relative, "source_hash": _hash(source)})
            sources.add(owner)
    for owner, store_root in available.items():
        scopes = _safe_path(store_root, "scopes")
        if not scopes.exists():
            continue
        for path in sorted(scopes.rglob("context.json"), key=str):
            relative = path.relative_to(store_root).as_posix()
            path = _safe_path(store_root, relative)
            pc.validate_git_store_target(store_root, path)
            data = pc.load_discovered_scope_context(path)
            domains = _scope_domains(pc, data)
            if domain not in domains:
                continue
            actual_owner = manifest.get("domain_owners", {}).get(domain, "main")
            if owner != actual_owner:
                blockers.append(f"Noncanonical duplicate domain scope exists: {path}")
            for other in sorted(domains - {domain}):
                other_owner = manifest.get("domain_owners", {}).get(other, "main")
                if other_owner != destination:
                    blockers.append(f"Mixed-domain scope {path} also belongs to {other!r} in {other_owner!r}.")
            moves.append({"owner": owner, "relative": relative, "source_hash": _hash(path)})
            sources.add(owner)
    original_owner = manifest.get("domain_owners", {}).get(domain, "main")
    if original_owner not in available:
        raise SystemExit(f"Domain {domain!r} belongs to unavailable domain store {original_owner!r}.")
    sources.add(original_owner)
    snapshots: dict[str, Any] = {}
    for owner in sorted({"main", *sources, *({destination} if destination in available else set())}):
        snapshot = _snapshot(pc, available[owner])
        snapshots[owner] = snapshot
        if snapshot["status"]:
            blockers.append(f"Store {owner!r} has uncommitted changes; synchronize it first.")
        if snapshot["head"] != snapshot["remote_head"]:
            blockers.append(f"Store {owner!r} is not at its remote main tip; synchronize it first.")
        if snapshot["local_main_head"] not in {"", snapshot["head"]} and pc.git_process(
            available[owner], "merge-base", "--is-ancestor",
            snapshot["local_main_head"], snapshot["head"],
        ).returncode != 0:
            blockers.append(f"Store {owner!r} has a different local main tip; activate and synchronize it first.")
    target_manifest = (
        pc.load_git_store_manifest(target_root) if destination in available
        else (remote_info or {}).get("manifest")
    )
    if destination != "main" and target_manifest:
        for project_id in project_ids:
            existing = target_manifest["projects"].get(project_id)
            source_metadata = manifest["projects"].get(
                project_id, imports.get("projects", {}).get(project_id)
            )
            if (
                existing is not None and existing != source_metadata
                and manifest.get("project_owners", {}).get(project_id, "main") != destination
            ):
                blockers.append(f"Destination project metadata conflicts: {project_id}")
        owns_domain = manifest.get("domain_owners", {}).get(domain, "main") == destination
        if (
            not owns_domain and domain in target_manifest["domains"]
            and target_manifest["domains"][domain] != project_ids
        ):
            blockers.append(f"Destination domain membership conflicts: {domain}")
        if not owns_domain and domain in target_manifest.get("domain_remotes", {}) and (
            target_manifest["domain_remotes"][domain]
            != manifest["domain_remotes"].get(domain, imports.get("domain_remotes", {}).get(domain, []))
        ):
            blockers.append(f"Destination domain remote metadata conflicts: {domain}")
    for item in moves:
        target = _safe_path(target_root, item["relative"])
        item["target_hash"] = (
            _hash(target) if destination in available
            else (remote_info or {}).get("files", {}).get(item["relative"], "")
        )
        if item["owner"] != destination and item["target_hash"] not in {"", item["source_hash"]}:
            blockers.append(f"Destination canonical content conflicts: {target}")
    payload = {
        "request": request,
        "manifest": manifest,
        "root": str(root),
        "remote": remote,
        "destination": str(target_root),
        "remote_info": remote_info,
        "snapshots": snapshots,
        "moves": sorted(moves, key=lambda item: (item["owner"], item["relative"])),
        "projects": project_ids,
        "blockers": sorted(set(blockers)),
        "modules_hash": _hash(root / ".gitmodules"),
        "github_identity": _github_identity() if request["create_repo"] else None,
        "imports": imports,
    }
    token = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**payload, "token": token}


def _journal_path(pc: Any, root: Path) -> Path:
    directory = pc.git_path(root, "--git-dir")
    if directory is None:
        raise SystemExit("Domain migration requires a Git directory.")
    return directory / JOURNAL


def _save_journal(path: Path, journal: dict[str, Any]) -> None:
    if path.is_symlink():
        raise SystemExit("Domain migration journal cannot be a symlink.")
    temporary = path.with_suffix(".tmp")
    if temporary.is_symlink():
        raise SystemExit("Domain migration temporary journal cannot be a symlink.")
    temporary.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _print_preview(plan: dict[str, Any], phase: str | None = None) -> None:
    request = plan["request"]
    print(f"Domain store preview: {request['domain']} -> {request['store']}")
    print(f"Main store: {plan['root']}")
    print(f"Destination: {plan['destination']}")
    if plan["remote"]:
        print(f"Destination remote: {plan['remote']}")
    if request["create_repo"]:
        print(f"Create GitHub repository: {request['create_repo']} ({request['visibility']})")
        print(f"Authenticated GitHub account: {plan['github_identity']}")
    for name, snapshot in sorted(plan["snapshots"].items()):
        print(f"Store {name}: HEAD={snapshot['head']} push={json.dumps(snapshot['push_urls'])}")
    if plan["remote_info"]:
        print(f"Destination push URLs: {json.dumps(plan['remote_info']['push_urls'])}")
    for project_id in sorted(plan["imports"].get("projects", {})):
        print(f"Import project: {project_id}")
    for domain in sorted(plan["imports"].get("domains", {})):
        print(f"Import domain: {domain} projects={json.dumps(plan['imports']['domains'][domain])}")
    for relative, digest in sorted(plan["imports"].get("files", {}).items()):
        print(f"Import canonical file: {relative} sha256={digest}")
    for item in plan["moves"]:
        print(
            f"Move {item['owner']}:{item['relative']} "
            f"sha256={item['source_hash']} destination_sha256={item['target_hash'] or 'missing'}"
        )
    for blocker in plan["blockers"]:
        print(f"BLOCKED: {blocker}")
    if phase:
        print(f"Pending migration phase: {phase}; rerun with the original approval to resume.")
    print(f"Snapshot token: {plan['token']}")


def _push(pc: Any, root: Path, journal: dict[str, Any], owner: str) -> None:
    upstream = pc.git_store_upstream(root)
    expected = journal["plan"]["snapshots"].get(owner)
    if expected is None:
        expected = {**journal["plan"]["remote_info"], "remote": "origin", "branch": "main"}
    if (
        upstream.remote != expected["remote"]
        or upstream.branch != expected["branch"]
        or _push_urls(pc, root, upstream.remote) != expected["push_urls"]
    ):
        raise SystemExit("Domain store push target changed after approval; journal retained.")
    result = pc.push_git_store(root, upstream)
    if result.returncode != 0:
        raise SystemExit(
            "Domain migration commit remains local; push failed and the migration journal "
            "was retained for retry: " + pc.git_error_detail(result)
        )
    pc.mark_git_store_synced(root)


def _commit(
    pc: Any, root: Path, message: str, extra: tuple[str, ...] = (),
    canonical: tuple[str, ...] = (),
) -> None:
    paths = (pc.GIT_STORE_MANIFEST_FILE, *canonical, *extra)
    staged = pc.git_checked(
        root, "diff", "--cached", "--name-only", "-z", action="migration staged-path inspection"
    ).stdout.split("\0")
    if any(path and path not in paths for path in staged):
        raise SystemExit("Unrelated staged changes appeared during migration; journal retained.")
    tracked = tuple(
        path for path in paths
        if (root / path).exists() or pc.git_process(root, "ls-files", "--", path).stdout
    )
    if not tracked:
        return
    pc.git_checked(root, "add", "-A", "--", *tracked, action="domain migration staging")
    result = pc.git_process(root, "diff", "--cached", "--quiet")
    if result.returncode == 1:
        pc.git_checked(root, "commit", "-m", message, action="domain migration commit")
    elif result.returncode != 0:
        raise SystemExit("Domain migration could not inspect its staged changes.")


def _create_destination(pc: Any, root: Path, journal: dict[str, Any], path: Path) -> Path:
    plan = journal["plan"]
    destination = plan["request"]["store"]
    if destination == "main":
        return root
    manifest = plan["manifest"]
    if destination in manifest.get("stores", {}):
        child = root / manifest["stores"][destination]["path"]
        attach_branch(pc, child)
        return child
    child = _safe_path(root, f"stores/{destination}")
    if plan["request"]["create_repo"] and not journal.get("repository_created"):
        if _github_identity() != plan["github_identity"]:
            raise SystemExit("Authenticated GitHub account changed after approval; journal retained.")
        existing = _github_repository(plan["request"])
        if existing is not None:
            if (
                not journal.get("repository_creation_started")
                or existing["isPrivate"] != (plan["request"]["visibility"] == "private")
                or _remote_head(pc, root, plan["remote"])
                or pc.git_checked(
                    root, "ls-remote", "--", plan["remote"], action="created repository emptiness inspection"
                ).stdout.strip()
            ):
                raise SystemExit("GitHub creation destination changed after approval; journal retained.")
            journal["repository_created"] = True
            journal["github_repository_id"] = existing["id"]
            _save_journal(path, journal)
        else:
            journal["repository_creation_started"] = True
            _save_journal(path, journal)
        command = [
            "gh", "repo", "create", plan["request"]["create_repo"],
            f"--{plan['request']['visibility']}",
        ]
        if not journal.get("repository_created"):
            try:
                environment = {**os.environ, "GH_HOST": "github.com"}
                result = subprocess.run(
                    command, env=environment, text=True, capture_output=True,
                    timeout=60, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SystemExit(f"GitHub repository creation failed; journal retained: {exc}") from exc
            if result.returncode != 0:
                raise SystemExit(
                    "GitHub repository creation failed; journal retained: "
                    + pc.safe_display_field(result.stderr or result.stdout, pc.DIAGNOSTIC_OUTPUT_LIMIT)
                )
            journal["repository_created"] = True
            _save_journal(path, journal)
    if not (child / ".git").exists():
        if child.exists() and any(child.iterdir()):
            raise SystemExit("Destination changed after approval; migration journal retained.")
        expected_head = (plan["remote_info"] or {}).get("head", "")
        if expected_head:
            pc.git_checked(
                root, "submodule", "add", "--name", destination, "--branch", "main",
                "--", plan["remote"], f"stores/{destination}", action="domain submodule attachment",
            )
            if _head(pc, child) != expected_head:
                raise SystemExit("Destination remote changed after approval; journal retained.")
        else:
            child.mkdir(parents=True, exist_ok=True)
            pc.git_checked(child, "init", "--quiet", "--initial-branch=main", action="domain store initialization")
            pc.git_checked(child, "remote", "add", "origin", plan["remote"], action="domain store remote setup")
    actual_remote = pc.git_process(child, "config", "--get-all", "remote.origin.url")
    if actual_remote.returncode != 0 and not _head(pc, child):
        pc.git_checked(
            child, "remote", "add", "origin", plan["remote"],
            action="interrupted domain store remote setup",
        )
    elif actual_remote.returncode != 0 or actual_remote.stdout.splitlines() != [plan["remote"]]:
        raise SystemExit("Destination remote changed after approval; journal retained.")
    expected_head = (plan["remote_info"] or {}).get("head", "")
    if expected_head and pc.git_process(
        child, "merge-base", "--is-ancestor", expected_head, "HEAD"
    ).returncode != 0:
        raise SystemExit("Destination no longer contains its approved revision; journal retained.")
    attach_branch(pc, child)
    return child


def _destination_manifest(pc: Any, journal: dict[str, Any], destination: Path) -> dict[str, Any]:
    plan = journal["plan"]
    main = plan["manifest"]
    manifest = pc.load_git_store_manifest(destination)
    identity = journal.get("destination_store_id")
    if identity is None:
        preview = (plan.get("remote_info") or {}).get("manifest")
        identity = (
            main["stores"][plan["request"]["store"]]["store_id"]
            if plan["request"]["store"] in main.get("stores", {})
            else (preview["store_id"] if preview else str(uuid.uuid4()))
        )
        journal["destination_store_id"] = identity
    if pc.git_store_manifest_path(destination).exists() and manifest["store_id"] != identity:
        raise SystemExit("Destination identity changed after approval; journal retained.")
    manifest["store_id"] = identity
    for project_id in plan["projects"]:
        manifest["projects"][project_id] = deepcopy(
            main["projects"].get(project_id, plan["imports"].get("projects", {}).get(project_id))
        )
    domain = plan["request"]["domain"]
    manifest["domains"][domain] = plan["projects"]
    if domain in main["domain_remotes"]:
        manifest["domain_remotes"][domain] = deepcopy(main["domain_remotes"][domain])
    return manifest


def _publication_manifest(journal: dict[str, Any]) -> dict[str, Any]:
    plan = journal["plan"]
    manifest = deepcopy(plan["manifest"])
    domain, owner = plan["request"]["domain"], plan["request"]["store"]
    imports = plan["imports"]
    for field in ("projects", "domains", "domain_remotes"):
        manifest[field].update(deepcopy(imports.get(field, {})))
    manifest["domains"].setdefault(domain, plan["projects"])
    if owner != "main":
        manifest.setdefault("stores", {})[owner] = {
            "path": f"stores/{owner}",
            "remote_url": plan["remote"],
            "store_id": journal["destination_store_id"],
        }
    for field, identities in (
        ("domain_owners", sorted({domain, *imports.get("domains", {})})),
        ("project_owners", sorted({*plan["projects"], *imports.get("projects", {})})),
    ):
        for identity in identities:
            if owner == "main":
                manifest.get(field, {}).pop(identity, None)
            else:
                manifest.setdefault(field, {})[identity] = owner
    return manifest


def _resume(pc: Any, root: Path, journal: dict[str, Any], path: Path) -> None:
    plan, phase = journal["plan"], journal["phase"]
    destination_name = plan["request"]["store"]
    domain = plan["request"]["domain"]
    if phase == "copy":
        destination = _create_destination(pc, root, journal, path)
        if destination_name != "main":
            target_manifest = _destination_manifest(pc, journal, destination)
            _save_journal(path, journal)
        for item in plan["moves"]:
            if item["owner"] == destination_name:
                continue
            source_root = root if item["owner"] == "main" else root / "stores" / item["owner"]
            source = _safe_path(source_root, item["relative"])
            target = _safe_path(destination, item["relative"])
            pc.validate_git_store_target(destination, target)
            if _hash(source) != item["source_hash"]:
                raise SystemExit(f"Migration source changed after approval; journal retained: {source}")
            if _hash(target) not in {"", item["source_hash"]}:
                raise SystemExit(f"Migration destination changed after approval; journal retained: {target}")
            if _hash(target) != item["source_hash"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".migration-tmp")
                pc.validate_git_store_target(destination, temporary)
                temporary.write_bytes(source.read_bytes())
                temporary.replace(target)
        for relative, digest in plan["imports"].get("files", {}).items():
            if relative == pc.GIT_STORE_MANIFEST_FILE:
                continue
            imported = _safe_path(destination, relative)
            if _hash(imported) != digest:
                raise SystemExit(f"Imported destination content changed after approval: {imported}")
        if destination_name != "main":
            pc.write_git_json(destination, pc.git_store_manifest_path(destination), target_manifest)
            _commit(
                pc, destination, f"feat(context): receive domain {domain}",
                canonical=tuple(item["relative"] for item in plan["moves"]),
            )
            _push(pc, destination, journal, destination_name)
            if destination_name not in plan["manifest"].get("stores", {}):
                registered = pc.git_process(root, "ls-files", "--stage", "--", f"stores/{destination_name}").stdout
                if not registered:
                    pc.git_checked(
                        root, "submodule", "add", "--name", destination_name,
                        "--branch", "main", "--", plan["remote"], f"stores/{destination_name}",
                        action="domain submodule registration",
                    )
                pc.git_checked(
                    root, "submodule", "absorbgitdirs", "--", f"stores/{destination_name}",
                    action="domain submodule Git directory setup",
                )
        journal["phase"] = "publish"
        _save_journal(path, journal)
        phase = "publish"
    if phase == "publish":
        expected = _publication_manifest(journal)
        current = pc.load_git_store_manifest(root)
        if current not in (plan["manifest"], expected):
            raise SystemExit("Main store registry changed during migration; journal retained.")
        initial = plan["snapshots"]["main"]["remote_head"]
        upstream = pc.git_store_upstream(root)
        remote_head = _remote_head(pc, root, upstream.remote)
        if remote_head not in {initial, journal.get("publication_head")}:
            published = journal.get("publication_head")
            pc.git_checked(root, "fetch", "--no-tags", upstream.remote, upstream.branch,
                           action="migration publication recovery")
            contains = published and pc.git_process(
                root, "merge-base", "--is-ancestor", published, "FETCH_HEAD"
            ).returncode == 0
            remote_manifest = pc.git_checked(
                root, "show", f"FETCH_HEAD:{pc.GIT_STORE_MANIFEST_FILE}",
                action="migration registry recovery",
            )
            if not contains or json.loads(remote_manifest.stdout) != expected:
                raise SystemExit("Main remote changed during migration; reconcile it before resuming.")
        pc.write_git_json(root, pc.git_store_manifest_path(root), expected)
        all_modules = tuple(entry["path"] for entry in expected.get("stores", {}).values())
        _commit(
            pc, root, f"feat(context): route domain {domain} to {destination_name}",
            (".gitmodules", *all_modules),
            canonical=tuple(
                item["relative"] for item in plan["moves"] if destination_name == "main"
            ),
        )
        journal["publication_head"] = _head(pc, root)
        _save_journal(path, journal)
        _push(pc, root, journal, "main")
        journal["phase"] = "cleanup"
        _save_journal(path, journal)
        phase = "cleanup"
    if phase == "cleanup":
        available = roots(pc, root)
        completed = set(journal.get("cleaned_stores", []))
        source_names = sorted({
            item["owner"] for item in plan["moves"] if item["owner"] != destination_name
        } | {
            plan["manifest"].get("domain_owners", {}).get(domain, "main")
        } - {destination_name})
        for name in source_names:
            if name in completed:
                continue
            source_root = available.get(name)
            if source_root is None:
                raise SystemExit(f"Cleanup source store {name!r} is unavailable; journal retained.")
            if name != "main":
                attach_branch(pc, source_root)
            for item in plan["moves"]:
                if item["owner"] != name:
                    continue
                source = _safe_path(source_root, item["relative"])
                pc.validate_git_store_target(source_root, source)
                if source.exists():
                    if _hash(source) != item["source_hash"]:
                        raise SystemExit(f"Cleanup source changed; refusing deletion: {source}")
                    source.unlink()
            if name != "main":
                manifest = pc.load_git_store_manifest(source_root)
                for project_id in plan["projects"]:
                    manifest["projects"].pop(project_id, None)
                for other in list(manifest["domains"]):
                    members = manifest["domains"][other]
                    manifest["domains"][other] = [member for member in members if member not in plan["projects"]]
                manifest["domains"].pop(domain, None)
                manifest["domain_remotes"].pop(domain, None)
                pc.write_git_json(source_root, pc.git_store_manifest_path(source_root), manifest)
                _commit(
                    pc, source_root, f"chore(context): finish domain {domain} migration",
                    canonical=tuple(
                        item["relative"] for item in plan["moves"] if item["owner"] == name
                    ),
                )
                _push(pc, source_root, journal, name)
            completed.add(name)
            journal["cleaned_stores"] = sorted(completed)
            _save_journal(path, journal)
        manifest = pc.load_git_store_manifest(root)
        _commit(
            pc, root, f"chore(context): finish domain {domain} migration",
            (".gitmodules", *(entry["path"] for entry in manifest.get("stores", {}).values())),
            canonical=tuple(
                item["relative"] for item in plan["moves"]
                if item["owner"] == "main" and destination_name != "main"
            ),
        )
        _push(pc, root, journal, "main")
        path.unlink()
        print(f"Domain {domain} is stored in {destination_name}; whole-bundle migration complete.")


def run(pc: Any, args: Any) -> None:
    request = _request(pc, args)
    configured = pc.configured_git_store()
    if configured is None:
        raise SystemExit("Domain repositories require a configured canonical Git store.")
    root = pc.validate_git_store_root(Path(configured["path"]))
    approval = getattr(args, "approve_snapshot", None)
    with pc.git_store_lock(root):
        manifest = pc.load_git_store_manifest(root)
        if manifest["store_id"] != configured["store_id"]:
            raise SystemExit("Configured Git context store identity does not match its manifest.")
        journal_path = _journal_path(pc, root)
        if journal_path.is_symlink():
            raise SystemExit("Domain migration journal cannot be a symlink.")
        journal = pc.read_json_object(journal_path) if journal_path.exists() else None
        if journal:
            if journal.get("plan", {}).get("request") != request:
                raise SystemExit("Another domain migration is pending; resume it before starting another.")
            plan = journal["plan"]
        else:
            plan = _plan(pc, root, request)
        _print_preview(plan, journal["phase"] if journal else None)
        if not approval:
            return
        if approval != plan["token"]:
            raise SystemExit("Approval snapshot is stale or incorrect; review the current preview.")
        if plan["blockers"]:
            raise SystemExit("Domain migration is blocked; resolve the listed conflicts and preview again.")
        with ExitStack() as locks:
            for name, snapshot in sorted(plan["snapshots"].items()):
                if name != "main":
                    child = Path(snapshot["path"])
                    locks.enter_context(pc.git_store_lock(child))
            if journal is None:
                checked = _plan(pc, root, request)
                if checked["token"] != approval:
                    raise SystemExit("Domain migration changed while acquiring locks; preview again.")
                journal = {"plan": plan, "phase": "copy"}
                _save_journal(journal_path, journal)
            try:
                _resume(pc, root, journal, journal_path)
            except SystemExit as exc:
                candidates = [root, root / "stores" / request["store"]]
                candidates.extend(Path(item["path"]) for item in plan["snapshots"].values())
                if not any(pc.git_store_publish.pending_path(pc, child).exists()
                           for child in candidates if (child / ".git").exists()):
                    raise
                error = pc.safe_display_field(exc, pc.DIAGNOSTIC_OUTPUT_LIMIT)
                pc.git_store_publish.enqueue(pc, root, pc.context_repo(args.repo), error=error)
                print("Domain migration publication pending; the migration journal and source copies are retained.")
                print("Last publication error: " + error)
