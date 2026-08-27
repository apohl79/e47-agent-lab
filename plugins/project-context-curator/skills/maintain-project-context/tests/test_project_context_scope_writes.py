from __future__ import annotations

from pathlib import Path

from scope_test_support import (
    initialize,
    isolated_environment,
    project_context,
    read_json,
    run_context,
)


def test_domain_write_rejects_unknown_and_nonmember_repositories(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "member"
    outsider = tmp_path / "outsider"
    initialize(member, env)
    initialize(outsider, env)
    unknown = run_context(
        "add-term",
        "--term",
        "Ledger",
        "--definition",
        "Revenue ledger",
        "--applicability",
        "domain:revenue",
        repo=outsider,
        env=env,
    )
    run_context(
        "domain-set",
        "--domain",
        "revenue",
        "--project",
        str(member),
        repo=member,
        env=env,
    )
    nonmember = run_context(
        "add-term",
        "--term",
        "Ledger",
        "--definition",
        "Revenue ledger",
        "--applicability",
        "domain:revenue",
        repo=outsider,
        env=env,
    )

    assert (
        unknown.returncode,
        "Unknown domain 'revenue'" in unknown.stderr,
        nonmember.returncode,
        "is not registered in domain 'revenue'" in nonmember.stderr,
        project_context(outsider)["terms"],
        (tmp_path / "xdg/data/contexts/domains/revenue/context.json").exists(),
    ) == (1, True, 1, True, [], False)


def test_universal_upsert_is_canonical_and_idempotent(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize(repo, env)
    arguments = (
        "add-term",
        "--term",
        "UTC",
        "--kind",
        "abbreviation",
        "--definition",
        "Coordinated Universal Time",
        "--applicability",
        "universal",
    )
    first = run_context(*arguments, repo=repo, env=env)
    path = tmp_path / "xdg/data/contexts/universal/context.json"
    first_id = read_json(path)["terms"][0]["id"]
    second = run_context(*arguments, repo=repo, env=env)
    stored = read_json(path)

    assert (
        first.returncode,
        second.returncode,
        project_context(repo)["terms"],
        len(stored["terms"]),
        stored["terms"][0]["id"],
        len(stored["terms"][0]["provenance"]),
        stored["default_applicability"],
    ) == (0, 0, [], 1, first_id, 1, [{"kind": "universal"}])


def test_multiple_applicability_dimensions_use_composite_store(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize(repo, env)
    added = run_context(
        "add-pattern",
        "--name",
        "Local signing",
        "--summary",
        "Sign commits for this user on this machine",
        "--applicability",
        "user:self",
        "--applicability",
        "machine:self",
        repo=repo,
        env=env,
    )
    stores = tuple((tmp_path / "xdg/data/contexts/composite").rglob("context.json"))
    scoped = read_json(stores[0])

    assert (
        added.returncode,
        len(stores),
        [item["kind"] for item in scoped["default_applicability"]],
        project_context(repo)["patterns"],
    ) == (0, 1, ["machine", "user"], [])
