from __future__ import annotations

import uuid
from pathlib import Path

from scope_test_support import (
    configure_workspace,
    initialize,
    isolated_environment,
    project_context,
    read_json,
    run_context,
)


def test_domain_fact_uses_xdg_store_and_only_matches_members(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "workspace/member"
    outsider = tmp_path / "outside"
    initialize(member, env)
    initialize(outsider, env)
    configured = run_context(
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(member),
        repo=member,
        env=env,
    )
    added = run_context(
        "add-pattern",
        "--name",
        "Billing ownership",
        "--summary",
        "Billing services belong to the revenue domain",
        "--applicability",
        "domain:billing",
        repo=member,
        env=env,
    )
    domain_path = tmp_path / "xdg/data/contexts/domains/billing/context.json"
    scoped = read_json(domain_path)
    member_search = run_context(
        "search", "--query", "revenue", repo=member, env=env
    )
    outsider_search = run_context(
        "search", "--query", "revenue", repo=outsider, env=env
    )

    record = scoped["patterns"][0]
    assert (
        configured.returncode,
        added.returncode,
        project_context(member)["patterns"],
        scoped["scope_store"]["applicability"],
        record["applicability"],
        str(uuid.UUID(record["id"])),
        record["provenance"][0]["repo"],
        "Billing ownership" in member_search.stdout,
        outsider_search.stdout,
    ) == (
        0,
        0,
        [],
        [{"kind": "domain", "selector": "billing"}],
        [{"kind": "domain", "selector": "billing"}],
        record["id"],
        str(member.resolve()),
        True,
        "No context matches for: revenue\n",
    )


def test_workspace_fact_uses_configured_xdg_store_and_membership(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    workspace = tmp_path / "workspace"
    member = workspace / "member"
    outsider = tmp_path / "outside"
    initialize(member, env)
    initialize(outsider, env)
    configure_workspace(env, workspace)
    added = run_context(
        "add-component",
        "--name",
        "Shared CI",
        "--responsibility",
        "Builds every repository in the workspace",
        "--applicability",
        "workspace:self",
        repo=member,
        env=env,
    )
    stores = tuple((tmp_path / "xdg/data/contexts/workspaces").rglob("context.json"))
    scoped = read_json(stores[0])
    member_search = run_context("search", "--query", "every", repo=member, env=env)
    outsider_search = run_context(
        "search", "--query", "every", repo=outsider, env=env
    )

    assert (
        added.returncode,
        len(stores),
        scoped["default_applicability"],
        project_context(member)["components"],
        "Shared CI" in member_search.stdout,
        outsider_search.stdout,
    ) == (
        0,
        1,
        [{"kind": "workspace", "selector": str(workspace.resolve())}],
        [],
        True,
        "No context matches for: every\n",
    )


def test_universal_fact_matches_another_initialized_project(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    source = tmp_path / "source"
    other = tmp_path / "other"
    initialize(source, env)
    initialize(other, env)
    added = run_context(
        "add-term",
        "--term",
        "UTC",
        "--definition",
        "Coordinated Universal Time",
        "--applicability",
        "universal",
        repo=source,
        env=env,
    )
    search = run_context("search", "--query", "UTC", repo=other, env=env)

    assert (added.returncode, "UTC" in search.stdout) == (0, True)
