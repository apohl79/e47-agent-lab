from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from scope_test_support import (
    SCRIPT,
    initialize_git_repository,
    initialize_git_store,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)


def load_project_context_module():
    spec = importlib.util.spec_from_file_location("remote_identity_context", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshot_token(output: str) -> str:
    prefix = "Snapshot token: "
    return next(
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    )


def configure_git_store_runtime(repo: Path, store: Path, env: dict[str, str]) -> None:
    preview = run_context("git-store-init", "--store", str(store), repo=repo, env=env)
    approved = run_context(
        "git-store-init",
        "--store",
        str(store),
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    assert approved.returncode == 0, approved.stderr


def git_repository_with_remote(path: Path, remote: str) -> Path:
    initialize_git_repository(path)
    run_git(path, "remote", "add", "origin", remote)
    return path


def manifest(store: Path) -> dict[str, object]:
    return read_json(store / "project-context-store.json")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("git@github.com:acme/service.git", "github.com/acme/service"),
        ("https://GitHub.com/acme/service.git", "github.com/acme/service"),
        ("ssh://git@github.com/acme/service", "github.com/acme/service"),
        ("https://user:token@github.com/acme/service/", "github.com/acme/service"),
        (
            "git+ssh://git@gitlab.example.org/group/sub/repo.git",
            "gitlab.example.org/group/sub/repo",
        ),
        ("/tmp/store-remote.git", None),
        ("file:///tmp/store-remote.git", None),
        ("https://github.com/", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_remote_url_partitions(raw: str | None, expected: str | None) -> None:
    module = load_project_context_module()

    assert module.normalize_remote_url(raw) == expected


def test_init_records_normalized_remote_url_in_manifest(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    store = tmp_path / "store"
    repo = git_repository_with_remote(
        tmp_path / "service", "git@github.com:acme/service.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(repo, store, env)

    initialized = run_context("init", repo=repo, env=env)
    projects = manifest(store)["projects"]

    assert (
        initialized.returncode,
        [metadata["remote_url"] for metadata in projects.values()],
    ) == (0, ["github.com/acme/service"])


def test_init_rejects_second_checkout_of_registered_remote(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    store = tmp_path / "store"
    first = git_repository_with_remote(
        tmp_path / "service", "git@github.com:acme/service.git"
    )
    second = git_repository_with_remote(
        tmp_path / "service-copy", "https://github.com/acme/service.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(first, store, env)
    assert run_context("init", repo=first, env=env).returncode == 0

    rejected = run_context("init", repo=second, env=env)

    assert (
        rejected.returncode,
        "git-store-bind --match-remote" in rejected.stderr,
        len(manifest(store)["projects"]),
    ) == (1, True, 1)


def test_git_store_bind_match_remote_attaches_fresh_checkout(tmp_path: Path) -> None:
    first_env = isolated_environment(tmp_path / "first-xdg")
    second_env = isolated_environment(tmp_path / "second-xdg")
    store = tmp_path / "store"
    original = git_repository_with_remote(
        tmp_path / "original", "git@github.com:acme/service.git"
    )
    fresh = git_repository_with_remote(
        tmp_path / "fresh", "https://github.com/acme/service.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(original, store, first_env)
    assert run_context("init", repo=original, env=first_env).returncode == 0
    run_context(
        "add-term",
        "--term",
        "Portable",
        "--definition",
        "Knowledge restored through the remote URL",
        repo=original,
        env=first_env,
    )
    project_id = next(iter(manifest(store)["projects"]))
    configure_git_store_runtime(fresh, store, second_env)

    bound = run_context("git-store-bind", "--match-remote", repo=fresh, env=second_env)
    search = run_context("search", "--query", "Portable", repo=fresh, env=second_env)
    config = read_json(tmp_path / "second-xdg/config/config.json")

    assert (
        bound.returncode,
        config["git_store"]["project_bindings"][str(fresh.resolve())],
        "Portable" in search.stdout,
        (fresh / "docs/context/index.md").exists(),
    ) == (0, project_id, True, True)


def test_git_store_bind_match_remote_without_registered_remote_fails(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    store = tmp_path / "store"
    repo = git_repository_with_remote(
        tmp_path / "service", "git@github.com:acme/unknown.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(repo, store, env)

    bound = run_context("git-store-bind", "--match-remote", repo=repo, env=env)

    assert (bound.returncode, "matches 0 project contexts" in bound.stderr) == (1, True)


def test_update_backfills_missing_remote_url(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    store = tmp_path / "store"
    repo = git_repository_with_remote(
        tmp_path / "service", "git@github.com:acme/service.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(repo, store, env)
    assert run_context("init", repo=repo, env=env).returncode == 0
    manifest_path = store / "project-context-store.json"
    stripped = manifest(store)
    for metadata in stripped["projects"].values():
        del metadata["remote_url"]
    manifest_path.write_text(json.dumps(stripped, indent=2) + "\n", encoding="utf-8")

    updated = run_context("update", repo=repo, env=env)
    projects = manifest(store)["projects"]

    assert (
        updated.returncode,
        [metadata.get("remote_url") for metadata in projects.values()],
    ) == (0, ["github.com/acme/service"])


def test_git_store_status_lists_remote_column(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    store = tmp_path / "store"
    repo = git_repository_with_remote(
        tmp_path / "service", "git@github.com:acme/service.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(repo, store, env)
    assert run_context("init", repo=repo, env=env).returncode == 0

    status = run_context("git-store-status", repo=repo, env=env)

    assert "| service | github.com/acme/service |" in status.stdout
