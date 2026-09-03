from __future__ import annotations

from pathlib import Path

from scope_test_support import (
    initialize,
    initialize_git_repository,
    initialize_git_store,
    isolated_environment,
    read_json,
    run_context,
    run_git,
)


DOMAIN = "software-engineering"


def run_successfully(
    *arguments: str,
    repo: Path,
    env: dict[str, str],
):
    result = run_context(*arguments, repo=repo, env=env)
    assert result.returncode == 0, result.stderr
    return result


def config(env: dict[str, str]) -> dict[str, object]:
    path = Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json"
    return read_json(path) if path.exists() else {}


def snapshot_token(output: str) -> str:
    return next(
        line.removeprefix("Snapshot token: ")
        for line in output.splitlines()
        if line.startswith("Snapshot token: ")
    )


def configure_git_store(repo: Path, store: Path, env: dict[str, str]) -> None:
    preview = run_successfully(
        "git-store-init", "--store", str(store), repo=repo, env=env
    )
    run_successfully(
        "git-store-init",
        "--store",
        str(store),
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )


def test_domain_root_enrolls_existing_and_future_projects_at_path_boundaries(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    root = tmp_path / "workspace/code"
    nested = root / "team/service"
    prefix_sibling = tmp_path / "workspace/code-other/service"
    previous = tmp_path / "previous"
    for project in (root, nested, prefix_sibling, previous):
        initialize(project, env)

    narrow = run_successfully(
        "domain-set",
        "--domain",
        "narrow",
        "--root",
        str(root / "docs"),
        repo=root,
        env=env,
    )
    configured = run_successfully(
        "domain-set",
        "--domain",
        DOMAIN,
        "--root",
        str(root),
        repo=root,
        env=env,
    )
    future = root / "future"
    initialize_git_repository(future)
    run_successfully(
        "init", "--visibility", "local", repo=future, env=env
    )
    moved = root / "moved"
    previous.rename(moved)
    run_successfully("update", repo=moved, env=env)
    rejected = run_context(
        "add-term",
        "--term",
        "Clamp",
        "--definition",
        "Mechanically enforced engineering constraint",
        "--applicability",
        f"domain:{DOMAIN}",
        repo=prefix_sibling,
        env=env,
    )
    domain = config(env)["domains"][DOMAIN]

    assert (
        narrow.stdout.strip(),
        config(env)["domains"]["narrow"]["projects"],
        configured.stdout.strip(),
        domain,
        rejected.returncode,
        "not registered in domain" in rejected.stderr,
    ) == (
        "Configured domain narrow: 0 projects, 0 remotes, 1 roots",
        [],
        f"Configured domain {DOMAIN}: 2 projects, 0 remotes, 1 roots",
        {
            "projects": sorted(str(path.resolve()) for path in (root, nested, future, moved)),
            "remotes": [],
            "roots": [str(root.resolve())],
        },
        1,
        True,
    )


def test_domain_root_rejects_missing_directory(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize(repo, env)

    configured = run_context(
        "domain-set",
        "--domain",
        DOMAIN,
        "--root",
        str(tmp_path / "missing"),
        repo=repo,
        env=env,
    )

    assert (
        configured.returncode,
        "Domain root is not a directory" in configured.stderr,
        config(env).get("domains"),
    ) == (1, True, None)


def test_domain_root_preserves_and_extends_git_store_membership(
    tmp_path: Path,
) -> None:
    first_env = isolated_environment(tmp_path / "first-xdg")
    second_env = isolated_environment(tmp_path / "second-xdg")
    original = tmp_path / "original"
    root = tmp_path / "workspace/code"
    fresh = root / "fresh"
    stale = root / "stale"
    store = tmp_path / "store"
    remote = "https://github.com/acme/original.git"
    for project in (original, fresh, stale):
        initialize_git_repository(project)
        run_git(project, "remote", "add", "origin", remote)
    initialize_git_store(store)
    configure_git_store(original, store, first_env)
    run_successfully("init", repo=original, env=first_env)
    original_id = config(first_env)["git_store"]["project_bindings"][
        str(original.resolve())
    ]
    run_successfully(
        "domain-set",
        "--domain",
        DOMAIN,
        "--project",
        str(original),
        repo=original,
        env=first_env,
    )

    configure_git_store(stale, store, second_env)
    run_successfully("git-store-bind", "--match-remote", repo=stale, env=second_env)
    stale.rename(tmp_path / "removed")
    run_successfully(
        "domain-set",
        "--domain",
        DOMAIN,
        "--root",
        str(root),
        repo=fresh,
        env=second_env,
    )
    preserved = read_json(store / "project-context-store.json")["domains"][DOMAIN]
    run_successfully("git-store-bind", "--match-remote", repo=fresh, env=second_env)
    future = root / "future"
    initialize_git_repository(future)
    run_successfully("init", repo=future, env=second_env)
    current = config(second_env)
    future_id = current["git_store"]["project_bindings"][str(future.resolve())]

    assert (
        preserved,
        current["domains"][DOMAIN],
        read_json(store / "project-context-store.json")["domains"][DOMAIN],
    ) == (
        [original_id],
        {
            "projects": [str(fresh.resolve()), str(future.resolve())],
            "remotes": [],
            "roots": [str(root.resolve())],
        },
        sorted((original_id, future_id)),
    )
