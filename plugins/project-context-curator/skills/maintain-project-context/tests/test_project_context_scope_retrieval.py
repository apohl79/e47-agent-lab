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


def test_workspace_applicability_is_rejected_for_new_records(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    workspace = tmp_path / "workspace"
    member = workspace / "member"
    initialize(member, env)
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
    stores = tuple(
        (tmp_path / "xdg/data/contexts/workspaces").rglob("context.json")
    )

    assert (
        added.returncode,
        "Invalid applicability kind 'workspace'" in added.stderr,
        len(stores),
        project_context(member)["components"],
    ) == (
        1,
        True,
        0,
        [],
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


def test_status_and_index_expose_applicable_scoped_context(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "workspace/member"
    outsider = tmp_path / "outside"
    initialize(member, env)
    initialize(outsider, env)
    run_context(
        "domain-set", "--domain", "billing", "--project", str(member), repo=member, env=env
    )
    run_context(
        "add-term",
        "--term",
        "Ledger",
        "--definition",
        "Append-only balance journal",
        "--applicability",
        "domain:billing",
        repo=member,
        env=env,
    )
    run_context(
        "add-component",
        "--name",
        "billing-worker",
        "--responsibility",
        "Settles invoices",
        "--applicability",
        "domain:billing",
        repo=member,
        env=env,
    )
    added = run_context(
        "add-term",
        "--term",
        "UTC",
        "--definition",
        "Coordinated Universal Time",
        "--applicability",
        "universal",
        repo=outsider,
        env=env,
    )
    domain_path = tmp_path / "xdg/data/contexts/domains/billing/context.json"
    universal_path = tmp_path / "xdg/data/contexts/universal/context.json"
    run_context("update", repo=member, env=env)
    run_context("update", repo=outsider, env=env)
    member_status = run_context("status", repo=member, env=env).stdout
    outsider_status = run_context("status", repo=outsider, env=env).stdout
    member_index = (member / "docs/context/index.md").read_text(encoding="utf-8")
    outsider_index = (outsider / "docs/context/index.md").read_text(encoding="utf-8")

    assert (
        added.returncode,
        f"Scoped context domain:billing: 1 terms, 1 components, 0 patterns "
        f"(canonical: {domain_path}; not in docs/context views, use search)." in member_status,
        f"Scoped context universal: 1 terms, 0 components, 0 patterns" in member_status,
        "domain:billing" in outsider_status,
        "### domain:billing — 1 terms, 1 components, 0 patterns" in member_index,
        f"- Canonical: `{domain_path}`" in member_index,
        "- Terms: Ledger" in member_index,
        "- Components: billing-worker" in member_index,
        f"### universal — 1 terms, 0 components, 0 patterns" in member_index,
        "a project record overrides a domain record, which overrides a universal record"
        in member_index,
        "domain:billing" in outsider_index,
        f"- Canonical: `{universal_path}`" in outsider_index,
    ) == (0, True, True, False, True, True, True, True, True, True, False, True)
