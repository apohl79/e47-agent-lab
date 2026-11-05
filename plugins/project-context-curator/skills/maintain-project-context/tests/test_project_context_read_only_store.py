from __future__ import annotations

import json
from pathlib import Path

import pytest

from scope_test_support import (
    initialize,
    initialize_git_repository,
    initialize_git_store,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)


@pytest.fixture
def snapshot(tmp_path: Path):
    producer_env = isolated_environment(tmp_path / "producer")
    project = tmp_path / "source"
    initialize(project, producer_env)
    for arguments in (
        ("add-term", "--term", "Runtime service", "--definition", "Project knowledge"),
        ("domain-set", "--domain", "voice", "--project", str(project)),
        ("add-term", "--term", "Voice stamp", "--definition", "Domain knowledge",
         "--applicability", "domain:voice"),
    ):
        result = run_context(*arguments, repo=project, env=producer_env)
        assert result.returncode == 0, result.stderr
    store = tmp_path / "producer-store"
    remote = initialize_git_store(store)
    arguments = ("storage-migrate", "--target", "git-store", "--store", str(store))
    preview = run_context(*arguments, repo=project, env=producer_env)
    token = next(line.removeprefix("Snapshot token: ") for line in preview.stdout.splitlines()
                 if line.startswith("Snapshot token: "))
    migrated = run_context(*arguments, "--approve-snapshot", token,
                           repo=project, env=producer_env)
    assert migrated.returncode == 0, migrated.stderr
    clone = tmp_path / "snapshot"
    run_git(tmp_path, "clone", str(remote), str(clone))
    run_git(clone, "checkout", "--detach")
    run_git(clone, "remote", "remove", "origin")
    project_id = next(iter(read_json(clone / "project-context-store.json")["projects"]))
    client = tmp_path / "consumer"
    client.mkdir()
    env = isolated_environment(tmp_path / "consumer-runtime")
    return clone, remote, client, env, project_id


def attach(snapshot):
    clone, _remote, client, env, _project_id = snapshot
    result = run_context("git-store-attach", "--store", str(clone), "--read-only",
                         repo=client, env=env)
    assert result.returncode == 0, result.stderr
    return result


def store_state(clone: Path, remote: Path):
    return (
        {str(path.relative_to(clone)): path.read_bytes()
         for path in clone.rglob("*") if path.is_file() and ".git" not in path.parts},
        run_git(clone, "show-ref").stdout,
        run_git(clone, "rev-parse", "HEAD").stdout,
        run_git(remote, "show-ref").stdout,
        run_git(clone, "status", "--porcelain").stdout,
    )


def test_attach_reads_standalone_snapshot_without_binding_or_publication(snapshot):
    clone, remote, client, env, project_id = snapshot
    before = store_state(clone, remote)
    attach(snapshot)
    config_path = Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json"
    config_before = config_path.read_bytes()
    attach(snapshot)
    assert config_path.read_bytes() == config_before
    for command in ("status", "search"):
        arguments = ("--query", "knowledge") if command == "search" else ()
        result = run_context(command, "--project-store-id", project_id, *arguments,
                             repo=client, env=env)
        assert result.returncode == 0, result.stderr
        if command == "search":
            assert "Project knowledge" in result.stdout
            assert "Domain knowledge" in result.stdout
    listed = run_context("git-store-status", repo=client, env=env)
    assert listed.returncode == 0, listed.stderr
    assert project_id in listed.stdout and "Read-only" in listed.stdout
    status = run_context("storage-status", "--format", "json", repo=client, env=env)
    assert json.loads(status.stdout)["read_only"] is True
    assert store_state(clone, remote) == before
    assert list(client.iterdir()) == []


@pytest.mark.parametrize("arguments", [
    ("init",), ("update",), ("ignore",), ("git-store-publish",),
    ("global-update",), ("git-store-activate", "--all"),
    ("domain-remove", "--domain", "voice"),
    ("add-term", "--term", "Forbidden", "--definition", "Must not be written"),
    ("pattern-category", "delete", "--category", "forbidden"),
    ("storage-migrate", "--target", "local", "--project-visibility", "local"),
])
def test_read_only_attachment_rejects_mutations_before_side_effects(snapshot, arguments):
    clone, remote, client, env, _project_id = snapshot
    attach(snapshot)
    before = store_state(clone, remote)
    result = run_context(*arguments, repo=client, env=env)
    assert result.returncode == 1, result.stderr
    assert "disabled for a read-only store attachment" in result.stderr
    assert store_state(clone, remote) == before
    assert list(client.iterdir()) == []


def test_attach_requires_manifest_and_preserves_existing_runtime(snapshot, tmp_path):
    clone, _remote, client, env, _project_id = snapshot
    empty = tmp_path / "empty"
    initialize_git_repository(empty)
    missing = run_context("git-store-attach", "--store", str(empty), "--read-only",
                          repo=client, env=env)
    assert missing.returncode == 1
    assert "existing store manifest" in missing.stderr
    assert not Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]).exists()
    initialized = run_context("init", "--visibility", "local", repo=client, env=env)
    assert initialized.returncode == 0, initialized.stderr
    config = Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json"
    before = config.read_bytes()
    rejected = run_context("git-store-attach", "--store", str(clone), "--read-only",
                           repo=client, env=env)
    assert rejected.returncode == 1
    assert "existing configuration will not be replaced" in rejected.stderr
    assert config.read_bytes() == before


def test_attach_refuses_runtime_files_inside_snapshot(snapshot):
    clone, remote, client, env, _project_id = snapshot
    env = {**env, "PROJECT_CONTEXT_CURATOR_CACHE_DIR": str(clone / "cache")}
    before = store_state(clone, remote)
    rejected = run_context("git-store-attach", "--store", str(clone), "--read-only",
                           repo=client, env=env)
    assert rejected.returncode == 1
    assert "outside the store" in rejected.stderr
    assert store_state(clone, remote) == before


@pytest.mark.parametrize("destination", [
    "canonical", "symlink", "git", "cache", "worktree-git", "cache-symlink",
])
def test_graph_cannot_write_into_read_only_snapshot(snapshot, tmp_path, destination):
    clone, remote, client, env, project_id = snapshot
    git_config_path = clone / ".git/config"
    if destination == "worktree-git":
        linked = tmp_path / "linked"
        run_git(clone, "worktree", "add", "--detach", str(linked))
        clone = linked
        snapshot = clone, remote, client, env, project_id
    attach(snapshot)
    before = store_state(clone, remote)
    git_config = git_config_path.read_bytes()
    canonical = clone / "projects" / project_id / "context.json"
    output = canonical
    if destination == "symlink":
        output = tmp_path / "alias.json"
        output.symlink_to(canonical)
    elif destination in {"git", "worktree-git"}:
        output = git_config_path
    elif destination == "cache":
        env = {**env, "PROJECT_CONTEXT_CURATOR_CACHE_DIR": str(clone / "cache")}
    elif destination == "cache-symlink":
        cache = Path(env["PROJECT_CONTEXT_CURATOR_CACHE_DIR"])
        cache.mkdir(parents=True)
        (cache / "graph").symlink_to(clone, target_is_directory=True)
    arguments = ("--format", "html") if destination.startswith("cache") else (
        "--format", "json", "--output", str(output)
    )
    rejected = run_context("graph", *arguments, repo=client, env=env)
    assert rejected.returncode == 1, rejected.stderr
    assert "outside the store" in rejected.stderr
    assert store_state(clone, remote) == before
    assert git_config_path.read_bytes() == git_config


def test_read_only_graph_can_export_outside_snapshot(snapshot, tmp_path):
    clone, remote, client, env, _project_id = snapshot
    attach(snapshot)
    before = store_state(clone, remote)
    output = tmp_path / "report.json"
    exported = run_context("graph", "--format", "json", "--output", str(output),
                           repo=client, env=env)
    assert exported.returncode == 0, exported.stderr
    assert read_json(output)["nodes"]
    assert store_state(clone, remote) == before
