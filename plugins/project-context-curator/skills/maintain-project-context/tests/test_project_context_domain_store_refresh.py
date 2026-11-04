from __future__ import annotations

import json
from pathlib import Path

from scope_test_support import isolated_environment, run_context, run_git
from test_project_context_domain_stores import (
    configured_store,
    domain_remote,
    place_domain,
    read_json,
    successful,
    token,
)


def test_activation_updates_clean_child_after_main_gitlink_advances(
    tmp_path: Path,
) -> None:
    env, repo, _store, remote, project_id = configured_store(tmp_path)
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    second_store = tmp_path / "second-machine-store"
    run_git(tmp_path, "clone", str(remote), str(second_store))
    client = tmp_path / "second-machine-client"
    client.mkdir()
    second_env = {**env, **isolated_environment(tmp_path / "second-xdg")}
    arguments = ("git-store-init", "--store", str(second_store))
    preview = successful(*arguments, repo=client, env=second_env)
    successful(
        *arguments, "--approve-snapshot", token(preview.stdout),
        repo=client, env=second_env,
    )
    successful("git-store-activate", "--store", "cpl", repo=client, env=second_env)
    successful(
        "add-term", "--term", "Shared update", "--definition", "Captured on the other machine",
        repo=repo, env=env,
    )
    run_git(second_store, "pull", "--ff-only")
    assert run_git(second_store, "status", "--porcelain").stdout.strip()

    activated = run_context(
        "git-store-activate", "--store", "cpl", repo=client, env=second_env
    )

    assert activated.returncode == 0, activated.stdout + activated.stderr
    child = second_store / "stores/cpl"
    assert run_git(child, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    data = read_json(child / "projects" / project_id / "context.json")
    assert any(record["term"] == "Shared update" for record in data["terms"])
    assert run_git(second_store, "status", "--porcelain").stdout.strip() == ""

    head = run_git(child, "rev-parse", "HEAD").stdout
    note = child / "uncommitted.txt"
    note.write_text("Keep my changes\n")
    rejected = run_context(
        "git-store-activate", "--store", "cpl", repo=client, env=second_env
    )
    assert rejected.returncode != 0
    assert "uncommitted child changes" in rejected.stderr
    assert note.read_text() == "Keep my changes\n"
    assert run_git(child, "rev-parse", "HEAD").stdout == head


def test_removing_membership_keeps_dedicated_knowledge_and_valid_routes(
    tmp_path: Path,
) -> None:
    env, repo, store, _remote, _project_id = configured_store(tmp_path)
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    scope = store / "stores/cpl/scopes/domains/cpl/context.json"
    before = scope.read_bytes()

    hook = tmp_path / "test-hooks/pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f'[ "$(git rev-parse --show-toplevel)" = "{store.resolve()}" ] || exit 0\n'
        "git diff --quiet origin/main HEAD -- project-context-store.json\n"
    )
    hook.chmod(0o755)
    rejected = run_context("domain-remove", "--domain", "cpl", repo=repo, env=env)
    assert rejected.returncode != 0
    assert "committed locally" in rejected.stderr
    hook.write_text("#!/bin/sh\nexit 0\n")
    successful("domain-remove", "--domain", "cpl", repo=repo, env=env)

    successful("git-store-status", repo=repo, env=env)
    assert scope.read_bytes() == before
    metadata = read_json(store / "project-context-store.json")
    assert metadata["domains"]["cpl"] == []
    assert metadata["domain_owners"]["cpl"] == "cpl"
    child_manifest = read_json(store / "stores/cpl/project-context-store.json")
    assert child_manifest["domains"]["cpl"] == []
    successful(
        "domain-set", "--domain", "cpl", "--project", str(repo), repo=repo, env=env
    )
    successful(
        "add-term", "--term", "Rejoined domain", "--definition", "Keeps its owner",
        "--applicability", "domain:cpl", repo=repo, env=env,
    )
    assert {record["term"] for record in read_json(scope)["terms"]} == {
        "Domain ledger", "Rejoined domain",
    }


def test_write_rejects_remote_membership_changes_before_touching_canonical_data(
    tmp_path: Path,
) -> None:
    env, repo, store, remote, project_id = configured_store(tmp_path)
    place_domain(repo, env, "cpl", domain_remote(tmp_path))
    canonical = store / "stores/cpl/projects" / project_id / "context.json"
    before = canonical.read_bytes()
    other = tmp_path / "other-store"
    run_git(tmp_path, "clone", str(remote), str(other))
    manifest_path = other / "project-context-store.json"
    manifest = read_json(manifest_path)
    manifest["domains"]["cpl"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    run_git(other, "add", "project-context-store.json")
    run_git(other, "commit", "-m", "Change domain membership")
    run_git(other, "push", "origin", "main")

    rejected = run_context(
        "add-term", "--term", "Concurrent write", "--definition", "Must not be written",
        repo=repo, env=env,
    )

    assert rejected.returncode != 0
    assert "Remote domain routing changed" in rejected.stderr
    assert canonical.read_bytes() == before
