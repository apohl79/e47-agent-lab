from __future__ import annotations

import json
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


WORKER_REMOTE = "github.com/acme/worker"


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


def config_domains(env: dict[str, str]) -> dict[str, object]:
    path = config_path(env)
    return read_json(path)["domains"] if path.exists() else {}


def config_path(env: dict[str, str]) -> Path:
    return Path(env["PROJECT_CONTEXT_CURATOR_CONFIG_DIR"]) / "config.json"


def add_billing_pattern(repo: Path, env: dict[str, str]) -> None:
    added = run_context(
        "add-pattern",
        "--name",
        "Billing ownership",
        "--summary",
        "Billing services belong to the revenue domain",
        "--applicability",
        "domain:billing",
        repo=repo,
        env=env,
    )
    assert added.returncode == 0, added.stderr


def test_domain_set_stores_projects_and_normalized_remotes(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "member"
    initialize(member, env)

    configured = run_context(
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(member),
        "--remote",
        "git@github.com:acme/worker.git",
        "--remote",
        "https://github.com/acme/worker",
        repo=member,
        env=env,
    )
    listed = run_context("domain-list", repo=member, env=env)

    assert (
        configured.returncode,
        configured.stdout.strip(),
        config_domains(env),
        listed.stdout.strip(),
    ) == (
        0,
        "Configured domain billing: 1 projects, 1 remotes",
        {"billing": {"projects": [str(member.resolve())], "remotes": [WORKER_REMOTE]}},
        f"billing: {member.resolve()}, {WORKER_REMOTE}",
    )


def test_domain_set_rejects_missing_members_and_non_remote_values(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "member"
    initialize(member, env)

    empty = run_context("domain-set", "--domain", "billing", repo=member, env=env)
    local_path = run_context(
        "domain-set",
        "--domain",
        "billing",
        "--remote",
        str(tmp_path / "worker"),
        repo=member,
        env=env,
    )

    assert (
        empty.returncode,
        "at least one --project or --remote" in empty.stderr,
        local_path.returncode,
        "not a Git remote URL" in local_path.stderr,
        config_domains(env),
    ) == (1, True, 1, True, {})


def test_remote_member_checkout_reads_and_writes_domain_facts(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "member"
    worker = git_repository_with_remote(
        tmp_path / "worker", "git@github.com:acme/worker.git"
    )
    outsider = git_repository_with_remote(
        tmp_path / "outsider", "git@github.com:acme/outsider.git"
    )
    initialize(member, env)
    assert run_context("init", "--visibility", "local", repo=worker, env=env).returncode == 0
    assert run_context("init", "--visibility", "local", repo=outsider, env=env).returncode == 0
    configured = run_context(
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(member),
        "--remote",
        "https://github.com/acme/worker.git",
        repo=member,
        env=env,
    )
    assert configured.returncode == 0, configured.stderr
    add_billing_pattern(member, env)

    worker_search = run_context("search", "--query", "revenue", repo=worker, env=env)
    outsider_search = run_context("search", "--query", "revenue", repo=outsider, env=env)
    worker_write = run_context(
        "add-term",
        "--term",
        "Invoice",
        "--definition",
        "Billing document issued per cycle",
        "--applicability",
        "domain:billing",
        repo=worker,
        env=env,
    )
    outsider_write = run_context(
        "add-term",
        "--term",
        "Invoice",
        "--definition",
        "Billing document issued per cycle",
        "--applicability",
        "domain:billing",
        repo=outsider,
        env=env,
    )

    assert (
        "Billing ownership" in worker_search.stdout,
        "Billing ownership" in outsider_search.stdout,
        worker_write.returncode,
        outsider_write.returncode,
        "not registered in domain" in outsider_write.stderr,
    ) == (True, False, 0, 1, True)


def test_legacy_list_domain_config_is_still_read(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "member"
    initialize(member, env)
    path = config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "enabled": False,
                "workspace_roots": [],
                "domains": {"billing": [str(member.resolve())]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    listed = run_context("domain-list", repo=member, env=env)
    add_billing_pattern(member, env)

    assert (listed.returncode, listed.stdout.strip()) == (
        0,
        f"billing: {member.resolve()}",
    )


def test_git_store_persists_domain_remotes_and_init_restores_membership(
    tmp_path: Path,
) -> None:
    first_env = isolated_environment(tmp_path / "first-xdg")
    second_env = isolated_environment(tmp_path / "second-xdg")
    store = tmp_path / "store"
    member = git_repository_with_remote(
        tmp_path / "member", "git@github.com:acme/member.git"
    )
    worker = git_repository_with_remote(
        tmp_path / "worker", "git@github.com:acme/worker.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(member, store, first_env)
    assert run_context("init", repo=member, env=first_env).returncode == 0
    configured = run_context(
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(member),
        "--remote",
        "git@github.com:acme/worker.git",
        repo=member,
        env=first_env,
    )
    assert configured.returncode == 0, configured.stderr
    add_billing_pattern(member, first_env)
    manifest = read_json(store / "project-context-store.json")
    configure_git_store_runtime(worker, store, second_env)

    initialized = run_context("init", repo=worker, env=second_env)
    search = run_context("search", "--query", "revenue", repo=worker, env=second_env)
    status = run_context("git-store-status", repo=worker, env=second_env)

    assert (
        manifest["domain_remotes"],
        initialized.returncode,
        config_domains(second_env)["billing"]["remotes"],
        "Billing ownership" in search.stdout,
        f"Domain billing remotes: {WORKER_REMOTE}" in status.stdout,
    ) == ({"billing": [WORKER_REMOTE]}, 0, [WORKER_REMOTE], True, True)


def test_git_store_bind_restores_domain_paths_and_remotes(tmp_path: Path) -> None:
    first_env = isolated_environment(tmp_path / "first-xdg")
    second_env = isolated_environment(tmp_path / "second-xdg")
    store = tmp_path / "store"
    member = git_repository_with_remote(
        tmp_path / "member", "git@github.com:acme/member.git"
    )
    fresh = git_repository_with_remote(
        tmp_path / "fresh", "https://github.com/acme/member.git"
    )
    initialize_git_store(store)
    configure_git_store_runtime(member, store, first_env)
    assert run_context("init", repo=member, env=first_env).returncode == 0
    configured = run_context(
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(member),
        "--remote",
        "git@github.com:acme/worker.git",
        repo=member,
        env=first_env,
    )
    assert configured.returncode == 0, configured.stderr
    configure_git_store_runtime(fresh, store, second_env)

    bound = run_context("git-store-bind", "--match-remote", repo=fresh, env=second_env)

    assert (bound.returncode, config_domains(second_env)) == (
        0,
        {"billing": {"projects": [str(fresh.resolve())], "remotes": [WORKER_REMOTE]}},
    )
