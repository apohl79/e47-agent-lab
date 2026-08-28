from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "project_context.py"


def run_context(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(
        {
            "PROJECT_CONTEXT_CURATOR_CONFIG_DIR": str(repo / ".pcc-test/config"),
            "PROJECT_CONTEXT_CURATOR_CACHE_DIR": str(repo / ".pcc-test/cache"),
            "PROJECT_CONTEXT_CURATOR_DATA_DIR": str(repo / ".pcc-test/data"),
        }
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--repo", str(repo)],
        env=process_env,
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
        "schema_version": 4,
        "store_id": "f0b9cb7c-2cc4-4eb2-907f-b69ec16d3702",
        "default_applicability": [{"kind": "project", "selector": "self"}],
        "terms": [
            {
                "term": "ACS",
                "kind": "abbreviation",
                "definition": "Agent Conversation Service",
                "id": "48e7278a-8b98-4ee5-a119-e1ae40c794ee",
                "provenance": [],
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
        "Schema version: 4\n"
        "Migrations applied: none\n"
        "Generated views: refreshed\n",
        "",
        original,
        True,
    )


def test_update_migrates_legacy_context_without_schema_version(tmp_path: Path) -> None:
    legacy = current_context()
    del legacy["schema_version"]
    del legacy["store_id"]
    del legacy["terms"][0]["id"]
    del legacy["terms"][0]["provenance"]
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
        "Migrations applied: 0 -> 1, 1 -> 2, 2 -> 3, 3 -> 4\n" in proc.stdout,
        data["schema_version"],
        data["storage_policy"].get("git_exclude_docs_context"),
        "gitignore_docs_context" in data["storage_policy"],
        data["terms"][0]["term"],
        bool(data["terms"][0]["id"]),
        data["terms"][0]["provenance"],
    ) == (0, True, 4, False, False, "ACS", True, [])


def test_update_migrates_v1_context_with_project_default_applicability(
    tmp_path: Path,
) -> None:
    version_one = current_context()
    version_one["schema_version"] = 1
    del version_one["store_id"]
    del version_one["terms"][0]["id"]
    del version_one["terms"][0]["provenance"]
    del version_one["default_applicability"]
    write_context(tmp_path, version_one)

    proc = run_context("update", repo=tmp_path)
    data = json.loads(
        (tmp_path / "docs/context/context.json").read_text(encoding="utf-8")
    )

    assert (
        proc.returncode,
        "Migrations applied: 1 -> 2, 2 -> 3, 3 -> 4\n" in proc.stdout,
        data["schema_version"],
        data["default_applicability"],
        "applicability" in data["terms"][0],
        bool(data["terms"][0]["id"]),
    ) == (
        0,
        True,
        4,
        [{"kind": "project", "selector": "self"}],
        False,
        True,
    )


@pytest.mark.parametrize("schema_version", [-1, "1", 5])
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


def test_update_merges_legacy_machine_scopes_without_hostnames(tmp_path: Path) -> None:
    write_context(tmp_path, current_context())
    legacy = current_context()
    legacy["schema_version"] = 3
    legacy["default_applicability"] = [{"kind": "machine", "selector": "old-host"}]
    legacy["scope_store"] = {
        "applicability": [{"kind": "machine", "selector": "old-host"}],
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }
    legacy_path = tmp_path / ".pcc-test/data/contexts/machines/old-host/context.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
    second = current_context()
    second["schema_version"] = 3
    second["store_id"] = "1226ad07-21bf-4d95-ae5e-59ed33355599"
    second["terms"][0]["term"] = "TLS"
    second["terms"][0]["id"] = "039b4cee-f4ad-4e0f-8641-e66d39977384"
    second["default_applicability"] = [{"kind": "machine", "selector": "new-host"}]
    second["scope_store"] = {
        "applicability": [{"kind": "machine", "selector": "new-host"}],
        "created_at": "2026-08-02T00:00:00+00:00",
        "updated_at": "2026-08-02T00:00:00+00:00",
    }
    second_path = tmp_path / ".pcc-test/data/contexts/machines/new-host/context.json"
    second_path.parent.mkdir(parents=True)
    second_path.write_text(json.dumps(second, indent=2) + "\n", encoding="utf-8")

    proc = run_context("update", repo=tmp_path)
    target = tmp_path / ".pcc-test/data/contexts/machines/context.json"
    migrated = json.loads(target.read_text(encoding="utf-8"))

    assert (
        proc.returncode,
        legacy_path.exists(),
        second_path.exists(),
        migrated["schema_version"],
        migrated["scope_store"]["applicability"],
        {(term["term"], term["id"]) for term in migrated["terms"]},
    ) == (
        0,
        False,
        False,
        4,
        [{"kind": "machine"}],
        {
            (legacy["terms"][0]["term"], legacy["terms"][0]["id"]),
            (second["terms"][0]["term"], second["terms"][0]["id"]),
        },
    )
