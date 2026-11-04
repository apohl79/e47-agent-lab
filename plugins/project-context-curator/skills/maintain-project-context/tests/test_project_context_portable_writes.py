from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from scope_test_support import (
    initialize_git_repository,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)
from test_project_context_domain_stores import (
    configured_store,
    domain_remote,
    manifest,
    place_domain,
    successful,
    token,
    tree_snapshot,
)


def test_remote_capture_reads_and_writes_without_component_checkout(
    tmp_path: Path,
) -> None:
    env, repo, store, remote, project_id = configured_store(tmp_path)
    shutil.move(str(repo), str(tmp_path / "unavailable-checkout"))
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    source_commit = "b" * 40
    successful(
        "add-term", "--project-store-id", project_id,
        "--term", "Remote evidence", "--definition", "Inspected on GitHub",
        "--source", "repo-docs",
        "--source-remote", "https://github.com/acme/service",
        "--source-commit", source_commit, repo=unrelated, env=env,
    )
    path = store / "projects" / project_id / "context.json"
    record = next(
        term for term in read_json(path)["terms"] if term["term"] == "Remote evidence"
    )
    provenance = record["provenance"][-1]
    assert provenance["commit"] == source_commit
    assert "github.com/acme/service" in json.dumps(provenance)
    assert str(unrelated) not in json.dumps(provenance)
    status = successful(
        "status", "--project-store-id", project_id, repo=unrelated, env=env
    )
    search = successful(
        "search", "--project-store-id", project_id, "--query", "Remote evidence",
        repo=unrelated, env=env,
    )
    assert "Remote evidence" in search.stdout
    assert str(path) in status.stdout
    assert not (unrelated / "docs/context").exists()
    assert run_git(store, "rev-parse", "HEAD").stdout == run_git(
        remote, "rev-parse", "refs/heads/main"
    ).stdout


def test_remote_capture_requires_valid_matching_remote_and_commit(
    tmp_path: Path,
) -> None:
    env, _repo, store, _remote, project_id = configured_store(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    for evidence in (
        ("--source", "repo-docs"),
        ("--source-remote", "https://github.com/acme/service"),
        (
            "--source-remote", "https://github.com/acme/service",
            "--source-commit", "not-a-sha",
        ),
        (
            "--source-remote", "https://github.com/acme/other",
            "--source-commit", "b" * 40,
        ),
    ):
        before = tree_snapshot(store, tmp_path / "xdg", unrelated)
        result = run_context(
            "add-term", "--project-store-id", project_id,
            "--term", "Rejected", "--definition", "Unverified source",
            *evidence, repo=unrelated, env=env,
        )
        assert result.returncode != 0, evidence
        assert tree_snapshot(store, tmp_path / "xdg", unrelated) == before
    assert not (unrelated / "docs/context").exists()


def test_register_remote_only_project_then_bootstrap_in_domain_store(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, _project_id = configured_store(tmp_path)
    child_remote = domain_remote(tmp_path)
    place_domain(repo, env, "cpl", child_remote)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    arguments = (
        "git-store-register", "--remote", "https://github.com/acme/remote-only.git",
        "--name", "Remote only", "--domain", "cpl",
    )
    before = tree_snapshot(store, tmp_path / "xdg", unrelated)
    preview = successful(*arguments, repo=unrelated, env=env)
    assert tree_snapshot(store, tmp_path / "xdg", unrelated) == before
    registered = successful(
        *arguments, "--approve-snapshot", token(preview.stdout),
        repo=unrelated, env=env,
    )
    project_id = next(
        line.removeprefix("Registered project context: ")
        for line in registered.stdout.splitlines()
        if line.startswith("Registered project context: ")
    )
    assert str(uuid.UUID(project_id)) == project_id
    successful(
        "add-term", "--project-store-id", project_id,
        "--term", "Remote bootstrap", "--definition", "First durable finding",
        "--source", "repo-docs",
        "--source-remote", "https://github.com/acme/remote-only.git",
        "--source-commit", "c" * 40, repo=unrelated, env=env,
    )
    metadata = manifest(store)
    assert metadata["project_owners"][project_id] == "cpl"
    assert project_id in metadata["domains"]["cpl"]
    path = store / "stores/cpl/projects" / project_id / "context.json"
    assert read_json(path)["terms"][0]["term"] == "Remote bootstrap"
    assert not (store / "projects" / project_id).exists()
    assert not (unrelated / "docs/context").exists()
    assert run_git(store / "stores/cpl", "rev-parse", "HEAD").stdout == run_git(
        child_remote, "rev-parse", "refs/heads/main"
    ).stdout


def test_remote_project_intersection_uses_identity_not_current_directory(
    tmp_path: Path,
) -> None:
    env, _repo, _store, _remote, project_id = configured_store(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    successful(
        "add-term", "--project-store-id", project_id,
        "--term", "Identity intersection", "--definition", "Scoped to the remote project",
        "--applicability", "project", "--applicability", "domain:cpl",
        repo=unrelated, env=env,
    )
    scopes = list(Path(env["PROJECT_CONTEXT_CURATOR_DATA_DIR"]).rglob("context.json"))
    assert len(scopes) == 1
    data = read_json(scopes[0])
    assert {"kind": "project", "selector": f"pcc://project/{project_id}"} in data["default_applicability"]
    assert str(unrelated) not in json.dumps(data)
    result = successful(
        "search", "--project-store-id", project_id, "--query", "Identity intersection",
        repo=unrelated, env=env,
    )
    assert "Identity intersection" in result.stdout
    assert not (unrelated / "docs/context").exists()


def test_attach_populated_legacy_store_imports_catalog_and_remote_writable_owner(
    tmp_path: Path,
) -> None:
    source_remote = "https://github.com/acme/legacy-service.git"
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    _seed_env, _seed_repo, seed, child_remote, imported_id = configured_store(
        seed_root, domain="legacy", source_remote=source_remote
    )
    seed_metadata = manifest(seed)
    seed_metadata["schema_version"] = 1
    for field in ("stores", "project_owners", "domain_owners"):
        seed_metadata.pop(field, None)
    (seed / "project-context-store.json").write_text(
        json.dumps(seed_metadata) + "\n", encoding="utf-8"
    )
    run_git(seed, "add", "project-context-store.json")
    run_git(seed, "commit", "-m", "Populated old-format store")
    run_git(seed, "push", "origin", "main")
    relative = Path("projects") / imported_id / "context.json"
    original = (seed / relative).read_bytes()
    scope_relative = Path("scopes/domains/legacy/context.json")
    original_scope = (seed / scope_relative).read_bytes()

    main_root = tmp_path / "main"
    main_root.mkdir()
    env, repo, store, _remote, main_id = configured_store(main_root)
    arguments = (
        "domain-store", "--domain", "legacy", "--store", "imported",
        "--remote", str(child_remote),
    )
    before = tree_snapshot(store, main_root / "xdg")
    preview = successful(*arguments, repo=repo, env=env)
    assert tree_snapshot(store, main_root / "xdg") == before
    successful(
        *arguments, "--approve-snapshot", token(preview.stdout), repo=repo, env=env
    )
    child = store / "stores/imported"
    assert (child / relative).read_bytes() == original
    assert (child / scope_relative).read_bytes() == original_scope
    metadata = manifest(store)
    assert set(metadata["projects"]) == {main_id, imported_id}
    assert metadata["project_owners"][imported_id] == "imported"
    assert metadata["domain_owners"]["legacy"] == "imported"
    assert metadata["domains"]["legacy"] == [imported_id]
    assert not (store / relative).exists()
    assert len(tuple(store.glob(f"**/projects/{imported_id}/context.json"))) == 1

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    successful(
        "status", "--project-store-id", imported_id, repo=unrelated, env=env
    )
    search = successful(
        "search", "--project-store-id", imported_id, "--query", "Ledger",
        repo=unrelated, env=env,
    )
    assert "Ledger" in search.stdout
    successful(
        "add-term", "--project-store-id", imported_id,
        "--term", "Imported write", "--definition", "Existing catalog stays writable",
        "--source", "repo-docs", "--source-remote", source_remote,
        "--source-commit", "d" * 40, repo=unrelated, env=env,
    )
    assert any(
        term["term"] == "Imported write"
        for term in read_json(child / relative)["terms"]
    )
    assert not (unrelated / "docs/context").exists()
    assert run_git(child, "rev-parse", "HEAD").stdout == run_git(
        child_remote, "rev-parse", "refs/heads/main"
    ).stdout


def test_second_machine_graph_uses_canonical_membership_without_bindings(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, project_id = configured_store(tmp_path)
    shutil.move(str(repo), str(tmp_path / "unavailable-checkout"))
    second_env = {
        **env,
        **isolated_environment(tmp_path / "second-xdg"),
    }
    unrelated = tmp_path / "second-machine"
    unrelated.mkdir()
    arguments = ("git-store-init", "--store", str(store))
    preview = successful(*arguments, repo=unrelated, env=second_env)
    successful(
        *arguments, "--approve-snapshot", token(preview.stdout),
        repo=unrelated, env=second_env,
    )
    config = read_json(
        Path(second_env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json"
    )
    assert config["git_store"]["project_bindings"] == {}
    exported = successful(
        "graph", "--domain", "cpl", "--format", "json",
        repo=unrelated, env=second_env,
    )
    graph = json.loads(exported.stdout)
    assert any(
        edge["source"] == f"project:{project_id}"
        and edge["target"] == "domain:cpl"
        and edge["relation"] == "member_of"
        for edge in graph["edges"]
    )
    assert not (unrelated / "docs/context").exists()


def test_detection_is_read_only_then_matching_bind_attaches_checkout(
    tmp_path: Path,
) -> None:
    env, _repo, store, _remote, project_id = configured_store(tmp_path)
    checkout = tmp_path / "new-path/service"
    initialize_git_repository(checkout)
    run_git(checkout, "remote", "add", "origin", "git@github.com:acme/service.git")
    before = tree_snapshot(store, tmp_path / "xdg", checkout)
    result = successful("git-store-detect", repo=checkout, env=env)
    assert json.loads(result.stdout) == {
        "status": "match", "matches": [project_id], "binding": None
    }
    assert tree_snapshot(store, tmp_path / "xdg", checkout) == before
    successful("git-store-bind", "--match-remote", repo=checkout, env=env)
    config = read_json(Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json")
    assert config["git_store"]["project_bindings"][str(checkout.resolve())] == project_id
    assert (checkout / "docs/context/index.md").exists()
    assert not (checkout / "docs/context/context.json").exists()
    successful(
        "add-term", "--term", "New checkout", "--definition", "Writes same owner",
        repo=checkout, env=env,
    )
    assert any(
        term["term"] == "New checkout"
        for term in read_json(store / "projects" / project_id / "context.json")["terms"]
    )


def test_ambiguous_detection_and_binding_do_not_select_arbitrary_project(
    tmp_path: Path,
) -> None:
    env, _repo, store, _remote, project_id = configured_store(tmp_path)
    duplicate_id = str(uuid.uuid4())
    metadata = manifest(store)
    metadata["projects"][duplicate_id] = dict(metadata["projects"][project_id])
    (store / "project-context-store.json").write_text(
        json.dumps(metadata) + "\n", encoding="utf-8"
    )
    run_git(store, "add", "project-context-store.json")
    run_git(store, "commit", "-m", "Ambiguous historical identities")
    run_git(store, "push", "origin", "main")
    checkout = tmp_path / "new-service"
    initialize_git_repository(checkout)
    run_git(checkout, "remote", "add", "origin", "https://github.com/acme/service")
    before = tree_snapshot(store, tmp_path / "xdg", checkout)
    detected = successful("git-store-detect", repo=checkout, env=env)
    result = json.loads(detected.stdout)
    assert result["status"] == "ambiguous"
    assert set(result["matches"]) == {project_id, duplicate_id}
    assert tree_snapshot(store, tmp_path / "xdg", checkout) == before
    bound = run_context("git-store-bind", "--match-remote", repo=checkout, env=env)
    assert bound.returncode != 0
    assert tree_snapshot(store, tmp_path / "xdg", checkout, include_git=False) == {
        path: content for path, content in before.items() if ".git" not in Path(path).parts
    }


def test_local_context_conflict_is_not_overwritten_or_bound(tmp_path: Path) -> None:
    env, repo, store, _remote, _project_id = configured_store(tmp_path)
    checkout = tmp_path / "conflicting-service"
    initialize_git_repository(checkout)
    run_git(checkout, "remote", "add", "origin", "https://github.com/acme/service")
    (checkout / "docs/context").mkdir(parents=True)
    (checkout / "docs/context/context.json").write_bytes(
        (store / "projects" / next(iter(manifest(store)["projects"])) / "context.json").read_bytes()
    )
    before = tree_snapshot(store, tmp_path / "xdg", checkout)
    detected = successful("git-store-detect", repo=checkout, env=env)
    assert json.loads(detected.stdout)["status"] == "conflict"
    assert tree_snapshot(store, tmp_path / "xdg", checkout) == before
    result = run_context("git-store-bind", "--match-remote", repo=checkout, env=env)
    assert result.returncode != 0
    assert tree_snapshot(store, tmp_path / "xdg", checkout, include_git=False) == {
        path: content for path, content in before.items() if ".git" not in Path(path).parts
    }
    assert (repo / "docs/context/index.md").exists()
