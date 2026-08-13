from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_context.py"


def run_context(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--repo", str(repo)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def seed_retrieval_context(repo: Path) -> None:
    commands = [
        (
            "add-term", "--term", "ACS", "--kind", "abbreviation",
            "--definition", "Agent Conversation Service", "--scope", "project",
        ),
        (
            "add-component",
            "--name",
            "Billing Orchestrator",
            "--responsibility",
            "Coordinates billing runs",
            "--paths",
            "services/billing",
        ),
        (
            "add-pattern",
            "--name",
            "Billing deployment",
            "--summary",
            "Deploy billing workers separately from the API",
            "--applies-to",
            "deploy/billing",
        ),
        (
            "add-question", "--question", "Who owns billing alerts?", "--context",
            "Operations ownership is undocumented",
        ),
    ]
    assert run_context("init", repo=repo).returncode == 0
    for command in commands:
        assert run_context(*command, repo=repo).returncode == 0


def test_index_renders_retrieval_workflow_and_topical_index(tmp_path: Path) -> None:
    seed_retrieval_context(tmp_path)

    index = (tmp_path / "docs" / "context" / "index.md").read_text(encoding="utf-8")
    topical_index = index[index.index("## Retrieval") :]

    assert topical_index == """## Retrieval

1. Scan the topical index below for task-specific names and concepts.
2. Run `project_context.py search --query \"<task term>\"` with the updater path reported by the active session.
3. Read only the matching generated sections; if nothing matches, search `context.json` with `rg -n -i`.

## Topical Index

### Terms and APIs

- [ACS](glossary.md) (abbreviation; scope: project) — Agent Conversation Service

### Components

- [Billing Orchestrator](components.md#billing-orchestrator) — Coordinates billing runs

### Architecture and conventions

- [Billing deployment](architecture.md#billing-deployment) — Deploy billing workers separately from the API

### Open questions

- [open] [Who owns billing alerts?](inbox.md) — Operations ownership is undocumented
"""


def test_search_ranks_records_matching_more_queries_first(tmp_path: Path) -> None:
    seed_retrieval_context(tmp_path)

    proc = run_context(
        "search",
        "--query",
        "BILLING",
        "--query",
        "deploy",
        repo=tmp_path,
    )

    assert proc.stdout == """pattern | Billing deployment | docs/context/architecture.md | Deploy billing workers separately from the API | matched: billing, deploy
component | Billing Orchestrator | docs/context/components.md | Coordinates billing runs | matched: billing
question | Who owns billing alerts? | docs/context/inbox.md | Operations ownership is undocumented | matched: billing
"""


def test_search_with_limit_returns_only_highest_ranked_result(tmp_path: Path) -> None:
    seed_retrieval_context(tmp_path)

    proc = run_context(
        "search",
        "--query",
        "billing",
        "--query",
        "deploy",
        "--limit",
        "1",
        repo=tmp_path,
    )

    assert proc.stdout == (
        "pattern | Billing deployment | docs/context/architecture.md | "
        "Deploy billing workers separately from the API | matched: billing, deploy\n"
    )


def test_search_without_matches_reports_empty_result(tmp_path: Path) -> None:
    seed_retrieval_context(tmp_path)

    proc = run_context("search", "--query", "invoices", repo=tmp_path)

    assert (proc.returncode, proc.stdout, proc.stderr) == (
        0,
        "No context matches for: invoices\n",
        "",
    )


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (("--query", " "), "Search query must contain at least one non-blank term.\n"),
        (("--query", "billing", "--limit", "0"), "Search limit must be greater than zero.\n"),
    ],
)
def test_search_rejects_invalid_input(
    tmp_path: Path,
    arguments: tuple[str, ...],
    error: str,
) -> None:
    seed_retrieval_context(tmp_path)

    proc = run_context("search", *arguments, repo=tmp_path)

    assert (proc.returncode, proc.stdout, proc.stderr) == (
        1,
        "",
        error,
    )
