from __future__ import annotations

import json
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


def write_context(repo: Path, data: dict[str, object], index: str = "stale\n") -> str:
    context_dir = repo / "docs" / "context"
    context_dir.mkdir(parents=True)
    serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
    (context_dir / "context.json").write_text(serialized, encoding="utf-8")
    (context_dir / "index.md").write_text(index, encoding="utf-8")
    return serialized


def current_context() -> dict[str, object]:
    return {
        "schema_version": 1,
        "terms": [
            {
                "term": "ACS",
                "kind": "abbreviation",
                "definition": "Agent Conversation Service",
                "scope": "project",
                "source": "repo-docs",
            }
        ],
        "components": [],
        "patterns": [],
        "open_questions": [],
    }


def test_update_current_context_preserves_data_and_refreshes_views(tmp_path: Path) -> None:
    original = write_context(tmp_path, current_context())

    proc = run_context("update", repo=tmp_path)
    updated = (tmp_path / "docs" / "context" / "context.json").read_text(encoding="utf-8")
    index = (tmp_path / "docs" / "context" / "index.md").read_text(encoding="utf-8")

    assert (proc.returncode, proc.stdout, proc.stderr, updated, "## Topical Index" in index) == (
        0,
        f"Updated project context: {tmp_path / 'docs/context'}\n"
        "Schema version: 1\n"
        "Migrations applied: none\n"
        "Generated views: refreshed\n",
        "",
        original,
        True,
    )


def test_update_migrates_legacy_context_without_schema_version(tmp_path: Path) -> None:
    legacy = current_context()
    del legacy["schema_version"]
    legacy["storage_policy"] = {
        "context_visibility": "local",
        "git_initialized": False,
        "gitignore_docs_context": False,
        "decision": "Legacy local context.",
        "source": "user-confirmed",
    }
    write_context(tmp_path, legacy)

    proc = run_context("update", repo=tmp_path)
    data = json.loads((tmp_path / "docs/context/context.json").read_text(encoding="utf-8"))

    assert (
        proc.returncode,
        "Migrations applied: 0 -> 1\n" in proc.stdout,
        data["schema_version"],
        data["storage_policy"].get("git_exclude_docs_context"),
        "gitignore_docs_context" in data["storage_policy"],
        data["terms"],
    ) == (0, True, 1, False, False, current_context()["terms"])


@pytest.mark.parametrize("schema_version", [-1, "1", 2])
def test_update_rejects_unsupported_schema_without_writes(
    tmp_path: Path,
    schema_version: int | str,
) -> None:
    data = {**current_context(), "schema_version": schema_version}
    original = write_context(tmp_path, data)

    proc = run_context("update", repo=tmp_path)

    assert (
        proc.returncode,
        proc.stdout,
        "schema_version" in proc.stderr,
        (tmp_path / "docs/context/context.json").read_text(encoding="utf-8"),
        (tmp_path / "docs/context/index.md").read_text(encoding="utf-8"),
    ) == (1, "", True, original, "stale\n")


def test_update_requires_initialized_context(tmp_path: Path) -> None:
    proc = run_context("update", repo=tmp_path)

    assert (proc.returncode, proc.stdout, "Context is not initialized" in proc.stderr) == (
        1,
        "",
        True,
    )
