from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scope_test_support import (
    initialize_git_store,
    isolated_environment,
    project_context,
    read_json,
    run_context,
)


def git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def initialize_git_project(repo: Path, env: dict[str, str]) -> None:
    git_init(repo)
    initialized = run_context(
        "init",
        "--visibility",
        "local",
        repo=repo,
        env=env,
    )
    assert initialized.returncode == 0


def snapshot_token(output: str) -> str:
    prefix = "Snapshot token: "
    return next(
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    )


def test_approved_git_store_migration_moves_shareable_context_and_keeps_private_xdg(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    workspace = tmp_path / "workspace"
    repo = workspace / "service"
    store = tmp_path / "shared-context"
    initialize_git_project(repo, env)
    initialize_git_store(store)
    run_context(
        "add-pattern",
        "--name",
        "Request ownership",
        "--summary",
        "The service owns request validation",
        repo=repo,
        env=env,
    )
    run_context(
        "domain-set",
        "--domain",
        "payments",
        "--project",
        str(repo),
        repo=repo,
        env=env,
    )
    for term, applicability in (
        ("Settlement", "domain:payments"),
        ("UTC", "universal"),
        ("Preferred shell", "user:self"),
    ):
        run_context(
            "add-term",
            "--term",
            term,
            "--definition",
            f"Definition for {term}",
            "--applicability",
            applicability,
            repo=repo,
            env=env,
        )
    original = project_context(repo)
    preview = run_context(
        "git-store-init",
        "--store",
        str(store),
        "--workspace-root",
        str(workspace),
        repo=repo,
        env=env,
    )
    approved = run_context(
        "git-store-init",
        "--store",
        str(store),
        "--workspace-root",
        str(workspace),
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    project_path = store / "projects" / str(original["store_id"]) / "context.json"
    private_paths = tuple((tmp_path / "xdg/data/contexts/users").rglob("context.json"))
    config = read_json(tmp_path / "xdg/config/config.json")

    assert (
        preview.returncode,
        '"type": "UNTRUSTED_SNAPSHOT_DATA"' in preview.stdout,
        '"change": "move"' in preview.stdout,
        approved.returncode,
        (repo / "docs/context/context.json").exists(),
        read_json(project_path)["patterns"][0]["id"],
        original["patterns"][0]["id"],
        read_json(store / "scopes/domains/payments/context.json")["terms"][0]["term"],
        read_json(store / "scopes/universal/context.json")["terms"][0]["term"],
        (tmp_path / "xdg/data/contexts/domains/payments/context.json").exists(),
        (tmp_path / "xdg/data/contexts/universal/context.json").exists(),
        len(private_paths),
        read_json(private_paths[0])["terms"][0]["term"],
        config["git_store"]["project_bindings"][str(repo.resolve())],
    ) == (
        0,
        True,
        True,
        0,
        False,
        original["patterns"][0]["id"],
        original["patterns"][0]["id"],
        "Settlement",
        "UTC",
        False,
        False,
        1,
        "Preferred shell",
        original["store_id"],
    )


def test_git_store_preview_and_invalid_checkout_do_not_mutate_sources(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "not-a-git-store"
    initialize_git_project(repo, env)
    run_context(
        "add-term",
        "--term",
        "Ledger",
        "--definition",
        "A durable ledger",
        repo=repo,
        env=env,
    )
    before = (repo / "docs/context/context.json").read_bytes()
    config_path = tmp_path / "xdg/config/config.json"
    config_before = config_path.read_bytes()
    store.mkdir()
    result = run_context(
        "git-store-init",
        "--store",
        str(store),
        repo=repo,
        env=env,
    )

    assert (
        result.returncode,
        "must be a Git checkout root" in result.stderr,
        (repo / "docs/context/context.json").read_bytes(),
        tuple(store.iterdir()),
        config_path.read_bytes(),
    ) == (1, True, before, (), config_before)


def test_git_store_rejects_symlinked_destination_without_writing_outside(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    initialize_git_project(repo, env)
    initialize_git_store(store)
    outside.mkdir()
    (store / "projects").symlink_to(outside, target_is_directory=True)
    result = run_context(
        "git-store-init",
        "--store",
        str(store),
        repo=repo,
        env=env,
    )

    assert (
        result.returncode,
        "symlink" in result.stderr.casefold(),
        (repo / "docs/context/context.json").exists(),
        tuple(outside.iterdir()),
        (store / "project-context-store.json").exists(),
    ) == (1, True, True, (), False)


def test_git_store_rejects_stale_snapshot_without_partial_migration(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    initialize_git_project(repo, env)
    initialize_git_store(store)
    preview = run_context(
        "git-store-init",
        "--store",
        str(store),
        repo=repo,
        env=env,
    )
    config_path = tmp_path / "xdg/config/config.json"
    config_before = config_path.read_bytes()
    run_context(
        "add-term",
        "--term",
        "Changed",
        "--definition",
        "Changed after preview",
        repo=repo,
        env=env,
    )
    approved = run_context(
        "git-store-init",
        "--store",
        str(store),
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )

    assert (
        approved.returncode,
        "snapshot changed" in approved.stderr.casefold(),
        (repo / "docs/context/context.json").exists(),
        (store / "project-context-store.json").exists(),
        config_path.read_bytes(),
    ) == (1, True, True, False, config_before)


def test_configured_git_store_is_canonical_for_new_project_writes(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    first = tmp_path / "first"
    second = tmp_path / "second"
    store = tmp_path / "store"
    initialize_git_project(first, env)
    initialize_git_store(store)
    preview = run_context(
        "git-store-init",
        "--store",
        str(store),
        repo=first,
        env=env,
    )
    run_context(
        "git-store-init",
        "--store",
        str(store),
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=first,
        env=env,
    )
    git_init(second)
    initialized = run_context("init", repo=second, env=env)
    added = run_context(
        "add-component",
        "--name",
        "Worker",
        "--responsibility",
        "Processes jobs",
        repo=second,
        env=env,
    )
    config = read_json(tmp_path / "xdg/config/config.json")
    store_id = config["git_store"]["project_bindings"][str(second.resolve())]
    canonical = read_json(store / "projects" / store_id / "context.json")
    search = run_context("search", "--query", "Processes", repo=second, env=env)

    assert (
        initialized.returncode,
        added.returncode,
        (second / "docs/context/context.json").exists(),
        canonical["components"][0]["name"],
        (second / "docs/context/components.md").exists(),
        "Worker" in search.stdout,
    ) == (0, 0, False, "Worker", True, True)


def test_workspace_applicability_is_read_only_and_blocks_git_store_migration(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    initialize_git_project(repo, env)
    initialize_git_store(store)
    rejected_write = run_context(
        "add-term",
        "--term",
        "Legacy",
        "--definition",
        "Legacy workspace fact",
        "--applicability",
        f"workspace:{tmp_path}",
        repo=repo,
        env=env,
    )
    legacy = tmp_path / "xdg/data/contexts/workspaces/legacy/context.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "store_id": "77384fb9-0c7c-4106-b02e-0d6d8e0f3e46",
                "default_applicability": [
                    {"kind": "workspace", "selector": str(tmp_path)}
                ],
                "scope_store": {
                    "applicability": [{"kind": "workspace", "selector": str(tmp_path)}]
                },
                "terms": [
                    {
                        "id": "43cd9ac3-024f-49ee-9e85-8473b8414007",
                        "term": "Legacy",
                        "kind": "domain-term",
                        "definition": "Legacy workspace fact",
                        "scope": "workspace",
                        "applicability": [
                            {"kind": "workspace", "selector": str(tmp_path)}
                        ],
                        "provenance": [],
                    }
                ],
                "components": [],
                "patterns": [],
                "open_questions": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    migration = run_context(
        "git-store-init",
        "--store",
        str(store),
        repo=repo,
        env=env,
    )

    assert (
        rejected_write.returncode,
        "workspace" in rejected_write.stderr,
        migration.returncode,
        "reclassify" in migration.stderr.casefold(),
        legacy.exists(),
        (store / "project-context-store.json").exists(),
    ) == (1, True, 1, True, True, False)


def test_existing_git_store_can_bind_a_fresh_checkout_by_stable_store_id(
    tmp_path: Path,
) -> None:
    first_env = isolated_environment(tmp_path / "first-xdg")
    original = tmp_path / "original"
    fresh = tmp_path / "fresh"
    store = tmp_path / "store"
    initialize_git_project(original, first_env)
    initialize_git_store(store)
    run_context(
        "add-term",
        "--term",
        "Portable",
        "--definition",
        "Knowledge restored from Git",
        repo=original,
        env=first_env,
    )
    run_context(
        "domain-set",
        "--domain",
        "portable-domain",
        "--project",
        str(original),
        repo=original,
        env=first_env,
    )
    run_context(
        "add-term",
        "--term",
        "Settlement",
        "--definition",
        "Domain knowledge restored from Git",
        "--applicability",
        "domain:portable-domain",
        repo=original,
        env=first_env,
    )
    project_id = str(project_context(original)["store_id"])
    first_preview = run_context(
        "git-store-init", "--store", str(store), repo=original, env=first_env
    )
    run_context(
        "git-store-init",
        "--store",
        str(store),
        "--approve-snapshot",
        snapshot_token(first_preview.stdout),
        repo=original,
        env=first_env,
    )
    second_env = isolated_environment(tmp_path / "second-xdg")
    git_init(fresh)
    second_preview = run_context(
        "git-store-init", "--store", str(store), repo=fresh, env=second_env
    )
    configured = run_context(
        "git-store-init",
        "--store",
        str(store),
        "--approve-snapshot",
        snapshot_token(second_preview.stdout),
        repo=fresh,
        env=second_env,
    )
    bound = run_context(
        "git-store-bind",
        "--project-store-id",
        project_id,
        repo=fresh,
        env=second_env,
    )
    search = run_context("search", "--query", "restored", repo=fresh, env=second_env)
    domain_search = run_context(
        "search", "--query", "Settlement", repo=fresh, env=second_env
    )
    config = read_json(tmp_path / "second-xdg/config/config.json")

    assert (
        configured.returncode,
        bound.returncode,
        config["git_store"]["project_bindings"][str(fresh.resolve())],
        "Portable" in search.stdout,
        "Settlement" in domain_search.stdout,
        config["domains"]["portable-domain"],
        (fresh / "docs/context/index.md").exists(),
        (fresh / "docs/context/context.json").exists(),
    ) == (
        0,
        0,
        project_id,
        True,
        True,
        {"projects": [str(fresh.resolve())], "remotes": []},
        True,
        False,
    )
