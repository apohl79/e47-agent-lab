from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scope_test_support import (
    initialize_git_repository,
    initialize_git_store,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)


def snapshot_token(output: str) -> str:
    prefix = "Snapshot token: "
    return next(
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    )


def migration_arguments(
    store: Path, workspace: Path, token: str | None = None
) -> tuple[str, ...]:
    arguments = (
        "storage-migrate",
        "--target",
        "git-store",
        "--store",
        str(store),
        "--workspace-root",
        str(workspace),
    )
    return arguments if token is None else (*arguments, "--approve-snapshot", token)


def initialize_local_context(tmp_path: Path) -> tuple[dict[str, str], Path]:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize_git_repository(repo)
    run_context("init", "--visibility", "local", repo=repo, env=env)
    return env, repo


def configure_store(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path]:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    initialize_git_repository(repo)
    run_context("init", "--visibility", "local", repo=repo, env=env)
    remote = initialize_git_store(store)
    preview = run_context(*migration_arguments(store, tmp_path), repo=repo, env=env)
    result = run_context(
        *migration_arguments(store, tmp_path, snapshot_token(preview.stdout)),
        repo=repo,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return env, repo, store, remote


def store_context_path(env: dict[str, str], repo: Path, store: Path) -> Path:
    config = read_json(Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json")
    project_id = config["git_store"]["project_bindings"][str(repo.resolve())]
    return store / "projects" / str(project_id) / "context.json"


def add_term(
    repo: Path, env: dict[str, str], term: str, definition: str
) -> subprocess.CompletedProcess[str]:
    return run_context(
        "add-term",
        "--term",
        term,
        "--definition",
        definition,
        repo=repo,
        env=env,
    )


def add_pattern(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_context(
        "add-pattern",
        "--name",
        "Ownership",
        "--summary",
        "The repository owns its context",
        repo=repo,
        env=env,
    )


def advance_remote_manifest(tmp_path: Path, remote: Path) -> str:
    clone = tmp_path / "collaborator"
    hooks = tmp_path / "collaborator-hooks"
    hooks.mkdir()
    run_git(tmp_path, "clone", "--quiet", str(remote), str(clone))
    run_git(clone, "config", "user.name", "Context Curator Tests")
    run_git(clone, "config", "user.email", "context-curator@example.invalid")
    run_git(clone, "config", "commit.gpgsign", "false")
    run_git(clone, "config", "core.hooksPath", str(hooks))
    manifest = clone / "project-context-store.json"
    data = read_json(manifest)
    data["remote_marker"] = "preserved"
    manifest.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    run_git(clone, "add", "project-context-store.json")
    run_git(clone, "commit", "-m", "chore(context): remote update")
    run_git(clone, "push", "origin", "main")
    return run_git(clone, "rev-parse", "HEAD").stdout.strip()


def test_changed_git_store_command_commits_and_pushes(tmp_path: Path) -> None:
    env, repo, store, remote = configure_store(tmp_path)
    before = run_git(store, "rev-parse", "HEAD").stdout.strip()
    result = add_pattern(repo, env)
    local = run_git(store, "rev-parse", "HEAD").stdout.strip()
    upstream = run_git(remote, "rev-parse", "refs/heads/main").stdout.strip()

    assert (
        result.returncode,
        local != before,
        upstream,
        run_git(store, "status", "--short").stdout,
    ) == (0, True, local, "")


def test_remote_advance_is_preserved_before_record_update(tmp_path: Path) -> None:
    env, repo, store, remote = configure_store(tmp_path)
    remote_commit = advance_remote_manifest(tmp_path, remote)

    result = add_term(repo, env, "Merged", "Written after upstream synchronization")
    local = run_git(store, "rev-parse", "HEAD").stdout.strip()
    run_git(store, "merge-base", "--is-ancestor", remote_commit, local)

    assert (
        result.returncode,
        run_git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
        read_json(store / "project-context-store.json")["remote_marker"],
        run_git(store, "status", "--short").stdout,
    ) == (0, local, "preserved", "")


def test_unchanged_update_does_not_create_empty_commit(tmp_path: Path) -> None:
    env, repo, store, _remote = configure_store(tmp_path)
    before = run_git(store, "rev-parse", "HEAD").stdout.strip()

    result = run_context("update", repo=repo, env=env)

    assert (
        result.returncode,
        run_git(store, "rev-parse", "HEAD").stdout.strip(),
        run_git(store, "status", "--short").stdout,
    ) == (0, before, "")


def test_missing_upstream_rejects_before_canonical_mutation(tmp_path: Path) -> None:
    env, repo = initialize_local_context(tmp_path)
    store = tmp_path / "store"
    initialize_git_repository(store)
    run_git(store, "branch", "-M", "main")
    result = run_context(*migration_arguments(store, tmp_path), repo=repo, env=env)

    assert (
        result.returncode,
        "push remote" in result.stderr,
        (repo / "docs/context/context.json").exists(),
        (store / "project-context-store.json").exists(),
    ) == (1, True, True, False)


def test_changed_push_target_rejects_approved_snapshot(tmp_path: Path) -> None:
    env, repo = initialize_local_context(tmp_path)
    store = tmp_path / "store"
    initialize_git_store(store)
    preview = run_context(*migration_arguments(store, tmp_path), repo=repo, env=env)
    alternate = tmp_path / "alternate.git"
    run_git(tmp_path, "init", "--bare", str(alternate))
    run_git(store, "remote", "set-url", "--push", "origin", str(alternate))

    result = run_context(
        *migration_arguments(store, tmp_path, snapshot_token(preview.stdout)),
        repo=repo,
        env=env,
    )

    assert (
        result.returncode,
        "snapshot changed" in result.stderr,
        (repo / "docs/context/context.json").exists(),
        (store / "project-context-store.json").exists(),
    ) == (1, True, True, False)


def test_non_main_checkout_rejects_before_canonical_mutation(tmp_path: Path) -> None:
    env, repo = initialize_local_context(tmp_path)
    store = tmp_path / "store"
    initialize_git_store(store)
    run_git(store, "branch", "-M", "feature")

    result = run_context(*migration_arguments(store, tmp_path), repo=repo, env=env)

    assert (
        result.returncode,
        "checked out on main" in result.stderr,
        (repo / "docs/context/context.json").exists(),
        (store / "project-context-store.json").exists(),
    ) == (1, True, True, False)


def test_unmanaged_store_change_rejects_record_update(tmp_path: Path) -> None:
    env, repo, store, _remote = configure_store(tmp_path)
    canonical = store_context_path(env, repo, store)
    before = canonical.read_bytes()
    (store / "README.md").write_text("unrelated\n", encoding="utf-8")

    result = add_term(repo, env, "Blocked", "Must not be written")

    assert (
        result.returncode,
        "unmanaged changes" in result.stderr,
        canonical.read_bytes(),
        (store / "README.md").read_text(encoding="utf-8"),
    ) == (1, True, before, "unrelated\n")


def test_rejected_push_keeps_local_commit(tmp_path: Path) -> None:
    env, repo, store, remote = configure_store(tmp_path)
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    upstream = run_git(remote, "rev-parse", "refs/heads/main").stdout.strip()

    result = add_term(repo, env, "Pending", "Committed locally after a rejected push")
    local = run_git(store, "rev-parse", "HEAD").stdout.strip()
    data = json.loads(store_context_path(env, repo, store).read_text(encoding="utf-8"))

    assert (
        result.returncode,
        "committed locally" in result.stderr,
        local != upstream,
        data["terms"][0]["term"],
        run_git(store, "status", "--short").stdout,
    ) == (1, True, True, "Pending", "")
