from __future__ import annotations

import json
from pathlib import Path

from scope_test_support import (
    initialize,
    isolated_environment,
    read_json,
    run_context,
)


def add(repo: Path, env: dict[str, str], *arguments: str) -> None:
    assert run_context(*arguments, repo=repo, env=env).returncode == 0


def finding_keys(output: str) -> list[tuple[str, str, str, str]]:
    return sorted(
        (item["check"], item["kind"], item["name"], item["store"])
        for item in json.loads(output)["findings"]
    )


def test_audit_reports_time_bound_shadowed_divergent_and_dead_path(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    member = tmp_path / "workspace/member"
    sibling = tmp_path / "workspace/sibling"
    initialize(member, env)
    initialize(sibling, env)
    add(
        member,
        env,
        "domain-set",
        "--domain",
        "billing",
        "--project",
        str(member),
        "--project",
        str(sibling),
    )
    add(member, env, "add-term", "--term", "ACS", "--definition", "Agent Config Service")
    add(sibling, env, "add-term", "--term", "ACS", "--definition", "App Config Service")
    add(
        member,
        env,
        "add-term",
        "--term",
        "ACS",
        "--definition",
        "Shared config service",
        "--applicability",
        "domain:billing",
    )
    add(
        member,
        env,
        "add-component",
        "--name",
        "Gateway",
        "--responsibility",
        "Routes calls",
        "--paths",
        "src/gateway",
    )
    add(
        member,
        env,
        "add-pattern",
        "--name",
        "Retry flag",
        "--summary",
        "Temporary workaround until the upstream fix lands",
    )
    audited = run_context("audit", "--format", "json", repo=member, env=env)

    assert (audited.returncode, finding_keys(audited.stdout)) == (
        0,
        [
            ("dead-path", "component", "Gateway", "project"),
            ("divergent", "term", "ACS", "domain:billing"),
            ("shadowed", "term", "ACS", "project"),
            ("time-bound", "pattern", "Retry flag", "project"),
        ],
    )


def test_audit_flags_burst_aged_records_stale_questions_and_oversized_index(
    tmp_path: Path,
) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize(repo, env)
    add(repo, env, "add-term", "--term", "SLO", "--definition", "Service level objective")
    add(repo, env, "add-pattern", "--name", "Frozen DTOs", "--summary", "DTOs are immutable")
    add(repo, env, "add-question", "--question", "Who owns billing?")
    canonical = repo / "docs/context/context.json"
    data = read_json(canonical)
    data["terms"][0]["updated_at"] = "2025-01-01T00:00:00+00:00"
    data["open_questions"][0]["created_at"] = "2025-01-01T00:00:00+00:00"
    canonical.write_text(json.dumps(data), encoding="utf-8")
    (repo / "docs/context/index.md").write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    audited = run_context(
        "audit", "--format", "json", "--burst", "2", repo=repo, env=env
    )
    hook = run_context("audit", "--format", "hook", "--burst", "2", repo=repo, env=env)

    assert (
        audited.returncode,
        finding_keys(audited.stdout),
        hook.stdout,
    ) == (
        0,
        [
            ("aged", "term", "SLO", "project"),
            ("burst", "store", "project", "project"),
            ("oversized", "view", "docs/context/index.md", "project"),
            ("stale-question", "question", "Who owns billing?", "project"),
        ],
        "Context audit: 4 findings (1 aged, 1 burst, 1 oversized, 1 stale-question); "
        "run $curate-project-context to review them.\n",
    )


def test_audit_is_quiet_without_findings(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    initialize(repo, env)
    add(repo, env, "add-term", "--term", "SLO", "--definition", "Service level objective")
    text = run_context("audit", repo=repo, env=env)
    hook = run_context("audit", "--format", "hook", repo=repo, env=env)

    assert (text.returncode, text.stdout, hook.returncode, hook.stdout) == (
        0,
        f"Context audit for {repo.resolve()} across project: no findings.\n",
        0,
        "",
    )


def test_audit_requires_initialized_context(tmp_path: Path) -> None:
    env = isolated_environment(tmp_path / "xdg")
    repo = tmp_path / "repo"
    repo.mkdir()
    audited = run_context("audit", repo=repo, env=env)

    assert (audited.returncode, audited.stdout) == (1, "")
