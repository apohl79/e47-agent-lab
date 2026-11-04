from __future__ import annotations

import json
from pathlib import Path

from scope_test_support import (
    initialize_git_repository,
    initialize_git_store,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)


def successful(*arguments: str, repo: Path, env: dict[str, str]):
    result = run_context(*arguments, repo=repo, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def token(output: str) -> str:
    return next(
        line.removeprefix("Snapshot token: ")
        for line in output.splitlines()
        if line.startswith("Snapshot token: ")
    )


def manifest(store: Path) -> dict:
    return read_json(store / "project-context-store.json")


def tree_snapshot(*roots: Path, include_git: bool = True) -> dict[str, bytes]:
    return {
        str(path): path.read_bytes()
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and (include_git or ".git" not in path.relative_to(root).parts)
    }


def configured_store(
    tmp_path: Path,
    domain: str = "cpl",
    source_remote: str = "https://github.com/acme/service.git",
):
    env = isolated_environment(tmp_path / "xdg")
    hooks = tmp_path / "test-hooks"
    hooks.mkdir()
    # File transport and test commit identity apply only to these CLI subprocesses.
    settings = (
        ("protocol.file.allow", "always"),
        ("user.name", "Context Curator Tests"),
        ("user.email", "context-curator@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    )
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    repo = tmp_path / "service"
    initialize_git_repository(repo)
    run_git(repo, "remote", "add", "origin", source_remote)
    successful("init", "--visibility", "local", repo=repo, env=env)
    successful(
        "add-term", "--term", "Ledger", "--definition", "Durable ledger",
        repo=repo, env=env,
    )
    store = tmp_path / "store"
    remote = initialize_git_store(store)
    arguments = ("git-store-init", "--store", str(store))
    preview = successful(*arguments, repo=repo, env=env)
    successful(
        *arguments, "--approve-snapshot", token(preview.stdout), repo=repo, env=env
    )
    project_id = next(iter(manifest(store)["projects"]))
    successful(
        "domain-set", "--domain", domain, "--project", str(repo), repo=repo, env=env
    )
    successful(
        "add-term", "--term", "Domain ledger", "--definition", "Domain invariant",
        "--applicability", f"domain:{domain}", repo=repo, env=env,
    )
    return env, repo, store, remote, project_id


def domain_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "cpl.git"
    run_git(tmp_path, "init", "--bare", str(remote))
    run_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def place_domain(repo: Path, env: dict[str, str], store: str, remote: Path | None = None):
    arguments = ("domain-store", "--domain", "cpl", "--store", store)
    if remote is not None:
        arguments += ("--remote", str(remote))
    preview = successful(*arguments, repo=repo, env=env)
    return successful(
        *arguments, "--approve-snapshot", token(preview.stdout), repo=repo, env=env
    )


def test_legacy_store_without_new_metadata_remains_readable_and_writable(
    tmp_path: Path,
) -> None:
    env, repo, store, remote, project_id = configured_store(tmp_path)
    data = manifest(store)
    data["schema_version"] = 1
    for field in ("stores", "domain_owners", "project_owners"):
        data.pop(field, None)
    path = store / "project-context-store.json"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    run_git(store, "add", path.name)
    run_git(store, "commit", "-m", "Legacy store fixture")
    run_git(store, "push", "origin", "main")

    successful("status", repo=repo, env=env)
    successful(
        "add-term", "--term", "Compatible", "--definition", "Old stores stay writable",
        repo=repo, env=env,
    )
    canonical = read_json(store / "projects" / project_id / "context.json")
    assert any(term["term"] == "Compatible" for term in canonical["terms"])
    assert run_git(store, "rev-parse", "HEAD").stdout == run_git(
        remote, "rev-parse", "refs/heads/main"
    ).stdout


def test_main_store_placement_requires_approval_and_keeps_context_in_main(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, project_id = configured_store(tmp_path)
    before = tree_snapshot(store, tmp_path / "xdg")
    arguments = ("domain-store", "--domain", "cpl", "--store", "main")
    preview = successful(*arguments, repo=repo, env=env)
    assert tree_snapshot(store, tmp_path / "xdg") == before

    successful(
        *arguments, "--approve-snapshot", token(preview.stdout), repo=repo, env=env
    )
    assert (store / "projects" / project_id / "context.json").is_file()
    assert (store / "scopes/domains/cpl/context.json").is_file()
    assert manifest(store).get("domain_owners", {}).get("cpl", "main") == "main"
    assert not (store / ".gitmodules").exists()


def test_domain_round_trip_preserves_whole_bundle_and_publishes_child_first(
    tmp_path: Path,
) -> None:
    env, repo, store, root_remote, project_id = configured_store(tmp_path)
    child_remote = domain_remote(tmp_path)
    project_relative = Path("projects") / project_id / "context.json"
    scope_relative = Path("scopes/domains/cpl/context.json")
    before_project = (store / project_relative).read_bytes()
    before_scope = (store / scope_relative).read_bytes()
    place_domain(repo, env, "cpl", child_remote)
    child = store / "stores/cpl"

    assert (child / project_relative).read_bytes() == before_project
    assert (child / scope_relative).read_bytes() == before_scope
    assert not (store / project_relative).exists()
    assert not (store / scope_relative).exists()
    assert manifest(store)["project_owners"][project_id] == "cpl"
    assert manifest(store)["domain_owners"]["cpl"] == "cpl"
    assert run_git(store, "ls-files", "--stage", "stores/cpl").stdout.startswith("160000 ")
    assert run_git(child, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert run_git(child, "rev-parse", "--abbrev-ref", "@{upstream}").stdout.strip() == "origin/main"

    # Reject publishing the root before its gitlink commit reaches the child remote.
    hook = tmp_path / "test-hooks/pre-push"
    marker = tmp_path / "publication-order-checked"
    hook.write_text(
        "#!/bin/sh\n"
        "[ \"$(git rev-parse --show-toplevel)\" = '"
        + str(store.resolve())
        + "' ] || exit 0\n"
        "child=$(git ls-tree HEAD stores/cpl | awk '{print $3}')\n"
        "[ -z \"$child\" ] || git --git-dir='"
        + str(child_remote)
        + "' cat-file -e \"$child^{commit}\" || exit 1\n"
        "touch '" + str(marker) + "'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    successful(
        "add-term", "--term", "Child write", "--definition", "Published before root",
        repo=repo, env=env,
    )
    child_head = run_git(child, "rev-parse", "HEAD").stdout.strip()
    assert marker.exists()
    assert run_git(child_remote, "rev-parse", "refs/heads/main").stdout.strip() == child_head
    assert run_git(root_remote, "ls-tree", "main", "stores/cpl").stdout.split()[2] == child_head
    assert not run_git(store, "status", "--porcelain").stdout
    updated_project = (child / project_relative).read_bytes()

    place_domain(repo, env, "main")
    assert (store / project_relative).read_bytes() == updated_project
    assert (store / scope_relative).read_bytes() == before_scope
    assert manifest(store).get("project_owners", {}).get(project_id, "main") == "main"
    assert not (child / project_relative).exists()


def test_stale_domain_snapshot_rejects_without_mutation(tmp_path: Path) -> None:
    env, repo, store, _remote, _project_id = configured_store(tmp_path)
    child_remote = domain_remote(tmp_path)
    arguments = (
        "domain-store", "--domain", "cpl", "--store", "cpl",
        "--remote", str(child_remote),
    )
    preview = successful(*arguments, repo=repo, env=env)
    successful(
        "add-term", "--term", "Changed", "--definition", "Invalidates migration",
        repo=repo, env=env,
    )
    before = tree_snapshot(store, child_remote, tmp_path / "xdg")
    result = run_context(
        *arguments, "--approve-snapshot", token(preview.stdout), repo=repo, env=env
    )
    assert result.returncode != 0
    assert tree_snapshot(store, child_remote, tmp_path / "xdg") == before
    assert not (store / ".gitmodules").exists()


def test_missing_domain_checkout_fails_without_main_store_fallback(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, project_id = configured_store(tmp_path)
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    run_git(store, "submodule", "deinit", "--force", "--", "stores/cpl")
    before = tree_snapshot(store, tmp_path / "xdg", include_git=False)
    head = run_git(store, "rev-parse", "HEAD").stdout
    result = run_context(
        "add-term", "--term", "Misplaced", "--definition", "Must not fall back",
        repo=repo, env=env,
    )
    assert result.returncode != 0
    assert tree_snapshot(store, tmp_path / "xdg", include_git=False) == before
    assert run_git(store, "rev-parse", "HEAD").stdout == head
    assert not (store / "projects" / project_id / "context.json").exists()
    assert "cpl" in result.stderr


def test_activation_attaches_registered_child_and_rejects_unknown_store(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, project_id = configured_store(tmp_path)
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    run_git(store, "submodule", "deinit", "--force", "--", "stores/cpl")
    before = tree_snapshot(store, tmp_path / "xdg")
    rejected = run_context(
        "git-store-activate", "--store", "unknown", repo=repo, env=env
    )
    assert rejected.returncode != 0
    assert tree_snapshot(store, tmp_path / "xdg") == before
    successful("git-store-activate", "--store", "cpl", repo=repo, env=env)
    child = store / "stores/cpl"
    assert (child / "projects" / project_id / "context.json").exists()
    assert run_git(child, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert run_git(child, "rev-parse", "--abbrev-ref", "@{upstream}").stdout.strip() == "origin/main"


def test_shared_project_retains_exactly_one_canonical_owner(tmp_path: Path) -> None:
    env, repo, store, _remote, project_id = configured_store(tmp_path)
    successful(
        "domain-set", "--domain", "shared", "--project", str(repo), repo=repo, env=env
    )
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    metadata = manifest(store)
    assert project_id in metadata["domains"]["cpl"]
    assert project_id in metadata["domains"]["shared"]
    assert metadata["project_owners"][project_id] == "cpl"
    assert len(tuple(store.glob(f"**/projects/{project_id}/context.json"))) == 1
