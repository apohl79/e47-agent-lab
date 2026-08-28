from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scope_test_support import (
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


def snapshot_token(output: str) -> str:
    prefix = "Snapshot token: "
    return next(
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    )


def select_local_runtime(
    repo: Path,
    env: dict[str, str],
    visibility: str,
) -> None:
    preview = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        visibility,
        repo=repo,
        env=env,
    )
    applied = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        visibility,
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    assert applied.returncode == 0


def migrate_to_git_store(
    repo: Path,
    env: dict[str, str],
    store: Path,
    workspace: Path,
) -> None:
    preview = run_context(
        "storage-migrate",
        "--target",
        "git-store",
        "--store",
        str(store),
        "--workspace-root",
        str(workspace),
        repo=repo,
        env=env,
    )
    applied = run_context(
        "storage-migrate",
        "--target",
        "git-store",
        "--store",
        str(store),
        "--workspace-root",
        str(workspace),
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    assert applied.returncode == 0


def test_unconfigured_runtime_can_select_local_mode_with_exact_approval(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    git_init(repo)
    preview = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "versioned",
        repo=repo,
        env=env,
    )
    config_path = tmp_path / "xdg/config/config.json"
    preview_mutated = config_path.exists()
    applied = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "versioned",
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    status = run_context(
        "storage-status",
        "--format",
        "json",
        repo=repo,
        env=env,
    )

    assert (
        preview.returncode,
        '"source_mode": "unconfigured"' in preview.stdout,
        '"target_mode": "local"' in preview.stdout,
        preview_mutated,
        applied.returncode,
        read_json(config_path)["storage_runtime"],
        json.loads(status.stdout),
    ) == (
        0,
        True,
        True,
        False,
        0,
        {
            "created_at": read_json(config_path)["storage_runtime"]["created_at"],
            "mode": "local",
            "project_visibility": "versioned",
            "source": "user-confirmed",
            "updated_at": read_json(config_path)["storage_runtime"]["updated_at"],
        },
        {
            "configured": True,
            "mode": "local",
            "project_visibility": "versioned",
        },
    )


def test_storage_migration_round_trip_preserves_records_and_private_context(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    workspace = tmp_path / "workspace"
    repo = workspace / "service"
    store = tmp_path / "context-store"
    git_init(repo)
    git_init(store)
    initialized = run_context("init", "--visibility", "local", repo=repo, env=env)
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
    migrate_to_git_store(repo, env, store, workspace)
    preview = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "versioned",
        repo=repo,
        env=env,
    )
    applied = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "versioned",
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    local = project_context(repo)
    config = read_json(tmp_path / "xdg/config/config.json")
    exclude = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    exclude_path = Path(exclude.stdout.strip())
    exclude_file = repo / exclude_path
    exclude_text = (
        exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    )
    private_paths = tuple((tmp_path / "xdg/data/contexts/users").rglob("context.json"))

    assert (
        initialized.returncode,
        applied.returncode,
        local["store_id"],
        local["patterns"][0]["id"],
        local["storage_policy"]["context_visibility"],
        read_json(tmp_path / "xdg/data/contexts/domains/payments/context.json")[
            "terms"
        ][0]["term"],
        read_json(tmp_path / "xdg/data/contexts/universal/context.json")["terms"][0][
            "term"
        ],
        read_json(private_paths[0])["terms"][0]["term"],
        (store / "project-context-store.json").exists(),
        tuple(store.glob("projects/*/context.json")),
        "git_store" in config,
        config["storage_runtime"]["mode"],
        "docs/context/" in exclude_text.splitlines(),
    ) == (
        0,
        0,
        original["store_id"],
        original["patterns"][0]["id"],
        "versioned",
        "Settlement",
        "UTC",
        "Preferred shell",
        False,
        (),
        False,
        "local",
        False,
    )


def test_reverse_migration_rejects_stale_snapshot_without_partial_changes(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    git_init(repo)
    git_init(store)
    run_context("init", "--visibility", "local", repo=repo, env=env)
    migrate_to_git_store(repo, env, store, tmp_path)
    preview = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "local",
        repo=repo,
        env=env,
    )
    changed = run_context(
        "add-term",
        "--term",
        "Changed",
        "--definition",
        "Changed after preview",
        repo=repo,
        env=env,
    )
    applied = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "local",
        "--approve-snapshot",
        snapshot_token(preview.stdout),
        repo=repo,
        env=env,
    )
    config = read_json(tmp_path / "xdg/config/config.json")

    assert (
        changed.returncode,
        applied.returncode,
        "snapshot changed" in applied.stderr.casefold(),
        (repo / "docs/context/context.json").exists(),
        (store / "project-context-store.json").exists(),
        config["storage_runtime"]["mode"],
        "git_store" in config,
    ) == (0, 1, True, False, True, "git-store", True)


def test_reverse_migration_requires_every_project_to_have_one_local_binding(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    git_init(repo)
    git_init(store)
    run_context("init", "--visibility", "local", repo=repo, env=env)
    migrate_to_git_store(repo, env, store, tmp_path)
    config_path = tmp_path / "xdg/config/config.json"
    config = read_json(config_path)
    config["git_store"]["project_bindings"] = {}
    config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    result = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "local",
        repo=repo,
        env=env,
    )

    assert (
        result.returncode,
        "exactly one local checkout binding" in result.stderr,
        (store / "project-context-store.json").exists(),
        (repo / "docs/context/context.json").exists(),
    ) == (1, True, True, False)


def test_reverse_migration_rejects_conflicting_local_context_without_mutation(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    git_init(repo)
    git_init(store)
    run_context("init", "--visibility", "local", repo=repo, env=env)
    run_context(
        "add-term",
        "--term",
        "Canonical",
        "--definition",
        "Canonical Git knowledge",
        repo=repo,
        env=env,
    )
    migrate_to_git_store(repo, env, store, tmp_path)
    config_path = tmp_path / "xdg/config/config.json"
    config = read_json(config_path)
    project_id = config["git_store"]["project_bindings"][str(repo.resolve())]
    canonical = store / "projects" / project_id / "context.json"
    local = read_json(canonical)
    local["terms"][0]["definition"] = "Conflicting local knowledge"
    local_path = repo / "docs/context/context.json"
    local_path.write_text(json.dumps(local) + "\n", encoding="utf-8")
    config_before = config_path.read_bytes()
    canonical_before = canonical.read_bytes()
    result = run_context(
        "storage-migrate",
        "--target",
        "local",
        "--project-visibility",
        "local",
        repo=repo,
        env=env,
    )

    assert (
        result.returncode,
        "target conflicts" in result.stderr,
        config_path.read_bytes(),
        canonical.read_bytes(),
        local_path.exists(),
    ) == (1, True, config_before, canonical_before, True)


def test_init_uses_configured_local_runtime_default_visibility(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    git_init(repo)
    select_local_runtime(repo, env, "versioned")
    initialized = run_context("init", repo=repo, env=env)
    data = project_context(repo)
    exclude_path = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    exclude_file = repo / exclude_path
    exclude_text = (
        exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    )

    assert (
        initialized.returncode,
        data["storage_policy"]["context_visibility"],
        "docs/context/" in exclude_text.splitlines(),
    ) == (0, "versioned", False)
